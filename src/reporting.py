"""EpiAlert Report Generation Module

Generates reproducible report outputs from detection evaluation results.

Report structure respects the existing dataset architecture:
- week_xxx/ reports/ person_XXX.txt files
- Weekly summary aggregations
- Detector comparison outputs
- Evaluation result CSVs/JSONs

Supports:
A. Person-level reports (per person per week)
B. Weekly summary reports
C. Detector comparison reports
D. Overall evaluation results
E. Outbreak detection summary
F. Demo/week_105 reports
"""

from src.database.db import get_connection, init_schema
from src.ingestion import week_number_to_date, load_people_map, WEEK_ZERO_REFERENCE
from src.detection.baseline import detect_baseline
from src.detection.cusum import detect_cusum
from src.detection.ewma import detect_ewma
from src.evaluation import TemporalEvaluationFramework, compute_evaluation_metrics
from src.outbreak_evaluation import load_ground_truth, detect_outbreak_at_week
import csv
import json
import os
from pathlib import Path
from datetime import date


# ---------------------------------------------------------------------------
# Report output paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(".")
DATA_ROOT = PROJECT_ROOT / "data"
RESULTS_ROOT = PROJECT_ROOT / "results"
REPORTS_ROOT = PROJECT_ROOT / "reports"

# Ensure output directories exist
RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
REPORTS_ROOT.mkdir(parents=True, exist_ok=True)


def get_report_output_path(report_type: str, week_num: int, person_id: str = None) -> Path:
    """Get the output path for a report.

    Parameters
    ----------
    report_type : str
        Type of report: 'person', 'weekly_summary', 'comparison', 'evaluation',
        'outbreak', 'week_105'
    week_num : int
        Week number
    person_id : str, optional
        Person identifier (required for person-level reports)
    """
    if report_type == "person":
        # week_xxx/reports/P0001.txt style
        week_dir = REPORTS_ROOT / f"week_{week_num:03d}"
        week_dir.mkdir(parents=True, exist_ok=True)
        return week_dir / f"{person_id}.txt"
    
    elif report_type == "weekly_summary":
        # REPORTS_ROOT/weekly_summary/week_XXX.json
        week_dir = REPORTS_ROOT / "weekly_summary"
        week_dir.mkdir(parents=True, exist_ok=True)
        return week_dir / f"week_{week_num:03d}.json"
    
    elif report_type == "comparison":
        # REPORTS_ROOT/comparison.json
        return REPORTS_ROOT / "comparison.json"
    
    elif report_type == "evaluation":
        # REPORTS_ROOT/evaluation_results.json
        return REPORTS_ROOT / "evaluation_results.json"
    
    elif report_type == "outbreak":
        # REPORTS_ROOT/outbreak_summary.json
        return REPORTS_ROOT / "outbreak_summary.json"
    
    elif report_type == "week_105":
        # REPORTS_ROOT/week_105/summary.json
        week_dir = REPORTS_ROOT / "week_105"
        week_dir.mkdir(parents=True, exist_ok=True)
        return week_dir / "summary.json"
    
    else:
        return REPORTS_ROOT / report_type


# ---------------------------------------------------------------------------
# Person-level report generation
# ---------------------------------------------------------------------------

