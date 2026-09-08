"""P8 Combined Detector unit tests.

Tests every status boundary (NORMAL / WATCH / ALERT / HIGH_ALERT) and
verifies the four signals fire independently and in combination.

Detection is at (disease, village, street) granularity — the tests
verify that the spatial key is (village, street) and not street alone.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.detection.combined import (
    CombinedDetector,
    _cusum_signal,
    _ewma_signal,
    _trend_signal,
    _baseline_signal,
    _count_signals,
    _determine_status,
)


# ---------------------------------------------------------------------------
# Signal unit tests
# ---------------------------------------------------------------------------

class TestCusumSignal:
    """cusum_signal = 1 when S_t > h, else 0."""

    def test_below_threshold(self):
        assert _cusum_signal(S_t=4.9, h=5.0) == 0

    def test_at_threshold(self):
        # S_t == h is NOT an alert (must strictly exceed)
        assert _cusum_signal(S_t=5.0, h=5.0) == 0

    def test_above_threshold(self):
        assert _cusum_signal(S_t=5.1, h=5.0) == 1

    def test_none_values(self):
        assert _cusum_signal(S_t=None, h=5.0) == 0
        assert _cusum_signal(S_t=5.0, h=None) == 0


class TestEwmaSignal:
    """ewma_signal = 1 when Z_t > UCL_t, else 0."""

    def test_below_limit(self):
        assert _ewma_signal(Z_t=2.9, UCL_t=3.0) == 0

    def test_at_limit(self):
        assert _ewma_signal(Z_t=3.0, UCL_t=3.0) == 0

    def test_above_limit(self):
        assert _ewma_signal(Z_t=3.1, UCL_t=3.0) == 1

    def test_none_values(self):
        assert _ewma_signal(Z_t=None, UCL_t=3.0) == 0
        assert _ewma_signal(Z_t=3.0, UCL_t=None) == 0


class TestTrendSignal:
    """trend_signal = 1 when counts rise for N consecutive weeks by >= min_pct%."""

    def test_no_increase(self):
        # Flat counts, no increase
        assert _trend_signal([10, 10, 10, 10]) == 0

    def test_decrease(self):
        assert _trend_signal([10, 9, 8, 7]) == 0

    def test_not_enough_history(self):
        # Need at least min_consecutive+1 = 4 values; only 3 given
        assert _trend_signal([10, 12, 14]) == 0

    def test_sufficient_increase(self):
        # 3 consecutive increases of >= 20% each (default)
        # 10 -> 12 (+20%) -> 14.4 (+20%) -> 17.28 (+20%)
        assert _trend_signal([10.0, 12.0, 14.4, 17.28]) == 1

    def test_partial_increase_insufficient(self):
        # Only 2 of 3 weeks increase enough
        assert _trend_signal([10, 12, 11, 13]) == 0

    def test_exactly_at_min_pct(self):
        # Exactly 20% each step (default)
        assert _trend_signal([100.0, 120.0, 144.0, 172.8]) == 1

    def test_just_below_min_pct(self):
        # 19.9% each — below 20% threshold
        assert _trend_signal([100.0, 119.9, 143.76, 172.37]) == 0

    def test_zero_prev_counts(self):
        # Division by zero protection
        assert _trend_signal([0, 5, 10, 15]) == 0

    def test_custom_thresholds(self):
        # With min_consecutive=2, min_pct=10%: 10->11 (+10%) -> 12.1 (+10%)
        # 11.0 * 1.10 = 12.10, exactly 10% increase
        # Floating-point: 11 * 1.1 = 12.100000000000001, so (12.1-11)/11*100 = 10.0...
        # Use values that produce clean floats: 100->110->121 (exactly 10%)
        assert _trend_signal(
            [100.0, 110.0, 121.0],
            min_consecutive=2,
            min_pct_increase=10.0,
        ) == 1


class TestBaselineSignal:
    """baseline_signal = 1 when |observed - mu| / sigma >= threshold."""

    def test_within_threshold(self):
        # deviation = 1.4 sigma, threshold = 1.5
        assert _baseline_signal(
            observed_rate=0.035, baseline_mu=0.021,
            baseline_sigma=0.01, threshold=1.5,
        ) == 0

    def test_at_threshold(self):
        # Use clean arithmetic: deviation=1.5*sigma exactly
        # observed_rate = mu + 1.5*sigma = 0.021 + 1.5*0.01 = 0.036
        # Floating-point: 0.021 + 0.015 = 0.036 (clean in decimal)
        # But 0.036 - 0.021 in float is 0.01499999... so use mu+1.5*sigma directly
        observed_rate = 0.021 + 1.5 * 0.01  # compute in one step
        assert _baseline_signal(
            observed_rate=observed_rate, baseline_mu=0.021,
            baseline_sigma=0.01, threshold=1.5,
        ) == 1

    def test_above_threshold(self):
        assert _baseline_signal(
            observed_rate=0.05, baseline_mu=0.021,
            baseline_sigma=0.01, threshold=1.5,
        ) == 1

    def test_none_sigma(self):
        assert _baseline_signal(
            observed_rate=0.05, baseline_mu=0.021,
            baseline_sigma=None, threshold=1.5,
        ) == 0

    def test_zero_sigma(self):
        assert _baseline_signal(
            observed_rate=0.05, baseline_mu=0.021,
            baseline_sigma=0.0, threshold=1.5,
        ) == 0

    def test_none_mu(self):
        assert _baseline_signal(
            observed_rate=0.05, baseline_mu=None,
            baseline_sigma=0.01, threshold=1.5,
        ) == 0


class TestCountSignals:
    """Signal counting is correct."""

    def test_all_off(self):
        assert _count_signals(0, 0, 0, 0) == 0

    def test_one_on(self):
        assert _count_signals(1, 0, 0, 0) == 1
        assert _count_signals(0, 1, 0, 0) == 1
        assert _count_signals(0, 0, 1, 0) == 1
        assert _count_signals(0, 0, 0, 1) == 1

    def test_two_on(self):
        assert _count_signals(1, 1, 0, 0) == 2
        assert _count_signals(1, 0, 1, 0) == 2
        assert _count_signals(1, 0, 0, 1) == 2
        assert _count_signals(0, 1, 1, 0) == 2
        assert _count_signals(0, 1, 0, 1) == 2
        assert _count_signals(0, 0, 1, 1) == 2

    def test_three_on(self):
        assert _count_signals(1, 1, 1, 0) == 3
        assert _count_signals(1, 1, 0, 1) == 3
        assert _count_signals(1, 0, 1, 1) == 3
        assert _count_signals(0, 1, 1, 1) == 3

    def test_all_on(self):
        assert _count_signals(1, 1, 1, 1) == 4


class TestDetermineStatus:
    """Status mapping: 0→NORMAL, 1→WATCH, 2→ALERT, 3+→HIGH_ALERT."""

    def test_zero_signals(self):
        assert _determine_status(signal_count=0, trend_sig=0, baseline_deviation_sigma=0.0) == "NORMAL"

    def test_one_signal_watch(self):
        assert _determine_status(signal_count=1, trend_sig=0, baseline_deviation_sigma=0.0) == "WATCH"

    def test_two_signals_alert(self):
        assert _determine_status(signal_count=2, trend_sig=0, baseline_deviation_sigma=0.0) == "ALERT"

    def test_three_signals_high_alert(self):
        assert _determine_status(signal_count=3, trend_sig=0, baseline_deviation_sigma=0.0) == "HIGH_ALERT"

    def test_four_signals_high_alert(self):
        assert _determine_status(signal_count=4, trend_sig=0, baseline_deviation_sigma=0.0) == "HIGH_ALERT"

    def test_severity_elevation_trend(self):
        # 1 signal + trend → bumped to ALERT
        assert _determine_status(signal_count=1, trend_sig=1, baseline_deviation_sigma=0.0) == "ALERT"

    def test_severity_elevation_baseline(self):
        # 1 signal + baseline >= 2.0 sigma → bumped to ALERT
        assert _determine_status(signal_count=1, trend_sig=0, baseline_deviation_sigma=2.0) == "ALERT"

    def test_severity_elevation_high_baseline(self):
        # 2 signals + baseline >= 2.0 sigma → bumped to HIGH_ALERT
        assert _determine_status(signal_count=2, trend_sig=0, baseline_deviation_sigma=2.5) == "HIGH_ALERT"

    def test_no_elevation_at_zero_signals(self):
        # 0 signals + trend → bumped to WATCH (not ALERT, min is NORMAL)
        assert _determine_status(signal_count=0, trend_sig=1, baseline_deviation_sigma=0.0) == "WATCH"

    def test_high_alert_capped(self):
        # HIGH_ALERT + elevation stays HIGH_ALERT
        assert _determine_status(signal_count=3, trend_sig=1, baseline_deviation_sigma=3.0) == "HIGH_ALERT"

    def test_baseline_below_severity_threshold_no_elevation(self):
        # baseline deviation 1.9 sigma (below 2.0) → no elevation
        assert _determine_status(signal_count=1, trend_sig=0, baseline_deviation_sigma=1.9) == "WATCH"


# ---------------------------------------------------------------------------
# CombinedDetector integration tests
# ---------------------------------------------------------------------------

class TestCombinedDetectorSpatialKey:
    """Detection must key on (village, street), not street alone."""

    def test_spatial_key_is_village_street(self):
        d = CombinedDetector("Dysentery", "Village A", "Street 1")
        assert d.spatial_key == ("Village A", "Street 1")

    def test_different_village_same_street_distinct_keys(self):
        # Street 1 appears in Village A and Village B — must be distinct
        d1 = CombinedDetector("Dysentery", "Village A", "Street 1")
        d2 = CombinedDetector("Dysentery", "Village B", "Street 1")
        assert d1.spatial_key != d2.spatial_key


class TestCombinedDetectorStatusBoundaries:
    """Each status boundary is reachable by tuning the signals."""

    @staticmethod
    def _make_detector():
        return CombinedDetector("Dysentery", "Village A", "Street 1",
                                baseline_expected=0.021, n_population=3000)

    def test_all_normal(self):
        d = self._make_detector()
        r = d.detect(
            week_num=50,
            observed_count=63,  # ~0.021 * 3000
            cusum_S=0.0, ewma_Z=0.0, cusum_h=5.0, ewma_UCL=3.0,
            baseline_mu=0.021, baseline_sigma=0.0026,
            weekly_counts=[63, 60, 62, 61],
        )
        assert r["status"] == "NORMAL"
        assert r["cusum_signal"] == 0
        assert r["ewma_signal"] == 0
        assert r["trend_sustained"] == 0
        assert r["baseline_deviation"] < 1.5  # not active

    def test_watch_cusum_only(self):
        d = self._make_detector()
        r = d.detect(
            week_num=50,
            observed_count=63,
            cusum_S=6.0, ewma_Z=0.0, cusum_h=5.0, ewma_UCL=3.0,
            baseline_mu=0.021, baseline_sigma=0.0026,
            weekly_counts=[63, 60, 62, 61],
        )
        assert r["status"] == "WATCH"
        assert r["cusum_signal"] == 1

    def test_watch_ewma_only(self):
        d = self._make_detector()
        r = d.detect(
            week_num=50,
            observed_count=63,
            cusum_S=0.0, ewma_Z=4.0, cusum_h=5.0, ewma_UCL=3.0,
            baseline_mu=0.021, baseline_sigma=0.0026,
            weekly_counts=[63, 60, 62, 61],
        )
        assert r["status"] == "WATCH"
        assert r["ewma_signal"] == 1

    def test_alert_cusum_and_ewma(self):
        d = self._make_detector()
        r = d.detect(
            week_num=50,
            observed_count=63,
            cusum_S=6.0, ewma_Z=4.0, cusum_h=5.0, ewma_UCL=3.0,
            baseline_mu=0.021, baseline_sigma=0.0026,
            weekly_counts=[63, 60, 62, 61],
        )
        assert r["status"] == "ALERT"
        assert r["cusum_signal"] == 1
        assert r["ewma_signal"] == 1

    def test_alert_three_signals(self):
        d = self._make_detector()
        r = d.detect(
            week_num=50,
            observed_count=63,
            cusum_S=6.0, ewma_Z=4.0, cusum_h=5.0, ewma_UCL=3.0,
            baseline_mu=0.021, baseline_sigma=0.0026,
            weekly_counts=[63, 60, 62, 61],
        )
        # We have 2 signals (cusum+ewma), need 3 for HIGH_ALERT
        # Add trend to get 3.  Use counts with clean 20% steps.
        # 50 -> 60 (20%) -> 72 (20%) -> 86.4 (20%)
        r = d.detect(
            week_num=50,
            observed_count=63,
            cusum_S=6.0, ewma_Z=4.0, cusum_h=5.0, ewma_UCL=3.0,
            baseline_mu=0.021, baseline_sigma=0.0026,
            weekly_counts=[50, 60, 72, 86.4],
        )
        assert r["status"] == "HIGH_ALERT"
        assert r["cusum_signal"] == 1
        assert r["ewma_signal"] == 1
        assert r["trend_sustained"] == 1

    def test_trend_only_elevates_to_alert(self):
        d = self._make_detector()
        r = d.detect(
            week_num=50,
            observed_count=63,
            cusum_S=0.0, ewma_Z=0.0, cusum_h=5.0, ewma_UCL=3.0,
            baseline_mu=0.021, baseline_sigma=0.0026,
            weekly_counts=[50, 60, 72, 86.4],  # sustained trend (20% each step)
        )
        # trend=1 provides both the signal AND the elevation:
        # 1 signal (trend) -> WATCH, +1 elevation (trend) -> ALERT
        assert r["status"] == "ALERT"
        assert r["trend_sustained"] == 1

    def test_baseline_deviation_alone_does_not_trigger(self):
        d = self._make_detector()
        r = d.detect(
            week_num=50,
            observed_count=63,
            cusum_S=0.0, ewma_Z=0.0, cusum_h=5.0, ewma_UCL=3.0,
            baseline_mu=0.021, baseline_sigma=0.0026,
            weekly_counts=[63, 60, 62, 61],
        )
        # deviation = 0, no signal
        assert r["status"] == "NORMAL"
        assert r["baseline_deviation"] == 0.0

    def test_baseline_deviation_above_threshold(self):
        d = self._make_detector()
        r = d.detect(
            week_num=50,
            observed_count=150,  # 0.05 rate, ~11 sigma above baseline
            cusum_S=0.0, ewma_Z=0.0, cusum_h=5.0, ewma_UCL=3.0,
            baseline_mu=0.021, baseline_sigma=0.0026,
            weekly_counts=[63, 60, 62, 61],
        )
        # 1 signal (baseline) + baseline >= 2.0 sigma -> bumped to ALERT
        assert r["status"] == "ALERT"
        assert r["baseline_deviation"] > 2.0

    def test_severity_elevation_watch_to_alert(self):
        d = self._make_detector()
        # Use a moderate deviation that activates baseline (>= 1.5 sigma)
        # but stays below 2.0 sigma so it does NOT trigger elevation.
        # observed_count=78 -> rate=0.026, deviation=(0.026-0.021)/0.0026=1.92 sigma
        # baseline_signal=1 (>=1.5), cusum_signal=0 (S=0 < h=5.0)
        # signal_count=1 -> WATCH, no elevation -> stays WATCH
        # Now add cusum: S=6.0 > h=5.0, so cusum_signal=1
        # signal_count=2 -> ALERT
        r = d.detect(
            week_num=50,
            observed_count=78,
            cusum_S=6.0, ewma_Z=0.0, cusum_h=5.0, ewma_UCL=3.0,
            baseline_mu=0.021, baseline_sigma=0.0026,
            weekly_counts=[63, 60, 62, 61],
        )
        # cusum=1, baseline=1 -> signal_count=2 -> ALERT
        # baseline deviation 1.92 sigma (< 2.0) -> no elevation -> stays ALERT
        assert r["status"] == "ALERT"

    def test_status_has_severity(self):
        d = self._make_detector()
        r = d.detect(
            week_num=50,
            observed_count=63,
            cusum_S=6.0, ewma_Z=0.0, cusum_h=5.0, ewma_UCL=3.0,
            baseline_mu=0.021, baseline_sigma=0.0026,
            weekly_counts=[63, 60, 62, 61],
        )
        assert r["severity"] in ("none", "low", "medium", "high")
        assert r["severity"] == "low"  # WATCH

    def test_status_has_explanation(self):
        d = self._make_detector()
        r = d.detect(
            week_num=50,
            observed_count=63,
            cusum_S=6.0, ewma_Z=0.0, cusum_h=5.0, ewma_UCL=3.0,
            baseline_mu=0.021, baseline_sigma=0.0026,
            weekly_counts=[63, 60, 62, 61],
        )
        assert "CUSUM=ACTIVE" in r["explanation"]
        assert "EWMA= inactive" in r["explanation"]
        assert "Signals=1/4" in r["explanation"]
        assert "WATCH" in r["explanation"]

    def test_expected_count_from_baseline(self):
        d = CombinedDetector("Dysentery", "V", "S", baseline_expected=0.021, n_population=3000)
        r = d.detect(
            week_num=1,
            observed_count=63,
            cusum_S=0.0, ewma_Z=0.0, cusum_h=5.0, ewma_UCL=3.0,
            baseline_mu=None, baseline_sigma=None,
            weekly_counts=[],
        )
        assert r["expected_count"] == 63.0  # 0.021 * 3000

    def test_zero_population(self):
        d = CombinedDetector("Dysentery", "V", "S", baseline_expected=0.0, n_population=0)
        r = d.detect(
            week_num=1,
            observed_count=5,
            cusum_S=0.0, ewma_Z=0.0, cusum_h=5.0, ewma_UCL=3.0,
            baseline_mu=None, baseline_sigma=None,
            weekly_counts=[],
        )
        assert r["observed_count"] == 5
        # rate = 5/0 = 0.0 due to guard
        assert r["expected_count"] == 0.0


class TestCombinedDetectorConfigDriven:
    """Verifies the detector reads thresholds from config, not magic numbers."""

    def test_uses_config_cusum_h_default(self):
        d = CombinedDetector("Dysentery", "V", "S")
        r = d.detect(
            week_num=1,
            observed_count=100,
            cusum_S=4.0, ewma_Z=0.0,
            cusum_h=None, ewma_UCL=3.0,  # None → use config
            baseline_mu=None, baseline_sigma=None,
            weekly_counts=[],
        )
        # CUSUM signal is 0 because 4.0 < CUSUM_H (5.0)
        assert r["cusum_signal"] == 0

    def test_uses_config_cusum_h_override(self):
        d = CombinedDetector("Dysentery", "V", "S")
        r = d.detect(
            week_num=1,
            observed_count=100,
            cusum_S=4.0, ewma_Z=0.0,
            cusum_h=3.0, ewma_UCL=3.0,  # override: 4.0 > 3.0 → signal
            baseline_mu=None, baseline_sigma=None,
            weekly_counts=[],
        )
        assert r["cusum_signal"] == 1


class TestCombinedDetectorNoImplicitImports:
    """Guard: CombinedDetector must not silently import globals from other modules."""

    def test_only_config_imported(self):
        import src.detection.combined as mod
        import inspect
        source = inspect.getsource(mod)
        # Should not reference src.evaluation, src.detection.cusum (module),
        # or any other detection module globally
        forbidden = ["from src.evaluation", "from src.detection.cusum import",
                     "from src.detection.ewma import"]
        for fb in forbidden:
            assert fb not in source, f"combined.py should not import {fb}"
