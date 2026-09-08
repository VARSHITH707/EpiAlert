"""EpiAlert EWMA Detector Module

Implements a proper Exponentially Weighted Moving Average detector.

EWMA (Exponentially Weighted Moving Average) uses a smoothing parameter
alpha to give more weight to recent observations and less to older ones.

The method supports:
- smoothing parameter alpha (0 < alpha <= 1)
- baseline/target value
- control limits or decision threshold
- initialization from first observation

Documentation of how EWMA generates an alert.
"""

from src.preprocessing import get_weekly_aggregation

__all__ = [
    "LAMBDA_EWMA", "L_EWMA",
    "ewma_compute", "ewma_ucl",
    "EWMADetector",
    "detect_ewma",
    "compute_ewma_sequence",
]

# Locked parameters — see BUILD directive section 4
# L_EWMA: follows Yi et al. K=3
L_EWMA = 3.0
# LAMBDA_EWMA: departs from Yi lambda=0.05; 0.05 gives memory ~2/lambda-1 ~= 39
#               observations, which would smooth away a 6-8 week street-level outbreak
LAMBDA_EWMA = 0.2


def ewma_compute(week_num: int, z_t: float, prev_Z: float = 0.0,
                 lambda_: float = LAMBDA_EWMA, L: float = L_EWMA) -> dict:
    """Compute one step of EWMA per equation (5).

    Z_t = lambda_ * z_t + (1 - lambda_) * Z_{t-1},  Z_0 = 0
    UCL_t = ewma_ucl(t, lambda_, L)
    Alert when Z_t > UCL_t

    Parameters
    ----------
    week_num : int
        Week number (1-based). Used only for bookkeeping.
    z_t : float
        Standardized observation this week: (x_t - mu_t) / sigma_t.
    prev_Z : float
        Previous EWMA statistic Z_{t-1}. Defaults to 0 (Z_0).
    lambda_ : float
        EWMA smoothing parameter. Default: LAMBDA_EWMA (0.2).
    L : float
        Control limit multiplier. Default: L_EWMA (3.0).

    Returns
    -------
    dict with keys: "week_num", "z_t", "Z_t", "UCL_t", "alert"
    """
    if prev_Z is None:
        prev_Z = 0.0
    Z_t = lambda_ * z_t + (1 - lambda_) * prev_Z
    UCL_t = ewma_ucl(t=week_num, lambda_=lambda_, L=L)
    alert = Z_t > UCL_t

    return {
        "week_num": week_num,
        "z_t": z_t,
        "Z_t": Z_t,
        "UCL_t": UCL_t,
        "alert": alert,
    }


def ewma_ucl(t: int, lambda_: float = LAMBDA_EWMA, L: float = L_EWMA) -> float:
    """Compute the time-dependent EWMA upper control limit per equation (6).

    UCL_t = L * sqrt( (lambda/(2-lambda)) * (1 - (1-lambda)^(2t)) )

    The factor (1 - (1-lambda)^(2t)) widens the limit in early periods,
    preventing spurious early signals before the EWMA has stabilised.

    Parameters
    ----------
    t : int
        Week number (1-based).
    lambda_ : float
        EWMA smoothing parameter. Default: LAMBDA_EWMA (0.2).
    L : float
        Control limit multiplier. Default: L_EWMA (3.0).

    Returns
    -------
    float : the upper control limit for week t.
    """
    factor = lambda_ / (2 - lambda_)
    time_factor = 1 - (1 - lambda_) ** (2 * t)
    return L * (factor * time_factor) ** 0.5



