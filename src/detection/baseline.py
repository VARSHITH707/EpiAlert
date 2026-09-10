"""EpiAlert Adaptive Baseline Detector

Implements the contamination-resistant adaptive baseline from the EpiAlert paper.

The baseline for week t excludes confirmed-alert weeks (a_i = 1) from BOTH
the numerator sum AND the denominator count |B_t|. A naive implementation
that zeroes alerting weeks while leaving them in the denominator depresses
the mean and understates the variance — the exact opposite of the intended
correction, and it makes a sustained outbreak progressively hide itself.

Equations (paper section 3):
    (1) B_t = { i : t-g-W <= i <= t-g-1,  a_i = 0 }    eligible weeks
    (2) mu_t = (1/|B_t|) * sum(x_i),  i in B_t
    (3) sigma_t = sqrt[ (1/(|B_t|-1)) * sum((x_i - mu_t)^2) ],  i in B_t

Locked parameters (see BUILD directive section 3):
    W = 20   baseline window (weeks)
    g = 2    guard band (weeks skipped before t)
    B_MIN = 10  minimum eligible weeks; below this return INSUFFICIENT_BASELINE

The guard band g=2 means weeks t-1 and t-2 are always excluded, so an
emerging rise cannot enter its own baseline.
"""

from src.preprocessing import get_weekly_aggregation
from src.ingestion import week_number_to_date

# Locked parameters — see BUILD directive section 3
# W: longer than EARS 8-week (low counts need more obs to stabilize variance),
#    far shorter than Yi's 5-year windows
W = 20
# g: weeks skipped immediately before t, so an emerging rise cannot enter its own baseline
g = 2
# B_MIN: below this, report INSUFFICIENT_BASELINE, never "in control"
B_MIN = 10


def compute_adaptive_baseline(week_num: int, x_series: dict, a_series: dict) -> dict:
    """Compute the adaptive baseline for week `week_num`.

    Parameters
    ----------
    week_num : int
        The week being evaluated (1-based).
    x_series : dict
        Mapping week_num -> observed infection rate (float).
        Must contain all weeks 1..(week_num-1) at minimum.
    a_series : dict
        Mapping week_num -> alert flag (0 or 1).  a_i = 1 marks a confirmed
        alert week that must be excluded from the baseline.

    Returns
    -------
    dict with keys:
        "mu"          : baseline mean (float)
        "sigma"       : baseline std (float)
        "Bt_size"    : |B_t|, number of eligible weeks (int)
        "Bt_weeks"  : list of eligible week numbers (list[int])
        "status"     : "IN_CONTROL" | "INSUFFICIENT_BASELINE"
        "week_num"   : the week this baseline is for (int)

    If |B_t| < B_MIN the status is INSUFFICIENT_BASELINE and mu/sigma are
    returned as None so callers can suppress signalling.
    """
    # Equation (1): eligible weeks
    t = week_num
    start = t - g - W
    end = t - g - 1
    if start < 1:
        start = 1
    if end < start:
        # No eligible window yet (very early weeks)
        return {
            "mu": None,
            "sigma": None,
            "Bt_size": 0,
            "Bt_weeks": [],
            "status": "INSUFFICIENT_BASELINE",
            "week_num": t,
        }

    eligible = []
    for i in range(start, end + 1):
        if i not in a_series or a_series[i] == 0:
            eligible.append(i)

    Bt_size = len(eligible)

    # B_MIN check — never say "in control" with too few baseline points
    if Bt_size < B_MIN:
        return {
            "mu": None,
            "sigma": None,
            "Bt_size": Bt_size,
            "Bt_weeks": eligible,
            "status": "INSUFFICIENT_BASELINE",
            "week_num": t,
        }

    # Equations (2) and (3)
    values = [x_series[i] for i in eligible]
    mu = sum(values) / Bt_size

    if Bt_size > 1:
        variance = sum((v - mu) ** 2 for v in values) / (Bt_size - 1)
        sigma = variance ** 0.5
    else:
        # Single eligible week — variance undefined, use 0
        sigma = 0.0

    return {
        "mu": mu,
        "sigma": sigma,
        "Bt_size": Bt_size,
        "Bt_weeks": eligible,
        "status": "IN_CONTROL",
        "week_num": t,
    }


