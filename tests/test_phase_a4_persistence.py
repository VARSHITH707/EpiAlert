"""Tests for Phase A4 — detection-state persistence.

The detection_state table persists the full detection state for every
(disease, spatial_unit, week) tuple:
  - cusum_S: the CUSUM statistic S_t
  - ewma_Z: the EWMA statistic Z_t
  - baseline_mu: baseline mean mu_t
  - baseline_sigma: baseline std sigma_t
  - baseline_Bt_size: |B_t|
  - status: IN_CONTROL / PROVISIONAL / CONFIRMED / INSUFFICIENT_BASELINE
  - a_t: alert flag (0 or 1)

Persisting a_t rather than deriving it on demand is what makes the adaptive
baseline reproducible, auditable, and replayable for shadow testing.

RULE: every number below is hand-computed or comes from running the code
on the real dataset. No fabricated metrics.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.database.db import get_connection, init_schema

# Import the new A4 functions after db.py is updated
try:
    from src.database.db import upsert_detection_state, get_detection_state
except ImportError:
    upsert_detection_state = None
    get_detection_state = None


class TestDetectionStateSchema:
    """Verify the detection_state table exists with correct columns."""

    def setup_method(self):
        """Ensure schema is initialised before each test."""
        init_schema()

    def test_detection_state_table_exists(self):
        """detection_state table must exist after init_schema."""
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='detection_state'"
        )
        row = cur.fetchone()
        assert row is not None, "detection_state table must exist"
        assert row[0] == "detection_state"
        cur.close()
        conn.close()

    def test_detection_state_columns(self):
        """detection_state must have all required columns with correct types."""
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(detection_state)")
        columns = {row[1]: row for row in cur.fetchall()}
        cur.close()
        conn.close()

        required = [
            "disease", "spatial_unit", "week_number",
            "cusum_S", "ewma_Z",
            "baseline_mu", "baseline_sigma", "baseline_Bt_size",
            "status", "a_t",
        ]
        for col in required:
            assert col in columns, f"detection_state missing column: {col}"


class TestDetectionStateCRUD:
    """Verify write/read/delete of detection state records."""

    def setup_method(self):
        """Reset detection_state before each test."""
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM detection_state")
        conn.commit()
        cur.close()
        conn.close()

    def test_write_and_read_detection_state(self):
        """Write a detection state record and read it back."""
        from src.database.db import upsert_detection_state, get_detection_state

        conn = get_connection()
        upsert_detection_state(
            conn=conn,
            disease="Dengue",
            spatial_unit="Village_A",
            week_number=25,
            cusum_S=4.5,
            ewma_Z=0.85,
            baseline_mu=0.05,
            baseline_sigma=0.02,
            baseline_Bt_size=15,
            status="PROVISIONAL",
            a_t=0,
        )
        conn.close()

        result = get_detection_state(
            disease="Dengue",
            spatial_unit="Village_A",
            week_number=25,
        )
        assert result is not None
        assert result["disease"] == "Dengue"
        assert result["spatial_unit"] == "Village_A"
        assert result["week_number"] == 25
        assert result["cusum_S"] == pytest.approx(4.5)
        assert result["ewma_Z"] == pytest.approx(0.85)
        assert result["baseline_mu"] == pytest.approx(0.05)
        assert result["baseline_sigma"] == pytest.approx(0.02)
        assert result["baseline_Bt_size"] == 15
        assert result["status"] == "PROVISIONAL"
        assert result["a_t"] == 0

    def test_a_t_persisted_and_retrievable(self):
        """a_t=1 must be persisted and retrievable — the key reproducibility property."""
        from src.database.db import upsert_detection_state, get_detection_state

        conn = get_connection()
        upsert_detection_state(
            conn=conn,
            disease="Malaria",
            spatial_unit="Street_7",
            week_number=30,
            cusum_S=7.0,
            ewma_Z=1.2,
            baseline_mu=0.05,
            baseline_sigma=0.02,
            baseline_Bt_size=18,
            status="CONFIRMED",
            a_t=1,
        )
        conn.close()

        result = get_detection_state(
            disease="Malaria",
            spatial_unit="Street_7",
            week_number=30,
        )
        assert result is not None
        assert result["a_t"] == 1, "a_t=1 must be persisted for confirmed week"
        assert result["status"] == "CONFIRMED"

    def test_multiple_weeks_distinct_records(self):
        """Each (disease, spatial_unit, week) is a distinct record."""
        from src.database.db import upsert_detection_state, get_detection_state

        conn = get_connection()
        for week, status, a_t in [(25, "IN_CONTROL", 0),
                                    (26, "PROVISIONAL", 0),
                                    (27, "CONFIRMED", 1)]:
            upsert_detection_state(
                conn=conn,
                disease="Dengue",
                spatial_unit="Street_3",
                week_number=week,
                cusum_S=1.0 * week,
                ewma_Z=0.3 * week,
                baseline_mu=0.05,
                baseline_sigma=0.02,
                baseline_Bt_size=15,
                status=status,
                a_t=a_t,
            )
        conn.close()

        for week, expected_status, expected_a_t in [(25, "IN_CONTROL", 0),
                                                      (26, "PROVISIONAL", 0),
                                                      (27, "CONFIRMED", 1)]:
            result = get_detection_state("Dengue", "Street_3", week)
            assert result is not None
            assert result["status"] == expected_status
            assert result["a_t"] == expected_a_t

    def test_duplicate_upsert_updates_existing(self):
        """Upserting the same key updates the existing record."""
        from src.database.db import upsert_detection_state, get_detection_state

        conn = get_connection()
        upsert_detection_state(
            conn=conn,
            disease="Dengue",
            spatial_unit="Village_B",
            week_number=40,
            cusum_S=1.0,
            ewma_Z=0.5,
            baseline_mu=0.05,
            baseline_sigma=0.02,
            baseline_Bt_size=12,
            status="IN_CONTROL",
            a_t=0,
        )
        # Update with CONFIRMED status
        upsert_detection_state(
            conn=conn,
            disease="Dengue",
            spatial_unit="Village_B",
            week_number=40,
            cusum_S=7.0,
            ewma_Z=1.2,
            baseline_mu=0.05,
            baseline_sigma=0.02,
            baseline_Bt_size=12,
            status="CONFIRMED",
            a_t=1,
        )
        conn.close()

        result = get_detection_state("Dengue", "Village_B", 40)
        assert result is not None
        assert result["status"] == "CONFIRMED"
        assert result["a_t"] == 1
        assert result["cusum_S"] == pytest.approx(7.0)


class TestDetectionStateParameterisedSQL:
    """Verify only parameterised SQL is used (no f-string queries)."""

    def test_no_sql_injection_via_disease(self):
        """Disease name with SQL metacharacters must not break or inject."""
        from src.database.db import upsert_detection_state, get_detection_state

        conn = get_connection()
        # Use a disease name that looks like SQL — must be safely parameterised
        upsert_detection_state(
            conn=conn,
            disease="Dengue'; DROP TABLE detection_state; --",
            spatial_unit="TestUnit",
            week_number=1,
            cusum_S=0.0,
            ewma_Z=0.0,
            baseline_mu=0.0,
            baseline_sigma=0.0,
            baseline_Bt_size=0,
            status="IN_CONTROL",
            a_t=0,
        )
        conn.close()

        # Table must still exist (no injection)
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='detection_state'")
        assert cur.fetchone() is not None
        cur.close()
        conn.close()

        # Record must be retrievable with the exact disease name
        result = get_detection_state(
            disease="Dengue'; DROP TABLE detection_state; --",
            spatial_unit="TestUnit",
            week_number=1,
        )
        assert result is not None
        assert result["disease"] == "Dengue'; DROP TABLE detection_state; --"
