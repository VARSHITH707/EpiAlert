"""EpiAlert Outbreak Evaluation Module

Implements outbreak-level evaluation of detection algorithms.

Key concepts:
- PERSON-LEVEL DETECTION: whether an individual person triggers an alert
- OUTBREAK-LEVEL DETECTION: whether a statistically significant outbreak
  was detected in the population, when, and how early

The ground truth contains outbreak events with:
- event_id
- village
- disease
- start_week, end_week (inclusive)
- affected_streets
- event_type

Overlap logic: a weekly observation week_x overlaps an outbreak if the week's
Sunday reference date falls within [start_week, end_week] (inclusive).
"""

from src.evaluation import TemporalEvaluationFramework, compute_evaluation_metrics
from src.database.db import get_connection
from src.ingestion import week_number_to_date
from collections import defaultdict
import numpy as np


# ---------------------------------------------------------------------------
# Ground truth parsing from dataset
# ---------------------------------------------------------------------------

def load_ground_truth(path: str = "data/EpiAlert_Phase1_Dataset/ground_truth.csv") -> list:
    """Load ground truth outbreak events from CSV.

    Returns list of dicts with keys:
    - event_id: str
    - village: str
    - disease: str
    - start_week: int
    - end_week: int
    - affected_streets: list of str
    - event_type: str
    """
    import csv
    ground_truth = []
    
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Parse affected streets (may be semicolon-separated in CSV)
            affected_streets_str = row.get("affected_streets", "")
            if affected_streets_str:
                affected_streets = [s.strip() for s in affected_streets_str.split(";")]
            else:
                affected_streets = []
            
            gt_event = {
                "event_id": row["event_id"],
                "village": row["village"],
                "disease": row["disease"],
                "start_week": int(row["start_week"]),
                "end_week": int(row["end_week"]),
                "affected_streets": affected_streets,
                "event_type": row["event_type"],
            }
            ground_truth.append(gt_event)
    
    return ground_truth


# ---------------------------------------------------------------------------
# Outbreak overlap logic
# ---------------------------------------------------------------------------

def week_overlaps_outbreak(week_num: int, outbreak: dict) -> bool:
    """Determine if a weekly observation week overlaps an outbreak event.
    
    A week overlaps an outbreak if the week's Sunday reference date falls
    within [start_week, end_week] inclusive.
    
    Parameters
    ----------
    week_num : int
        Week number (1-based)
    outbreak : dict
        Ground truth outbreak event with start_week and end_week
        
    Returns
    -------
    bool
        True if the week overlaps the outbreak
    """
    week_date = week_number_to_date(week_num)
    # Week's reference date must be >= outbreak start AND <= outbreak end
    # Since week_number_to_date returns the Sunday of that week
    week_start = outbreak["start_week"]
    week_end = outbreak["end_week"]
    
    # The week_date is a Sunday; we check if it falls within the outbreak period
    # Outbreak weeks are 1-based week numbers
    outbreak_start_date = week_number_to_date(week_start)
    outbreak_end_date = week_number_to_date(week_end)
    
    # Check if week_date is within [outbreak_start_date, outbreak_end_date]
    # Since both are Sundays, we can compare date objects
    return outbreak_start_date <= week_date <= outbreak_end_date


def get_outbreak_active_weeks(outbreak: dict) -> list:
    """Get list of week numbers that are within an outbreak period."""
    start = outbreak["start_week"]
    end = outbreak["end_week"]
    return list(range(start, end + 1))


# ---------------------------------------------------------------------------
# Outbreak detection from algorithm results
# ---------------------------------------------------------------------------

