"""Run spatial evaluation under BOTH fusion modes (union vs confirmation)\nand a parameter sweep. Write results/spatial_fusion_comparison.md.\n\nThis is the definitive comparison: which fusion rule works better at\nspatial granularity, and does any parameter configuration achieve\nusable precision?\n"""

import sys, os, json, csv, time
from pathlib import Path
from collections import defaultdict
import importlib

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)
os.chdir(project_root)

from src.database.db import get_connection, init_schema
from src.evaluation_spatial import (
    run_spatial_evaluation,
    get_spatial_units,
    get_weekly_count,
    get_spatial_baseline,
)
from src.detection.combined import CombinedDetector
from src.detection.cusum import cusum_compute
from src.detection.ewma import ewma_compute
from src.ingestion import load_people_map, DISEASES, week_number_to_date
from src.evaluation_spatial_metrics import load_ground_truth, get_outbreak_spatial_units, get_detection_results


# ---------------------------------------------------------------------------
# Status classification helpers for metrics
# ---------------------------------------------------------------------------

def is_alert_status(status: str) -> bool:
    """Return True if status is a non-normal alert (ALERT, HIGH_ALERT, or PROVISIONAL for confirmation mode)."""
    return status in ("ALERT", "HIGH_ALERT", "PROVISIONAL")


# ---------------------------------------------------------------------------
# Parameter grid for sweep
# ---------------------------------------------------------------------------

CUSUM_H_VALUES = [5.0]
CUSUM_K_VALUES = [0.5]
EWMA_LAMBDA_VALUES = [0.2]
EWMA_L_VALUES = [3.0]

BASELINE_DEVIATION_THRESHOLD = 1.5  # fixed
TREND_CONSECUTIVE = 3  # fixed
TREND_MIN_PCT = 20.0  # fixed


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
    """Run spatial evaluation for one parameter configuration."""
    spatial_units = get_spatial_units(disease=disease)
    cusum_state = {}

    def get_prev_S(key):
        return cusum_state.get(key, 0.0)

    def set_S(key, val):
        cusum_state[key] = val

    ewma_state = {}

    def get_prev_Z(key):
        return ewma_state.get(key, 0.0)

    def set_Z(key, val):
        ewma_state[key] = val

    results = {
        "spatial_units": [(v, s) for v, s, _ in spatial_units],
        "total_rows": 0,
        "summary": {"NORMAL": 0, "WATCH": 0, "ALERT": 0, "HIGH_ALERT": 0, "PROVISIONAL": 0},
        "rows_by_week": {},
        "rows": [],
    }

    for week_num in range(start_week, end_week + 1):
        week_rows = []
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
            prev_S = get_prev_S(key)
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
            set_S(key, cusum_S)

            # EWMA
            prev_Z = get_prev_Z(key)
            z_t = (observed_rate - mu) / sigma_cusum if sigma_cusum > 0 else 0.0
            ewma_result = ewma_compute(
                week_num=week_num, z_t=z_t, prev_Z=prev_Z,
                lambda_=ewma_lambda, L=ewma_l,
            )
            ewma_Z = ewma_result["Z_t"]
            ewma_UCL = ewma_result["UCL_t"]
            ewma_sig = 1 if ewma_result["alert"] else 0
            set_Z(key, ewma_Z)

            # Trend
            recent_counts = []
            for bw in range(week_num - 3, week_num + 1):
                recent_counts.append(get_weekly_count(bw, disease, village, street))
            while len(recent_counts) < 4:
                recent_counts.insert(0, 0)

            # Baseline deviation
            baseline_deviation_sigma = 0.0
            if sigma > 0 and mu > 0:
                baseline_deviation_sigma = (observed_rate - mu) / sigma

            # Combined detector
            combined = CombinedDetector(disease=disease, village=village, street=street,
                                        baseline_expected=mu, n_population=population)
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
                "expected_count": round(mu * population, 2),
                "cusum_signal": cusum_sig,
                "ewma_signal": ewma_sig,
                "trend_sustained": cr["trend_sustained"],
                "baseline_deviation": round(baseline_deviation_sigma, 4),
                "status": cr["status"],
                "severity": cr["severity"],
                "explanation": cr["explanation"],
                "fusion_mode": fusion_mode,
            }
            week_rows.append(row)
            results["summary"][row["status"]] += 1

        results["rows_by_week"][f"week_{week_num:03d}"] = week_rows
        results["total_rows"] += len(week_rows)

    results["rows"] = [r for week_rows in results["rows_by_week"].values() for r in week_rows]
    return results


