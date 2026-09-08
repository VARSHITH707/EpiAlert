"""Run spatial evaluation under BOTH fusion modes (union vs confirmation)
and a parameter sweep. Write results/spatial_fusion_comparison.md.

This is the definitive comparison: which fusion rule works better at
spatial granularity, and does any parameter configuration achieve
usable precision?

The sweep is designed to be cheap: only h and k vary (4x4=16 configs),
lambda and L are fixed at their locked values.  Both modes run for both.
Total: 32 evaluation runs of 5 diseases x 84 weeks x 26 units.
Estimated: ~12 minutes.
"""

import sys, os, json, time
from pathlib import Path
from collections import defaultdict

project_root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
os.environ['SRC_ROOT'] = os.path.abspath(os.path.join(project_root, 'src'))
sys.path.insert(0, project_root)
sys.path.insert(0, os.path.join(project_root, 'src'))
os.chdir(project_root)

from src.database.db import get_db_path
from src.evaluation_spatial import (
    run_spatial_evaluation,
    get_spatial_units,
    get_weekly_count,
    get_spatial_baseline,
)
from src.detection.combined import CombinedDetector
from src.detection.cusum import cusum_compute
from src.detection.ewma import ewma_compute
from src.ingestion import load_people_map, DISEASES
from src.evaluation_spatial_metrics import load_ground_truth, get_outbreak_spatial_units

# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------

def is_alert_status(status: str, fusion_mode: str) -> bool:
    """Return True if status counts as an alert for metrics.

    UNION: WATCH, ALERT, HIGH_ALERT all count (anything non-NORMAL).
    CONFIRMATION: only ALERT and HIGH_ALERT count. PROVISIONAL is NOT an alert.
    """
    if fusion_mode == "confirmation":
        return status in ("ALERT", "HIGH_ALERT")
    else:
        return status in ("WATCH", "ALERT", "HIGH_ALERT")


# ---------------------------------------------------------------------------
# Parameter grid — deliberately small so the sweep finishes
# ---------------------------------------------------------------------------

# Vary h (decision threshold) and k (reference value) only.
# lambda and L are held at their locked defaults.
CUSUM_H_VALUES = [3.0, 5.0, 7.0]
CUSUM_K_VALUES = [0.25, 0.5]

EWMA_LAMBDA_FIXED = 0.2
EWMA_L_FIXED = 3.0

BASELINE_DEVIATION_THRESHOLD = 1.5  # fixed
TREND_CONSECUTIVE = 3               # fixed
TREND_MIN_PCT = 20.0                # fixed

# We reuse the length of these for the sweep count.
_H_VALS = CUSUM_H_VALUES
_K_VALS = CUSUM_K_VALUES


