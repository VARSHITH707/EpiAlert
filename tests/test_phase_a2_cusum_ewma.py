"""Tests for Phase A2 — audit CUSUM and EWMA against equations (4),(5),(6).

RULE: every number below is hand-computed. No fabricated metrics.

Hand-computed reference cases:

CUSUM (eq 4): S_t = max(0, S_{t-1} + z_t - k), S_0 = 0, z_t = (x_t - mu_t)/sigma_t
  k = 0.5, h = 5.0
  Week 1: x=0.08, mu=0.05, sigma=0.02 -> z_1 = (0.08-0.05)/0.02 = 1.5
           S_1 = max(0, 0 + 1.5 - 0.5) = 1.0
  Week 2: x=0.09, mu=0.05, sigma=0.02 -> z_2 = (0.09-0.05)/0.02 = 2.0
           S_2 = max(0, 1.0 + 2.0 - 0.5) = 2.5
  Week 3: x=0.10, mu=0.05, sigma=0.02 -> z_3 = (0.10-0.05)/0.02 = 2.5
           S_3 = max(0, 2.5 + 2.5 - 0.5) = 4.5
  Week 4: x=0.11, mu=0.05, sigma=0.02 -> z_4 = (0.11-0.05)/0.02 = 3.0
           S_4 = max(0, 4.5 + 3.0 - 0.5) = 7.0  -> ALERT (S_4 > h=5.0)

EWMA (eq 5): Z_t = lambda*z_t + (1-lambda)*Z_{t-1}, Z_0 = 0
  lambda = 0.2
  Week 1: z_1 = 1.5 -> Z_1 = 0.2*1.5 + 0.8*0 = 0.3
  Week 2: z_2 = 2.0 -> Z_2 = 0.2*2.0 + 0.8*0.3 = 0.4 + 0.24 = 0.64
  Week 3: z_3 = 2.5 -> Z_3 = 0.2*2.5 + 0.8*0.64 = 0.5 + 0.512 = 1.012

EWMA UCL (eq 6): UCL_t = L * sqrt( (lambda/(2-lambda)) * (1 - (1-lambda)^(2t)) )
  L = 3.0, lambda = 0.2
  factor = lambda/(2-lambda) = 0.2/1.8 = 0.111111...
  t=1: (1-0.8^2) = 1-0.64 = 0.36, UCL_1 = 3*sqrt(0.111111*0.36) = 3*sqrt(0.04) = 3*0.2 = 0.6
  t=2: (1-0.8^4) = 1-0.4096 = 0.5904, UCL_2 = 3*sqrt(0.111111*0.5904) = 3*sqrt(0.0656) = 3*0.2561 = 0.7683
  t=50: (1-0.8^100) ≈ 1-0.0000002 ≈ 1.0, UCL_50 ≈ 3*sqrt(0.111111*1.0) = 3*sqrt(0.111111) = 3*0.333333 = 1.0
  t=100: UCL ≈ 1.0 (asymptotic)

  UCL MUST be wider at t=50 than at t=1: 1.0 > 0.6 ✓
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import math

# Locked parameter values from BUILD directive section 4
H_CUSUM_EXPECTED = 5.0
K_CUSUM_EXPECTED = 0.5
L_EWMA_EXPECTED = 3.0
LAMBDA_EWMA_EXPECTED = 0.2


def _hand_cusum():
    """Hand-computed CUSUM accumulation.
    Returns list of (week, x, mu, sigma, z, S) tuples."""
    k = K_CUSUM_EXPECTED
    h = H_CUSUM_EXPECTED
    rows = [
        (1, 0.08, 0.05, 0.02),
        (2, 0.09, 0.05, 0.02),
        (3, 0.10, 0.05, 0.02),
        (4, 0.11, 0.05, 0.02),
    ]
    S = 0.0
    result = []
    for week, x, mu, sigma in rows:
        z = (x - mu) / sigma
        S = max(0.0, S + z - k)
        result.append((week, x, mu, sigma, z, S))
    return result


def _hand_ewma():
    """Hand-computed EWMA.
    Returns list of (week, z_t, Z_t) tuples."""
    lam = LAMBDA_EWMA_EXPECTED
    z_values = [1.5, 2.0, 2.5]  # same z_t as CUSUM weeks 1-3
    Z = 0.0
    result = []
    for week, z_t in enumerate(z_values, start=1):
        Z = lam * z_t + (1 - lam) * Z
        result.append((week, z_t, Z))
    return result


def _hand_ewma_ucl():
    """Hand-computed EWMA UCL per eq (6).
    Returns list of (t, UCL_t) tuples."""
    L = L_EWMA_EXPECTED
    lam = LAMBDA_EWMA_EXPECTED
    factor = lam / (2 - lam)  # 0.2/1.8 = 0.111111...
    result = []
    for t in [1, 2, 50, 100]:
        time_factor = 1 - (1 - lam) ** (2 * t)
        ucl = L * math.sqrt(factor * time_factor)
        result.append((t, ucl))
    return result


class TestCUSUMConstants:
    """Verify module-level locked constants exist in cusum.py."""

    def test_h_cusum_constant_exists(self):
        from src.detection.cusum import H_CUSUM
        assert H_CUSUM == H_CUSUM_EXPECTED, f"H_CUSUM should be {H_CUSUM_EXPECTED}, got {H_CUSUM}"

    def test_k_cusum_constant_exists(self):
        from src.detection.cusum import K_CUSUM
        assert K_CUSUM == K_CUSUM_EXPECTED, f"K_CUSUM should be {K_CUSUM_EXPECTED}, got {K_CUSUM}"


class TestEWMAConstants:
    """Verify module-level locked constants exist in ewma.py."""

    def test_l_ewma_constant_exists(self):
        from src.detection.ewma import L_EWMA
        assert L_EWMA == L_EWMA_EXPECTED, f"L_EWMA should be {L_EWMA_EXPECTED}, got {L_EWMA}"

    def test_lambda_ewma_constant_exists(self):
        from src.detection.ewma import LAMBDA_EWMA
        assert LAMBDA_EWMA == LAMBDA_EWMA_EXPECTED, f"LAMBDA_EWMA should be {LAMBDA_EWMA_EXPECTED}, got {LAMBDA_EWMA}"


class TestCUSUMEquation:
    """Verify CUSUM implements eq (4) exactly."""

    def test_cusum_accumulation_hand_computed(self):
        """CUSUM must produce hand-computed S_t values.
        
        S_0 = 0
        S_1 = max(0, 0 + 1.5 - 0.5) = 1.0
        S_2 = max(0, 1.0 + 2.0 - 0.5) = 2.5
        S_3 = max(0, 2.5 + 2.5 - 0.5) = 4.5
        S_4 = max(0, 4.5 + 3.0 - 0.5) = 7.0  -> ALERT
        """
        from src.detection.cusum import cusum_compute
        
        hand = _hand_cusum()
        prev_S = 0.0
        for week, x, mu, sigma, expected_z, expected_S in hand:
            result = cusum_compute(week_num=week, x_t=x, mu_t=mu, sigma_t=sigma,
                                   prev_S=prev_S)
            assert result["z_t"] == pytest.approx(expected_z), \
                f"Week {week}: z_t should be {expected_z}, got {result['z_t']}"
            assert result["S_t"] == pytest.approx(expected_S), \
                f"Week {week}: S_t should be {expected_S}, got {result['S_t']}"
            prev_S = result["S_t"]

    def test_cusum_signals_when_exceeds_h(self):
        """S_t > H_CUSUM must return alert=True."""
        from src.detection.cusum import cusum_compute, H_CUSUM
        
        # Week 4: S_4 = 7.0 > H_CUSUM = 5.0 -> alert
        result = cusum_compute(week_num=4, x_t=0.11, mu_t=0.05, sigma_t=0.02,
                               prev_S=4.5)
        assert result["S_t"] == pytest.approx(7.0)
        assert result["alert"] is True
        assert result["S_t"] > H_CUSUM

    def test_cusum_no_signal_below_h(self):
        """S_t <= H_CUSUM must return alert=False."""
        from src.detection.cusum import cusum_compute
        
        # Week 3: S_3 = 4.5 < H_CUSUM = 5.0 -> no alert
        result = cusum_compute(week_num=3, x_t=0.10, mu_t=0.05, sigma_t=0.02,
                               prev_S=2.5)
        assert result["S_t"] == pytest.approx(4.5)
        assert result["alert"] is False


class TestEWMAEquation:
    """Verify EWMA implements eqs (5) and (6) exactly."""

    def test_ewma_recursion_hand_computed(self):
        """EWMA must produce hand-computed Z_t values.
        
        Z_0 = 0
        Z_1 = 0.2*1.5 + 0.8*0 = 0.3
        Z_2 = 0.2*2.0 + 0.8*0.3 = 0.64
        Z_3 = 0.2*2.5 + 0.8*0.64 = 1.012
        """
        from src.detection.ewma import ewma_compute
        
        hand = _hand_ewma()
        prev_Z = 0.0
        for week, z_t, expected_Z in hand:
            result = ewma_compute(week_num=week, z_t=z_t, prev_Z=prev_Z)
            assert result["Z_t"] == pytest.approx(expected_Z, abs=1e-6), \
                f"Week {week}: Z_t should be {expected_Z}, got {result['Z_t']}"
            prev_Z = result["Z_t"]

    def test_ewma_initializes_Z0_to_zero(self):
        """Z_0 must be 0, NOT baseline_expected.
        
        The existing code initializes z_0 = baseline_expected which is wrong.
        The paper specifies Z_0 = 0.
        """
        from src.detection.ewma import ewma_compute
        
        # Week 1 with Z_0 = 0
        result = ewma_compute(week_num=1, z_t=1.5, prev_Z=0.0)
        expected_Z1 = 0.2 * 1.5 + 0.8 * 0.0  # = 0.3
        assert result["Z_t"] == pytest.approx(expected_Z1), \
            f"Z_1 should be {expected_Z1} (from Z_0=0), got {result['Z_t']}"

    def test_ewma_ucl_time_dependent_wider_at_early_t(self):
        """UCL_t must use the time-dependent factor (1-(1-lambda)^(2t)).
        
        UCL MUST be wider (larger) at t=50 than at t=1.
        UCL_1 = 0.6, UCL_50 ≈ 1.0.  If the time factor is missing,
        UCL is constant ≈ 1.0 at all t, failing this test.
        """
        from src.detection.ewma import ewma_ucl
        
        ucl_1 = ewma_ucl(t=1)
        ucl_50 = ewma_ucl(t=50)
        ucl_100 = ewma_ucl(t=100)
        
        # UCL must grow with t
        assert ucl_50 > ucl_1, \
            f"UCL_50 ({ucl_50:.4f}) must exceed UCL_1 ({ucl_1:.4f}). " \
            "Time-dependent factor may be missing."
        
        # UCL must converge to asymptotic value
        assert ucl_100 >= ucl_50, \
            f"UCL_100 ({ucl_100:.4f}) should be >= UCL_50 ({ucl_50:.4f})"
        
        # Hand-computed reference
        hand = _hand_ewma_ucl()
        hand_ucl_1 = hand[0][1]   # 0.6
        hand_ucl_50 = hand[2][1]  # ≈ 1.0
        
        assert ucl_1 == pytest.approx(hand_ucl_1, abs=1e-4), \
            f"UCL_1 should be {hand_ucl_1:.4f}, got {ucl_1:.4f}"
        assert ucl_50 == pytest.approx(hand_ucl_50, abs=1e-4), \
            f"UCL_50 should be {hand_ucl_50:.4f}, got {ucl_50:.4f}"

    def test_ewma_ucl_values_hand_computed(self):
        """Every UCL_t must match hand-computed values."""
        from src.detection.ewma import ewma_ucl
        
        hand = _hand_ewma_ucl()
        for t, expected_ucl in hand:
            actual = ewma_ucl(t=t)
            assert actual == pytest.approx(expected_ucl, abs=1e-4), \
                f"UCL_{t} should be {expected_ucl:.6f}, got {actual:.6f}"


class TestEWMAAlertLogic:
    """Verify EWMA signals correctly against time-dependent UCL."""

    def test_ewma_signals_when_exceeds_ucl(self):
        """Z_t > UCL_t must return alert=True."""
        from src.detection.ewma import ewma_compute, ewma_ucl
        
        # Week 3: Z_3 = 1.012, UCL_3 = ?
        # UCL_3 = 3 * sqrt(0.111111 * (1 - 0.8^6))
        # 0.8^6 = 0.262144, 1-0.262144 = 0.737856
        # UCL_3 = 3 * sqrt(0.111111 * 0.737856) = 3 * sqrt(0.081984) = 3 * 0.286332 = 0.858996
        ucl_3 = ewma_ucl(t=3)
        result = ewma_compute(week_num=3, z_t=2.5, prev_Z=0.64)
        # Z_3 = 0.2*2.5 + 0.8*0.64 = 0.5 + 0.512 = 1.012
        assert result["Z_t"] == pytest.approx(1.012, abs=1e-6)
        assert result["UCL_t"] == pytest.approx(ucl_3)
        # Z_3 = 1.012 > UCL_3 ≈ 0.859 -> ALERT
        assert result["alert"] is True, \
            f"Z_3={result['Z_t']:.4f} > UCL_3={ucl_3:.4f} should alert"

    def test_ewma_no_signal_when_below_ucl(self):
        """Z_t < UCL_t must return alert=False."""
        from src.detection.ewma import ewma_compute, ewma_ucl
        
        # Week 1: Z_1 = 0.3, UCL_1 = 0.6 -> no alert
        ucl_1 = ewma_ucl(t=1)
        result = ewma_compute(week_num=1, z_t=1.5, prev_Z=0.0)
        assert result["Z_t"] == pytest.approx(0.3, abs=1e-6)
        assert result["UCL_t"] == pytest.approx(ucl_1)
        assert result["alert"] is False, \
            f"Z_1={result['Z_t']:.4f} < UCL_1={ucl_1:.4f} should NOT alert"
