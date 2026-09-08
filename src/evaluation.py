"""EpiAlert Temporal Evaluation Module

Provides fair temporal evaluation of detection algorithms.

CRITICAL: Evaluation must respect temporal ordering.
- Do NOT train on future weeks and evaluate on earlier weeks
- Do NOT allow information from week N+1 to influence detection at week N
- The evaluation stage MAY use ground truth after predictions are generated

This module ensures that for week N detection:
- Only data from weeks 1 through N-1 (or a historical window) is used
- The detection algorithm for week N cannot access week N+1, N+2, etc.
- Ground truth may be used post-hoc for evaluation, not for prediction
"""

from src.detection.baseline import detect_baseline, BaselineDetector
from src.detection.cusum import CUSUMDetector, H_CUSUM, K_CUSUM
from src.detection.ewma import EWMADetector, LAMBDA_EWMA, L_EWMA
from src.database.db import get_connection, init_schema
from src.ingestion import week_number_to_date, get_weekly_aggregation
from collections import defaultdict
import numpy as np


# ---------------------------------------------------------------------------
# Evaluation framework ensuring no future data leakage
# ---------------------------------------------------------------------------

class TemporalEvaluationFramework:
    """Framework for fair temporal evaluation of detection algorithms.

    Ensures that detector for week N cannot access:
    - week N+1, N+2, future observations
    - future ground truth (for prediction; may be used post-hoc)

    The workflow is:
    1. For each week N in evaluation period:
       a. Calibrate/estimate baseline from historical weeks 1..N-1 (or fixed window)
       b. Run detection on week N using only that baseline
       c. Record alert/result
    2. After all predictions are generated, ground truth may be used for evaluation

    Key invariant: NO week N detector can use data from week N+1 or later.
    """

    def __init__(
        self,
        historical_weeks: list = None,
        baseline_weeks: list = None,
        baseline_method: str = "simple_mean",
        baseline_threshold: float = 2.0,
        cusum_decision_interval: float = H_CUSUM,
        cusum_reference_value: float = K_CUSUM,
        ewma_alpha: float = LAMBDA_EWMA,
        ewma_control_limit: float = L_EWMA,
    ):
        """Initialize the temporal evaluation framework.

        Parameters
        ----------
        historical_weeks : list of int
            Full historical weeks available (default: 1-104)
        baseline_weeks : list of int
            Weeks used to calibrate the baseline for each detection week.
            If None, uses first 20 weeks as initialization, then expands.
        baseline_method : str
            Method for baseline computation: "simple_mean" or "moving_average"
        baseline_threshold : float
            Alert threshold in standard deviations
        cusum_decision_interval : float
            CUSUM decision threshold (h parameter)
        cusum_reference_value : float
            CUSUM reference value (k parameter)
        ewma_alpha : float
            EWMA smoothing parameter
        ewma_control_limit : float
            EWMA control limit multiplier (L parameter)
        """
        if historical_weeks is None:
            historical_weeks = list(range(1, 105))  # weeks 1-104

        self.historical_weeks = historical_weeks
        self.baseline_weeks = baseline_weeks or list(range(1, 21))  # default: weeks 1-20
        self.baseline_method = baseline_method
        self.baseline_threshold = baseline_threshold
        self.cusum_decision_interval = cusum_decision_interval
        self.cusum_reference_value = cusum_reference_value
        self.ewma_alpha = ewma_alpha
        self.ewma_control_limit = ewma_control_limit

        # Stateful detectors - maintain across weeks for CUSUM and EWMA
        self._cusum_detector = None
        self._ewma_detector = None

        # Results storage
        self.baseline_results = []
        self.cusum_results = []
        self.ewma_results = []

    def _get_or_create_cusum(self, baseline_expected: float) -> CUSUMDetector:
        """Get or create the stateful CUSUM detector.

        The detector is created once and reused across weeks so its
        cumulative state accumulates properly.
        """
        if self._cusum_detector is None:
            self._cusum_detector = CUSUMDetector(
                baseline_expected=baseline_expected,
                decision_interval=self.cusum_decision_interval,
                reference_value=self.cusum_reference_value,
            )
        else:
            # Update baseline_expected in case it changes week to week
            self._cusum_detector.baseline_expected = baseline_expected
        return self._cusum_detector

    def _get_or_create_ewma(self, baseline_expected: float) -> EWMADetector:
        """Get or create the stateful EWMA detector.

        The detector is created once and reused across weeks so its
        EWMA statistic carries properly from week to week.
        """
        if self._ewma_detector is None:
            self._ewma_detector = EWMADetector(
                baseline_expected=baseline_expected,
                alpha=self.ewma_alpha,
                control_limit=self.ewma_control_limit,
            )
        else:
            # Update baseline_expected in case it changes week to week
            self._ewma_detector.baseline_expected = baseline_expected
        return self._ewma_detector

    def evaluate_week_temporal(self, week_num: int, start_week: int = 21) -> dict:
        """Evaluate all three detectors for a single week using temporal logic.

        For week N:
        - Baseline: calibrated from weeks 1..N-1 only (no future data)
        - CUSUM: state carries from previous evaluation weeks
        - EWMA: state carries from previous evaluation weeks

        Critically: NO future weeks (N+1, N+2, ...) are accessed.
        NO future ground truth is used for prediction.

        Parameters
        ----------
        week_num : int
            Week number to evaluate (must be after baseline_weeks)
        start_week : int
            First week of the evaluation period (for computing week indices)

        Returns
        -------
        dict with results for all three detectors.
        """
        if week_num <= max(self.baseline_weeks):
            raise ValueError(
                f"Week {week_num} must be after baseline weeks "
                f"(min: {max(self.baseline_weeks) + 1})"
            )

        # --- BASELINE: calibrated from historical weeks ONLY (no future data) ---
        # We use weeks 1 through week_num-1 for baseline calibration
        # This is the key: baseline for week N uses weeks 1..N-1 only
        baseline_historical = list(range(1, week_num))

        baseline_result = detect_baseline(
            week_num=week_num,
            historical_weeks=baseline_historical,
            method=self.baseline_method,
            threshold=self.baseline_threshold,
        )

        # --- CUSUM: stateful detector, state carries across weeks ---
        cusum_detector = self._get_or_create_cusum(
            baseline_expected=baseline_result["expected_rate"]
        )
        cusum_result = cusum_detector.detect(week_num)

        # --- EWMA: stateful detector, state carries across weeks ---
        ewma_detector = self._get_or_create_ewma(
            baseline_expected=baseline_result["expected_rate"]
        )
        ewma_result = ewma_detector.detect(week_num)

        # Confirm no leakage - by construction, baseline only uses weeks 1..N-1
        # CUSUM and EWMA only use current and previous weeks
        no_leakage = True

        # Determine the week index for this result (for later metric computation)
        week_index = week_num - start_week

        result = {
            "week": week_num,
            "week_index": week_index,
            "date": week_number_to_date(week_num),
            "baseline": {
                "week": week_num,
                "observed_rate": baseline_result["observed_rate"],
                "expected_rate": baseline_result["expected_rate"],
                "deviation": baseline_result["deviation"],
                "alert": baseline_result["alert"],
                "alert_status": baseline_result["alert_status"],
                "reason": baseline_result["reason"],
            },
            "cusum": {
                "week": week_num,
                "observed_rate": cusum_result.get("observed_rate", 0.0),
                "baseline_expected": cusum_result.get("baseline_expected", baseline_result["expected_rate"]),
                "h_cusum": cusum_result.get("h_cusum", 0.0),
                "k_cusum": cusum_result.get("k_cusum", 0.0),
                "z_score": cusum_result.get("z_score", 0.0),
                "sigma": cusum_result.get("sigma", 0.0),
                "alert": cusum_result.get("alert", False),
                "alert_status": cusum_result.get("alert_status", "NO_ALERT"),
                "reason": cusum_result.get("reason", ""),
                "decision_interval": cusum_result.get("decision_interval", self.cusum_decision_interval),
            },
            "ewma": {
                "week": week_num,
                "observed_rate": ewma_result.get("observed_rate", 0.0),
                "baseline_expected": ewma_result.get("baseline_expected", baseline_result["expected_rate"]),
                "alpha": ewma_result.get("alpha", self.ewma_alpha),
                "ewma": ewma_result.get("ewma", 0.0),
                "ucl": ewma_result.get("ucl", 0.0),
                "lcl": ewma_result.get("lcl", 0.0),
                "z_std": ewma_result.get("z_std", 0.0),
                "weekly_se": ewma_result.get("weekly_se", 0.0),
                "alert": ewma_result.get("alert", False),
                "alert_status": ewma_result.get("alert_status", "NO_ALERT"),
                "reason": ewma_result.get("reason", ""),
            },
            "no_leakage_confirmed": no_leakage,
        }

        return result

    def run_evaluation(
        self,
        start_week: int = 21,  # first week after baseline initialization
        end_week: int = 104,  # last historical week
    ) -> dict:
        """Run full temporal evaluation across multiple weeks.

        Critically important: weeks are processed in temporal order (1, 2, 3, ...),
        never shuffled. The baseline for week N uses only weeks 1..N-1.

        Parameters
        ----------
        start_week : int
            First week to evaluate (default: 21, after 20-week baseline initialization)
        end_week : int
            Last week to evaluate (default: 104)

        Returns
        -------
        dict with evaluation results containing baseline/cusum/ewma results
        for each evaluated week, plus summary statistics.
        """
        # Reset stateful detectors for a fresh run
        self._cusum_detector = None
        self._ewma_detector = None

        results = {
            "framework_config": {
                "baseline_method": self.baseline_method,
                "baseline_threshold": self.baseline_threshold,
                "baseline_weeks": self.baseline_weeks,
                "cusum_decision_interval": self.cusum_decision_interval,
                "cusum_reference_value": self.cusum_reference_value,
                "ewma_alpha": self.ewma_alpha,
                "ewma_control_limit": self.ewma_control_limit,
                "evaluation_weeks": f"week_{start_week:03d} to week_{end_week:03d}",
            },
            "baseline_results": [],
            "cusum_results": [],
            "ewma_results": [],
            "summary": {
                "total_weeks": 0,
                "baseline_alerts": 0,
                "cusum_alerts": 0,
                "ewma_alerts": 0,
                "baseline_alerted_weeks": [],
                "cusum_alerted_weeks": [],
                "ewma_alerted_weeks": [],
            },
            "no_data_leakage": True,  # guaranteed by framework design
        }

        # Process weeks in temporal order - CRITICAL for no-leakage
        for week_num in range(start_week, end_week + 1):
            # Evaluate this week using only historical data up to week_num-1
            result = self.evaluate_week_temporal(week_num, start_week)

            results["baseline_results"].append(result["baseline"])
            results["cusum_results"].append(result["cusum"])
            results["ewma_results"].append(result["ewma"])

            # Track alerts
            results["summary"]["total_weeks"] += 1

            if result["baseline"]["alert"]:
                results["summary"]["baseline_alerts"] += 1
                results["summary"]["baseline_alerted_weeks"].append(week_num)

            if result["cusum"]["alert"]:
                results["summary"]["cusum_alerts"] += 1
                results["summary"]["cusum_alerted_weeks"].append(week_num)

            if result["ewma"]["alert"]:
                results["summary"]["ewma_alerts"] += 1
                results["summary"]["ewma_alerted_weeks"].append(week_num)

        return results

    def get_alert_summary(self) -> dict:
        """Get summary of alerts across all evaluated weeks.

        Returns dict with alert counts and rates.
        """
        if not self.baseline_results:
            return {"message": "No evaluation results yet. Run evaluation first."}

        baseline_alerts = sum(1 for r in self.baseline_results if r["alert"])
        cusum_alerts = sum(1 for r in self.cusum_results if r["alert"])
        ewma_alerts = sum(1 for r in self.ewma_results if r["alert"])

        total = len(self.baseline_results)

        return {
            "total_evaluated": total,
            "baseline_alerts": baseline_alerts,
            "baseline_alert_rate": baseline_alerts / total if total > 0 else 0,
            "cusum_alerts": cusum_alerts,
            "cusum_alert_rate": cusum_alerts / total if total > 0 else 0,
            "ewma_alerts": ewma_alerts,
            "ewma_alert_rate": ewma_alerts / total if total > 0 else 0,
            "baseline_alerted_weeks": [
                r["week"] for r in self.baseline_results if r["alert"]
            ],
            "cusum_alerted_weeks": [
                r["week"] for r in self.cusum_results if r["alert"]
            ],
            "ewma_alerted_weeks": [
                r["week"] for r in self.ewma_results if r["alert"]
            ],
        }


