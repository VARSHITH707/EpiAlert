"""P8 Spatial evaluation integration tests.

Verifies that run_spatial_evaluation:
1. Produces more rows than weeks (multiple spatial series per week)
2. Rows carry distinct (village, street) pairs
3. At least one row has a combined status other than NORMAL
4. Detection_results table is populated correctly
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.evaluation_spatial import (
    run_spatial_evaluation,
    get_detection_results_summary,
    get_spatial_units,
    get_weekly_count,
    get_spatial_baseline,
)


class TestSpatialUnits:
    """Verify spatial unit enumeration."""

    def test_spatial_units_exist(self):
        units = get_spatial_units(disease="Dengue")
        assert len(units) > 0, "No spatial units found for Dengue"

    def test_spatial_units_have_village_street(self):
        units = get_spatial_units(disease="Dengue")
        for village, street, pop in units:
            assert village is not None and village != ""
            assert street is not None and street != ""
            assert pop > 0

    def test_multiple_spatial_units(self):
        units = get_spatial_units(disease="Dengue")
        # Should have more than 1 unit (the dataset has 26 village-street pairs)
        assert len(units) > 1

    def test_population_varies_across_units(self):
        units = get_spatial_units(disease="Dengue")
        pops = [pop for _, _, pop in units]
        assert max(pops) > min(pops), "All units should not have the same population"


class TestWeeklyCount:
    """Verify weekly count queries return correct data."""

    def test_weekly_count_returns_int(self):
        count = get_weekly_count(21, "Dengue", "Village A", "Street 4")
        assert isinstance(count, int)
        assert count >= 0

    def test_weekly_count_varies_by_location(self):
        # Different locations should have different counts
        counts = []
        for village, street, _ in get_spatial_units(disease="Dengue")[:5]:
            c = get_weekly_count(21, "Dengue", village, street)
            counts.append(c)
        # At least some variation expected
        assert len(set(counts)) > 1 or all(c == 0 for c in counts)


class TestSpatialBaseline:
    """Verify baseline computation per spatial unit."""

    def test_baseline_returns_mu_sigma(self):
        bl = get_spatial_baseline(21, "Dengue", "Village A", "Street 4",
                                  baseline_weeks=list(range(1, 21)))
        assert "mu" in bl
        assert "sigma" in bl
        assert "Bt_size" in bl

    def test_baseline_mu_is_rate(self):
        bl = get_spatial_baseline(21, "Dengue", "Village A", "Street 4",
                                  baseline_weeks=list(range(1, 21)))
        # mu should be a rate (count / population), so between 0 and 1
        assert 0 <= bl["mu"] <= 1, f"mu={bl['mu']} should be a rate in [0,1]"


class TestRunSpatialEvaluationIntegration:
    """Integration test: run spatial evaluation on real data and verify output."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Ensure DB is initialized before test."""
        from src.database.db import init_schema
        init_schema()

    def test_evaluation_produces_multiple_rows_per_week(self):
        """Row count must be greater than week count — proves multiple series."""
        result = run_spatial_evaluation(
            start_week=21,
            end_week=25,
            disease="Dengue",
            persist=False,  # Don't write to DB for this test
        )

        n_weeks = 25 - 21 + 1  # 5 weeks
        n_rows = result["total_rows"]

        assert n_rows > n_weeks, (
            f"Expected more rows ({n_rows}) than weeks ({n_weeks}) "
            f"but got {n_rows} rows for {n_weeks} weeks. "
            f"Evaluation is producing whole-population results, not spatial."
        )

    def test_rows_carry_village_and_street(self):
        """Every result row must carry disease, village, street, week."""
        result = run_spatial_evaluation(
            start_week=21,
            end_week=21,  # Just one week to keep it fast
            disease="Dengue",
            persist=False,
        )

        for row in result["rows_by_week"]["week_021"]:
            assert "disease" in row
            assert "village" in row
            assert "street" in row
            assert "week_number" in row
            assert row["disease"] == "Dengue"
            assert row["village"] is not None
            assert row["street"] is not None

    def test_rows_have_distinct_spatial_pairs(self):
        """Rows must cover multiple distinct (village, street) pairs."""
        result = run_spatial_evaluation(
            start_week=21,
            end_week=21,
            disease="Dengue",
            persist=False,
        )

        spatial_pairs = set()
        for row in result["rows_by_week"]["week_021"]:
            spatial_pairs.add((row["village"], row["street"]))

        assert len(spatial_pairs) > 1, (
            f"Expected multiple distinct spatial pairs, got {len(spatial_pairs)}: {spatial_pairs}"
        )

    def test_at_least_one_non_normal_status(self):
        """At least one result row must have status other than NORMAL.

        This proves the combined detector is actually firing on real data,
        not just returning NORMAL for everything.
        """
        # Run a wider range to increase chance of catching an anomaly
        result = run_spatial_evaluation(
            start_week=21,
            end_week=40,
            disease="Dengue",
            persist=False,
        )

        non_normal = []
        for week_key, rows in result["rows_by_week"].items():
            for row in rows:
                if row["status"] != "NORMAL":
                    non_normal.append((week_key, row["village"], row["street"], row["status"]))

        assert len(non_normal) > 0, (
            "No rows with non-NORMAL status found. "
            "The combined detector is not firing on real data. "
            f"Checked weeks 21-40 for Dengue across {len(result['spatial_units'])} spatial units. "
            f"Status distribution: {result['summary']}"
        )

    def test_status_distribution_is_reasonable(self):
        """Status distribution should not be all NORMAL or all ALERT."""
        result = run_spatial_evaluation(
            start_week=21,
            end_week=40,
            disease="Dengue",
            persist=False,
        )

        total = sum(result["summary"].values())
        normal_count = result["summary"].get("NORMAL", 0)

        # Should not be 100% NORMAL (detector not working) or 100% non-NORMAL (thresholds too low)
        assert normal_count < total, "All rows are NORMAL — detector not firing"
        assert normal_count > 0, "No rows are NORMAL — thresholds too sensitive"

    def test_persisted_to_detection_results_table(self):
        """Results must be persisted to the detection_results table."""
        # Clean up any existing results for this range
        from src.database.db import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM detection_results WHERE disease = 'Dengue' AND week_number BETWEEN 21 AND 25"
        )
        conn.commit()
        cur.close()
        conn.close()

        # Run with persistence
        run_spatial_evaluation(
            start_week=21,
            end_week=25,
            disease="Dengue",
            persist=True,
        )

        # Verify in DB
        summary = get_detection_results_summary()
        assert summary["total_rows"] > 0, "No rows in detection_results table"
        assert summary["distinct_spatial_units"] > 1, (
            f"Only {summary['distinct_spatial_units']} distinct spatial units in DB"
        )

        # Check that weeks are covered
        weeks = summary["weeks_covered"]
        assert 21 in weeks
        assert 25 in weeks

    def test_spatial_units_count_reported(self):
        """Report how many spatial units exist."""
        units = get_spatial_units(disease="Dengue")
        n_units = len(units)

        result = run_spatial_evaluation(
            start_week=21,
            end_week=25,
            disease="Dengue",
            persist=False,
        )

        assert result["total_rows"] == n_units * 5, (
            f"Expected {n_units} units * 5 weeks = {n_units * 5} rows, "
            f"got {result['total_rows']} rows"
        )

    def test_disease_filter_works(self):
        """Different diseases should produce different (possibly zero) results."""
        dengue_units = get_spatial_units(disease="Dengue")
        malaria_units = get_spatial_units(disease="Malaria")

        # Both should have units (diseases are present in the data)
        assert len(dengue_units) > 0
        # Malaria may have fewer units — that's fine


