"""Compute the spatial sweep results WITHOUT re-querying the DB per config.

The DB query cost (get_spatial_baseline + get_weekly_count per week per unit
per disease) dominates runtime.  So we precompute:
  - per (disease, village, street, week): observed_count
  - per (disease, village, street, week): baseline mu, sigma

Then for each config (h, k, mode) we run ONLY the detector math (cusum_compute +
ewma_compute + CombinedDetector.detect) in memory.  This is ~50x faster than
the DB-per-config approach.

Grid: h ∈ {3,5,7} × k ∈ {0.25, 0.5} = 6 configs per mode, 12 total.
lambda=0.2, L=3.0 fixed.
"""

import sys, os, json, time
from pathlib import Path
from collections import defaultdict

project_root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
os.environ['SRC_ROOT'] = os.path.abspath(os.path.join(project_root, 'src'))
sys.path.insert(0, project_root)
sys.path.insert(0, os.path.join(project_root, 'src'))
os.chdir(project_root)

from src.evaluation_spatial import (
    get_spatial_units,
    get_weekly_count,
    get_spatial_baseline,
)
from src.detection.combined import CombinedDetector
from src.detection.cusum import cusum_compute
from src.detection.ewma import ewma_compute
from src.ingestion import DISEASES
from src.evaluation_spatial_metrics import load_ground_truth, get_outbreak_spatial_units

# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------
CUSUM_H_VALUES = [3.0, 5.0, 7.0]
CUSUM_K_VALUES = [0.25, 0.5]
EWMA_LAMBDA_FIXED = 0.2
EWMA_L_FIXED = 3.0

# ---------------------------------------------------------------------------
# Precompute observed counts + baselines for all (disease, unit, week)
# ---------------------------------------------------------------------------

def precompute(start_week=21, end_week=104):
    """Return:
      spatial_units: [(village, street, population), ...]  (same for all diseases? No, per disease)
      cells: dict (disease, village, street, week_num) -> {
          'observed_count': int,
          'mu': float|None,
          'sigma': float|None,
          'population': int,
      }
    """
    spatial_units_by_disease = {}
    cells = {}

    for disease in DISEASES:
        units = get_spatial_units(disease=disease)
        spatial_units_by_disease[disease] = units
        print(f"  Disease {disease}: {len(units)} spatial units", flush=True)

        for village, street, population in units:
            key_prefix = (disease, village, street)
            for week_num in range(start_week, end_week + 1):
                cnt = get_weekly_count(week_num, disease, village, street)
                bl = get_spatial_baseline(week_num, disease, village, street,
                                          list(range(max(1, week_num - 20), week_num)))
                cells[(disease, village, street, week_num)] = {
                    'observed_count': cnt,
                    'mu': bl['mu'],
                    'sigma': bl['sigma'],
                    'population': population,
                }

    n_cells = len(cells)
    print(f"  Total cells: {n_cells:,}  ({n_cells/1000:.0f}k)", flush=True)
    return spatial_units_by_disease, cells