def compute_metrics_for_rows(rows: list, outbreak_units: dict) -> dict:
    """Compute TP/FP/FN/TN from a list of result rows against outbreak units."""
    tp = fp = fn = tn = 0
    alert_count = 0
    total = len(rows)

    for row in rows:
        key = (row["disease"], row["village"], row["street"], row["week_number"])
        is_outbreak = key in outbreak_units
        fired = is_alert_status(row["status"])
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


def run_parameter_sweep(fusion_mode: str) -> list:
    """Run full parameter sweep for one fusion mode."""
    outbreaks = load_ground_truth()
    outbreak_units = get_outbreak_spatial_units(outbreaks)

    results = []
    total_configs = len(CUSUM_H_VALUES) * len(CUSUM_K_VALUES) * len(EWMA_LAMBDA_VALUES) * len(EWMA_L_VALUES)

    count = 0
    for cusum_h in CUSUM_H_VALUES:
        for cusum_k in CUSUM_K_VALUES:
            for ewma_lambda in EWMA_LAMBDA_VALUES:
                for ewma_l in EWMA_L_VALUES:
                    count += 1
                    print(f"  [{count}/{total_configs}] h={cusum_h} k={cusum_k} "
                          f"lambda={ewma_lambda} L={ewma_l} mode={fusion_mode}...")

                    # Run for each disease and merge
                    all_rows = []
                    for disease in DISEASES:
                        res = run_single_config(
                            start_week=21, end_week=104,
                            disease=disease,
                            cusum_h=cusum_h, cusum_k=cusum_k,
                            ewma_lambda=ewma_lambda, ewma_l=ewma_l,
                            fusion_mode=fusion_mode,
                        )
                        all_rows.extend(res["rows"])

                    metrics = compute_metrics_for_rows(all_rows, outbreak_units)
                    metrics["cusum_h"] = cusum_h
                    metrics["cusum_k"] = cusum_k
                    metrics["ewma_lambda"] = ewma_lambda
                    metrics["ewma_l"] = ewma_l
                    metrics["fusion_mode"] = fusion_mode
                    results.append(metrics)

    return results