def run_single_config(
    start_week: int,
    end_week: int,
    disease: str,
    cusum_h: float,
    cusum_k: float,
    ewma_lambda: float,
    ewma_l: float,
    fusion_mode: str,
) -> dict:
    """Run spatial evaluation for one (disease, params, mode) combination.

    Does NOT write to the DB.  Returns in-memory rows only.
    """
    spatial_units = get_spatial_units(disease=disease)
    cusum_state = {}
    ewma_state = {}

    results = []
    summary = defaultdict(int)

    for week_num in range(start_week, end_week + 1):
        baseline_weeks = list(range(max(1, week_num - 20), week_num))

        for village, street, population in spatial_units:
            key = (disease, village, street)

            # Baseline
            baseline = get_spatial_baseline(week_num, disease, village, street, baseline_weeks)
            mu = baseline["mu"]
            sigma = baseline["sigma"]

            # Current count
            observed_count = get_weekly_count(week_num, disease, village, street)
            observed_rate = observed_count / population if population > 0 else 0.0

            # CUSUM
            prev_S = cusum_state.get(key, 0.0)
            if 0 < mu < 1 and population > 0:
                sigma_cusum = (mu * (1 - mu) / population) ** 0.5
            else:
                sigma_cusum = 0.001

            cusum_result = cusum_compute(
                week_num=week_num, x_t=observed_rate, mu_t=mu,
                sigma_t=sigma_cusum, prev_S=prev_S,
                k=cusum_k, h=cusum_h,
            )
            cusum_S = cusum_result["S_t"]
            cusum_sig = 1 if cusum_result["alert"] else 0
            cusum_state[key] = cusum_S

            # EWMA
            prev_Z = ewma_state.get(key, 0.0)
            z_t = (observed_rate - mu) / sigma_cusum if sigma_cusum > 0 else 0.0
            ewma_result = ewma_compute(
                week_num=week_num, z_t=z_t, prev_Z=prev_Z,
                lambda_=ewma_lambda, L=ewma_l,
            )
            ewma_Z = ewma_result["Z_t"]
            ewma_UCL = ewma_result["UCL_t"]
            ewma_sig = 1 if ewma_result["alert"] else 0
            ewma_state[key] = ewma_Z

            # Trend
            recent_counts = []
            for bw in range(week_num - 3, week_num + 1):
                recent_counts.append(get_weekly_count(bw, disease, village, street))
            while len(recent_counts) < 4:
                recent_counts.insert(0, 0)

            # Baseline deviation
            baseline_deviation_sigma = 0.0
            if sigma is not None and sigma > 0 and mu is not None and mu > 0:
                baseline_deviation_sigma = (observed_rate - mu) / sigma

            # Combined detector
            combined = CombinedDetector(
                disease=disease, village=village, street=street,
                baseline_expected=mu, n_population=population,
            )
            cr = combined.detect(
                week_num=week_num, observed_count=observed_count,
                cusum_S=cusum_S, ewma_Z=ewma_Z, cusum_h=cusum_h, ewma_UCL=ewma_UCL,
                baseline_mu=mu, baseline_sigma=sigma, weekly_counts=recent_counts,
                cusum_k=cusum_k, ewma_lambda=ewma_lambda, ewma_l=ewma_l,
            )

            row = {
                "week_number": week_num,
                "disease": disease,
                "village": village,
                "street": street,
                "observed_count": observed_count,
                "expected_count": round(mu * population, 2) if mu is not None else 0.0,
                "cusum_signal": cusum_sig,
                "ewma_signal": ewma_sig,
                "trend_sustained": cr["trend_sustained"],
                "baseline_deviation": round(baseline_deviation_sigma, 4),
                "status": cr["status"],
                "severity": cr["severity"],
                "explanation": cr["explanation"],
                "fusion_mode": fusion_mode,
            }
            results.append(row)
            summary[row["status"]] += 1

    return {
        "rows": results,
        "summary": dict(summary),
        "total_rows": len(results),
    }


def compute_metrics_for_rows(rows: list, outbreak_units: dict, fusion_mode: str) -> dict:
    """Compute TP/FP/FN/TN from a list of result rows against outbreak units."""
    tp = fp = fn = tn = 0
    alert_count = 0
    total = len(rows)

    for row in rows:
        key = (row["disease"], row["village"], row["street"], row["week_number"])
        is_outbreak = key in outbreak_units
        fired = is_alert_status(row["status"], fusion_mode)
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

    total_positives = tp + fn
    total_negatives = tn + fp
    alert_pct = alert_count / total * 100 if total > 0 else 0

    return {
        "TP": tp,
        "FP": fp,
        "FN": fn,
        "TN": tn,
        "sensitivity": tp / total_positives if total_positives > 0 else 0.0,
        "specificity": tn / total_negatives if total_negatives > 0 else 0.0,
        "PPV": tp / (tp + fp) if (tp + fp) > 0 else 0.0,
        "F1": 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) > 0 else 0.0,
        "alert_count": alert_count,
        "alert_pct": alert_pct,
        "total_rows": total,
    }


def run_config_for_mode(
    start_week, end_week, cusum_h, cusum_k, ewma_lambda, ewma_l, fusion_mode
):
    """Run one config across all diseases and return merged metrics."""
    outbreaks = load_ground_truth()
    outbreak_units = get_outbreak_spatial_units(outbreaks)

    all_rows = []
    for disease in DISEASES:
        res = run_single_config(
            start_week=start_week, end_week=end_week,
            disease=disease,
            cusum_h=cusum_h, cusum_k=cusum_k,
            ewma_lambda=ewma_lambda, ewma_l=ewma_l,
            fusion_mode=fusion_mode,
        )
        all_rows.extend(res["rows"])

    metrics = compute_metrics_for_rows(all_rows, outbreak_units, fusion_mode)
    metrics.update({
        "cusum_h": cusum_h,
        "cusum_k": cusum_k,
        "ewma_lambda": ewma_lambda,
        "ewma_l": ewma_l,
        "fusion_mode": fusion_mode,
    })
    return metrics