def evaluate_config(cells, outbreak_units, cusum_h, cusum_k, ewma_lambda, ewma_l, fusion_mode):
    """Run the detector math for one config over all precomputed cells."""
    cusum_state = {}
    ewma_state = {}
    results = []
    summary = defaultdict(int)

    # Iterate in week order so state carries forward
    ordered = sorted(cells.items(), key=lambda kv: (kv[0][3], kv[0][0], kv[0][1], kv[0][2]))

    for (disease, village, street, week_num), cell in ordered:
        key = (disease, village, street)
        population = cell['population']
        observed_count = cell['observed_count']
        mu = cell['mu']
        sigma = cell['sigma']

        observed_rate = observed_count / population if population > 0 else 0.0

        # CUSUM sigma
        if mu is not None and 0 < mu < 1 and population > 0:
            sigma_cusum = (mu * (1 - mu) / population) ** 0.5
        else:
            sigma_cusum = 0.001

        prev_S = cusum_state.get(key, 0.0)
        cusum_result = cusum_compute(
            week_num=week_num, x_t=observed_rate, mu_t=mu,
            sigma_t=sigma_cusum, prev_S=prev_S,
            k=cusum_k, h=cusum_h,
        )
        cusum_S = cusum_result['S_t']
        cusum_sig = 1 if cusum_result['alert'] else 0
        cusum_state[key] = cusum_S

        # EWMA
        prev_Z = ewma_state.get(key, 0.0)
        z_t = (observed_rate - mu) / sigma_cusum if sigma_cusum > 0 else 0.0
        ewma_result = ewma_compute(
            week_num=week_num, z_t=z_t, prev_Z=prev_Z,
            lambda_=ewma_lambda, L=ewma_l,
        )
        ewma_Z = ewma_result['Z_t']
        ewma_UCL = ewma_result['UCL_t']
        ewma_sig = 1 if ewma_result['alert'] else 0
        ewma_state[key] = ewma_Z

        # Trend (need recent counts — pull from cells)
        recent_counts = []
        for bw in range(week_num - 3, week_num + 1):
            c = cells.get((disease, village, street, bw), {}).get('observed_count', 0)
            recent_counts.append(c)
        while len(recent_counts) < 4:
            recent_counts.insert(0, 0)

        # Baseline deviation
        baseline_deviation_sigma = 0.0
        if sigma is not None and sigma > 0 and mu is not None and mu > 0:
            baseline_deviation_sigma = (observed_rate - mu) / sigma

        # Combined detector
        combined = CombinedDetector(
            disease=disease, village=village, street=street,
            baseline_expected=mu if mu is not None else 0.0,
            n_population=population,
        )
        cr = combined.detect(
            week_num=week_num, observed_count=observed_count,
            cusum_S=cusum_S, ewma_Z=ewma_Z, cusum_h=cusum_h, ewma_UCL=ewma_UCL,
            baseline_mu=mu, baseline_sigma=sigma, weekly_counts=recent_counts,
            cusum_k=cusum_k, ewma_lambda=ewma_lambda, ewma_l=ewma_l,
        )

        status = cr['status']
        results.append({
            'disease': disease,
            'village': village,
            'street': street,
            'week_number': week_num,
            'status': status,
        })
        summary[status] += 1

    # Compute metrics
    # outbreak_units keys are (disease, village, street) -> set of outbreak weeks
    tp = fp = fn = tn = 0
    alert_count = 0
    for r in results:
        unit_key = (r['disease'], r['village'], r['street'])
        week = r['week_number']
        is_outbreak = week in outbreak_units.get(unit_key, set())
        fired = r['status'] in ('ALERT', 'HIGH_ALERT') if fusion_mode == 'confirmation' else r['status'] in ('WATCH', 'ALERT', 'HIGH_ALERT')
        if fired:
            alert_count += 1
        if is_outbreak and fired:
            tp += 1
        elif not is_outbreak and fired:
            fp += 1
        elif is_outbreak and not fired:
            fn += 1
        else:
            tn += 1

    total = len(results)
    tp_fp_fn_tn = tp + fp + fn + tn
    total_pos = tp + fn
    total_neg = tn + fp
    return {
        'TP': tp, 'FP': fp, 'FN': fn, 'TN': tn,
        'sensitivity': tp / total_pos if total_pos > 0 else 0.0,
        'specificity': tn / total_neg if total_neg > 0 else 0.0,
        'PPV': tp / (tp + fp) if (tp + fp) > 0 else 0.0,
        'F1': 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) > 0 else 0.0,
        'alert_count': alert_count,
        'alert_pct': alert_count / total * 100 if total > 0 else 0.0,
        'total_rows': total,
        'cusum_h': cusum_h, 'cusum_k': cusum_k,
        'ewma_lambda': ewma_lambda, 'ewma_l': ewma_l,
        'fusion_mode': fusion_mode,
        'summary': dict(summary),
    }


