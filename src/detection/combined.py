"""P8 Combined Detector

Fuses four independent signals into a single documented deterministic rule
producing one of: NORMAL, WATCH, ALERT, HIGH_ALERT.

The four signals (each 0 or 1):
    1. cusum_signal   — CUSUM S_t exceeds its decision threshold h
    2. ewma_signal    — EWMA Z_t exceeds its upper control limit UCL_t
    3. trend_signal   — observed count has risen for N consecutive weeks
                        (N = COMBINED_TREND_CONSECUTIVE_WEEKS, default 3)
                        by at least COMBINED_TREND_MIN_PCT_INCREASE% each week
    4. baseline_signal — observed rate deviates from baseline mean by more
                        than COMBINED_BASELINE_DEVIATION_THRESHOLD standard
                        deviations (sigma)

Status mapping (signal count → status):
    0 signals  → NORMAL
    1 signal   → WATCH
    2 signals  → ALERT
    3+ signals → HIGH_ALERT

Severity elevation: if trend_signal OR |baseline_deviation| >=
COMBINED_SEVERITY_ELEVATION_THRESHOLD (default 2.0 sigma), the status
is elevated by one level (WATCH→ALERT, ALERT→HIGH_ALERT, but
HIGH_ALERT stays HIGH_ALERT and NORMAL stays NORMAL).

All thresholds are imported from src.detection.config — no magic numbers.

Detection is performed at (disease, village, street) spatial granularity.
Street names alone are NOT sufficient as a key because 9 of 10 street
names appear in more than one village.

Public API
----------
CombinedDetector(disease, village, street, baseline_expected, n_population)
    .detect(week_num, observed_count,
            cusum_S=None, ewma_Z=None, cusum_h=None, ewma_UCL=None,
            baseline_mu=None, baseline_sigma=None,
            weekly_counts=None) -> dict
"""

from src.detection import config as _cfg


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------

def _cusum_signal(S_t: float, h: float) -> int:
    """Return 1 if CUSUM S_t has crossed its decision threshold h, else 0."""
    return 1 if (S_t is not None and h is not None and S_t > h) else 0


def _ewma_signal(Z_t: float, UCL_t: float) -> int:
    """Return 1 if EWMA Z_t has crossed its upper control limit, else 0."""
    return 1 if (Z_t is not None and UCL_t is not None and Z_t > UCL_t) else 0


def _trend_signal(
    weekly_counts: list,
    min_consecutive: int = None,
    min_pct_increase: float = None,
) -> int:
    """Return 1 if the last `min_consecutive` weeks show a sustained increase.

    A sustained increase means each week is strictly greater than the previous
    week AND the increase is at least `min_pct_increase` percent.

    Parameters
    ----------
    weekly_counts : list of int
        Ordered observed counts, most recent last.  E.g. [c_{t-3}, c_{t-2},
        c_{t-1}, c_t].
    min_consecutive : int
        Number of consecutive increases required.  Defaults to
        COMBINED_TREND_CONSECUTIVE_WEEKS.
    min_pct_increase : float
        Minimum percentage increase per step.  Defaults to
        COMBINED_TREND_MIN_PCT_INCREASE (20.0).
    """
    if min_consecutive is None:
        min_consecutive = _cfg.COMBINED_TREND_CONSECUTIVE_WEEKS
    if min_pct_increase is None:
        min_pct_increase = _cfg.COMBINED_TREND_MIN_PCT_INCREASE

    if len(weekly_counts) < min_consecutive + 1:
        return 0

    # Look at the most recent min_consecutive+1 values
    window = weekly_counts[-(min_consecutive + 1):]

    for i in range(1, len(window)):
        prev = window[i - 1]
        curr = window[i]
        if prev <= 0:
            return 0
        pct = (curr - prev) / prev * 100.0
        if curr <= prev or pct < min_pct_increase:
            return 0

    return 1


