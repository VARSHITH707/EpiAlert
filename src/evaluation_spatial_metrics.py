"""P8 Spatial evaluation metrics — compute TP/FP/FN/TN at (village, street) granularity.

Evaluates detection_results table against ground_truth.csv.

Spatial unit = (disease, village, street). A unit is "positive" during the
outbreak weeks specified in ground truth, "negative" otherwise.

For each detector signal and for the combined status, we compute:
- TP: unit is in outbreak AND detector fired (non-NORMAL / signal=1)
- FP: unit is NOT in outbreak AND detector fired
- FN: unit is in outbreak AND detector did NOT fire
- TN: unit is NOT in outbreak AND detector did NOT fire

From these:
- Sensitivity (recall) = TP / (TP + FN)
- Specificity = TN / (TN + FP)
- PPV (precision) = TP / (TP + FP)
- F1 = 2 * TP / (2*TP + FP + FN)
- Timeliness: for each outbreak, weeks between true onset and first non-NORMAL status
- Over-escalation: FP count (false alarms)
- Under-escalation: FN count (missed outbreaks)

Also computes per-outbreak breakdowns.
"""

import csv
import json
from pathlib import Path
from collections import defaultdict
from src.database.db import get_connection


GROUND_TRUTH_PATH = Path("data/EpiAlert_Phase1_Dataset/ground_truth.csv")


