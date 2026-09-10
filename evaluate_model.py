"""Evaluate EpiAlert's detection performance with scikit-learn.

    python evaluate_model.py                      # default: confirmation mode
    python evaluate_model.py --fusion-mode union  # compare against union
    python evaluate_model.py --detector cusum_signal
    python evaluate_model.py --all                # every detector, side by side

What is being classified
------------------------
One sample = one (disease, village, street, week). For each, the question is:
"was there an outbreak here this week?"

    y_true   1 if ground_truth.csv places an outbreak on this street this week
    y_pred   1 if the detector raised ALERT or HIGH_ALERT

Labels come from data/EpiAlert_Phase1_Dataset/ground_truth.csv, which EpiAlert
itself never reads. Predictions come from the detection_results table, produced
by an actual detection run. Nothing here is simulated.

A note on accuracy
------------------
Outbreaks are about 1.3% of samples, so always predicting "no outbreak" scores
roughly 0.987 accuracy while catching nothing at all. Accuracy is reported
because it is asked for, but precision and recall are the metrics that mean
anything on data this imbalanced.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from sklearn.metrics import (
        classification_report,
        confusion_matrix,
        precision_recall_fscore_support,
    )
except ImportError:
    sys.exit(
        "scikit-learn is not installed.\n"
        "  pip install scikit-learn"
    )

from src.database.db import get_connection
from src.evaluation_spatial_metrics import load_ground_truth

# Which column each detector alerts on, and what counts as firing.
DETECTORS = {
    "combined_status": "status IN ('ALERT','HIGH_ALERT')",
    "cusum_signal": "cusum_signal = 1",
    "ewma_signal": "ewma_signal = 1",
    "trend_sustained": "trend_sustained = 1",
    "baseline_deviation": "ABS(baseline_deviation) >= 1.5",
}

# ground_truth.csv labels the 104-week historical period. Anything later --
# a week uploaded through the web interface, for instance -- has no label, so
# it must not be scored.
MAX_GROUND_TRUTH_WEEK = 104

POSITIVE = "Outbreak"
NEGATIVE = "No outbreak"


def outbreak_weeks() -> dict[tuple[str, str, str], set[int]]:
    """Map (disease, village, street) -> the weeks an outbreak was present.

    Built from ground_truth.csv, which lists each event's disease, village,
    affected streets and week range.
    """
    units: dict[tuple[str, str, str], set[int]] = {}
    for event in load_ground_truth():
        weeks = set(range(int(event["start_week"]), int(event["end_week"]) + 1))
        for street in event["affected_streets"]:
            key = (event["disease"], event["village"], street)
            units.setdefault(key, set()).update(weeks)
    return units


def build_labels(fusion_mode: str, detector: str,
                 max_week: int = MAX_GROUND_TRUTH_WEEK) -> tuple[list[int], list[int]]:
    """Return (y_true, y_pred) over every evaluated unit-week.

    Bounded to max_week. ground_truth.csv only covers the historical period, so
    a week beyond it has no labels -- every detection there would be scored as
    a false positive purely because nothing says otherwise. Weeks uploaded
    through the web interface fall in that range, and including them silently
    depressed precision.
    """
    if detector not in DETECTORS:
        sys.exit(f"Unknown detector {detector!r}. Choose from: {', '.join(DETECTORS)}")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        f"""SELECT disease, village, street, week_number,
                   CASE WHEN {DETECTORS[detector]} THEN 1 ELSE 0 END AS fired
            FROM detection_results
            WHERE fusion_mode = ? AND week_number <= ?""",
        (fusion_mode, max_week),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        sys.exit(
            f"No detection results for fusion_mode={fusion_mode!r}.\n"
            "Run:  python -m src.cli detect --start-week 21 --end-week 104"
        )

    truth = outbreak_weeks()
    y_true, y_pred = [], []
    for disease, village, street, week, fired in rows:
        y_true.append(1 if week in truth.get((disease, village, street), set()) else 0)
        y_pred.append(int(fired))

    return y_true, y_pred


def report(y_true: list[int], y_pred: list[int], title: str) -> dict:
    """Print the confusion matrix and classification report."""
    print("=" * 66)
    print(title)
    print("=" * 66)

    # ravel() gives the four counts in this order for a binary problem.
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
    total = tn + fp + fn + tp

    print(f"\nSamples: {total:,} unit-weeks "
          f"({tp + fn:,} outbreak, {tn + fp:,} normal — "
          f"{100 * (tp + fn) / total:.1f}% positive)\n")

    print("CONFUSION MATRIX")
    print("                          ACTUAL")
    print(f"                  {POSITIVE:>11}  {NEGATIVE:>11}")
    print(f"  PREDICTED Alert {tp:>11,}  {fp:>11,}")
    print(f"  PREDICTED None  {fn:>11,}  {tn:>11,}")

    print("\nCOUNTS")
    print(f"  True Negatives  (TN) : {tn:>8,}   correctly stayed quiet")
    print(f"  False Positives (FP) : {fp:>8,}   false alarms")
    print(f"  False Negatives (FN) : {fn:>8,}   outbreaks missed")
    print(f"  True Positives  (TP) : {tp:>8,}   outbreaks caught")

    print("\nCLASSIFICATION REPORT")
    print(classification_report(
        y_true, y_pred,
        labels=[0, 1],
        target_names=[NEGATIVE, POSITIVE],
        digits=3,
        zero_division=0,
    ))

    precision, recall, f1, _ = precision_recall_fscore_support(
        y_true, y_pred, labels=[1], zero_division=0,
    )
    accuracy = (tp + tn) / total if total else 0.0
    specificity = tn / (tn + fp) if (tn + fp) else 0.0
    baseline = max(tn + fp, tp + fn) / total if total else 0.0

    print("OUTBREAK CLASS")
    print(f"  Precision   {precision[0]:.3f}   of alerts raised, this share were real")
    print(f"  Recall      {recall[0]:.3f}   of real outbreaks, this share were caught")
    print(f"  F1          {f1[0]:.3f}")
    print(f"  Specificity {specificity:.3f}")
    print(f"  Accuracy    {accuracy:.3f}")
    print(f"\n  Always predicting the majority class scores {baseline:.3f} accuracy")
    print("  while catching nothing. Accuracy is not a useful target here.\n")

    return {
        "tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp),
        "precision": float(precision[0]), "recall": float(recall[0]),
        "f1": float(f1[0]), "accuracy": float(accuracy),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate EpiAlert detection with scikit-learn."
    )
    parser.add_argument("--fusion-mode", default="confirmation",
                        choices=["confirmation", "union"])
    parser.add_argument("--detector", default="combined_status",
                        choices=list(DETECTORS))
    parser.add_argument("--all", action="store_true",
                        help="Evaluate every detector and compare.")
    parser.add_argument("--max-week", type=int, default=MAX_GROUND_TRUTH_WEEK,
                        help="Last week to score. Beyond the ground-truth "
                             "period there are no labels.")
    args = parser.parse_args()

    if not args.all:
        y_true, y_pred = build_labels(args.fusion_mode, args.detector,
                                      args.max_week)
        report(y_true, y_pred,
               f"{args.detector}  ({args.fusion_mode} mode)")
        return 0

    rows = []
    for detector in DETECTORS:
        y_true, y_pred = build_labels(args.fusion_mode, detector,
                                      args.max_week)
        rows.append((detector, report(
            y_true, y_pred, f"{detector}  ({args.fusion_mode} mode)"
        )))

    print("=" * 66)
    print(f"COMPARISON — {args.fusion_mode} mode")
    print("=" * 66)
    print(f"  {'detector':<20} {'TP':>5} {'FP':>6} {'FN':>5} "
          f"{'Prec':>7} {'Recall':>7} {'F1':>7}")
    for name, m in rows:
        print(f"  {name:<20} {m['tp']:>5} {m['fp']:>6} {m['fn']:>5} "
              f"{m['precision']:>7.3f} {m['recall']:>7.3f} {m['f1']:>7.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