class EWMADetector:
    """EWMA (Exponentially Weighted Moving Average) detector.

    EWMA uses a weighted average of current and previous observations,
    where the weights decrease exponentially as observations age.

    The EWMA statistic at time t is:
        z_t = alpha * x_t + (1 - alpha) * z_{t-1}

    Where:
    - z_t = EWMA statistic at time t
    - x_t = observed infection rate at time t
    - alpha = smoothing parameter (0 < alpha <= 1), typically 0.1-0.3
    - z_{t-1} = EWMA statistic at time t-1

    Initialization: z_0 is typically set to the target/baseline value,
    or to the first observation x_1.

    Control limits are typically set at z_t +/- L * sigma_z where:
    - L is the control limit parameter (typically 3)
    - sigma_z = sqrt(alpha * p * (1-p) / (2 - alpha)) is the
      asymptotic standard deviation of the EWMA statistic

    An alert is generated when |z_t - target| > L * sigma_z (typically
    only the upper limit for outbreak detection).

    Parameters
    ----------
    baseline_expected : float
        The expected infection rate from the baseline model (target/control target)
    alpha : float, optional
        Smoothing parameter (default: 0.2). Higher alpha gives more
        weight to recent observations.
    control_limit : float, optional
        Multiplier for the standard error to set the control limit
        (default: 3.0, which gives approximate 3-sigma limits).
    """

    def __init__(
        self,
        baseline_expected: float,
        alpha: float = 0.2,
        control_limit: float = 3.0,
    ):
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be in (0, 1], got: {}".format(alpha))

        self.baseline_expected = baseline_expected
        self.alpha = alpha
        self.control_limit = control_limit

        # EWMA state - carries across weeks
        self._ewma_z = None  # EWMA statistic from previous week
        self._initialized = False

    def reset(self):
        """Reset EWMA state to initial values."""
        self._ewma_z = None
        self._initialized = False

    def detect(self, week_num: int) -> dict:
        """Detect EWMA alert for a specific week.

        Delegates the EWMA formula to ewma_compute() — the single
        authoritative implementation. This class only manages state
        (previous Z value), data access (get_weekly_aggregation),
        and conversion between rate space and z-score space.

        The EWMA statistic is computed in z-score space:
            z_t = (x_t - mu) / sigma          # standardize observation
            Z_t = lambda * z_t + (1-lambda) * Z_{t-1}   # EWMA of z-scores
            UCL_t = ewma_ucl(t)               # time-dependent threshold
            alert = Z_t > UCL_t

        Parameters
        ----------
        week_num : int
            Week number to evaluate

        Returns
        -------
        dict with EWMA detection results. The 'ewma' and 'ucl' fields
        are in rate space (converted from z-score space for consumers).
        """
        # Get current week's infection rate
        agg = get_weekly_aggregation(week_num)
        current_rate = agg["infection_rate"]
        n = 3000  # people per week

        # Baseline
        mu = self.baseline_expected

        # Standard error of a binomial proportion under the baseline
        sigma = (mu * (1 - mu) / n) ** 0.5 if 0 < mu < 1 else 0.001

        # Initialize state on first call
        if not self._initialized:
            # Z_0 = 0 in z-score space (matches ewma_compute default)
            self._ewma_z = 0.0
            self._initialized = True

        # Convert observation to z-score space for delegation
        z_t = (current_rate - mu) / sigma if sigma > 0 else 0.0

        # Delegate to the single authoritative EWMA formula,
        # passing the detector's configured alpha (lambda) and L (control limit).
        result = ewma_compute(
            week_num=week_num,
            z_t=z_t,
            prev_Z=self._ewma_z,
            lambda_=self.alpha,
            L=self.control_limit,
        )

        # Update state from the computed result
        self._ewma_z = result["Z_t"]

        Z_t = result["Z_t"]
        UCL_t = result["UCL_t"]
        alert = result["alert"]

        # Convert back to rate space for consumers
        ewma_rate = mu + Z_t * sigma
        ucl_rate = mu + UCL_t * sigma

        # Standardized EWMA deviation in z-score units
        z_std = Z_t  # Z_t is already in z-score units relative to baseline

        # Determine reason
        if alert:
            alert_status = "ALERT"
            reason = (
                f"EWMA week {week_num}: EWMA statistic {ewma_rate:.4f} "
                f"exceeds upper control limit {ucl_rate:.4f} "
                f"(alpha={self.alpha}, L={self.control_limit}, "
                f"Z={Z_t:.2f}, UCL_z={UCL_t:.2f}). "
                f"Observed rate: {current_rate:.4f}"
            )
        else:
            alert_status = "NO_ALERT"
            reason = (
                f"EWMA week {week_num}: EWMA statistic {ewma_rate:.4f} "
                f"within control limits (UCL={ucl_rate:.4f}, "
                f"alpha={self.alpha}, Z={Z_t:.2f}, UCL_z={UCL_t:.2f}). "
                f"Observed rate: {current_rate:.4f}"
            )

        return {
            "week": week_num,
            "date": agg["date"],
            "observed_rate": current_rate,
            "baseline_expected": self.baseline_expected,
            "alpha": self.alpha,
            "ewma": ewma_rate,
            "ucl": ucl_rate,
            "lcl": max(0, mu - UCL_t * sigma),
            "alert": alert,
            "alert_status": alert_status,
            "reason": reason,
            "z_std": z_std,
            "Z_t": Z_t,
            "UCL_t": UCL_t,
            "sigma": sigma,
        }


def detect_ewma(
    week_num: int,
    baseline_expected: float,
    alpha: float = 0.2,
    control_limit: float = 3.0,
    reset_state: bool = False,
) -> dict:
    """Detect EWMA alert for a single week.

    Parameters
    ----------
    week_num : int
        Week number to evaluate
    baseline_expected : float
        Expected infection rate from baseline model
    alpha : float
        Smoothing parameter (default: 0.2)
    control_limit : float
        Control limit multiplier (default: 3.0)
    reset_state : bool
        If True, reset the global EWMA state before computing

    Returns
    -------
    dict with EWMA detection results
    """
    detector = EWMADetector(
        baseline_expected=baseline_expected,
        alpha=alpha,
        control_limit=control_limit,
    )
    if reset_state:
        detector.reset()
    return detector.detect(week_num)


def compute_ewma_sequence(
    weeks: list,
    baseline_expected: float,
    alpha: float = LAMBDA_EWMA,
    control_limit: float = L_EWMA,
    initial_ewma: float = None,
) -> list:
    """Compute EWMA for a sequence of weeks, maintaining state across weeks.

    EWMA is recursive - each week's value depends on the previous week's.

    Parameters
    ----------
    weeks : list of int
        Week numbers in temporal order
    baseline_expected : float
        Expected infection rate from baseline
    alpha : float
        Smoothing parameter. Default: LAMBDA_EWMA (0.2).
    control_limit : float
        Control limit multiplier. Default: L_EWMA (3.0).
    initial_ewma : float
        Initial EWMA value z_0 (default: baseline_expected)

    Returns
    -------
    list of dicts, one per week, with EWMA results
    """
    detector = EWMADetector(
        baseline_expected=baseline_expected,
        alpha=alpha,
        control_limit=control_limit,
    )

    if initial_ewma is not None:
        detector._ewma_z = initial_ewma
        detector._initialized = True

    results = []
    for week_num in weeks:
        result = detector.detect(week_num)
        results.append(result)

    return results