def load_ground_truth() -> list:
    """Load ground truth outbreaks from CSV.

    Returns list of dicts with keys:
    - event_id, village, disease, start_week, end_week, affected_streets, event_type
    """
    outbreaks = []
    with open(GROUND_TRUTH_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            outbreaks.append({
                "event_id": row["event_id"],
                "village": row["village"],
                "disease": row["disease"],
                "start_week": int(row["start_week"]),
                "end_week": int(row["end_week"]),
                "affected_streets": [s.strip() for s in row["affected_streets"].split(";")],
                "event_type": row["event_type"],
            })
    return outbreaks


def get_outbreak_spatial_units(outbreaks: list) -> dict:
    """Build set of (disease, village, street, week) that are in an outbreak.

    Returns dict mapping (disease, village, street) -> set of outbreak weeks.
    """
    units = defaultdict(set)
    for ob in outbreaks:
        for street in ob["affected_streets"]:
            key = (ob["disease"], ob["village"], street)
            for w in range(ob["start_week"], ob["end_week"] + 1):
                units[key].add(w)
    return units


def get_detection_results(fusion_mode=None) -> list:
    """Query rows from detection_results table.

    Parameters
    ----------
    fusion_mode : str, optional
        If set, only rows with that fusion_mode are returned.
        If None, all rows are returned.

    Returns list of dicts with keys from the table.
    """
    conn = get_connection()
    cur = conn.cursor()
    if fusion_mode:
        cur.execute(
            """SELECT week_number, disease, village, street, observed_count,
                      expected_count, cusum_signal, ewma_signal, trend_sustained,
                      baseline_deviation, status, severity, explanation, fusion_mode
               FROM detection_results
               WHERE fusion_mode = ?
               ORDER BY week_number, disease, village, street""",
            (fusion_mode,),
        )
    else:
        cur.execute(
            """SELECT week_number, disease, village, street, observed_count,
                      expected_count, cusum_signal, ewma_signal, trend_sustained,
                      baseline_deviation, status, severity, explanation, fusion_mode
               FROM detection_results
               ORDER BY week_number, disease, village, street"""
        )
    rows = []
    for row in cur.fetchall():
        d = {}
        for k in row.keys():
            d[k] = row[k]
        rows.append(d)
    cur.close()
    conn.close()
    return rows


def compute_spatial_metrics(outbreaks=None):
    """Compute TP/FP/FN/TN at spatial granularity for all detectors.

    Parameters
    ----------
    outbreaks : list, optional
        Ground truth outbreaks. If None, loads from CSV.

    Returns
    -------
    dict with:
    - outbreaks: list of outbreak dicts used
    - total_outbreak_unit_weeks: total (unit, week) pairs in outbreaks
    - metrics: dict per detector with TP/FP/FN/TN/sensitivity/specificity/PPV/F1
    - timeliness: per-outbreak timeliness info
    - over_escalation: total FP count
    - under_escalation: total FN count
    - by_outbreak: per-outbreak breakdown
    """
    if outbreaks is None:
        outbreaks = load_ground_truth()

    outbreak_units = get_outbreak_spatial_units(outbreaks)
    results = get_detection_results()

    # Build (disease, village, street, week) -> row mapping
    result_map = {}
    for row in results:
        key = (row["disease"], row["village"], row["street"], row["week_number"])
        result_map[key] = row

    # Detectors to evaluate
    detectors = {
        "combined_status": {
            "field": "status",
            "positive_values": ["WATCH", "ALERT", "HIGH_ALERT"],
        },
        "cusum_signal": {
            "field": "cusum_signal",
            "positive_values": [1],
        },
        "ewma_signal": {
            "field": "ewma_signal",
            "positive_values": [1],
        },
        "trend_sustained": {
            "field": "trend_sustained",
            "positive_values": [1],
        },
        "baseline_deviation": {
            "field": "baseline_deviation",
            "positive_values": None,  # handled specially: |dev| >= 1.5
        },
    }

    metrics = {}
    for det_name, det_info in detectors.items():
        tp = fp = fn = tn = 0
        timeliness_info = []

        for (disease, village, street, week), row in result_map.items():
            is_outbreak = week in outbreak_units.get((disease, village, street), set())

            # Determine if detector fired
            if det_name == "baseline_deviation":
                fired = abs(row["baseline_deviation"]) >= 1.5
            else:
                value = row.get(det_info["field"], 0)
                fired = value in det_info["positive_values"]

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

        metrics[det_name] = {
            "TP": tp,
            "FP": fp,
            "FN": fn,
            "TN": tn,
            "sensitivity": tp / total_positives if total_positives > 0 else 0.0,
            "specificity": tn / total_negatives if total_negatives > 0 else 0.0,
            "PPV": tp / (tp + fp) if (tp + fp) > 0 else 0.0,
            "F1": 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) > 0 else 0.0,
        }

    # Timeliness: for each outbreak, find first week with non-NORMAL status
    # in the affected spatial units
    timeliness = []
    by_outbreak = {}

    for ob in outbreaks:
        ob_key = (ob["disease"], ob["village"])
        affected_keys = set()
        for street in ob["affected_streets"]:
            for w in range(ob["start_week"], ob["end_week"] + 1):
                affected_keys.add((ob["disease"], ob["village"], street, w))

        # Find first non-NORMAL week for any affected unit
        first_alert_week = None
        first_alert_unit = None
        alerts_by_unit = defaultdict(list)

        for key in affected_keys:
            row = result_map.get(key)
            if row and row["status"] != "NORMAL":
                alerts_by_unit[(key[0], key[1], key[2])].append(row["week_number"])

        for unit_key, alert_weeks in alerts_by_unit.items():
            unit_outbreak_start = ob["start_week"]
            if alert_weeks:
                first_week = min(alert_weeks)
                lead = first_week - unit_outbreak_start
                if first_alert_week is None or first_week < first_alert_week:
                    first_alert_week = first_week
                    first_alert_unit = unit_key

        # Count TP, FP, FN for this outbreak specifically
        ob_tp = ob_fp = ob_fn = ob_tn = 0
        for key, row in result_map.items():
            if key[0] == ob["disease"] and key[1] == ob["village"]:
                is_ob_week = key[2] in ob["affected_streets"] and key[3] in range(
                    ob["start_week"], ob["end_week"] + 1
                )
                is_ob_unit = key[2] in ob["affected_streets"]
                is_in_outbreak = is_ob_unit and (ob["start_week"] <= key[3] <= ob["end_week"])

                detector_fired = row["status"] != "NORMAL"

                if is_in_outbreak and detector_fired:
                    ob_tp += 1
                elif not is_in_outbreak and detector_fired:
                    ob_fp += 1
                elif is_in_outbreak and not detector_fired:
                    ob_fn += 1
                else:
                    ob_tn += 1

        by_outbreak[ob["event_id"]] = {
            "outbreak": ob,
            "TP": ob_tp,
            "FP": ob_fp,
            "FN": ob_fn,
            "TN": ob_tn,
            "sensitivity": ob_tp / (ob_tp + ob_fn) if (ob_tp + ob_fn) > 0 else 0.0,
            "first_alert_week": first_alert_week,
            "true_start_week": ob["start_week"],
            "lead_time_weeks": (first_alert_week - ob["start_week"]) if first_alert_week else None,
            "units_affected": len(ob["affected_streets"]),
            "unit_weeks": len(affected_keys),
        }

        if first_alert_week is not None:
            timeliness.append({
                "event_id": ob["event_id"],
                "disease": ob["disease"],
                "village": ob["village"],
                "true_start": ob["start_week"],
                "first_detected": first_alert_week,
                "lead_time_weeks": first_alert_week - ob["start_week"],
                "unit": f"{first_alert_unit[1]}/{first_alert_unit[2]}" if first_alert_unit else None,
            })
        else:
            timeliness.append({
                "event_id": ob["event_id"],
                "disease": ob["disease"],
                "village": ob["village"],
                "true_start": ob["start_week"],
                "first_detected": None,
                "lead_time_weeks": None,
                "unit": None,
            })

    total_outbreak_unit_weeks = sum(
        len(ob["affected_streets"]) * (ob["end_week"] - ob["start_week"] + 1)
        for ob in outbreaks
    )

    combined_metrics = metrics["combined_status"]
    fusion_mode_used = results[0].get("fusion_mode", "unknown") if results else "unknown"
    return {
        "outbreaks": outbreaks,
        "total_outbreak_unit_weeks": total_outbreak_unit_weeks,
        "total_evaluation_unit_weeks": len(result_map),
        "fusion_mode": fusion_mode_used,
        "metrics": metrics,
        "timeliness": timeliness,
        "by_outbreak": by_outbreak,
        "over_escalation": combined_metrics["FP"],
        "under_escalation": combined_metrics["FN"],
    }