def generate_person_report(
    week_num: int,
    person_id: str,
    algorithm_alert: bool = None,
    algorithm_result: dict = None,
) -> dict:
    """Generate a person-level surveillance report for a specific week and person.

    Parameters
    ----------
    week_num : int
        Week number
    person_id : str
        Person identifier
    algorithm_alert : bool, optional
        Whether the detection algorithm raised an alert for this person
    algorithm_result : dict, optional
        Full detection result dict with details

    Returns
    -------
    dict with report content metadata
    """
    # Get people map for village/street info
    from src.ingestion import load_people_map
    people_map = load_people_map()
    
    # Get weekly aggregation data
    from src.preprocessing import get_weekly_aggregation
    agg = get_weekly_aggregation(week_num)
    
    # Find the person's record in the aggregation
    person_record = None
    for p in agg["people"]:
        if p["person_id"] == person_id:
            person_record = p
            break
    
    # Get reference date
    from src.ingestion import week_number_to_date
    ref_date = week_number_to_date(week_num)
    
    # Determine infection status from aggregation
    is_infected = person_record["infected"] if person_record else False
    disease = person_record["disease"] if person_record else None
    
    # Get village and street from people map
    village = people_map.get(person_id, ("Unknown", "Unknown"))[0]
    street = people_map.get(person_id, ("Unknown", "Unknown"))[1]
    
    # Determine alert status
    alert_status = "ALERT" if algorithm_alert else "NO_ALERT"
    if algorithm_result:
        # Use the algorithm's reason if available
        alert_reason = algorithm_result.get("reason", "No alert reason specified")
    else:
        alert_reason = "No detection algorithm applied"
    
    # Build report text
    report_lines = [
        f"EpiAlert Surveillance Report",
        f"============================",
        f"",
        f"Week: {week_num}",
        f"Reference Date: {ref_date.strftime('%B %d, %Y')} (Sunday)",
        f"Person ID: {person_id}",
        f"",
        f"Person Details:",
        f"  Village: {village}",
        f"  Street: {street}",
        f"",
        f"Disease Surveillance:",
        f"  Infection Status: {'Infected' if is_infected else 'No infection'}",
        f"  Disease: {disease if disease else 'No infection reported'}",
        f"",
        f"Detection Results:",
        f"  Alert Status: {alert_status}",
        f"  {alert_reason}",
        f"",
        f"Statistical Summary (Week {week_num}):",
        f"  Total people in week: {agg['total_people']}",
        f"  Infection rate: {agg['infection_rate']:.4f}",
        f"  Disease distribution: {json.dumps(agg['disease_counts'], indent=4)}",
        f"",
        f"Ground Truth Status:",
        f"  (Not included in routine reports; available for evaluation)",
    ]
    
    report_text = "\n".join(report_lines)
    
    # Write the report file
    report_path = get_report_output_path("person", week_num, person_id)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    
    return {
        "report_path": str(report_path),
        "week": week_num,
        "person_id": person_id,
        "report_type": "person-level",
        "generated": True,
    }


def generate_all_person_reports(
    week_num: int,
    baseline_alert: bool,
    cusum_alert: bool,
    ewma_alert: bool,
) -> dict:
    """Generate person-level reports for all 3,000 people in a week.

    Parameters
    ----------
    week_num : int
        Week number
    baseline_alert : bool
        Whether Baseline detector raised an alert
    cusum_alert : bool
        Whether CUSUM detector raised an alert
    ewma_alert : bool
        Whether EWMA detector raised an alert

    Returns
    -------
    dict with generation statistics
    """
    # Get people map
    people_map = load_people_map()
    
    # Get weekly aggregation
    from src.preprocessing import get_weekly_aggregation
    agg = get_weekly_aggregation(week_num)
    
    stats = {
        "week": week_num,
        "total_people": len(people_map),
        "reports_generated": 0,
        "reports_by_alert_status": {"ALERT": 0, "NO_ALERT": 0},
    }
    
    for person_id in sorted(people_map.keys()):
        # Generate separate reports for each detector
        for detector_name, algorithm_alert in [
            ("baseline", baseline_alert),
            ("cusum", cusum_alert),
            ("ewma", ewma_alert),
        ]:
            result = generate_person_report(
                week_num=week_num,
                person_id=person_id,
                algorithm_alert=algorithm_alert,
            )
            stats["reports_generated"] += 1
            alert_label = "ALERT" if algorithm_alert else "NO_ALERT"
            stats["reports_by_alert_status"][alert_label] += 1
    
    return stats


# ---------------------------------------------------------------------------
# Weekly summary report generation
# ---------------------------------------------------------------------------