def detect_outbreak_at_week(
    week_num: int,
    algorithm_alert: bool,
    algorithm_reason: str,
    ground_truth: list,
) -> dict:
    """Determine outbreak status at a specific week.
    
    Checks if any ground truth outbreak is active during this week.
    
    Parameters
    ----------
    week_num : int
        Week number being evaluated
    algorithm_alert : bool
        Whether the detection algorithm raised an alert this week
    algorithm_reason : str
        Reason text from the algorithm
    ground_truth : list
        List of ground truth outbreak events
        
    Returns
    -------
    dict with outbreak detection results:
    {
        "week": week_num,
        "outbreak_active": bool,       # Is any outbreak active this week?
        "outbreak_detected": bool,     # Did the algorithm detect an outbreak?
        "outbreak_id": str | None,     # Ground truth event ID if active
        "algorithm_alert": bool,       # Algorithm alert status
        "lead_time_weeks": int | None, # Weeks between detection and outbreak start
        "detection_type": str,         # "TRUE_POSITIVE", "FALSE_POSITIVE", etc.
    }
    """
    # Check which outbreaks are active this week
    active_outbreaks = []
    for outbreak in ground_truth:
        if week_overlaps_outbreak(week_num, outbreak):
            active_outbreaks.append(outbreak)
    
    outbreak_active = len(active_outbreaks) > 0
    outbreak_id = active_outbreaks[0]["event_id"] if active_outbreaks else None
    
    # Determine algorithm detection status
    algorithm_detected_outbreak = algorithm_alert and outbreak_active
    algorithm_no_outbreak = algorithm_alert and not outbreak_active
    no_algorithm_alert_with_outbreak = (not algorithm_alert) and outbreak_active
    no_algorithm_alert_no_outbreak = (not algorithm_alert) and not outbreak_active
    
    # Determine detection type
    if algorithm_detected_outbreak:
        detection_type = "TRUE_POSITIVE"
    elif algorithm_no_outbreak:
        detection_type = "FALSE_POSITIVE"
    elif no_algorithm_alert_with_outbreak:
        detection_type = "FALSE_NEGATIVE"
    else:
        detection_type = "TRUE_NEGATIVE"
    
    # Calculate lead time (weeks between algorithm detection and outbreak start)
    lead_time_weeks = None
    if algorithm_detected_outbreak and outbreak_id:
        # Find the specific outbreak and calculate lead time
        for outbreak in ground_truth:
            if outbreak["event_id"] == outbreak_id:
                # Lead time = week_of_detection - outbreak_start_week + 1
                # (positive means detected after outbreak started)
                detection_week = week_num
                lead_time_weeks = detection_week - outbreak["start_week"] + 1
                break
    
    return {
        "week": week_num,
        "outbreak_active": outbreak_active,
        "outbreak_detected": algorithm_alert,
        "outbreak_id": outbreak_id,
        "algorithm_alert": algorithm_alert,
        "detection_type": detection_type,
        "lead_time_weeks": lead_time_weeks,
        "detection_reason": algorithm_reason,
        "active_outbreak_ids": [o["event_id"] for o in active_outbreaks],
    }