def get_observed_rate_series(up_to_week: int) -> dict:
    """Return {week_num: infection_rate} for weeks 1..up_to_week.

    One grouped query rather than one call to get_weekly_aggregation per week.
    That function also builds a dict for every person in the week, which the
    detectors never look at -- only dashboard and reporting use it. Over 104
    weeks that was 312,000 discarded objects per call, and the call is made
    once per evaluated week, so the cost was quadratic. It was the single
    slowest thing in the test suite at 161 seconds.

    The rate matches get_weekly_aggregation exactly: people whose infection is
    anything other than "No infection", over all people reporting that week.
    """
    from src.database.db import get_connection

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT week_number,
                  COUNT(*) AS total,
                  SUM(CASE WHEN infection != 'No infection' THEN 1 ELSE 0 END)
                      AS infected
           FROM reports
           WHERE week_number <= ?
           GROUP BY week_number""",
        (up_to_week,),
    )
    series = {
        week: (infected / total if total else 0.0)
        for week, total, infected in cur.fetchall()
    }
    cur.close()
    conn.close()

    # Weeks with no reports at all are absent from the query; report them as
    # zero so callers see a continuous series rather than a gap.
    return {w: series.get(w, 0.0) for w in range(1, up_to_week + 1)}


# ---------------------------------------------------------------------------
# Public API — single-week baseline check
# ---------------------------------------------------------------------------

def baseline_check(week_num: int, a_series: dict) -> dict:
    """Run the adaptive baseline check for `week_num`.

    Returns a dict suitable for consumption by CUSUM / EWMA / confirmation:
        "week"         : week_num
        "date"         : Sunday reference date
        "x_t"          : observed rate this week
        "mu_t"         : baseline mean (or None if insufficient)
        "sigma_t"      : baseline std (or None)
        "Bt_size"      : eligible weeks count
        "status"       : IN_CONTROL | INSUFFICIENT_BASELINE
        "x_series"     : full observed-rate series up to week_num

    The caller is responsible for passing a_series (the confirmed-alert flags
    from previous weeks) so the baseline is contamination-resistant.
    """
    x_series = get_observed_rate_series(week_num)
    bl = compute_adaptive_baseline(week_num, x_series, a_series)

    return {
        "week": week_num,
        "date": week_number_to_date(week_num),
        "x_t": x_series.get(week_num, 0.0),
        "mu_t": bl["mu"],
        "sigma_t": bl["sigma"],
        "Bt_size": bl["Bt_size"],
        "Bt_weeks": bl["Bt_weeks"],
        "status": bl["status"],
        "x_series": x_series,
    }


# ---------------------------------------------------------------------------
# Backward-compatible re-exports — preserve existing test_suite.py imports
# ---------------------------------------------------------------------------
# These are thin wrappers kept for compatibility with the existing test
# suite and CLI.  The new adaptive baseline lives in compute_adaptive_baseline
# and baseline_check above.  Do NOT refactor these away — R4.

import numpy as np
from collections import Counter
from src.database.db import get_connection


def _simple_mean_baseline(historical_weeks, window=None):
    """Legacy simple_mean baseline — kept for backward compatibility."""
    weekly_data = {}
    for w in historical_weeks:
        agg = get_weekly_aggregation(w)
        weekly_data[w] = agg["infection_rate"]
    expected = float(np.mean([weekly_data[w] for w in historical_weeks]))
    return {
        "method": "simple_mean",
        "expected": expected,
        "weeks_used": len(historical_weeks),
        "per_week": {w: weekly_data[w] for w in historical_weeks},
    }


def _moving_average_baseline(historical_weeks, window=4):
    """Legacy moving_average baseline — kept for backward compatibility."""
    weekly_data = {}
    for w in historical_weeks:
        agg = get_weekly_aggregation(w)
        weekly_data[w] = agg["infection_rate"]

    expected_per_week = {}
    for w in historical_weeks:
        idx = historical_weeks.index(w)
        relevant = [
            weekly_data[historical_weeks[j]]
            for j in range(max(0, idx - window + 1), idx + 1)
        ]
        expected_per_week[w] = float(np.mean(relevant)) if relevant else weekly_data[w]

    overall_expected = float(np.mean(list(expected_per_week.values())))
    return {
        "method": f"moving_average_window_{window}",
        "expected": overall_expected,
        "weeks_used": len(historical_weeks),
        "per_week": expected_per_week,
    }


def compute_baseline_expected(historical_weeks, method="simple_mean", window=4, **_kwargs):
    """Legacy compute_baseline_expected — kept for backward compatibility.

    Delegates to _simple_mean_baseline or _moving_average_baseline per `method`.
    """
    if not historical_weeks:
        historical_weeks = list(range(1, 105))

    if method == "simple_mean":
        return _simple_mean_baseline(historical_weeks)
    elif method == "moving_average":
        return _moving_average_baseline(historical_weeks, window=window)
    else:
        raise ValueError(f"Unknown baseline method: {method}")


class BaselineDetector:
    """Legacy BaselineDetector — kept for backward compatibility with
    test_suite.py and any external callers.

    Uses the simple_mean / moving_average methods via compute_baseline_expected.
    """

    def __init__(self, historical_weeks=None, method="simple_mean", window=4, threshold=2.0):
        if historical_weeks is None:
            historical_weeks = list(range(1, 21))

        self.historical_weeks = historical_weeks
        self.method = method
        self.window = window
        self.threshold = threshold

        self.baseline_params = compute_baseline_expected(
            historical_weeks=historical_weeks,
            method=method,
            window=window,
        )

        self.expected_value = self.baseline_params["expected"]
        self.per_week_baseline = self.baseline_params["per_week"]
        self.calculation_details = {
            "method": self.method,
            "historical_weeks": self.historical_weeks,
            "weeks_used": self.baseline_params["weeks_used"],
        }

        self._document_baseline()

    def _document_baseline(self):
        self.baseline_documentation = {
            "baseline_calculation": self.method,
            "historical_window": {
                "start_week": min(self.historical_weeks),
                "end_week": max(self.historical_weeks),
                "weeks_included": len(self.historical_weeks),
            },
            "expected_value": self.expected_value,
            "threshold": self.threshold,
            "alert_condition": f"observed_rate > expected_value + ({self.threshold} * std_error)",
            "per_week_baseline": {
                week: round(rate, 6) for week, rate in self.per_week_baseline.items()
            },
        }

    def detect(self, week_num: int) -> dict:
        agg = get_weekly_aggregation(week_num)
        observed_rate = agg["infection_rate"]

        deviation = observed_rate - self.expected_value
        alert = deviation > self.threshold * self._std_error_estimate()

        if alert:
            alert_status = "ALERT"
            reason = (
                f"Week {week_num} infection rate {observed_rate:.4f} exceeds "
                f"baseline {self.expected_value:.4f} by {deviation:.4f} "
                f"(>{self.threshold} * SE = {self.threshold * self._std_error_estimate():.4f})"
            )
        else:
            alert_status = "NO_ALERT"
            reason = (
                f"Week {week_num} infection rate {observed_rate:.4f} within baseline "
                f"± {self.threshold} * SE ({self._std_error_estimate():.4f})"
            )

        return {
            "week": week_num,
            "date": week_number_to_date(week_num),
            "observed_rate": observed_rate,
            "expected_rate": self.expected_value,
            "deviation": deviation,
            "alert": alert,
            "alert_status": alert_status,
            "threshold_used": self.threshold * self._std_error_estimate(),
            "reason": reason,
        }

    def _std_error_estimate(self):
        rates = [self.per_week_baseline[w] for w in self.historical_weeks
                 if w in self.per_week_baseline]
        if self.method == "simple_mean":
            if len(rates) > 1:
                return float((np.var(rates, ddof=0) / len(rates)) ** 0.5)
            return 0.01
        elif self.method == "moving_average":
            return 0.02
        return 0.01

    def detect_week(self, week_num: int):
        return self.detect(week_num)


def detect_baseline(week_num, historical_weeks=None, method="simple_mean", threshold=2.0):
    """Legacy detect_baseline — kept for backward compatibility."""
    if historical_weeks is None:
        historical_weeks = list(range(1, 21))
    detector = BaselineDetector(
        historical_weeks=historical_weeks,
        method=method,
        threshold=threshold,
    )
    return detector.detect(week_num)
