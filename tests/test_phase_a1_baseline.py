"""Tests for Phase A1 — adaptive baseline audit and correction.

RULE: every number below is hand-computed or comes from running the code
on the real dataset. No fabricated metrics.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import numpy as np
from src.detection.baseline import (
    compute_adaptive_baseline,
    W,
    g,
    B_MIN,
)


# ---------------------------------------------------------------------------
# Hand-computed reference case
# ---------------------------------------------------------------------------

def test_denominator_exclusion_property():
    """Prove that excluding a_t=1 from BOTH numerator AND denominator differs
    from zeroing it in the numerator only (the naive bug).

    Weeks are 1-based and the guard band g=2 shifts the window back, so the
    demonstration data must sit fully inside B_t. Evaluating at week 16 with
    W=20, g=2 gives start=max(1, 16-2-20)=1 and end=16-2-1=13, i.e. weeks 1..13.

    Data: weeks 1..13 all at rate 0.01, except week 7 at 0.05 flagged a=1.

    Correct (exclude from numerator AND denominator):
        Bt = 1..13 minus {7} -> 12 weeks, all 0.01
        mu = 0.01,  sigma = 0
        12 >= B_MIN(10) so status is IN_CONTROL

    Naive (zero week 7 but keep it in the denominator):
        13 weeks, rates [0.01 x12, 0.0]
        mu = 0.12/13 = 0.009230769...
        sigma > 0, entirely an artefact of the retained zero

    The naive version depresses the mean AND invents a spurious variance.
    """
    x = {i: 0.01 for i in range(1, 14)}
    x[7] = 0.05
    a = {7: 1}

    result = compute_adaptive_baseline(week_num=16, x_series=x, a_series=a)

    expected_weeks = [i for i in range(1, 14) if i != 7]
    assert result["Bt_weeks"] == expected_weeks
    assert result["Bt_size"] == 12
    assert result["mu"] == pytest.approx(0.01)
    assert result["sigma"] == pytest.approx(0.0)
    assert result["status"] == "IN_CONTROL"

    # Naive comparison, computed here rather than hardcoded
    naive_rates = [0.0 if i == 7 else 0.01 for i in range(1, 14)]
    naive_mu = sum(naive_rates) / len(naive_rates)
    naive_var = sum((r - naive_mu) ** 2 for r in naive_rates) / (len(naive_rates) - 1)
    naive_sigma = naive_var ** 0.5

    assert naive_mu < result["mu"]          # naive depresses the mean
    assert naive_sigma > result["sigma"]    # naive invents variance


def test_guard_band_excludes_recent_weeks():
    """g=2 means weeks t-1 and t-2 are always excluded from B_t."""
    x = {i: 0.01 for i in range(1, 22)}
    a = {}

    # Week 10: B_t should be weeks 1..7
    result = compute_adaptive_baseline(week_num=10, x_series=x, a_series=a)
    assert result["Bt_weeks"] == list(range(1, 8))
    assert result["Bt_size"] == 7


def test_b_min_triggers_insufficient_baseline():
    """When |B_t| < B_MIN (10), status must be INSUFFICIENT_BASELINE."""
    x = {i: 0.01 for i in range(1, 50)}
    a = {}

    # Week 10: window 1..7, only 7 eligible < 10 -> INSUFFICIENT_BASELINE
    result = compute_adaptive_baseline(week_num=10, x_series=x, a_series=a)
    assert result["status"] == "INSUFFICIENT_BASELINE"
    assert result["mu"] is None
    assert result["sigma"] is None
    assert result["Bt_size"] == 7

    # Week 20: window 1..17, 17 eligible >= 10 -> IN_CONTROL
    result = compute_adaptive_baseline(week_num=20, x_series=x, a_series=a)
    assert result["status"] == "IN_CONTROL"
    assert result["Bt_size"] == 17
    assert result["mu"] is not None


def test_early_weeks_no_baseline():
    """Weeks before enough history returns INSUFFICIENT_BASELINE."""
    x = {i: 0.01 for i in range(1, 50)}
    a = {}
    result = compute_adaptive_baseline(week_num=3, x_series=x, a_series=a)
    assert result["status"] == "INSUFFICIENT_BASELINE"
    assert result["Bt_size"] == 0


def test_locked_parameter_values():
    """Verify the locked constants match the BUILD directive."""
    assert W == 20
    assert g == 2
    assert B_MIN == 10
