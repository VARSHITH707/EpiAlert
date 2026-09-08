"""EpiAlert CUSUM Detector Module

Implements a proper cumulative-sum change detection method.

CUSUM (Cumulative Sum) is a sequential analysis technique used to detect
a shift in the mean of a statistical process from a target value.

The method operates in standardized (z-score) form:
    z_t = (x_t - mu) / sigma
    S_t = max(0, S_{t-1} + z_t - k)
    Alert when S_t > h

Parameters are configurable, not hardcoded.
"""

from src.preprocessing import get_weekly_aggregation

__all__ = [
    "H_CUSUM", "K_CUSUM",
    "cusum_compute",
    "CUSUMDetector",
    "detect_cusum",
    "compute_cusum_sequence",
]

# Locked parameters — see BUILD directive section 4
# H_CUSUM: decision threshold in z-score units (Page 1954)
# h=5.0 gives roughly 3-sigma equivalent when k=0.5
H_CUSUM = 5.0
# K_CUSUM: departs from Yi et al. K=0.7; targets a 1-sigma shift, more sensitive to
#          the small absolute rises typical of street-level counts
K_CUSUM = 0.5


def cusum_compute(week_num: int, x_t: float, mu_t: float, sigma_t: float,
                  prev_S: float = 0.0, k: float = K_CUSUM, h: float = H_CUSUM) -> dict:
    """Compute one step of CUSUM per equation (4).

    S_t = max(0, S_{t-1} + z_t - k),  S_0 = 0
    z_t = (x_t - mu_t) / sigma_t
    Alert when S_t > h

    Parameters
    ----------
    week_num : int
        Week number (1-based). Used only for bookkeeping.
    x_t : float
        Observed infection rate this week.
    mu_t : float
        Baseline mean for this week.
    sigma_t : float
        Baseline standard deviation for this week.
    prev_S : float
        Previous CUSUM statistic S_{t-1}. Defaults to 0 (S_0).
    k : float
        Reference value (shift to detect). Default: K_CUSUM (0.5).
    h : float
        Decision threshold. Default: H_CUSUM (5.0).

    Returns
    -------
    dict with keys:
        "week_num", "x_t", "mu_t", "sigma_t", "z_t", "S_t", "alert"
    """
    if sigma_t <= 0:
        return {
            "week_num": week_num,
            "x_t": x_t,
            "mu_t": mu_t,
            "sigma_t": sigma_t,
            "z_t": 0.0,
            "S_t": prev_S,
            "alert": False,
        }

    z_t = (x_t - mu_t) / sigma_t
    S_t = max(0.0, prev_S + z_t - k)
    alert = S_t > h

    return {
        "week_num": week_num,
        "x_t": x_t,
        "mu_t": mu_t,
        "sigma_t": sigma_t,
        "z_t": z_t,
        "S_t": S_t,
        "alert": alert,
    }