def generate_weekly_summary_report(
    week_num: int,
    baseline_result: dict,
    cusum_result: dict,
    ewma_result: dict,
    ground_truth: list = None,
) -> dict:
    """Generate a weekly summary report aggregating all surveillance data.

    Parameters
    ----------
    week_num : int
        Week number
    baseline_result : dict
        Result dict from Baseline detector
    cusum_result : dict
        Result dict from CUSUM detector
    ewma_result : dict
        Result dict from EWMA detector
    ground_truth : list, optional
        Ground truth outbreak events for outbreak-level analysis

    Returns
    -------
    dict with report metadata
    """
    from src.preprocessing import get_weekly_aggregation
    agg = get_weekly_aggregation(week_num)
    
    ref_date = week_number_to_date(week_num)
    
    # Determine outbreak status
    outbreak_active = False
    active_outbreak_ids = []
    if ground_truth:
        for outbreak in ground_truth:
            if week_overlaps_outbreak(week_num, outbreak):
                outbreak_active = True
                active_outbreak_ids.append(outbreak["event_id"])
    
    # Build summary data structure
    summary = {
        "week": week_num,
        "reference_date": ref_date.isoformat(),
        "weekly_summary": {
            "total_people": agg["total_people"],
            "infection_rate": agg["infection_rate"],
            "disease_distribution": agg["disease_counts"],
            "baseline": {
                "observed_rate": baseline_result["observed_rate"],
                "expected_rate": baseline_result["expected_rate"],
                "deviation": baseline_result["deviation"],
                "alert": baseline_result["alert"],
                "alert_status": baseline_result["alert_status"],
            },
            "cusum": {
                "observed_rate": cusum_result["observed_rate"],
                "h_cusum": cusum_result["h_cusum"],
                "k_cusum": cusum_result["k_cusum"],
                "alert": cusum_result["alert"],
                "alert_status": cusum_result["alert_status"],
            },
            "ewma": {
                "observed_rate": ewma_result["observed_rate"],
                "ewma_statistic": ewma_result["ewma"],
                "ucl": ewma_result["ucl"],
                "lcl": ewma_result["lcl"],
                "alert": ewma_result["alert"],
                "alert_status": ewma_result["alert_status"],
            },
        },
        "outbreak_detection": {
            "outbreak_active": outbreak_active,
            "active_outbreak_ids": active_outbreak_ids,
            "outbreak_ids": active_outbreak_ids if active_outbreak_ids else [],
        },
        "alert_summary": {
            "baseline_alert": baseline_result["alert"],
            "cusum_alert": cusum_result["alert"],
            "ewma_alert": ewma_result["alert"],
            "any_alert": baseline_result["alert"] or cusum_result["alert"] or ewma_result["alert"],
        },
    }
    
    # Write the report file
    report_path = get_report_output_path("weekly_summary", week_num)
    # Convert all values to JSON-serializable types
    def make_serializable(obj):
        if isinstance(obj, bool):
            return bool(obj)
        elif isinstance(obj, (int, float, str)):
            return obj
        elif isinstance(obj, dict):
            return {k: make_serializable(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [make_serializable(item) for item in obj]
        else:
            return str(obj)
    
    serializable_summary = make_serializable(summary)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(serializable_summary, f, indent=2)
    
    return {
        "report_path": str(report_path),
        "week": week_num,
        "report_type": "weekly-summary",
        "generated": True,
        "outbreak_active": outbreak_active,
    }


# ---------------------------------------------------------------------------
# Detector comparison report generation
# ---------------------------------------------------------------------------

def generate_detector_comparison_report(
    evaluation_results: dict,
    ground_truth: list = None,
) -> dict:
    """Generate a comprehensive detector comparison report.

    Compares Baseline, CUSUM, and EWMA using the same evaluation framework.

    Parameters
    ----------
    evaluation_results : dict
        Results from TemporalEvaluationFramework.run_evaluation()
    ground_truth : list, optional
        Ground truth outbreak events for outbreak-level analysis

    Returns
    -------
    dict with report metadata
    """
    # Compute evaluation metrics
    metrics = compute_evaluation_metrics(evaluation_results, ground_truth)
    
    # Compute outbreak-level evaluation
    outbreak_eval = None
    if ground_truth:
        from src.outbreak_evaluation import evaluate_outbreak_detection
        outbreak_eval = evaluate_outbreak_detection(
            evaluation_results, ground_truth
        )
    
    # Build comparison report
    report = {
        "report_type": "detector-comparison",
        "generated": __import__('datetime').datetime.now().isoformat(),
        "evaluation_framework": {
            "baseline_method": evaluation_results["framework_config"]["baseline_method"],
            "baseline_threshold": evaluation_results["framework_config"]["baseline_threshold"],
            "evaluation_weeks": evaluation_results["framework_config"]["evaluation_weeks"],
        },
        "comparison_metrics": metrics["comparison"],
        "outbreak_detection": None,
        "fairness_check": metrics["fairness_check"],
    }
    
    if outbreak_eval:
        report["outbreak_detection"] = outbreak_eval
    
    # Write the report file
    report_path = get_report_output_path("comparison", 0)  # 0 means no week-specific
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    
    return {
        "report_path": str(report_path),
        "report_type": "detector-comparison",
        "generated": True,
    }


# ---------------------------------------------------------------------------
# Evaluation results report generation
# ---------------------------------------------------------------------------

def generate_evaluation_results_report(
    evaluation_results: dict,
    ground_truth: list = None,
) -> dict:
    """Generate comprehensive evaluation results report.

    Parameters
    ----------
    evaluation_results : dict
        Results from TemporalEvaluationFramework.run_evaluation()
    ground_truth : list, optional
        Ground truth outbreak events

    Returns
    -------
    dict with report metadata
    """
    # Compute metrics
    metrics = compute_evaluation_metrics(evaluation_results, ground_truth)
    
    # Build report
    report = {
        "report_type": "evaluation-results",
        "generated": __import__('datetime').datetime.now().isoformat(),
        "evaluation_summary": {
            "total_weeks_evaluated": evaluation_results["summary"]["total_weeks"],
            "baseline_alerts": evaluation_results["summary"]["baseline_alerts"],
            "cusum_alerts": evaluation_results["summary"]["cusum_alerts"],
            "ewma_alerts": evaluation_results["summary"]["ewma_alerts"],
        },
        "comparison_metrics": metrics["comparison"],
        "fairness_check": metrics["fairness_check"],
    }
    
    if ground_truth:
        # Add outbreak summary
        from src.outbreak_evaluation import load_ground_truth
        if not ground_truth:
            ground_truth = load_ground_truth()
        outbreak_summary = {
            "total_outbreaks_in_ground_truth": len(ground_truth),
        }
        # Count weeks with outbreaks
        weeks_with_outbreak = set()
        for week_num in range(21, 105):
            for outbreak in ground_truth:
                if week_overlaps_outbreak(week_num, outbreak):
                    weeks_with_outbreak.add(week_num)
        outbreak_summary["weeks_with_outbreak"] = len(weeks_with_outbreak)
        report["outbreak_summary"] = outbreak_summary
    
    # Write the report file
    report_path = get_report_output_path("evaluation", 0)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    
    return {
        "report_path": str(report_path),
        "report_type": "evaluation-results",
        "generated": True,
    }


# ---------------------------------------------------------------------------
# Week 105 demo report generation
# ---------------------------------------------------------------------------

def generate_week_105_report() -> dict:
    """Generate a demo report for week_105 as new incoming data.

    Week 105 represents new incoming data for the live/demo scenario,
    NOT part of the 104-week historical evaluation.

    Returns
    -------
    dict with report metadata
    """
    # Check if week_105 data exists in database
    from src.database.db import get_connection, people_count, get_weekly_stats
    
    conn = get_connection()
    cur = conn.cursor()
    
    # Check if week 105 has been ingested
    cur.execute("SELECT COUNT(*) FROM reports WHERE week_number = 105")
    week_105_count = cur.fetchone()[0]
    
    # Get stats if available
    if week_105_count > 0:
        cur.execute(
            "SELECT infection, COUNT(*) FROM reports WHERE week_number = 105 GROUP BY infection"
        )
        infection_dist = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute(
            "SELECT COUNT(DISTINCT person_id) FROM reports WHERE week_number = 105"
        )
        person_count_105 = cur.fetchone()[0]
    else:
        infection_dist = {}
        person_count_105 = 0
    
    cur.close()
    conn.close()
    
    # Build week 105 report
    report = {
        "report_type": "week-105-demo",
        "generated": __import__('datetime').datetime.now().isoformat(),
        "week_105": {
            "week": 105,
            "date": "2027-01-10 (Sunday)",  # Week 105 reference date
            "people_processed": person_count_105,
            "infection_distribution": infection_dist,
            "baseline_alert": False,  # Would be computed from models
            "cusum_alert": False,
            "ewma_alert": False,
            "notes": (
                "Week 105 is the new incoming week for live/demo mode. "
                "It is NOT part of the 104-week historical evaluation. "
                "Models were calibrated on weeks 1-104; week_105 demonstrates "
                "how EpiAlert processes new incoming data."
            ),
        },
    }
    
    # Write the report file
    report_path = get_report_output_path("week_105", 0)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    
    return {
        "report_path": str(report_path),
        "report_type": "week-105-demo",
        "generated": True,
    }


# ---------------------------------------------------------------------------
# Export functions
# ---------------------------------------------------------------------------

__all__ = [
    'generate_person_report',
    'generate_all_person_reports',
    'generate_weekly_summary_report',
    'generate_detector_comparison_report',
    'generate_evaluation_results_report',
    'generate_week_105_report',
    'get_report_output_path',
]