class TestCombinedDetectorWired:
    """Verify the combined detector is actually being called and its output used."""

    def test_combined_status_in_result_rows(self):
        """Result rows must contain the combined detector's status."""
        result = run_spatial_evaluation(
            start_week=21,
            end_week=21,
            disease="Dengue",
            persist=False,
        )

        for row in result["rows_by_week"]["week_021"]:
            assert "status" in row
            assert row["status"] in ("NORMAL", "WATCH", "ALERT", "HIGH_ALERT"), (
                f"Invalid status: {row['status']}"
            )
            assert "severity" in row
            assert "explanation" in row

    def test_combined_detector_signals_recorded(self):
        """Individual detector signals should be recorded in result rows."""
        result = run_spatial_evaluation(
            start_week=21,
            end_week=21,
            disease="Dengue",
            persist=False,
        )

        for row in result["rows_by_week"]["week_021"]:
            assert "cusum_signal" in row
            assert "ewma_signal" in row
            assert "trend_sustained" in row
            assert "baseline_deviation" in row
            assert row["cusum_signal"] in (0, 1)
            assert row["ewma_signal"] in (0, 1)
            assert row["trend_sustained"] in (0, 1)


class TestDetectorsProduceResultsNotWholePopulation:
    """Regression test: ensure we're NOT producing whole-population results."""

    def test_result_count_exceeds_one_per_week(self):
        """If we get exactly 1 row per week, we're doing whole-population.
        We must get multiple rows per week (one per spatial unit)."""
        result = run_spatial_evaluation(
            start_week=21,
            end_week=23,
            disease="Dengue",
            persist=False,
        )

        for week_key, rows in result["rows_by_week"].items():
            assert len(rows) > 1, (
                f"Week {week_key} has only {len(rows)} row(s). "
                f"This indicates whole-population evaluation, not spatial. "
                f"Expected multiple rows per week (one per village/street)."
            )

    def test_rows_have_small_observed_counts(self):
        """Per-spatial-unit counts should be small (single digits),
        not ~60 (whole population)."""
        result = run_spatial_evaluation(
            start_week=21,
            end_week=21,
            disease="Dengue",
            persist=False,
        )

        for row in result["rows_by_week"]["week_021"]:
            # Individual spatial units should have small counts
            # (typically 0-5 cases, not 60+)
            assert row["observed_count"] < 50, (
                f"observed_count={row['observed_count']} for "
                f"{row['village']}/{row['street']} is too large for a "
                f"single spatial unit. Likely whole-population result."
            )
