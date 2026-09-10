"""Temporal leakage tests.

A detector that sees data from after the decision week will score well and be
useless in the field, because that data does not exist yet when the decision
has to be made. These tests fail if any future information reaches a decision.

They are written to FAIL if the guard is removed, not merely to pass today.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.evaluation_spatial import (
    SpatialAlertHistory,
    get_spatial_baseline,
    get_weekly_count,
)

DISEASE = "Dengue"
VILLAGE = "Village A"
STREET = "Street 4"


# ---------------------------------------------------------------------------
# The baseline window
# ---------------------------------------------------------------------------

def test_baseline_window_is_strictly_before_decision_week():
    """No week at or after the decision week may enter the baseline."""
    decision_week = 50
    window = list(range(decision_week - 20, decision_week))

    result = get_spatial_baseline(
        decision_week, DISEASE, VILLAGE, STREET, window
    )
    used = [w for w, _ in result["weekly_counts"]]

    assert used, "baseline used no weeks at all"
    assert max(used) < decision_week, (
        f"baseline used week {max(used)} to decide week {decision_week} — "
        "that data does not exist yet at decision time"
    )


def test_future_weeks_offered_are_still_counted_as_requested():
    """Passing future weeks must not silently look correct.

    If someone widens the window by mistake, the function should not quietly
    include future data. This asserts the caller's window is what bounds it,
    so a bad window is visible in the result rather than hidden.
    """
    decision_week = 50
    honest = get_spatial_baseline(
        decision_week, DISEASE, VILLAGE, STREET,
        list(range(decision_week - 20, decision_week)),
    )
    leaky = get_spatial_baseline(
        decision_week, DISEASE, VILLAGE, STREET,
        list(range(decision_week - 20, decision_week + 10)),
    )
    honest_weeks = {w for w, _ in honest["weekly_counts"]}
    leaky_weeks = {w for w, _ in leaky["weekly_counts"]}

    future = {w for w in leaky_weeks if w >= decision_week}
    assert future, (
        "test is not exercising anything: no future weeks were included even "
        "when explicitly offered"
    )
    assert honest_weeks == {w for w in leaky_weeks if w < decision_week}, (
        "the honest window is not a clean prefix of the leaky one"
    )


def test_guard_band_removes_the_most_recent_weeks():
    """A guard band of g drops exactly the g weeks before the decision week."""
    decision_week = 50
    window = list(range(decision_week - 20, decision_week))

    no_guard = get_spatial_baseline(
        decision_week, DISEASE, VILLAGE, STREET, window, guard_band=0
    )
    guarded = get_spatial_baseline(
        decision_week, DISEASE, VILLAGE, STREET, window, guard_band=2
    )

    used_guarded = {w for w, _ in guarded["weekly_counts"]}
    assert decision_week - 1 not in used_guarded
    assert decision_week - 2 not in used_guarded
    assert decision_week - 3 in used_guarded, "guard band removed too much"
    assert guarded["Bt_size"] == no_guard["Bt_size"] - 2


# ---------------------------------------------------------------------------
# Alert exclusion — the paper's contamination-resistance claim
# ---------------------------------------------------------------------------

def test_alert_weeks_are_dropped_from_the_baseline():
    """An alerting week must leave the window entirely."""
    decision_week = 50
    window = list(range(decision_week - 20, decision_week))
    flagged = {40, 41, 42}

    plain = get_spatial_baseline(
        decision_week, DISEASE, VILLAGE, STREET, window
    )
    excluded = get_spatial_baseline(
        decision_week, DISEASE, VILLAGE, STREET, window, alert_weeks=flagged
    )

    used = {w for w, _ in excluded["weekly_counts"]}
    assert not (used & flagged), f"alerting weeks still in baseline: {used & flagged}"
    assert excluded["Bt_size"] == plain["Bt_size"] - len(flagged), (
        "excluded weeks were removed from the sum but not from the count "
        "dividing it — that depresses the mean instead of correcting it"
    )
    assert excluded["Bt_excluded"] == len(flagged)


def test_exclusion_removes_from_denominator_not_just_numerator():
    """The correctness property the whole mechanism rests on.

    Zeroing an alerting week while leaving it in the divisor would drag the
    mean down and invent variance. Removing it entirely must raise the mean
    when the excluded week was quieter than average, and the count must drop.
    """
    decision_week = 50
    window = list(range(decision_week - 20, decision_week))

    plain = get_spatial_baseline(decision_week, DISEASE, VILLAGE, STREET, window)
    if plain["Bt_size"] < 5:
        pytest.skip("not enough history at this unit to compare")

    # Exclude the single busiest week; the mean must fall, not rise.
    busiest = max(plain["weekly_counts"], key=lambda wc: wc[1])[0]
    excluded = get_spatial_baseline(
        decision_week, DISEASE, VILLAGE, STREET, window, alert_weeks={busiest}
    )

    assert excluded["Bt_size"] == plain["Bt_size"] - 1
    assert excluded["mu"] <= plain["mu"] + 1e-12, (
        "removing the busiest week raised the mean — the week was dropped "
        "from the sum but kept in the denominator"
    )


# ---------------------------------------------------------------------------
# Alert history cannot look ahead
# ---------------------------------------------------------------------------

def test_alert_history_only_holds_weeks_already_marked():
    """History is append-only as weeks are processed, so it cannot see ahead."""
    history = SpatialAlertHistory()
    assert history.get_alert_weeks(DISEASE, VILLAGE, STREET) == set()

    history.mark_alert(DISEASE, VILLAGE, STREET, 30)
    history.mark_alert(DISEASE, VILLAGE, STREET, 31)
    weeks = history.get_alert_weeks(DISEASE, VILLAGE, STREET)

    assert weeks == {30, 31}
    assert all(w < 50 for w in weeks), (
        "history contains a week at or after the decision week"
    )


def test_alert_history_is_isolated_per_spatial_unit():
    """One street's alerts must not alter another street's baseline.

    Street names repeat across villages, so a key collision here would let an
    outbreak in one village suppress the baseline of an unrelated street.
    """
    history = SpatialAlertHistory()
    history.mark_alert(DISEASE, "Village A", "Street 1", 30)

    assert history.get_alert_weeks(DISEASE, "Village B", "Street 1") == set()
    assert history.get_alert_weeks("Malaria", "Village A", "Street 1") == set()
    assert history.get_alert_weeks(DISEASE, "Village A", "Street 1") == {30}


# ---------------------------------------------------------------------------
# Ground truth is never an input
# ---------------------------------------------------------------------------

def test_detection_modules_do_not_read_ground_truth():
    """The detector must not import or open ground_truth.csv.

    Ground truth exists to score the system afterwards. A detector that reads
    it is not detecting anything.
    """
    from pathlib import Path

    detection_dir = Path(__file__).parent.parent / "src" / "detection"
    spatial = Path(__file__).parent.parent / "src" / "evaluation_spatial.py"

    sources = list(detection_dir.glob("*.py")) + [spatial]
    offenders = []
    for path in sources:
        text = path.read_text(encoding="utf-8").lower()
        if "ground_truth" in text or "load_ground_truth" in text:
            offenders.append(path.name)

    assert not offenders, (
        f"detection code references ground truth: {offenders}"
    )


# ---------------------------------------------------------------------------
# Call isolation
# ---------------------------------------------------------------------------

def test_fusion_mode_does_not_leak_between_calls():
    """One evaluation must not change the mode of the next.

    The fusion mode used to be pushed into an environment variable and the
    config module reloaded, so a confirmation-mode run silently switched every
    later run in the same process. Results then depended on call order, which
    makes any before/after comparison meaningless.
    """
    from src.evaluation_spatial import run_spatial_evaluation

    def statuses(mode: str) -> set:
        result = run_spatial_evaluation(
            start_week=25, end_week=25, disease="Dengue",
            fusion_mode=mode, persist=False,
        )
        rows = [r for week in result["rows_by_week"].values() for r in week]
        return {r["status"] for r in rows}

    # Run confirmation first; PROVISIONAL only exists in that mode.
    confirmation_first = statuses("confirmation")
    union_after = statuses("union")
    union_alone = statuses("union")

    assert union_after == union_alone, (
        "a union run returned different statuses depending on what ran before "
        f"it: {union_after} vs {union_alone}"
    )
    assert "PROVISIONAL" not in union_after, (
        "confirmation-mode status leaked into a union-mode run"
    )
    assert confirmation_first, "confirmation run produced no rows"