def _baseline_signal(
    observed_rate: float,
    baseline_mu: float,
    baseline_sigma: float,
    threshold: float = None,
) -> int:
    """Return 1 if the observed rate deviates from baseline by >= threshold sigma."""
    if threshold is None:
        threshold = _cfg.COMBINED_BASELINE_DEVIATION_THRESHOLD
    if baseline_sigma is None or baseline_sigma <= 0 or baseline_mu is None:
        return 0
    deviation = abs(observed_rate - baseline_mu)
    sigma = baseline_sigma
    # Use a small epsilon to avoid floating-point boundary failures
    return 1 if (deviation / sigma) >= (threshold - 1e-9) else 0


def _count_signals(cusum_sig, ewma_sig, trend_sig, baseline_sig):
    """Count how many of the four signals are active."""
    return sum([cusum_sig, ewma_sig, trend_sig, baseline_sig])


def _determine_status(signal_count: int, trend_sig: int, baseline_deviation_sigma: float) -> str:
    """Map signal count (+ optional severity boost) to a status string.

    Status levels (low → high):
        NORMAL, WATCH, ALERT, HIGH_ALERT

    Severity elevation: if trend_sig == 1 OR baseline_deviation_sigma >=
    COMBINED_SEVERITY_ELEVATION_THRESHOLD, bump the status up by one level,
    clamped to [NORMAL, HIGH_ALERT].
    """
    levels = ["NORMAL", "WATCH", "ALERT", "HIGH_ALERT"]

    # Base status from signal count
    if signal_count >= _cfg.COMBINED_HIGH_ALERT_SIGNAL_COUNT:
        idx = 3
    elif signal_count >= _cfg.COMBINED_ALERT_SIGNAL_COUNT:
        idx = 2
    elif signal_count >= _cfg.COMBINED_WATCH_SIGNAL_COUNT:
        idx = 1
    else:
        idx = 0

    # Severity elevation
    baseline_deviation_sigma = abs(baseline_deviation_sigma)
    if (trend_sig == 1 or baseline_deviation_sigma >=
            _cfg.COMBINED_SEVERITY_ELEVATION_THRESHOLD):
        idx = min(idx + 1, 3)  # bump up one, cap at HIGH_ALERT

    return levels[idx]


def _determine_confirmation_status(
    cusum_sig: int, ewma_sig: int, trend_sig: int, baseline_sig: int,
    baseline_deviation_sigma: float,
) -> str:
    """Determine status under CONFIRMATION fusion mode.

    Core rule: CUSUM AND EWMA must BOTH fire for ALERT.
    - Both fire: ALERT (or HIGH_ALERT if 3+ total signals or severity boost)
    - Exactly one of {cusum, ewma} fires: PROVISIONAL (logged, no alert)
    - Neither fires: check if trend/baseline alone can trigger (rare, treated as
      PROVISIONAL unless both trend AND baseline fire, then ALERT)

    This implements the Yi et al. 2025 concordance rule: requiring >=2 concordant
    models gave the best Youden index (0.651, sens 0.739, spec 0.912).

    Returns one of: NORMAL, PROVISIONAL, ALERT, HIGH_ALERT
    """
    levels = ["NORMAL", "PROVISIONAL", "ALERT", "HIGH_ALERT"]

    # Determine core confirmation status
    if cusum_sig and ewma_sig:
        # Both CUSUM and EWMA fire -> ALERT (concordant)
        idx = 2  # ALERT
    elif cusum_sig or ewma_sig:
        # Exactly one of the two core detectors fires -> PROVISIONAL
        idx = 1  # PROVISIONAL
    elif trend_sig and baseline_sig:
        # Neither CUSUM nor EWMA, but trend AND baseline both fire -> ALERT
        idx = 2
    elif trend_sig or baseline_sig:
        # Only one of trend/baseline -> PROVISIONAL
        idx = 1
    else:
        # Nothing fires
        idx = 0  # NORMAL

    # Severity elevation applies to ALERT and HIGH_ALERT rows.
    # PROVISIONAL rows (idx=1) are NOT elevated by severity alone;
    # they require CUSUM+EWMA concordance.
    baseline_deviation_sigma = abs(baseline_deviation_sigma)
    if idx == 2 and (trend_sig == 1 or baseline_deviation_sigma >= _cfg.COMBINED_SEVERITY_ELEVATION_THRESHOLD):
        idx = 3  # ALERT → HIGH_ALERT

    # High-alert override: 3+ signals bumps ALERT→HIGH_ALERT
    if idx == 2 and _count_signals(cusum_sig, ewma_sig, trend_sig, baseline_sig) >= _cfg.COMBINED_HIGH_ALERT_SIGNAL_COUNT:
        idx = 3

    return levels[idx]