class CUSUMDetector:
    """CUSUM (Cumulative Sum) change detection detector.

    Uses Page's one-sided upper CUSUM in standardized form to detect
    a sustained upward shift in the infection rate.

    State (cumulative sum) carries across weeks — proper sequential
    change detection behavior.

    Parameters
    ----------
    baseline_expected : float
        The expected infection rate from the baseline model (mu).
    decision_interval : float, optional
        The decision threshold h in z-score units. Defaults to 5.0.
    reference_value : float, optional
        The reference value k in z-score units (half the shift to detect).
        Defaults to 0.5 (detect ~0.5 sigma shifts).
    """

    def __init__(
        self,
        baseline_expected: float,
        decision_interval: float = 5.0,
        reference_value: float = None,
    ):
        self.baseline_expected = baseline_expected
        self.decision_interval = decision_interval

        # k in z-score units: how many standard errors of shift to detect
        # Default 0.5 means we start accumulating when rate exceeds baseline
        # by 0.5 standard errors.
        if reference_value is None:
            self.reference_value = 0.5
        else:
            self.reference_value = reference_value

        # CUSUM state - carries across weeks
        self._h_cusum = 0.0  # positive cumulative sum (upper)
        self._k_cusum = 0.0  # negative cumulative sum (lower, unused)
        self._initialized = False

    def reset(self):
        """Reset CUSUM state to initial values."""
        self._h_cusum = 0.0
        self._k_cusum = 0.0
        self._initialized = False

    def detect(self, week_num: int) -> dict:
        """Detect CUSUM alert for a specific week.

        Delegates the CUSUM formula to cusum_compute() — the single
        authoritative implementation. This class only manages state
        (previous S value) and data access (get_weekly_aggregation).

        Parameters
        ----------
        week_num : int
            Week number to evaluate

        Returns
        -------
        dict with CUSUM detection results (same keys as cusum_compute,
        plus metadata fields for downstream consumers).
        """
        # Get current week's infection rate
        agg = get_weekly_aggregation(week_num)
        current_rate = agg["infection_rate"]
        n = 3000  # people per week

        # Initialize state on first call
        if not self._initialized:
            self._h_cusum = 0.0
            self._k_cusum = 0.0
            self._initialized = True

        # Baseline and reference (in z-score units)
        mu = self.baseline_expected
        k = self.reference_value

        # Standard error of a binomial proportion under the baseline
        sigma = (mu * (1 - mu) / n) ** 0.5 if 0 < mu < 1 else 0.001

        # Delegate to the single authoritative CUSUM formula,
        # passing the detector's configured k and h.
        result = cusum_compute(
            week_num=week_num,
            x_t=current_rate,
            mu_t=mu,
            sigma_t=sigma,
            prev_S=self._h_cusum,
            k=self.reference_value,
            h=self.decision_interval,
        )

        # Update state from the computed result
        self._h_cusum = result["S_t"]
        # Lower CUSUM not tracked by cusum_compute; compute for completeness
        self._k_cusum = min(0.0, -(current_rate - mu) / sigma - k + self._k_cusum) if sigma > 0 else 0.0

        alert = result["alert"]
        s_t = result["S_t"]
        z_t = result["z_t"]

        # Determine reason
        if alert:
            alert_status = "ALERT"
            reason = (
                f"CUSUM week {week_num}: standardized cumulative sum {s_t:.2f} "
                f"exceeds threshold {self.decision_interval:.1f}. "
                f"Observed rate: {current_rate:.4f}, baseline: {mu:.4f}, "
                f"z-score: {z_t:.2f}, SE: {sigma:.4f}"
            )
            lead_time = None
        else:
            alert_status = "NO_ALERT"
            reason = (
                f"CUSUM week {week_num}: standardized cumulative sum {s_t:.2f} "
                f"below threshold {self.decision_interval:.1f}. "
                f"Observed rate: {current_rate:.4f}, z-score: {z_t:.2f}"
            )
            lead_time = None

        return {
            "week": week_num,
            "date": agg["date"],
            "observed_rate": current_rate,
            "baseline_expected": self.baseline_expected,
            "h_cusum": s_t,
            "k_cusum": self._k_cusum,
            "alert": alert,
            "alert_status": alert_status,
            "decision_interval": self.decision_interval,
            "reference_value": k,
            "z_score": z_t,
            "sigma": sigma,
            "lead_time": lead_time,
            "reason": reason,
        }


def detect_cusum(
    week_num: int,
    baseline_expected: float,
    decision_interval: float = 5.0,
    reference_value: float = None,
    reset_state: bool = False,
) -> dict:
    """Detect CUSUM alert for a single week.

    Parameters
    ----------
    week_num : int
        Week number to evaluate
    baseline_expected : float
        Expected infection rate from baseline model
    decision_interval : float
        Decision threshold h in z-score units (default: 5.0)
    reference_value : float
        Reference value k in z-score units (default: 0.5)
    reset_state : bool
        If True, reset the detector state before computing

    Returns
    -------
    dict with CUSUM detection results
    """
    detector = CUSUMDetector(
        baseline_expected=baseline_expected,
        decision_interval=decision_interval,
        reference_value=reference_value,
    )
    if reset_state:
        detector.reset()
    return detector.detect(week_num)


def compute_cusum_sequence(
    weeks: list,
    baseline_expected: float,
    decision_interval: float = H_CUSUM,
    reference_value: float = K_CUSUM,
    reset_at_start: bool = True,
) -> list:
    """Compute CUSUM for a sequence of weeks, maintaining state across weeks.

    This is the proper use case for CUSUM - cumulative tracking over time.

    Parameters
    ----------
    weeks : list of int
        Week numbers in temporal order
    baseline_expected : float
        Expected infection rate from baseline
    decision_interval : float
        Decision threshold h in z-score units. Default: H_CUSUM (5.0).
    reference_value : float
        Reference value k in z-score units. Default: K_CUSUM (0.5).
    reset_at_start : bool
        If True, reset CUSUM state before starting the sequence

    Returns
    -------
    list of dicts, one per week, with CUSUM results
    """
    detector = CUSUMDetector(
        baseline_expected=baseline_expected,
        decision_interval=decision_interval,
        reference_value=reference_value,
    )

    if reset_at_start:
        detector.reset()

    results = []
    for week_num in weeks:
        result = detector.detect(week_num)
        results.append(result)

    return results