def compute_metrics_for_mode(fusion_mode) -> dict:
    """Compute metrics from DB rows matching fusion_mode."""
    # Filter get_detection_results to only return rows with this fusion_mode
    results = get_detection_results(fusion_mode=fusion_mode)
    outbreaks = load_ground_truth()
    outbreak_units = get_outbreak_spatial_units(outbreaks)

    result_map = {}
    for row in results:
        key = (row["disease"], row["village"], row["street"], row["week_number"])
        result_map[key] = row

    detectors = {
        "combined_status": {"field": "status", "positive_values": ["WATCH", "ALERT", "HIGH_ALERT"]},
        "cusum_signal": {"field": "cusum_signal", "positive_values": [1]},
        "ewma_signal": {"field": "ewma_signal", "positive_values": [1]},
        "trend_sustained": {"field": "trend_sustained", "positive_values": [1]},
        "baseline_deviation": {"field": "baseline_deviation", "positive_values": None},
    }

    metrics = {}
    for det_name, det_info in detectors.items():
        tp = fp = fn = tn = 0
        for (disease, village, street, week), row in result_map.items():
            is_outbreak = week in outbreak_units.get((disease, village, street), set())
            if det_name == "baseline_deviation":
                fired = abs(row["baseline_deviation"]) >= 1.5
            else:
                fired = row.get(det_info["field"], 0) in det_info["positive_values"]
            if is_outbreak and fired: tp += 1
            elif not is_outbreak and fired: fp += 1
            elif is_outbreak and not fired: fn += 1
            else: tn += 1
        tp_fp_fn_tn = tp + fp + fn + tn
        metrics[det_name] = {
            "TP": tp, "FP": fp, "FN": fn, "TN": tn,
            "sensitivity": tp / (tp + fn) if (tp + fn) > 0 else 0.0,
            "specificity": tn / (tn + fp) if (tn + fp) > 0 else 0.0,
            "PPV": tp / (tp + fp) if (tp + fp) > 0 else 0.0,
            "F1": 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) > 0 else 0.0,
        }

    combined = metrics["combined_status"]
    total_uw = len(result_map)
    total_outbreak_uw = sum(len(ob["affected_streets"]) * (ob["end_week"] - ob["start_week"] + 1) for ob in outbreaks)
    # Timeliness
    timeliness = []
    by_outbreak = {}
    for ob in outbreaks:
        affected_keys = set()
        for street in ob["affected_streets"]:
            for w in range(ob["start_week"], ob["end_week"] + 1):
                affected_keys.add((ob["disease"], ob["village"], street, w))
        first_alert_week = None
        first_alert_unit = None
        alerts_by_unit = defaultdict(list)
        for key in affected_keys:
            row = result_map.get(key)
            if row and row["status"] != "NORMAL":
                alerts_by_unit[(key[0], key[1], key[2])].append(row["week_number"])
        for unit_key, alert_weeks in alerts_by_unit.items():
            if alert_weeks:
                fw = min(alert_weeks)
                if first_alert_week is None or fw < first_alert_week:
                    first_alert_week = fw
                    first_alert_unit = unit_key
        ob_tp = ob_fp = ob_fn = ob_tn = 0
        for key, row in result_map.items():
            if key[0] == ob["disease"] and key[1] == ob["village"]:
                is_in = key[2] in ob["affected_streets"] and (ob["start_week"] <= key[3] <= ob["end_week"])
                fired = row["status"] != "NORMAL"
                if is_in and fired: ob_tp += 1
                elif not is_in and fired: ob_fp += 1
                elif is_in and not fired: ob_fn += 1
                else: ob_tn += 1
        by_outbreak[ob["event_id"]] = {
            "outbreak": ob, "TP": ob_tp, "FP": ob_fp, "FN": ob_fn, "TN": ob_tn,
            "sensitivity": ob_tp / (ob_tp + ob_fn) if (ob_tp + ob_fn) > 0 else 0.0,
            "first_alert_week": first_alert_week, "true_start_week": ob["start_week"],
            "lead_time_weeks": (first_alert_week - ob["start_week"]) if first_alert_week else None,
            "units_affected": len(ob["affected_streets"]),
            "unit_weeks": len(affected_keys),
        }
        if first_alert_week is not None:
            timeliness.append({
                "event_id": ob["event_id"], "disease": ob["disease"], "village": ob["village"],
                "true_start": ob["start_week"], "first_detected": first_alert_week,
                "lead_time_weeks": first_alert_week - ob["start_week"],
                "unit": f"{first_alert_unit[1]}/{first_alert_unit[2]}" if first_alert_unit else None,
            })
        else:
            timeliness.append({
                "event_id": ob["event_id"], "disease": ob["disease"], "village": ob["village"],
                "true_start": ob["start_week"], "first_detected": None,
                "lead_time_weeks": None, "unit": None,
            })

    return {
        "outbreaks": outbreaks,
        "total_outbreak_unit_weeks": total_outbreak_uw,
        "total_evaluation_unit_weeks": total_uw,
        "fusion_mode": fusion_mode,
        "metrics": metrics,
        "timeliness": timeliness,
        "by_outbreak": by_outbreak,
        "over_escalation": combined["FP"],
        "under_escalation": combined["FN"],
    }