def _determine_severity(status: str, signal_count: int) -> str:
    """Derive a human-readable severity string from the status."""
    mapping = {
        "NORMAL": "none",
        "WATCH": "low",
        "ALERT": "medium",
        "HIGH_ALERT": "high",
    }
    return mapping.get(status, "unknown")


# ---------------------------------------------------------------------------
# CombinedDetector
# ---------------------------------------------------------------------------

class CombinedDetector:
    """Fuses CUSUM, EWMA, trend and baseline deviation into one status.

    Parameters
    ----------
    disease : str
        Disease / syndrome name (e.g. "Dysentery").
    village : str
        Village name — part of the spatial key.
    street : str
        Street name — part of the spatial key.  Must be combined with
        village because 9 of 10 street names appear in multiple villages.
    baseline_expected : float
        Expected infection rate (baseline mean) for this (disease, village, street).
    n_population : int
        Number of people in this spatial unit this week.
    """

    def __init__(self, disease: str, village: str, street: str,
                 baseline_expected: float = 0.0, n_population: int = 3000):
        self.disease = disease
        self.village = village
        self.street = street
        self.baseline_expected = baseline_expected
        self.n_population = n_population

    @property
    def spatial_key(self):
        """Return the (village, street) tuple used as the spatial key."""
        return (self.village, self.street)

    def detect(
        self,
        week_num: int,
        observed_count: int,
        cusum_S: float = None,
        ewma_Z: float = None,
        cusum_h: float = None,
        ewma_UCL: float = None,
        baseline_mu: float = None,
        baseline_sigma: float = None,
        weekly_counts: list = None,
        cusum_k: float = None,
        ewma_lambda: float = None,
        ewma_l: float = None,
    ) -> dict:
        """Run the combined detection rule for one week.

        Parameters
        ----------
        week_num : int
            Week number (1-based).
        observed_count : int
            Observed number of infections this week for this (disease, village, street).
        cusum_S : float, optional
            CUSUM statistic S_t for this week.  If None, signal is 0.
        ewma_Z : float, optional
            EWMA statistic Z_t for this week.  If None, signal is 0.
        cusum_h : float, optional
            CUSUM decision threshold h.  Defaults to config CUSUM_H.
        ewma_UCL : float, optional
            EWMA upper control limit UCL_t.  If None, signal is 0.
        baseline_mu : float, optional
            Baseline mean mu_t.  If None, baseline signal is 0.
        baseline_sigma : float, optional
            Baseline std sigma_t.  If None, baseline signal is 0.
        weekly_counts : list of int, optional
            Ordered observed counts (most recent last).  Used for trend signal.
            If None or too short, trend signal is 0.
        cusum_k : float, optional
            CUSUM reference value k.  Defaults to config CUSUM_K.  Used only
            for the explanation string.
        ewma_lambda : float, optional
            EWMA smoothing parameter lambda.  Defaults to config EWMA_LAMBDA.
        ewma_l : float, optional
            EWMA control limit multiplier L.  Defaults to config EWMA_L.

        Returns
        -------
        dict with keys:
            week_number, disease, village, street, observed_count,
            expected_count, cusum_signal, ewma_signal, trend_sustained,
            baseline_deviation, status, severity, explanation
        """
        # Defaults from config — one source of truth
        if cusum_h is None:
            cusum_h = _cfg.CUSUM_H
        if cusum_k is None:
            cusum_k = _cfg.CUSUM_K
        if ewma_lambda is None:
            ewma_lambda = _cfg.EWMA_LAMBDA
        if ewma_l is None:
            ewma_l = _cfg.EWMA_L

        # ---- Compute the four signals ----
        cusum_sig = _cusum_signal(cusum_S, cusum_h)

        ewma_sig = _ewma_signal(ewma_Z, ewma_UCL)

        trend_sig = _trend_signal(
            weekly_counts=weekly_counts,
            min_consecutive=_cfg.COMBINED_TREND_CONSECUTIVE_WEEKS,
            min_pct_increase=_cfg.COMBINED_TREND_MIN_PCT_INCREASE,
        )

        observed_rate = observed_count / self.n_population if self.n_population > 0 else 0.0
        baseline_deviation_sigma = 0.0
        baseline_sig = _baseline_signal(
            observed_rate=observed_rate,
            baseline_mu=baseline_mu,
            baseline_sigma=baseline_sigma,
            threshold=_cfg.COMBINED_BASELINE_DEVIATION_THRESHOLD,
        )

        if baseline_sigma is not None and baseline_sigma > 0 and baseline_mu is not None:
            baseline_deviation_sigma = (observed_rate - baseline_mu) / baseline_sigma

        # ---- Determine status ----
        fusion_mode = _cfg.COMBINED_FUSION_MODE

        signal_count = _count_signals(cusum_sig, ewma_sig, trend_sig, baseline_sig)

        if fusion_mode == "confirmation":
            # Confirmation mode: CUSUM AND EWMA must BOTH fire for ALERT.
            # Exactly one of {cusum, ewma} firing = PROVISIONAL (not an alert).
            status = _determine_confirmation_status(
                cusum_sig=cusum_sig,
                ewma_sig=ewma_sig,
                trend_sig=trend_sig,
                baseline_sig=baseline_sig,
                baseline_deviation_sigma=baseline_deviation_sigma,
            )
        else:
            # Union mode (original P8 rule): any N of 4 signals -> status
            status = _determine_status(
                signal_count=signal_count,
                trend_sig=trend_sig,
                baseline_deviation_sigma=baseline_deviation_sigma,
            )

        # ---- Severity ----
        severity = _determine_severity(status, signal_count)

        # ---- Explanation ----
        parts = []
        parts.append(f"CUSUM={'ACTIVE' if cusum_sig else ' inactive'} (S={cusum_S:.3f}, h={cusum_h})")
        parts.append(f"EWMA={'ACTIVE' if ewma_sig else ' inactive'} (Z={ewma_Z:.3f}, UCL={ewma_UCL:.3f})")
        parts.append(f"Trend={'SUSTAINED' if trend_sig else ' not sustained'}")
        parts.append(f"Baseline deviation={baseline_deviation_sigma:+.2f} sigma ({'ACTIVE' if baseline_sig else ' inactive'})")
        parts.append(f"Fusion={fusion_mode} Signals={signal_count}/4 → {status}")

        return {
            "week_number": week_num,
            "disease": self.disease,
            "village": self.village,
            "street": self.street,
            "observed_count": observed_count,
            "expected_count": round(self.baseline_expected * self.n_population, 2),
            "cusum_signal": cusum_sig,
            "ewma_signal": ewma_sig,
            "trend_sustained": trend_sig,
            "baseline_deviation": round(baseline_deviation_sigma, 4),
            "status": status,
            "severity": severity,
            "explanation": " | ".join(parts),
            "fusion_mode": fusion_mode,
        }
