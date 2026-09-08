"""P8 Detection configuration — all thresholds as named constants.

No magic numbers anywhere in the detection pipeline. Every threshold
referenced by CUSUM, EWMA, trend, baseline, or combined detectors is
defined here.

These are the locked defaults established in the BUILD directive.
They can be overridden via environment variables for tuning runs,
but the code must never silently substitute one value for another.
"""

import os

# ---------------------------------------------------------------------------
# CUSUM parameters (BUILD directive section 4)
# ---------------------------------------------------------------------------
# h: decision threshold in z-score units (Page 1954)
# h=5.0 gives roughly 3-sigma equivalent when k=0.5
CUSUM_H = float(os.getenv("CUSUM_H", "5.0"))
# k: reference value — half the shift to detect (z-score units)
# k=0.5 targets a 1-sigma shift, more sensitive to small absolute rises
CUSUM_K = float(os.getenv("CUSUM_K", "0.5"))

# ---------------------------------------------------------------------------
# EWMA parameters (BUILD directive section 4)
# ---------------------------------------------------------------------------
EWMA_LAMBDA = float(os.getenv("EWMA_LAMBDA", "0.2"))
EWMA_L = float(os.getenv("EWMA_L", "3.0"))

# ---------------------------------------------------------------------------
# Combined detector thresholds (P8)
# ---------------------------------------------------------------------------
# Each detector signal is considered ACTIVE when its statistic crosses
# this fraction of its alert threshold.  E.g. CUSUM at 0.5*h is a
# "building" signal, at h it is "triggered".
COMBINED_CUSUM_ACTIVE_FRAC = float(os.getenv("COMBINED_CUSUM_ACTIVE_FRAC", "0.5"))
COMBINED_EWMA_ACTIVE_FRAC  = float(os.getenv("COMBINED_EWMA_ACTIVE_FRAC", "0.5"))

# Trend: number of consecutive weeks of increase to count as "sustained"
COMBINED_TREND_CONSECUTIVE_WEEKS = int(os.getenv("COMBINED_TREND_CONSECUTIVE_WEEKS", "3"))
# Trend: minimum percentage increase per week to count as "rising"
COMBINED_TREND_MIN_PCT_INCREASE = float(os.getenv("COMBINED_TREND_MIN_PCT_INCREASE", "20.0"))

# Baseline deviation: standardised deviation (|deviation| / sigma) that
# counts as a meaningful baseline departure.
COMBINED_BASELINE_DEVIATION_THRESHOLD = float(
    os.getenv("COMBINED_BASELINE_DEVIATION_THRESHOLD", "1.5")
)

# Status thresholds (signal counts that map to NORMAL/WATCH/ALERT/HIGH_ALERT)
# Used by UNION fusion mode: any-N-of-4 signals -> status level
COMBINED_WATCH_SIGNAL_COUNT = int(os.getenv("COMBINED_WATCH_SIGNAL_COUNT", "1"))
COMBINED_ALERT_SIGNAL_COUNT = int(os.getenv("COMBINED_ALERT_SIGNAL_COUNT", "2"))
COMBINED_HIGH_ALERT_SIGNAL_COUNT = int(os.getenv("COMBINED_HIGH_ALERT_SIGNAL_COUNT", "3"))

# Severity boost: a sustained trend OR a baseline deviation > 2*sigma
# elevates the status by one level (e.g. WATCH -> ALERT).
COMBINED_SEVERITY_ELEVATION_THRESHOLD = float(
    os.getenv("COMBINED_SEVERITY_ELEVATION_THRESHOLD", "2.0")
)

# Fusion mode: which rule to use for combining signals.
# "union" = any N of 4 signals (original P8 rule)
# "confirmation" = CUSUM AND EWMA must both fire (Yi et al. 2025 concordance)
COMBINED_FUSION_MODE = os.getenv("COMBINED_FUSION_MODE", "union")  # union | confirmation

# Confirmation mode thresholds (only used when FUSION_MODE=confirmation)
# Both CUSUM and EWMA must fire for ALERT. One alone = PROVISIONAL.
CONFIRMATION_PROVISIONAL_MIN_SIGNALS = int(os.getenv("CONFIRMATION_PROVISIONAL_MIN_SIGNALS", "1"))
CONFIRMATION_ALERT_CUSP_EWMA_BOTH = True  # ALERT requires cusum_sig AND ewma_sig