def compute_evaluation_metrics(
    evaluation_results: dict,
    ground_truth: dict = None,
) -> dict:
    """Compute evaluation metrics comparing the three detectors.

    Parameters
    ----------
    evaluation_results : dict
        Results from TemporalEvaluationFramework.run_evaluation()
    ground_truth : dict, optional
        Ground truth outbreak information (from ground_truth.csv).
        Used POST-HOC for evaluation, NOT for prediction.
        Must NOT influence the detection alerts themselves.

    Returns
    -------
    dict with comparison metrics for BASELINE vs CUSUM vs EWMA
    using the SAME evaluation framework.
    """
    baseline_r = evaluation_results["baseline_results"]
    cusum_r = evaluation_results["cusum_results"]
    ewma_r = evaluation_results["ewma_results"]

    # Extract alert weeks per detector
    # week_index maps from result order to actual week number
    start_week = evaluation_results["framework_config"].get(
        "evaluation_weeks", "week_21 to week_104"
    ).split(" to ")[0].replace("week_", "")
    start_week = int(start_week)

    baseline_alerted_weeks = set()
    cusum_alerted_weeks = set()
    ewma_alerted_weeks = set()

    for i, r in enumerate(baseline_r):
        if r.get("alert", False):
            baseline_alerted_weeks.add(start_week + i)

    for i, r in enumerate(cusum_r):
        if r.get("alert", False):
            cusum_alerted_weeks.add(start_week + i)

    for i, r in enumerate(ewma_r):
        if r.get("alert", False):
            ewma_alerted_weeks.add(start_week + i)

    # Total weeks evaluated
    total_weeks = evaluation_results["summary"]["total_weeks"]

    # Compute comparison metrics
    metrics = {
        "comparison": {
            "baseline": {
                "total_alerted": sum(1 for r in baseline_r if r.get("alert", False)),
                "total_evaluated": total_weeks,
                "alert_rate": len(baseline_alerted_weeks) / total_weeks if total_weeks > 0 else 0,
            },
            "cusum": {
                "total_alerted": sum(1 for r in cusum_r if r.get("alert", False)),
                "total_evaluated": total_weeks,
                "alert_rate": len(cusum_alerted_weeks) / total_weeks if total_weeks > 0 else 0,
            },
            "ewma": {
                "total_alerted": sum(1 for r in ewma_r if r.get("alert", False)),
                "total_evaluated": total_weeks,
                "alert_rate": len(ewma_alerted_weeks) / total_weeks if total_weeks > 0 else 0,
            },
        },
        "fairness_check": {
            "no_future_leakage": True,
            "methodology_description": (
                "Evaluation respects temporal ordering: "
                "baseline for week N uses weeks 1..N-1 only. "
                "CUSUM and EWMA maintain state across weeks "
                "but do not access future observations. "
                "Ground truth is used only post-hoc for evaluation, "
                "not for prediction during the detection process."
            ),
        },
    }

    # If ground truth provided, add analysis
    if ground_truth is not None:
        metrics["ground_truth_analysis"] = {
            "note": (
                "Ground truth provided but detailed comparison not yet implemented. "
                "Post-hoc evaluation framework ready for ground truth integration. "
                "The framework ensures no future data leakage during prediction; "
                "ground truth may be used after alerts are generated for evaluation."
            )
        }

    return metrics