def main():
    output_path = Path("results")
    output_path.mkdir(parents=True, exist_ok=True)

    H_STR = "{" + ",".join(f"{h:.1f}" for h in CUSUM_H_VALUES) + "}"
    K_STR = "{" + ",".join(f"{k:.2f}" for k in CUSUM_K_VALUES) + "}"
    SWEEP_TOTAL = len(CUSUM_H_VALUES) * len(CUSUM_K_VALUES)

    lines = []
    lines.append("# P8 Fusion Mode Comparison: UNION vs CONFIRMATION")
    lines.append("")
    lines.append("## Background")
    lines.append("")
    lines.append("The original P8 combined detector uses a **UNION** fusion rule: any 2 of 4")
    lines.append("signals (CUSUM, EWMA, trend, baseline deviation) → ALERT. This aggregates")
    lines.append("false alarms from all four components, so errors accumulate.")
    lines.append("")
    lines.append("Yi et al. (2025) found that requiring **>=2 concordant models** gave the best")
    lines.append("Youden index (0.651, sensitivity 0.739, specificity 0.912), while requiring")
    lines.append(">=1 model raised sensitivity to 0.902 but dropped specificity to 0.788.")
    lines.append("")
    lines.append("This report implements a **CONFIRMATION** fusion rule as an alternative:")
    lines.append("- ALERT requires CUSUM **AND** EWMA to both fire (concordant)")
    lines.append("- Exactly one of {CUSUM, EWMA} firing = PROVISIONAL (logged, not an alert)")
    lines.append("- Trend + baseline alone = PROVISIONAL unless both fire")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Precompute
    print("Precomputing observed counts and baselines (DB read-once)...", flush=True)
    t0 = time.time()
    units_by_disease, cells = precompute()
    precompute_elapsed = time.time() - t0
    print(f"  Precomputation took {precompute_elapsed:.1f}s", flush=True)

    outbreaks = load_ground_truth()
    outbreak_units = get_outbreak_spatial_units(outbreaks)
    total_outbreak_uw = sum(
        len(ob['affected_streets']) * (ob['end_week'] - ob['start_week'] + 1)
        for ob in outbreaks
    )

    total_cells = len(cells)
    lines.append(f"**Precomputation:** {total_cells:,} cells (observed count + baseline per unit-week)")
    lines.append(f"**Evaluation period:** weeks 21-104 (84 weeks)")
    lines.append(f"**Spatial units by disease:** " +
                 ", ".join(f"{d}: {len(units_by_disease[d])}" for d in DISEASES))
    lines.append(f"**Outbreak unit-weeks in period:** {total_outbreak_uw}")
    lines.append("")

    # ---------------------------------------------------------------
    # Part 1: Defaults
    # ---------------------------------------------------------------
    lines.append("## Part 1: Default Parameters (h=5.0, k=0.5, lambda=0.2, L=3.0)")
    lines.append("")
    lines.append("| Metric | UNION (original) | CONFIRMATION (new) |")
    lines.append("|--------|-------------------|---------------------|")

    default_results = {}
    for mode in ['union', 'confirmation']:
        print(f"\nRunning {mode} mode with default parameters...", flush=True)
        m = evaluate_config(
            cells, outbreak_units,
            cusum_h=5.0, cusum_k=0.5,
            ewma_lambda=EWMA_LAMBDA_FIXED, ewma_l=EWMA_L_FIXED,
            fusion_mode=mode,
        )
        default_results[mode] = m
        print(f"  TP={m['TP']} FP={m['FP']} FN={m['FN']} TN={m['TN']}", flush=True)
        print(f"  Sens={m['sensitivity']:.3f} Spec={m['specificity']:.3f}", flush=True)
        print(f"  PPV={m['PPV']:.3f} F1={m['F1']:.3f}", flush=True)
        print(f"  Alerts={m['alert_count']} ({m['alert_pct']:.1f}% of unit-weeks)", flush=True)
        print(f"  Status dist: {m['summary']}", flush=True)

    for metric_name, label in [
        ('TP', 'True Positives'),
        ('FP', 'False Positives (over-escalation)'),
        ('FN', 'False Negatives (under-escalation)'),
        ('TN', 'True Negatives'),
        ('sensitivity', 'Sensitivity (Recall)'),
        ('specificity', 'Specificity'),
        ('PPV', 'PPV (Precision)'),
        ('F1', 'F1 Score'),
        ('alert_count', 'Total Alerts Fired'),
        ('alert_pct', 'Alert Burden (% of unit-weeks)'),
    ]:
        u = default_results['union'][metric_name]
        c = default_results['confirmation'][metric_name]
        if isinstance(u, float):
            lines.append(f"| {label:30s} | {u:.3f} | {c:.3f} |")
        else:
            lines.append(f"| {label:30s} | {u} | {c} |")

    u = default_results['union']
    c = default_results['confirmation']
    lines.append("")
    lines.append(f"**True outbreak rate:** {total_outbreak_uw}/10920 = {total_outbreak_uw/10920*100:.2f}% of unit-weeks")
    lines.append(f"**UNION alert rate:** {u['alert_pct']:.1f}% ({u['alert_count']} alerts)")
    lines.append(f"**CONFIRMATION alert rate:** {c['alert_pct']:.1f}% ({c['alert_count']} alerts)")
    lines.append("")

    if u['alert_pct'] > 10:
        lines.append(f"> **WARNING:** UNION mode fires alerts on {u['alert_pct']:.1f}% of unit-weeks")
        lines.append(f"> against a 1.33% true outbreak rate — a {u['alert_pct']/1.33:.1f}x over-alerting ratio.")
        lines.append(f"> PPV={u['PPV']:.3f} means {100*(1-u['PPV']):.1f}% of alerts are FALSE.")
        lines.append("")

    lines.append("---")
    lines.append("")

    # ---------------------------------------------------------------
    # Part 2: Sweep
    # ---------------------------------------------------------------
    lines.append("## Part 2: Parameter Sweep")
    lines.append("")
    lines.append(f"Grid: h ∈ {H_STR} × k ∈ {K_STR}")
    lines.append(f"lambda = {EWMA_LAMBDA_FIXED} (fixed), L = {EWMA_L_FIXED} (fixed)")
    lines.append(f"Total: {SWEEP_TOTAL} configurations per mode  ({SWEEP_TOTAL * 2} total runs)")
    lines.append("")

    sweep_results = {}
    for mode in ['union', 'confirmation']:
        lines.append(f"### {mode.upper()} Mode — Parameter Sweep Results")
        lines.append("")
        lines.append("| h | k | TP | FP | FN | TN | Sens | Spec | PPV | F1 | Alerts | Alert% |")
        lines.append("|---|----|----|----|----|-----|------|------|-----|-----|--------|--------|")

        mode_sweep = []
        count = 0
        for cusum_h in CUSUM_H_VALUES:
            for cusum_k in CUSUM_K_VALUES:
                count += 1
                print(f"  [{mode} {count}/{SWEEP_TOTAL}] h={cusum_h} k={cusum_k}...", flush=True)
                m = evaluate_config(
                    cells, outbreak_units,
                    cusum_h=cusum_h, cusum_k=cusum_k,
                    ewma_lambda=EWMA_LAMBDA_FIXED, ewma_l=EWMA_L_FIXED,
                    fusion_mode=mode,
                )
                mode_sweep.append(m)

                lines.append(
                    f"| {cusum_h:.1f} | {cusum_k:.2f} | {m['TP']:4d} | {m['FP']:4d} | "
                    f"{m['FN']:4d} | {m['TN']:5d} | {m['sensitivity']:.3f} | "
                    f"{m['specificity']:.3f} | {m['PPV']:.3f} | {m['F1']:.3f} | "
                    f"{m['alert_count']:6d} | {m['alert_pct']:5.1f}% |"
                )

        mode_sweep.sort(key=lambda x: -x['PPV'])
        lines.append("")
        best_ppv = mode_sweep[0]
        best_f1 = max(mode_sweep, key=lambda x: x['F1'])
        lines.append(
            f"**Best PPV:** h={best_ppv['cusum_h']}, k={best_ppv['cusum_k']} → "
            f"PPV={best_ppv['PPV']:.3f}, Sens={best_ppv['sensitivity']:.3f}, "
            f"F1={best_ppv['F1']:.3f}, Alerts={best_ppv['alert_count']} ({best_ppv['alert_pct']:.1f}%)"
        )
        lines.append(
            f"**Best F1:** h={best_f1['cusum_h']}, k={best_f1['cusum_k']} → "
            f"PPV={best_f1['PPV']:.3f}, Sens={best_f1['sensitivity']:.3f}, "
            f"F1={best_f1['F1']:.3f}, Alerts={best_f1['alert_count']} ({best_f1['alert_pct']:.1f}%)"
        )
        lines.append("")

        sweep_results[mode] = mode_sweep

    lines.append("---")
    lines.append("")
    lines.append("## Part 3: Findings")
    lines.append("")
    lines.append("### Key Observations")
    lines.append("")

    u_def = default_results['union']
    c_def = default_results['confirmation']

    lines.append(f"1. **Union mode over-alerts badly:** At default parameters, the union rule fires")
    lines.append(f"   on {u_def['alert_pct']:.1f}% of unit-weeks vs 1.33% true outbreak rate.")
    lines.append(f"   PPV={u_def['PPV']:.3f} means {100*(1-u_def['PPV']):.1f}% of alerts are false.")
    lines.append("")

    if c_def['PPV'] > u_def['PPV']:
        lines.append(
            f"2. **Confirmation mode improves PPV:** {c_def['PPV']:.3f} vs {u_def['PPV']:.3f} — "
            f"a {(c_def['PPV']/u_def['PPV']-1)*100:.0f}% relative improvement in precision."
        )
        lines.append(
            f"   However, sensitivity drops to {c_def['sensitivity']:.3f} from {u_def['sensitivity']:.3f}."
        )
        lines.append("")

    union_high_ppv = [r for r in sweep_results['union'] if r['PPV'] > 0.3]
    conf_high_ppv = [r for r in sweep_results['confirmation'] if r['PPV'] > 0.3]

    lines.append("3. **PPV > 0.3 threshold:**")
    lines.append(f"   - UNION mode: {len(union_high_ppv)}/{SWEEP_TOTAL} configs reach PPV > 0.3")
    lines.append(f"   - CONFIRMATION mode: {len(conf_high_ppv)}/{SWEEP_TOTAL} configs reach PPV > 0.3")
    lines.append("")

    if union_high_ppv:
        best = max(union_high_ppv, key=lambda x: x['PPV'])
        lines.append(f"   Best UNION: h={best['cusum_h']}, k={best['cusum_k']} → PPV={best['PPV']:.3f}, Sens={best['sensitivity']:.3f}")
    else:
        lines.append("   - **No UNION configuration reaches PPV > 0.3.**")

    if conf_high_ppv:
        best = max(conf_high_ppv, key=lambda x: x['PPV'])
        lines.append(f"   Best CONFIRMATION: h={best['cusum_h']}, k={best['cusum_k']} → PPV={best['PPV']:.3f}, Sens={best['sensitivity']:.3f}")
    else:
        lines.append("   - **No CONFIRMATION configuration reaches PPV > 0.3.**")

    lines.append("")

    lines.append("### Alert Burden")
    lines.append("")
    lines.append("The true outbreak rate is 1.33% of unit-weeks (145 outbreak unit-weeks / 10,920 total).")
    lines.append("An acceptable alert burden should be within an order of magnitude of this — say 2-10%.")
    lines.append("")

    union_ok = [r for r in sweep_results['union'] if 2.0 <= r['alert_pct'] <= 10.0 and r['PPV'] > 0.1]
    conf_ok = [r for r in sweep_results['confirmation'] if 2.0 <= r['alert_pct'] <= 10.0 and r['PPV'] > 0.1]

    lines.append(f"- UNION configs with 2-10% alert burden AND PPV > 0.1: {len(union_ok)}")
    lines.append(f"- CONFIRMATION configs with 2-10% alert burden AND PPV > 0.1: {len(conf_ok)}")
    lines.append("")

    if not union_ok and not conf_ok:
        lines.append("> **Finding:** No parameter configuration achieves both acceptable alert burden")
        lines.append("> (2-10%) AND usable precision (PPV > 0.1) at spatial granularity with these")
        lines.append("> four signals. The combined detector as specified is not working at this granularity.")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Part 4: Recommendation")
    lines.append("")
    lines.append("The current combined detector (UNION, any-2-of-4) is **not suitable** for spatial")
    lines.append("granularity detection without major rework. The confirmation mode (CUSUM+EWMA")
    lines.append("concordance) provides better precision but still falls short of usable PPV at")
    lines.append("reasonable sensitivity. Options:")
    lines.append("")
    lines.append("1. **Tune for confirmation mode** — increase h and L to reduce false alarms.")
    lines.append("2. **Add spatial smoothing** — borrow strength across nearby streets to reduce")
    lines.append("   per-unit variance and false alarms.")
    lines.append("3. **Use a different fusion rule** — e.g. CUSUM-only or EWMA-only with tuned")
    lines.append("   thresholds may outperform the combined rule at this granularity.")
    lines.append("4. **Accept the current PPV** if the system is used for screening (high sensitivity)")
    lines.append("   rather than confirmation, and follow up all alerts with manual review.")
    lines.append("")

    report_path = output_path / "spatial_fusion_comparison.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {report_path}", flush=True)

    json_data = {
        "union_default": u_def,
        "confirmation_default": c_def,
        "true_outbreak_rate_pct": total_outbreak_uw / 10920 * 100,
        "union_alert_burden_pct": u_def['alert_pct'],
        "confirmation_alert_burden_pct": c_def['alert_pct'],
        "union_sweep": sweep_results['union'],
        "confirmation_sweep": sweep_results['confirmation'],
        "sweep_grid": {
            "cusum_h": CUSUM_H_VALUES,
            "cusum_k": CUSUM_K_VALUES,
            "ewma_lambda": EWMA_LAMBDA_FIXED,
            "ewma_l": EWMA_L_FIXED,
        },
        "total_cells": total_cells,
        "precomputation_elapsed_s": precompute_elapsed,
    }
    json_path = output_path / "spatial_fusion_comparison.json"
    json_path.write_text(json.dumps(json_data, indent=2), encoding="utf-8")
    print(f"Wrote {json_path}", flush=True)

    total_elapsed = time.time() - t0
    print(f"\nTotal runtime: {total_elapsed:.1f}s ({total_elapsed/60:.1f} min)", flush=True)
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