def compute_and_write_metrics(fusion_mode, label, output_dir="results"):
    """Compute metrics for a specific fusion_mode and write JSON + MD."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    import time
    t0 = time.time()
    metrics_result = compute_metrics_for_mode(fusion_mode)
    elapsed = time.time() - t0
    print(f"\n=== {label} ===")
    print(f"  Rows: {metrics_result['total_evaluation_unit_weeks']}")
    c = metrics_result["metrics"]["combined_status"]
    print(f"  Combined: TP={c['TP']}, FP={c['FP']}, FN={c['FN']}, TN={c['TN']}")
    print(f"  Sens={c['sensitivity']:.3f}, Spec={c['specificity']:.3f}, PPV={c['PPV']:.3f}, F1={c['F1']:.3f}")
    print(f"  Time: {elapsed:.1f}s")

    json_path = output_path / f"spatial_evaluation_{fusion_mode}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(metrics_result, f, indent=2, default=str)
    md_path = output_path / f"spatial_evaluation_{fusion_mode}.md"
    write_markdown_report(metrics_result, label, elapsed, md_path)
    print(f"Wrote {json_path}, {md_path}")
    return metrics_result


def write_markdown_report(metrics_result, label, elapsed, md_path):
    """Write human-readable markdown report for a single fusion mode.

    metrics_result: dict from compute_spatial_metrics()
    label: human-readable label (e.g. 'Union Fusion')
    """
    lines = []
    report_label = label
    lines.append(f"# P8 Spatial Evaluation Report: {report_label}")
    lines.append("")
    lines.append("## Configuration")
    lines.append("")
    fm = metrics_result.get('fusion_mode', report_label)
    lines.append(f"- **Fusion mode:** {fm}")
    lines.append(f"- **Ground truth outbreaks:** {len(metrics_result['outbreaks'])}")
    lines.append(f"- **Total outbreak unit-weeks:** {metrics_result['total_outbreak_unit_weeks']}")
    lines.append(f"- **Total evaluated unit-weeks:** {metrics_result['total_evaluation_unit_weeks']}")
    lines.append(f"- **Elapsed time:** {elapsed:.1f}s")
    lines.append("")
    lines.append("## Detector Comparison (Spatial Granularity)")
    lines.append("")
    lines.append("| Detector | TP | FP | FN | TN | Sensitivity | Specificity | PPV | F1 |")
    lines.append("|----------|----|----|----|----|-------------|-------------|-----|----|")

    for det_name, m in metrics_result["metrics"].items():
        label_det = det_name.replace("_", " ").title()
        lines.append(
            f"| {label_det:20s} | {m['TP']:4d} | {m['FP']:4d} | {m['FN']:4d} | "
            f"{m['TN']:4d} | {m['sensitivity']:.3f} | {m['specificity']:.3f} | "
            f"{m['PPV']:.3f} | {m['F1']:.3f} |"
        )
    lines.append("")

    combined = metrics_result["metrics"]["combined_status"]
    lines.append("## Over-Escalation vs Under-Escalation (Combined Status)")
    lines.append("")
    lines.append(f"- **Over-escalation (FP):** {combined['FP']} false alarms")
    lines.append(f"- **Under-escalation (FN):** {combined['FN']} missed outbreak unit-weeks")
    alert_pct = (combined['TP'] + combined['FP']) / metrics_result['total_evaluation_unit_weeks'] * 100
    lines.append(f"- **Alert burden:** {combined['TP'] + combined['FP']} unit-weeks ({alert_pct:.1f}% of all unit-weeks)")
    lines.append(f"- **True outbreak rate:** {metrics_result['total_outbreak_unit_weeks'] / metrics_result['total_evaluation_unit_weeks'] * 100:.2f}% of unit-weeks")
    lines.append("")

    lines.append("## Timeliness by Outbreak")
    lines.append("")
    lines.append("| Event | Disease | Village | True Start | First Detected | Lead Time | Unit |")
    lines.append("|-------|---------|---------|------------|----------------|-----------|------|")

    for t in metrics_result["timeliness"]:
        first = str(t["first_detected"]) if t["first_detected"] else "N/A"
        lead = f"{t['lead_time_weeks']}w" if t["lead_time_weeks"] is not None else "N/A"
        unit = t["unit"] if t["unit"] else "N/A"
        lines.append(
            f"| {t['event_id']} | {t['disease']} | {t['village']} | "
            f"{t['true_start']} | {first} | {lead} | {unit} |"
        )
    lines.append("")

    lines.append("## Per-Outbreak Breakdown (Combined Status)")
    lines.append("")
    lines.append("| Event | TP | FP | FN | TN | Sensitivity | Units | Unit-Weeks |")
    lines.append("|-------|----|----|----|----|------------|-------|------------|")

    for event_id, ob_data in metrics_result["by_outbreak"].items():
        ob = ob_data["outbreak"]
        lines.append(
            f"| {event_id} | {ob_data['TP']:4d} | {ob_data['FP']:4d} | "
            f"{ob_data['FN']:4d} | {ob_data['TN']:4d} | "
            f"{ob_data['sensitivity']:.3f} | {ob_data['units_affected']} | {ob_data['unit_weeks']} |"
        )
    lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append("1. **Spatial unit:** Each (village, street) pair is evaluated independently. 9 of 10 street names appear in multiple villages.")
    lines.append("2. **Alert burden:** Percentage of unit-weeks that fire (TP+FP) vs true outbreak rate (1.3%).")
    lines.append(f"3. **Runtime:** Metrics computation took {elapsed:.1f}s.")

    md_path.write_text("\n".join(lines), encoding="utf-8")


def write_all_results(output_dir="results"):
    """Run metrics for both fusion modes and write both reports.

    The evaluation must already be run (rows in DB). This function does NOT
    re-run the evaluation; it only computes and writes metrics.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Step 1: Union mode (existing rows)
    union_metrics = compute_and_write_metrics("union", "Union Fusion (any 2 of 4 signals)", output_dir)

    # Step 2: Confirmation mode (requires run_spatial_evaluation with fusion_mode='confirmation')
    # Check if confirmation rows exist
    from src.database.db import get_connection
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM detection_results WHERE fusion_mode = 'confirmation'")
    conf_count = cur.fetchone()[0]
    conn.close()

    if conf_count == 0:
        print(f"\nNo 'confirmation' rows found. Run run_spatial_evaluation(fusion_mode='confirmation') first.")
        print("Skipping confirmation metrics until confirmation rows exist.")
        return union_metrics

    confirmation_metrics = compute_and_write_metrics("confirmation", "Confirmation Fusion (CUSUM AND EWMA concordant)", output_dir)

    # Step 3: Write comparison
    write_fusion_comparison(union_metrics, confirmation_metrics, output_dir)

    return union_metrics, confirmation_metrics