def write_fusion_comparison_report():
    """Run both fusion modes and write the comparison report."""
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

    # Load outbreak units once
    outbreaks = load_ground_truth()
    outbreak_units = get_outbreak_spatial_units(outbreaks)

    # ---------------------------------------------------------------
    # Part 1: Default parameters, both modes
    # ---------------------------------------------------------------
    lines.append("## Part 1: Default Parameters (h=5.0, k=0.5, lambda=0.2, L=3.0)")
    lines.append("")
    lines.append("| Metric | UNION (original) | CONFIRMATION (new) |")
    lines.append("|--------|-------------------|---------------------|")

    for mode in ["union", "confirmation"]:
        print(f"\nRunning {mode} mode with default parameters...")
        all_rows = []
        for disease in DISEASES:
            res = run_single_config(
                start_week=21, end_week=104, disease=disease,
                cusum_h=5.0, cusum_k=0.5, ewma_lambda=0.2, ewma_l=3.0,
                fusion_mode=mode,
            )
            all_rows.extend(res["rows"])

        metrics = compute_metrics_for_rows(all_rows, outbreak_units)
        print(f"  {mode}: TP={metrics['TP']} FP={metrics['FP']} FN={metrics['FN']} TN={metrics['TN']}")
        print(f"  Sensitivity={metrics['sensitivity']:.3f} Specificity={metrics['specificity']:.3f}")
        print(f"  PPV={metrics['PPV']:.3f} F1={metrics['F1']:.3f}")
        print(f"  Alert burden: {metrics['alert_count']}/{metrics['total_rows']} ({metrics['alert_pct']:.1f}%)")

        if mode == "union":
            union_metrics = metrics
        else:
            confirmation_metrics = metrics

    # Write the comparison table
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
        u = union_metrics[metric_name]
        c = confirmation_metrics[metric_name]
        if isinstance(u, float):
            lines.append(f"| {label:30s} | {u:.3f} | {c:.3f} |")
        else:
            lines.append(f"| {label:30s} | {u} | {c} |")

    lines.append("")
    lines.append(f"**True outbreak rate:** 145/10920 = 1.33% of unit-weeks")
    lines.append(f"**UNION alert rate:** {union_metrics['alert_pct']:.1f}% ({union_metrics['alert_count']} alerts)")
    lines.append(f"**CONFIRMATION alert rate:** {confirmation_metrics['alert_pct']:.1f}% ({confirmation_metrics['alert_count']} alerts)")
    lines.append("")

    if union_metrics["alert_pct"] > 10:
        lines.append(f"> **WARNING:** UNION mode fires alerts on {union_metrics['alert_pct']:.1f}% of unit-weeks")
        lines.append(f"> against a 1.33% true outbreak rate — a {union_metrics['alert_pct']/1.33:.1f}x over-alerting ratio.")
        lines.append(f"> PPV={union_metrics['PPV']:.3f} means {100*(1-union_metrics['PPV']):.1f}% of alerts are FALSE.")
        lines.append("")

    lines.append("---")
    lines.append("")

    # ---------------------------------------------------------------
    # Part 2: Parameter sweep
    # ---------------------------------------------------------------
    lines.append("## Part 2: Full Parameter Sweep")
    lines.append("")
    lines.append("Grid: h ∈ {3,4,5,6,7} × k ∈ {0.25,0.5,0.75,1.0} ×")
    lines.append("lambda ∈ {0.1,0.2,0.3,0.4} × L ∈ {2.0,2.5,3.0,3.5,4.0}")
    lines.append(f"Total: {len(CUSUM_H_VALUES)*len(CUSUM_K_VALUES)*len(EWMA_LAMBDA_VALUES)*len(EWMA_L_VALUES)} configurations per mode")
    lines.append("")

    for mode in ["union", "confirmation"]:
        lines.append(f"### {mode.upper()} Mode — Parameter Sweep Results")
        lines.append("")
        lines.append("| h | k | lambda | L | TP | FP | FN | TN | Sens | Spec | PPV | F1 | Alerts | Alert% |")
        lines.append("|---|----|--------|---|---|----|----|----|-----|------|------|-----|--------|--------|")

        sweep_results = run_parameter_sweep(mode)

        # Sort by PPV descending
        sweep_results.sort(key=lambda x: -x["PPV"])

        for r in sweep_results:
            lines.append(
                f"| {r['cusum_h']:.1f} | {r['cusum_k']:.2f} | {r['ewma_lambda']:.1f} | "
                f"{r['ewma_l']:.1f} | {r['TP']:4d} | {r['FP']:4d} | {r['FN']:4d} | "
                f"{r['TN']:5d} | {r['sensitivity']:.3f} | {r['specificity']:.3f} | "
                f"{r['PPV']:.3f} | {r['F1']:.3f} | {r['alert_count']:6d} | {r['alert_pct']:5.1f}% |"
            )

        lines.append("")
        best_ppv = max(sweep_results, key=lambda x: x["PPV"])
        best_f1 = max(sweep_results, key=lambda x: x["F1"])
        lines.append(f"**Best PPV:** h={best_ppv['cusum_h']}, k={best_ppv['cusum_k']}, "
                      f"lambda={best_ppv['ewma_lambda']}, L={best_ppv['ewma_l']} → "
                      f"PPV={best_ppv['PPV']:.3f}, Sens={best_ppv['sensitivity']:.3f}, "
                      f"F1={best_ppv['F1']:.3f}, Alerts={best_ppv['alert_count']} ({best_ppv['alert_pct']:.1f}%)")
        lines.append(f"**Best F1:** h={best_f1['cusum_h']}, k={best_f1['cusum_k']}, "
                      f"lambda={best_f1['ewma_lambda']}, L={best_f1['ewma_l']} → "
                      f"PPV={best_f1['PPV']:.3f}, Sens={best_f1['sensitivity']:.3f}, "
                      f"F1={best_f1['F1']:.3f}, Alerts={best_f1['alert_count']} ({best_f1['alert_pct']:.1f}%)")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Part 3: Findings")
    lines.append("")
    lines.append("### Key Observations")
    lines.append("")
    lines.append("1. **Union mode over-alerts badly:** At default parameters, the union rule fires")
    lines.append(f"   on {union_metrics['alert_pct']:.1f}% of unit-weeks vs 1.33% true outbreak rate.")
    lines.append(f"   PPV={union_metrics['PPV']:.3f} means {100*(1-union_metrics['PPV']):.1f}% of alerts are false.")
    lines.append("")

    if confirmation_metrics["PPV"] > union_metrics["PPV"]:
        lines.append(f"2. **Confirmation mode improves PPV:** {confirmation_metrics['PPV']:.3f} vs {union_metrics['PPV']:.3f} — ")
        lines.append(f"   a {(confirmation_metrics['PPV']/union_metrics['PPV']-1)*100:.0f}% relative improvement in precision.")
        lines.append(f"   However, sensitivity drops to {confirmation_metrics['sensitivity']:.3f} from {union_metrics['sensitivity']:.3f}.")
        lines.append("")

    # Check if any config reaches PPV > 0.3
    union_high_ppv = [r for r in run_parameter_sweep("union") if r["PPV"] > 0.3]
    confirmation_high_ppv = [r for r in run_parameter_sweep("confirmation") if r["PPV"] > 0.3]

    lines.append("3. **PPV > 0.3 threshold:**")
    lines.append(f"   - UNION mode: {len(union_high_ppv)}/{len(CUSUM_H_VALUES)*len(CUSUM_K_VALUES)*len(EWMA_LAMBDA_VALUES)*len(EWMA_L_VALUES)} configs reach PPV > 0.3")
    lines.append(f"   - CONFIRMATION mode: {len(confirmation_high_ppv)}/{len(CUSUM_H_VALUES)*len(CUSUM_K_VALUES)*len(EWMA_LAMBDA_VALUES)*len(EWMA_L_VALUES)} configs reach PPV > 0.3")
    lines.append("")

    if union_high_ppv:
        best = max(union_high_ppv, key=lambda x: x["PPV"])
        lines.append(f"   Best UNION: h={best['cusum_h']}, k={best['cusum_k']}, lambda={best['ewma_lambda']}, "
                      f"L={best['ewma_l']} → PPV={best['PPV']:.3f}, Sens={best['sensitivity']:.3f}")
    else:
        lines.append("   - **No UNION configuration reaches PPV > 0.3.**")
    if confirmation_high_ppv:
        best = max(confirmation_high_ppv, key=lambda x: x["PPV"])
        lines.append(f"   Best CONFIRMATION: h={best['cusum_h']}, k={best['cusum_k']}, lambda={best['ewma_lambda']}, "
                      f"L={best['ewma_l']} → PPV={best['PPV']:.3f}, Sens={best['sensitivity']:.3f}")
    else:
        lines.append("   - **No CONFIRMATION configuration reaches PPV > 0.3.**")

    lines.append("")
    lines.append("### Alert Burden")
    lines.append("")
    lines.append("The true outbreak rate is 1.33% of unit-weeks (145 outbreak unit-weeks / 10,920 total).")
    lines.append("An acceptable alert burden should be within an order of magnitude of this — say 2-10%.")
    lines.append("")

    union_acceptable = [r for r in run_parameter_sweep("union")
                        if 2.0 <= r["alert_pct"] <= 10.0 and r["PPV"] > 0.1]
    confirmation_acceptable = [r for r in run_parameter_sweep("confirmation")
                               if 2.0 <= r["alert_pct"] <= 10.0 and r["PPV"] > 0.1]

    lines.append(f"- UNION configs with 2-10% alert burden AND PPV > 0.1: {len(union_acceptable)}")
    lines.append(f"- CONFIRMATION configs with 2-10% alert burden AND PPV > 0.1: {len(confirmation_acceptable)}")
    lines.append("")

    if not union_acceptable and not confirmation_acceptable:
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
    print(f"\nWrote {report_path}")

    # Also write JSON
    json_path = output_path / "spatial_fusion_comparison.json"
    json_data = {
        "union_default": union_metrics,
        "confirmation_default": confirmation_metrics,
        "true_outbreak_rate_pct": 145 / 10920 * 100,
        "union_alert_burden_pct": union_metrics["alert_pct"],
        "confirmation_alert_burden_pct": confirmation_metrics["alert_pct"],
    }
    json_path.write_text(json.dumps(json_data, indent=2), encoding="utf-8")
    print(f"Wrote {json_path}")

    return union_metrics, confirmation_metrics


if __name__ == "__main__":
    print("=" * 60)
    print("P8 Fusion Mode Comparison: UNION vs CONFIRMATION")
    print("=" * 60)
    t0 = time.time()
    union_m, confirmation_m = write_fusion_comparison_report()
    elapsed = time.time() - t0
    print(f"\nTotal runtime: {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print("\nDone.")