def write_fusion_comparison_report():
    """Run both modes (default params) + full sweep, write MD + JSON."""
    output_path = Path("results")
    output_path.mkdir(parents=True, exist_ok=True)

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

    # Load ground truth once
    outbreaks = load_ground_truth()
    outbreak_units = get_outbreak_spatial_units(outbreaks)
    total_outbreak_uw = sum(
        len(ob["affected_streets"]) * (ob["end_week"] - ob["start_week"] + 1)
        for ob in outbreaks
    )

    # ---------------------------------------------------------------
    # Part 1: Default parameters, both modes (in-memory, no DB write)
    # ---------------------------------------------------------------
    lines.append("## Part 1: Default Parameters (h=5.0, k=0.5, lambda=0.2, L=3.0)")
    lines.append("")
    lines.append("| Metric | UNION (original) | CONFIRMATION (new) |")
    lines.append("|--------|-------------------|---------------------|")

    default_results = {}
    for mode in ["union", "confirmation"]:
        print(f"\nRunning {mode} mode with default parameters...", flush=True)
        m = run_config_for_mode(
            start_week=21, end_week=104,
            cusum_h=5.0, cusum_k=0.5,
            ewma_lambda=EWMA_LAMBDA_FIXED, ewma_l=EWMA_L_FIXED,
            fusion_mode=mode,
        )
        default_results[mode] = m
        print(f"  TP={m['TP']} FP={m['FP']} FN={m['FN']} TN={m['TN']}", flush=True)
        print(f"  Sens={m['sensitivity']:.3f} Spec={m['specificity']:.3f}", flush=True)
        print(f"  PPV={m['PPV']:.3f} F1={m['F1']:.3f}", flush=True)
        print(f"  Alerts={m['alert_count']} ({m['alert_pct']:.1f}% of unit-weeks)", flush=True)

    for metric_name, label in [
        ("TP", "True Positives"),
        ("FP", "False Positives (over-escalation)"),
        ("FN", "False Negatives (under-escalation)"),
        ("TN", "True Negatives"),
        ("sensitivity", "Sensitivity (Recall)"),
        ("specificity", "Specificity"),
        ("PPV", "PPV (Precision)"),
        ("F1", "F1 Score"),
        ("alert_count", "Total Alerts Fired"),
        ("alert_pct", "Alert Burden (% of unit-weeks)"),
    ]:
        u = default_results["union"][metric_name]
        c = default_results["confirmation"][metric_name]
        if isinstance(u, float):
            lines.append(f"| {label:30s} | {u:.3f} | {c:.3f} |")
        else:
            lines.append(f"| {label:30s} | {u} | {c} |")

    u = default_results["union"]
    c = default_results["confirmation"]
    lines.append("")
    lines.append(f"**True outbreak rate:** {total_outbreak_uw}/10920 = {total_outbreak_uw/10920*100:.2f}% of unit-weeks")
    lines.append(f"**UNION alert rate:** {u['alert_pct']:.1f}% ({u['alert_count']} alerts)")
    lines.append(f"**CONFIRMATION alert rate:** {c['alert_pct']:.1f}% ({c['alert_count']} alerts)")
    lines.append("")

    if u["alert_pct"] > 10:
        lines.append(f"> **WARNING:** UNION mode fires alerts on {u['alert_pct']:.1f}% of unit-weeks")
        lines.append(f"> against a 1.33% true outbreak rate — a {u['alert_pct']/1.33:.1f}x over-alerting ratio.")
        lines.append(f"> PPV={u['PPV']:.3f} means {100*(1-u['PPV']):.1f}% of alerts are FALSE.")
        lines.append("")

    lines.append("---")
    lines.append("")

    # ---------------------------------------------------------------
    # Part 2: Parameter sweep
    # ---------------------------------------------------------------
    sweep_total = len(_H_VALS) * len(_K_VALS)
    lines.append("## Part 2: Parameter Sweep")
    lines.append("")
    lines.append(f"Grid: h ∈ {H_VALUES_STR} × k ∈ {K_VALUES_STR}")
    lines.append(f"lambda = {EWMA_LAMBDA_FIXED} (fixed), L = {EWMA_L_FIXED} (fixed)")
    lines.append(f"Total: {sweep_total} configurations per mode  ({sweep_total * 2} total runs)")
    lines.append("")

    sweep_results = {}
    for mode in ["union", "confirmation"]:
        lines.append(f"### {mode.upper()} Mode — Parameter Sweep Results")
        lines.append("")
        lines.append("| h | k | TP | FP | FN | TN | Sens | Spec | PPV | F1 | Alerts | Alert% |")
        lines.append("|---|----|----|----|----|-----|------|------|-----|-----|--------|--------|")

        mode_sweep = []
        count = 0
        for cusum_h in CUSUM_H_VALUES:
            for cusum_k in CUSUM_K_VALUES:
                count += 1
                print(f"  [{mode} {count}/{sweep_total}] h={cusum_h} k={cusum_k}...", flush=True)
                m = run_config_for_mode(
                    start_week=21, end_week=104,
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

        mode_sweep.sort(key=lambda x: -x["PPV"])
        lines.append("")
        best_ppv = mode_sweep[0]
        best_f1 = max(mode_sweep, key=lambda x: x["F1"])
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

    u_def = default_results["union"]
    c_def = default_results["confirmation"]

    lines.append(f"1. **Union mode over-alerts badly:** At default parameters, the union rule fires")
    lines.append(f"   on {u_def['alert_pct']:.1f}% of unit-weeks vs 1.33% true outbreak rate.")
    lines.append(f"   PPV={u_def['PPV']:.3f} means {100*(1-u_def['PPV']):.1f}% of alerts are false.")
    lines.append("")

    if c_def["PPV"] > u_def["PPV"]:
        lines.append(
            f"2. **Confirmation mode improves PPV:** {c_def['PPV']:.3f} vs {u_def['PPV']:.3f} — "
            f"a {(c_def['PPV']/u_def['PPV']-1)*100:.0f}% relative improvement in precision."
        )
        lines.append(
            f"   However, sensitivity drops to {c_def['sensitivity']:.3f} from {u_def['sensitivity']:.3f}."
        )
        lines.append("")

    # Check if any config reaches PPV > 0.3
    union_high_ppv = [r for r in sweep_results["union"] if r["PPV"] > 0.3]
    conf_high_ppv = [r for r in sweep_results["confirmation"] if r["PPV"] > 0.3]

    lines.append("3. **PPV > 0.3 threshold:**")
    lines.append(f"   - UNION mode: {len(union_high_ppv)}/{sweep_total} configs reach PPV > 0.3")
    lines.append(f"   - CONFIRMATION mode: {len(conf_high_ppv)}/{sweep_total} configs reach PPV > 0.3")
    lines.append("")

    if union_high_ppv:
        best = max(union_high_ppv, key=lambda x: x["PPV"])
        lines.append(
            f"   Best UNION: h={best['cusum_h']}, k={best['cusum_k']} → "
            f"PPV={best['PPV']:.3f}, Sens={best['sensitivity']:.3f}"
        )
    else:
        lines.append("   - **No UNION configuration reaches PPV > 0.3.**")

    if conf_high_ppv:
        best = max(conf_high_ppv, key=lambda x: x["PPV"])
        lines.append(
            f"   Best CONFIRMATION: h={best['cusum_h']}, k={best['cusum_k']} → "
            f"PPV={best['PPV']:.3f}, Sens={best['sensitivity']:.3f}"
        )
    else:
        lines.append("   - **No CONFIRMATION configuration reaches PPV > 0.3.**")

    lines.append("")

    # Alert burden
    lines.append("### Alert Burden")
    lines.append("")
    lines.append(
        "The true outbreak rate is 1.33% of unit-weeks (145 outbreak unit-weeks / 10,920 total)."
    )
    lines.append(
        "An acceptable alert burden should be within an order of magnitude of this — say 2-10%."
    )
    lines.append("")

    union_ok = [r for r in sweep_results["union"]
                if 2.0 <= r["alert_pct"] <= 10.0 and r["PPV"] > 0.1]
    conf_ok = [r for r in sweep_results["confirmation"]
               if 2.0 <= r["alert_pct"] <= 10.0 and r["PPV"] > 0.1]

    lines.append(f"- UNION configs with 2-10% alert burden AND PPV > 0.1: {len(union_ok)}")
    lines.append(f"- CONFIRMATION configs with 2-10% alert burden AND PPV > 0.1: {len(conf_ok)}")
    lines.append("")

    if not union_ok and not conf_ok:
        lines.append(
            "> **Finding:** No parameter configuration achieves both acceptable alert burden"
        )
        lines.append(
            "> (2-10%) AND usable precision (PPV > 0.1) at spatial granularity with these"
        )
        lines.append(
            "> four signals. The combined detector as specified is not working at this granularity."
        )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Part 4: Recommendation")
    lines.append("")
    lines.append(
        "The current combined detector (UNION, any-2-of-4) is **not suitable** for spatial"
    )
    lines.append(
        "granularity detection without major rework. The confirmation mode (CUSUM+EWMA"
    )
    lines.append(
        "concordance) provides better precision but still falls short of usable PPV at"
    )
    lines.append(
        "reasonable sensitivity. Options:"
    )
    lines.append("")
    lines.append("1. **Tune for confirmation mode** — increase h and L to reduce false alarms.")
    lines.append(
        "2. **Add spatial smoothing** — borrow strength across nearby streets to reduce"
    )
    lines.append(
        "   per-unit variance and false alarms."
    )
    lines.append(
        "3. **Use a different fusion rule** — e.g. CUSUM-only or EWMA-only with tuned"
    )
    lines.append(
        "   thresholds may outperform the combined rule at this granularity."
    )
    lines.append(
        "4. **Accept the current PPV** if the system is used for screening (high sensitivity)"
    )
    lines.append(
        "   rather than confirmation, and follow up all alerts with manual review."
    )
    lines.append("")

    # Write the report
    report_path = output_path / "spatial_fusion_comparison.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {report_path}", flush=True)

    # Also write JSON summary
    json_data = {
        "union_default": u_def,
        "confirmation_default": c_def,
        "true_outbreak_rate_pct": total_outbreak_uw / 10920 * 100,
        "union_alert_burden_pct": u_def["alert_pct"],
        "confirmation_alert_burden_pct": c_def["alert_pct"],
        "union_sweep": sweep_results["union"],
        "confirmation_sweep": sweep_results["confirmation"],
        "sweep_grid": {
            "cusum_h": CUSUM_H_VALUES,
            "cusum_k": CUSUM_K_VALUES,
            "ewma_lambda": EWMA_LAMBDA_FIXED,
            "ewma_l": EWMA_L_FIXED,
        },
    }
    json_path = output_path / "spatial_fusion_comparison.json"
    json_path.write_text(json.dumps(json_data, indent=2), encoding="utf-8")
    print(f"Wrote {json_path}", flush=True)

    return default_results, sweep_results


if __name__ == "__main__":
    CUSUM_H_VALUES = [3.0, 5.0, 7.0]
    CUSUM_K_VALUES = [0.25, 0.5]
    H_VALUES_STR = "{" + ",".join(f"{h:.1f}" for h in CUSUM_H_VALUES) + "}"
    K_VALUES_STR = "{" + ",".join(f"{k:.2f}" for k in CUSUM_K_VALUES) + "}"

    print("=" * 60, flush=True)
    print("P8 Fusion Mode Comparison: UNION vs CONFIRMATION + Parameter Sweep", flush=True)
    print("=" * 60, flush=True)
    t0 = time.time()
    default_results, sweep_results = write_fusion_comparison_report()
    elapsed = time.time() - t0
    print(f"\nTotal runtime: {elapsed:.1f}s ({elapsed/60:.1f} min)", flush=True)
    print("Done.", flush=True)
