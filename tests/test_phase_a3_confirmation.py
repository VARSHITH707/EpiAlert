"""Tests for Phase A3 — two-of-two confirmation rule.

The confirmation rule implements a three-state machine:
  IN_CONTROL  — neither CUSUM nor EWMA exceeds its limit
  PROVISIONAL — exactly one exceeds (log + queue, NO alert, a_t = 0)
  CONFIRMED   — both exceed (a_t = 1, enter localisation + alert path)

This is the direct analogue of Yi et al.'s Amber grade (>=2 of 4 concordant
models, Youden=0.651) applied to two statistics: requiring both is the
intersection of two error rates rather than their union.

RULE: every number below is hand-computed or comes from running the code
on the real dataset. No fabricated metrics.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.detection.cusum import H_CUSUM, K_CUSUM, cusum_compute
from src.detection.ewma import L_EWMA, LAMBDA_EWMA, ewma_compute, ewma_ucl


class TestConfirmationConstants:
    """Verify the confirmation module exports expected constants."""

    def test_n_c_constant_exists(self):
        from src.detection.confirmation import N_C
        assert N_C == 2

    def test_n_v_constant_exists(self):
        from src.detection.confirmation import N_V
        assert N_V == 2


class TestConfirmationThreeStates:
    """Verify the three-state confirmation logic."""

    def test_in_control_when_neither_exceeds(self):
        """Neither CUSUM nor EWMA exceeds -> IN_CONTROL, a_t = 0."""
        from src.detection.confirmation import confirm

        # Week 1: CUSUM S_1=1.0 < H_CUSUM=5.0, EWMA Z_1=0.3 < UCL_1=0.6
        cusum_result = cusum_compute(week_num=1, x_t=0.08, mu_t=0.05, sigma_t=0.02)
        ewma_result = ewma_compute(week_num=1, z_t=cusum_result["z_t"])

        result = confirm(week_num=1, cusum_result=cusum_result, ewma_result=ewma_result)
        assert result["status"] == "IN_CONTROL"
        assert result["a_t"] == 0
        assert result["alert"] is False

    def test_provisional_when_only_cusum_exceeds(self):
        """Only CUSUM exceeds -> PROVISIONAL, a_t = 0, NO community alert."""
        from src.detection.confirmation import confirm

        # Construct a case where CUSUM exceeds but EWMA doesn't
        # High x_t to push CUSUM up but moderate enough that EWMA stays below UCL
        # Week with x=0.20, mu=0.05, sigma=0.02 -> z_t = 7.5
        # CUSUM: S = max(0, 0 + 7.5 - 0.5) = 7.0 > 5.0 -> CUSUM alerts
        # EWMA: Z = 0.2*7.5 + 0.8*0 = 1.5 > UCL_1=0.6 -> EWMA also alerts
        # Need a case where only CUSUM exceeds
        # Use a smaller z_t so CUSUM crosses but EWMA doesn't
        # z_t = 6.0: CUSUM S = max(0, 0+6.0-0.5) = 5.5 > 5.0 (ALERT)
        #          EWMA Z = 0.2*6.0 + 0.8*0 = 1.2 > UCL_1=0.6 (ALERT)
        # Hmm, both alert. Need z_t where CUSUM > h but EWMA < UCL
        # CUSUM: S = z_t - 0.5 > 5.0 -> z_t > 5.5
        # EWMA: Z = 0.2*z_t < 0.6 -> z_t < 3.0
        # Contradiction: can't have both with Z_0=0
        # With accumulated S from prior weeks, CUSUM can exceed while EWMA doesn't
        # Use prev_S=5.0, z_t=0.6: CUSUM S = max(0, 5.0+0.6-0.5) = 5.1 > 5.0 (ALERT)
        # EWMA: Z = 0.2*0.6 + 0.8*0 = 0.12 < 0.6 (NO ALERT)
        cusum_result = cusum_compute(week_num=10, x_t=0.062, mu_t=0.05, sigma_t=0.02,
                                      prev_S=5.0)
        assert cusum_result["alert"] is True  # S=5.1 > 5.0
        ewma_result = ewma_compute(week_num=10, z_t=cusum_result["z_t"], prev_Z=0.0)
        assert ewma_result["alert"] is False  # Z=0.12 < UCL_10≈1.0

        result = confirm(week_num=10, cusum_result=cusum_result, ewma_result=ewma_result)
        assert result["status"] == "PROVISIONAL"
        assert result["a_t"] == 0
        assert result["alert"] is False
        assert "PROVISIONAL" in result["reason"]

    def test_provisional_when_only_ewma_exceeds(self):
        """Only EWMA exceeds -> PROVISIONAL, a_t = 0, NO community alert."""
        from src.detection.confirmation import confirm

        # EWMA exceeds but CUSUM doesn't
        # Use large z_t but low prev_S so CUSUM stays below threshold
        # z_t = 3.0: EWMA Z = 0.2*3.0 = 0.6 = UCL_1 -> borderline
        # Use z_t = 3.5: Z = 0.7 > UCL_1=0.6 (ALERT)
        # CUSUM: S = max(0, 0+3.5-0.5) = 3.0 < 5.0 (NO ALERT)
        cusum_result = cusum_compute(week_num=1, x_t=0.12, mu_t=0.05, sigma_t=0.02)
        assert cusum_result["alert"] is False  # S=3.0 < 5.0
        ewma_result = ewma_compute(week_num=1, z_t=cusum_result["z_t"], prev_Z=0.0)
        assert ewma_result["alert"] is True  # Z=0.7 > 0.6

        result = confirm(week_num=1, cusum_result=cusum_result, ewma_result=ewma_result)
        assert result["status"] == "PROVISIONAL"
        assert result["a_t"] == 0
        assert result["alert"] is False

    def test_confirmed_when_both_exceed(self):
        """Both exceed -> CONFIRMED, a_t = 1, enter alert path."""
        from src.detection.confirmation import confirm

        # Both exceed: z_t = 6.0
        # CUSUM: S = max(0, 0+6.0-0.5) = 5.5 > 5.0 (ALERT)
        # EWMA: Z = 0.2*6.0 = 1.2 > UCL_1=0.6 (ALERT)
        cusum_result = cusum_compute(week_num=1, x_t=0.17, mu_t=0.05, sigma_t=0.02)
        assert cusum_result["alert"] is True  # S=5.5 > 5.0
        assert cusum_result["z_t"] == pytest.approx(6.0)
        ewma_result = ewma_compute(week_num=1, z_t=6.0, prev_Z=0.0)
        assert ewma_result["alert"] is True  # Z=1.2 > 0.6

        result = confirm(week_num=1, cusum_result=cusum_result, ewma_result=ewma_result)
        assert result["status"] == "CONFIRMED"
        assert result["a_t"] == 1
        assert result["alert"] is True
        assert "CONFIRMED" in result["reason"]


class TestConfirmationFeedback:
    """Verify a_t=1 is written back so baseline excludes the confirmed week.

    This is the critical feedback loop: a confirmed week must be absent from
    the following week's B_t (the adaptive baseline).
    """

    def test_confirmed_week_excluded_from_next_baseline(self):
        """After a CONFIRMED week (a_t=1), the next week's baseline must exclude it.

        This test verifies the feedback loop: the confirmation module writes a_t=1,
        and the baseline module's compute_adaptive_baseline excludes weeks with a_i=1
        from B_t.
        """
        from src.detection.confirmation import confirm, ConfirmationState
        from src.detection.baseline import compute_adaptive_baseline

        # Use week 30 so B_31 has enough eligible weeks (>= B_MIN=10)
        # Week 30: CONFIRMED (both exceed)
        cusum_result = cusum_compute(week_num=30, x_t=0.17, mu_t=0.05, sigma_t=0.02)
        ewma_result = ewma_compute(week_num=30, z_t=cusum_result["z_t"], prev_Z=0.0)
        confirm_result = confirm(week_num=30, cusum_result=cusum_result,
                                 ewma_result=ewma_result)
        assert confirm_result["status"] == "CONFIRMED"
        assert confirm_result["a_t"] == 1

        # Track a_t series including the confirmed week
        a_series = {30: 1}  # week 30 is confirmed

        # Week 31: compute baseline — week 30 must be excluded from B_31
        # B_31 = weeks max(1, 31-2-20)..31-2-1 = 9..28, then remove week 30 (not in range anyway)
        # Actually week 30 IS in range 9..28? No, 30 > 28. So week 30 excluded by guard band.
        # Let me use a week where the confirmed week falls within the baseline window.
        # Week 25: CONFIRMED
        cusum_result2 = cusum_compute(week_num=25, x_t=0.17, mu_t=0.05, sigma_t=0.02)
        ewma_result2 = ewma_compute(week_num=25, z_t=cusum_result2["z_t"], prev_Z=0.0)
        confirm_result2 = confirm(week_num=25, cusum_result=cusum_result2,
                                  ewma_result=ewma_result2)
        assert confirm_result2["status"] == "CONFIRMED"

        a_series2 = {25: 1}

        # Week 26: B_26 = weeks max(1, 26-2-20)..26-2-1 = 4..23
        # Week 25 is NOT in 4..23 (it's week 25, after the window end 23)
        # So need to check a week where confirmed week is inside B_t window
        # Week 35: B_35 = 35-2-20..35-2-1 = 13..32. Week 30 IS in 13..32.
        cusum_result3 = cusum_compute(week_num=30, x_t=0.17, mu_t=0.05, sigma_t=0.02)
        ewma_result3 = ewma_compute(week_num=30, z_t=cusum_result3["z_t"], prev_Z=0.0)
        confirm(week_num=30, cusum_result=cusum_result3, ewma_result=ewma_result3)

        a_series3 = {30: 1}

        # Week 35: B_35 should exclude week 30
        x_series3 = {i: 0.05 for i in range(1, 50)}
        x_series3[30] = 0.17

        bl = compute_adaptive_baseline(week_num=35, x_series=x_series3, a_series=a_series3)

        # Week 30 (the confirmed week) must NOT be in B_35
        assert 30 not in bl["Bt_weeks"], \
            f"Confirmed week 30 must be excluded from B_35, but found in {bl['Bt_weeks']}"
        # B_35 = weeks 13..32 minus week 30 = 19 weeks
        assert bl["Bt_size"] == 19, \
            f"Expected 19 eligible weeks (13..32 minus week 30), got {bl['Bt_size']}"
        assert bl["status"] == "IN_CONTROL"

    def test_provisional_week_not_excluded_from_baseline(self):
        """A PROVISIONAL week (a_t=0) must still be included in the baseline.

        PROVISIONAL means one statistic exceeded but not both — the week is
        NOT marked as alerting, so it stays in the baseline.
        """
        from src.detection.confirmation import confirm
        from src.detection.baseline import compute_adaptive_baseline

        # Week 30: PROVISIONAL (only CUSUM exceeds)
        cusum_result = cusum_compute(week_num=30, x_t=0.062, mu_t=0.05, sigma_t=0.02,
                                      prev_S=4.95)
        assert cusum_result["alert"] is True
        ewma_result = ewma_compute(week_num=30, z_t=cusum_result["z_t"], prev_Z=0.0)
        assert ewma_result["alert"] is False

        confirm_result = confirm(week_num=30, cusum_result=cusum_result,
                                 ewma_result=ewma_result)
        assert confirm_result["status"] == "PROVISIONAL"
        assert confirm_result["a_t"] == 0

        # No confirmed alerts, so a_series is empty
        a_series = {}

        x_series = {i: 0.05 for i in range(1, 50)}
        x_series[30] = 0.062

        # Week 35: B_35 = weeks 13..32. Week 30 IS in 13..32.
        bl = compute_adaptive_baseline(week_num=35, x_series=x_series, a_series=a_series)

        # Week 30 should be INCLUDED in B_35 (it wasn't confirmed)
        assert 30 in bl["Bt_weeks"], \
            f"Provisional week 30 should be in B_35, but not found in {bl['Bt_weeks']}"
        assert bl["Bt_size"] == 20, \
            f"Expected 20 eligible weeks (13..32), got {bl['Bt_size']}"


class TestConfirmationStateMachine:
    """Verify ConfirmationState tracks state correctly across weeks."""

    def test_state_machine_in_control_to_provisional(self):
        """Transition from IN_CONTROL to PROVISIONAL when one exceeds."""
        from src.detection.confirmation import confirm, ConfirmationState

        state = ConfirmationState()

        # Week 1: IN_CONTROL
        cusum = cusum_compute(week_num=1, x_t=0.06, mu_t=0.05, sigma_t=0.02)
        ewma = ewma_compute(week_num=1, z_t=cusum["z_t"])
        result = confirm(week_num=1, cusum_result=cusum, ewma_result=ewma,
                         state=state)
        assert result["status"] == "IN_CONTROL"
        assert state.last_status == "IN_CONTROL"

        # Week 2: PROVISIONAL (only CUSUM exceeds)
        # prev_S=4.95, z_t=0.6 -> S = max(0, 4.95+0.6-0.5) = 5.05 > 5.0 (CUSUM ALERT)
        # EWMA: Z = 0.2*0.6 = 0.12 < UCL_2 ≈ 0.768 (EWMA NO ALERT)
        cusum2 = cusum_compute(week_num=2, x_t=0.062, mu_t=0.05, sigma_t=0.02, prev_S=4.95)
        ewma2 = ewma_compute(week_num=2, z_t=cusum2["z_t"])
        result2 = confirm(week_num=2, cusum_result=cusum2, ewma_result=ewma2,
                          state=state)
        assert result2["status"] == "PROVISIONAL", \
            f"Expected PROVISIONAL, got {result2['status']}. CUSUM alert={cusum2['alert']}, EWMA alert={ewma2['alert']}"
        assert state.last_status == "PROVISIONAL"
        assert state.provisional_queue_count == 1

    def test_state_machine_provisional_to_confirmed(self):
        """Transition from PROVISIONAL to CONFIRMED when both exceed next week."""
        from src.detection.confirmation import confirm, ConfirmationState

        state = ConfirmationState()

        # Week 1: PROVISIONAL (only CUSUM exceeds)
        cusum1 = cusum_compute(week_num=1, x_t=0.062, mu_t=0.05, sigma_t=0.02, prev_S=4.95)
        ewma1 = ewma_compute(week_num=1, z_t=cusum1["z_t"])
        confirm(week_num=1, cusum_result=cusum1, ewma_result=ewma1, state=state)
        assert state.last_status == "PROVISIONAL"

        # Week 2: CONFIRMED (both exceed)
        cusum2 = cusum_compute(week_num=2, x_t=0.17, mu_t=0.05, sigma_t=0.02)
        ewma2 = ewma_compute(week_num=2, z_t=cusum2["z_t"])
        result2 = confirm(week_num=2, cusum_result=cusum2, ewma_result=ewma2,
                          state=state)
        assert result2["status"] == "CONFIRMED"
        assert state.last_status == "CONFIRMED"
        assert state.confirmed_count == 1