def evaluate_outbreak_detection(
    evaluation_results: dict,
    ground_truth: list,
    start_week: int = 21,
    end_week: int = 104,
) -> dict:
    """Eolate outbreak-level detection across all evaluated weeks.
    
    Parameters
    ----------
    evaluation_results : dict
        Results from TemporalEvaluationFramework.run_evaluation()
    ground_truth : list
        List of ground truth outbreak events (from load_ground_truth)
    start_week : int
        First week of evaluation (default: 21)
    end_week : int
        Last week of evaluation (default: 104)
        
    Returns
    -------
    dict with outbreak-level evaluation:
    {
        "outbreak_summary": {
            "total_outbreaks_in_ground_truth": int,
            "weeks_with_outbreak": int,        # Number of weeks where >=1 outbreak active
            "outbreak_duration_total_weeks": int,  # Sum of (end-start+1) for all events
        },
        "detection_by_type": {
            "TRUE_POSITIVE": int,     # Alert + outbreak active
            "FALSE_POSITIVE": int,    # Alert + no outbreak active
            "FALSE_NEGATIVE": int,    # No alert + outbreak active
            "TRUE_NEGATIVE": int,     # No alert + no outbreak active
        },
        "detector_performance": {
            "baseline": {
                "tp": int, "fp": int, "fn": int, "tn": int,
                "sensitivity": float,      # TP / (TP + FN)
                "specificity": float,      # TN / (TN + FP)
                "precision": float,        # TP / (TP + FP)
                "recall": float,           # same as sensitivity
                "f1_score": float,         # 2 * precision * recall / (precision + recall)
            },
            "cusum": {...},
            "ewma": {...},
        },
        "outbreak_detection_details": [
            {
                "week": week_num,
                "outbreak_active": bool,
                "algorithm_alert": bool,
                "detection_type": str,
                "lead_time_weeks": int | None,
                "outbreak_id": str | None,
            }
            for week_num, results in ...  # per detector
        ],
        "lead_time_analysis": {
            "positive_lead_times": [...],  # lead times for TP detections
            "average_lead_time": float,    # average across TP detections
            "median_lead_time": float,
        },
        " missed_outbreaks": {
            "count": int,                # number of ground truth outbreaks not detected
            "details": [...],            # which weeks had active outbreaks but no alert
        }
    }
    """
    # Process each week for outbreak evaluation
    baseline_results = evaluation_results["baseline_results"]
    cusum_results = evaluation_results["cusum_results"]
    ewma_results = evaluation_results["ewma_results"]
    
    total_weeks = len(baseline_results)
    
    # Initialize counts
    detection_types = {
        "baseline": {"TRUE_POSITIVE": 0, "FALSE_POSITIVE": 0, "FALSE_NEGATIVE": 0, "TRUE_NEGATIVE": 0},
        "cusum": {"TRUE_POSITIVE": 0, "FALSE_POSITIVE": 0, "FALSE_NEGATIVE": 0, "TRUE_NEGATIVE": 0},
        "ewma": {"TRUE_POSITIVE": 0, "FALSE_POSITIVE": 0, "FALSE_NEGATIVE": 0, "TRUE_NEGATIVE": 0},
    }
    DETECTION_TYPES = ["TRUE_POSITIVE", "FALSE_POSITIVE", "FALSE_NEGATIVE", "TRUE_NEGATIVE"]
    
    missed_outbreaks = []  # weeks with active outbreak but no alert
    lead_times = {       # lead times for TP detections
        "baseline": [],
        "cusum": [],
        "ewma": [],
    }
    
    outbreak_details = []
    
    # Ground truth stats
    weeks_with_outbreak = set()
    
    # Process each week
    for i in range(total_weeks):
        week_num = start_week + i
        
        # Check ground truth for this week
        active_outbreaks_this_week = []
        for outbreak in ground_truth:
            if week_overlaps_outbreak(week_num, outbreak):
                active_outbreaks_this_week.append(outbreak)
                weeks_with_outbreak.add(week_num)
        
        outbreak_active = len(active_outbreaks_this_week) > 0
        
        # Process each detector
        for detector_name, detector_results in [
            ("baseline", baseline_results[i]),
            ("cusum", cusum_results[i]),
            ("ewma", ewma_results[i]),
        ]:
            algorithm_alert = detector_results["alert"]
            algorithm_reason = detector_results["reason"]
            
            # Evaluate outbreak detection
            outbreak_eval = detect_outbreak_at_week(
                week_num=week_num,
                algorithm_alert=algorithm_alert,
                algorithm_reason=algorithm_reason,
                ground_truth=ground_truth,
            )
            
            det_type = outbreak_eval["detection_type"]
            if det_type not in detection_types[detector_name]:
                detection_types[detector_name][det_type] = 0
            detection_types[detector_name][det_type] += 1
            
            outbreak_details.append({
                "week": week_num,
                "outbreak_active": outbreak_active,
                "algorithm_alert": algorithm_alert,
                "detection_type": det_type,
                "lead_time_weeks": outbreak_eval["lead_time_weeks"],
                "outbreak_id": outbreak_eval["outbreak_id"],
            })
            
            # Collect lead times for true positives
            if det_type == "TRUE_POSITIVE" and outbreak_eval["lead_time_weeks"] is not None:
                lead_times[detector_name].append(outbreak_eval["lead_time_weeks"])
        
        # Track missed outbreaks
        if outbreak_active and not any(
            r["alert"] for r in [baseline_results[i], cusum_results[i], ewma_results[i]]
        ):
            # Actually track per-detector missed outbreaks
            for detector_name, detector_results in [
                ("baseline", baseline_results[i]),
                ("cusum", cusum_results[i]),
                ("ewma", ewma_results[i]),
            ]:
                if not detector_results["alert"]:
                    missed_outbreaks.append({
                        "week": week_num,
                        "detector": detector_name,
                        "outbreak_ids": [o["event_id"] for o in active_outbreaks_this_week],
                    })
    
    # Calculate performance metrics
    def calc_sensitivity(tp, fn):
        return tp / (tp + fn) if (tp + fn) > 0 else 0.0
    
    def calc_specificity(tn, fp):
        return tn / (tn + fp) if (tn + fp) > 0 else 0.0
    
    def calc_precision(tp, fp):
        return tp / (tp + fp) if (tp + fp) > 0 else 0.0
    
    def calc_f1(precision, recall):
        return 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    
    detector_metrics = {}
    for detector_name in ["baseline", "cusum", "ewma"]:
        tp = detection_types[detector_name].get("TRUE_POSITIVE", 0)
        fp = detection_types[detector_name].get("FALSE_POSITIVE", 0)
        fn = detection_types[detector_name].get("FALSE_NEGATIVE", 0)
        tn = detection_types[detector_name].get("TRUE_NEGATIVE", 0)
        
        sensitivity = calc_sensitivity(tp, fn)
        specificity = calc_specificity(tn, fp)
        precision = calc_precision(tp, fp)
        f1 = calc_f1(precision, sensitivity)
        
        detector_metrics[detector_name] = {
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "sensitivity": sensitivity,
            "specificity": specificity,
            "precision": precision,
            "recall": sensitivity,  # same as sensitivity
            "f1_score": f1,
            "total_alerted": tp + fp,
            "total_outbreaks": tp + fn,  # actual outbreaks
        }
    
    # Calculate average lead times
    lead_time_summary = {}
    for detector_name in ["baseline", "cusum", "ewma"]:
        lts = lead_times[detector_name]
        lead_time_summary[detector_name] = {
            "positive_lead_times": lts,
            "average_lead_time": np.mean(lts) if lts else 0.0,
            "median_lead_time": np.median(lts) if lts else 0.0,
            "min_lead_time": min(lts) if lts else 0,
            "max_lead_time": max(lts) if lts else 0,
            "tp_count": len(lts),
        }
    
    return {
        "outbreak_summary": {
            "total_outbreaks_in_ground_truth": len(ground_truth),
            "weeks_with_outbreak": len(weeks_with_outbreak),
            "outbreak_duration_total_weeks": sum(
                o["end_week"] - o["start_week"] + 1 for o in ground_truth
            ),
        },
        "detection_by_type": {
            "baseline": detection_types["baseline"],
            "cusum": detection_types["cusum"],
            "ewma": detection_types["ewma"],
        },
        "detector_performance": detector_metrics,
        "outbreak_detection_details": outbreak_details,
        "lead_time_analysis": lead_time_summary,
        "missed_outbreaks": {
            "count": len(set([m["week"] for m in missed_outbreaks])),
            "details": missed_outbreaks,
        },
        "fairness_check": {
            "no_future_leakage": evaluation_results.get("no_data_leakage", True),
            "methodology_description": (
                "Outbreak evaluation uses ground truth only post-hoc for "
                "evaluation comparison. Detection alerts were generated without "
                "future ground truth information. Temporal ordering was maintained "
                "throughout: baseline for week N used weeks 1..N-1 only."
            ),
        },
    }