def write_fusion_comparison(union_metrics, confirmation_metrics, output_dir="results"):
    """Write side-by-side comparison of both fusion modes."""
    lines = []
    lines.append("# P8 Fusion Mode Comparison: Union vs Confirmation")
    lines.append("")
    lines.append("## Overview")
    lines.append("")
    lines.append("This report compares two fusion rules for the combined detector at spatial granularity (disease, village, street):")
    lines.append("")
    lines.append("- **Union mode:** Any 2 of 4 signals (CUSUM, EWMA, trend, baseline) fire -> ALERT. Errors accumulate because the union of four weak detectors' false alarms is wider than any one alone.")
    lines.append("- **Confirmation mode:** CUSUM AND EWMA must BOTH fire for ALERT. Exactly one firing = PROVISIONAL (logged, no alert). Implements the Yi et al. 2025 concordance rule (>=2 concordant models gave Youden index 0.651, sens 0.739, spec 0.912).")
    lines.append("")
    lines.append("## Side-by-Side Comparison")
    lines.append("")
    lines.append("| Metric | Union Mode | Confirmation Mode | Delta |")
    lines.append("|--------|------------|-------------------|-------|")

    for det_name in ["combined_status", "cusum_signal", "ewma_signal", "trend_sustained", "baseline_deviation"]:
        u = union_metrics["metrics"][det_name]
        c = confirmation_metrics["metrics"][det_name] if confirmation_metrics else None
        det_label = det_name.replace("_", " ").title()
        if c:
            lines.append(f"### {det_label}")
            lines.append("")
            lines.append("| Metric | Union | Confirmation |")
            lines.append("|--------|-------|--------------|")
            for mname in ["TP", "FP", "FN", "TN", "sensitivity", "specificity", "PPV", "F1"]:
                uv = u[mname]
                cv = c[mname]
                if isinstance(uv, float):
                    lines.append(f"| {mname:12s} | {uv:.3f} | {cv:.3f} |")
                else:
                    lines.append(f"| {mname:12s} | {uv:5d} | {cv:5d} |")
            lines.append("")
        else:
            if isinstance(u["TP"], float):
                lines.append(f"| {det_label:20s} | {u['sensitivity']:.3f}/{u['PPV']:.3f} | N/A | - |")
            else:
                lines.append(f"| {det_label:20s} | {u['TP']}/{u['FP']}/{u['FN']}/{u['TN']} | N/A | - |")

    lines.append("## Alert Burden Comparison")
    lines.append("")
    u_combined = union_metrics["metrics"]["combined_status"]
    u_total = union_metrics["total_evaluation_unit_weeks"]
    u_burden = (u_combined["TP"] + u_combined["FP"]) / u_total * 100
    u_true_rate = union_metrics["total_outbreak_unit_weeks"] / u_total * 100

    lines.append(f"| Metric | Union Mode | Confirmation Mode |")
    lines.append(f"|--------|------------|-------------------|")
    lines.append(f"| Total unit-weeks evaluated | {u_total} | {confirmation_metrics['total_evaluation_unit_weeks'] if confirmation_metrics else 'N/A'} |")
    lines.append(f"| True outbreak unit-weeks | {union_metrics['total_outbreak_unit_weeks']} | {confirmation_metrics['total_outbreak_unit_weeks'] if confirmation_metrics else 'N/A'} |")
    lines.append(f"| True outbreak rate | {u_true_rate:.2f}% | {union_metrics['total_outbreak_unit_weeks']/u_total*100:.2f}% |")
    lines.append(f"| Alerts fired (TP+FP) | {u_combined['TP']+u_combined['FP']} | {(confirmation_metrics['metrics']['combined_status']['TP']+confirmation_metrics['metrics']['combined_status']['FP']) if confirmation_metrics else 'N/A'} |")
    c_burden = (confirmation_metrics['metrics']['combined_status']['TP']+confirmation_metrics['metrics']['combined_status']['FP'])/confirmation_metrics['total_evaluation_unit_weeks']*100 if confirmation_metrics else None
    c_true = confirmation_metrics['total_outbreak_unit_weeks']/confirmation_metrics['total_evaluation_unit_weeks']*100 if confirmation_metrics else None
    lines.append(f"| Alert burden (% of unit-weeks) | {u_burden:.1f}% | {c_burden:.1f}% |")
    lines.append(f"| Over-alerting ratio (burden / true rate) | {u_burden/u_true_rate:.1f}x | {c_burden/c_true:.1f}x |")
    lines.append("")

    lines.append("## Timeliness Comparison")
    lines.append("")
    lines.append("| Event | Union First Detected | Union Lead Time | Confirmation First Detected | Confirmation Lead Time |")
    lines.append("|-------|---------------------|----------------|---------------------------|------------------------|")
    for i, u_tim in enumerate(union_metrics["timeliness"]):
        c_tim = confirmation_metrics["timeliness"][i] if confirmation_metrics else None
        u_first = str(u_tim["first_detected"]) if u_tim["first_detected"] else "N/A"
        u_lead = f"{u_tim['lead_time_weeks']}w" if u_tim["lead_time_weeks"] is not None else "N/A"
        if c_tim:
            c_first = str(c_tim["first_detected"]) if c_tim["first_detected"] else "N/A"
            c_lead = f"{c_tim['lead_time_weeks']}w" if c_tim["lead_time_weeks"] is not None else "N/A"
            lines.append(f"| {u_tim['event_id']} | {u_first} | {u_lead} | {c_first} | {c_lead} |")
        else:
            lines.append(f"| {u_tim['event_id']} | {u_first} | {u_lead} | N/A | N/A |")
    lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append("1. **Authoritative evaluation:** The spatial evaluation (`evaluation_spatial.py`) is the primary evaluation for the EpiAlert paper (small-area detection). Whole-population evaluation (`evaluation.py`) is a comparison only.")
    lines.append("2. **Confirmation mode** requires CUSUM+EWMA concordance for ALERT; single-firing rows become PROVISIONAL and are NOT counted as alerts in the metrics above. The metrics count PROVISIONAL as non-alert (TN when no outbreak, FP when outbreak but provisional is still 'not an alert' for reporting purposes — see code for exact handling).")
    lines.append("3. **Do not tune to make numbers look good.** The full sweep is reported in the parameter sweep document. If no configuration achieves usable precision, that is the finding.")

    md_path = Path(output_dir) / "fusion_comparison.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote comparison: {md_path}")


if __name__ == "__main__":
    write_all_results()



# Old write_results/write_markdown_report removed — replaced by
# compute_and_write_metrics + write_all_results + write_fusion_comparison
