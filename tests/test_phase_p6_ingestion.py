"""Tests for P6 — Ingestion pipeline.

Verifies: week reading, extraction integration, validation integration,
idempotency, processing run tracking. LLM sampling validation tested
separately (requires Ollama, tested in integration).
"""

import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.extraction.ingestion import (
    IngestionPipeline,
    ProcessingRun,
    get_raw_reports_for_week,
    compute_cache_key,
    run_extraction_validation,
)
from src.extraction.rule_based import RuleBasedExtractor
from src.extraction.schema import ExtractedRecord


class TestProcessingRun:
    """ProcessingRun tracks a single ingestion run."""

    def test_initial_state(self):
        run = ProcessingRun(5)
        assert run.week_number == 5
        assert run.run_type == "extraction"
        assert run.status == "pending"
        assert run.records_processed == 0
        assert run.records_quarantined == 0
        assert run.started_at is None
        assert run.completed_at is None

    def test_start_and_complete(self):
        run = ProcessingRun(5)
        run.start()
        assert run.status == "running"
        assert run.started_at is not None
        run.complete()
        assert run.status == "completed"
        assert run.completed_at is not None

    def test_to_dict(self):
        run = ProcessingRun(5)
        run.start()
        run.records_processed = 100
        run.records_quarantined = 2
        run.complete()
        d = run.to_dict()
        assert d["week_number"] == 5
        assert d["status"] == "completed"
        assert d["records_processed"] == 100
        assert d["records_quarantined"] == 2


class TestGetRawReportsForWeek:
    """get_raw_reports_for_week reads .txt files from a week directory."""

    def test_returns_empty_for_missing_week(self):
        result = get_raw_reports_for_week(9999)
        assert result == []

    def test_reads_week_1_reports(self):
        # Week 1 has 3000 reports
        reports = get_raw_reports_for_week(1)
        assert len(reports) == 3000
        # Check first report
        pid, text = reports[0]
        assert pid == "P0001"
        assert "P0001" in text
        assert len(text) > 0

    def test_person_ids_are_upper(self):
        reports = get_raw_reports_for_week(1)
        assert all(pid == pid.upper() for pid, _ in reports)


class TestCacheKey:
    """Cache keys are deterministic hashes of raw text."""

    def test_same_text_same_key(self):
        key1 = compute_cache_key("some report text")
        key2 = compute_cache_key("some report text")
        assert key1 == key2

    def test_different_text_different_key(self):
        key1 = compute_cache_key("text A")
        key2 = compute_cache_key("text B")
        assert key1 != key2

    def test_key_is_short_hash(self):
        key = compute_cache_key("x" * 1000)
        assert len(key) == 16


class TestIngestionPipeline:
    """IngestionPipeline processes weeks through extraction → validation."""

    def setup_method(self):
        self.extractor = RuleBasedExtractor()
        self.mock_conn = MagicMock()
        self.pipeline = IngestionPipeline(extractor=self.extractor)

    def test_process_week_reads_reports(self):
        run = self.pipeline.process_week(1)
        assert run.status == "completed"
        assert run.records_processed > 0
        assert run.records_quarantined >= 0

    def test_process_week_counts_valid_and_quarantined(self):
        run = self.pipeline.process_week(1)
        # Week 1 should have mostly valid records
        assert run.records_processed > 0
        # Some may be quarantined due to validation rules
        assert run.records_processed + run.records_quarantined <= 3000

    def test_idempotency_skip_completed_week(self):
        # First run
        run1 = self.pipeline.process_week(5)
        assert run1.status == "completed"

        # Second run should skip
        run2 = self.pipeline.process_week(5)
        assert run2.status == "skipped"

    def test_force_reprocess(self):
        run1 = self.pipeline.process_week(10)
        count1 = run1.records_processed

        run2 = self.pipeline.process_week(10, force=True)
        # Should re-process, not skip
        assert run2.status == "completed"

    def test_multiple_weeks(self):
        runs = {}
        for w in [1, 2, 3]:
            runs[w] = self.pipeline.process_week(w)
        assert all(r.status == "completed" for r in runs.values())


class TestIngestionPipelineWithDB:
    """Pipeline with a database connection writes to extracted_records and quarantine."""

    def setup_method(self):
        # Create a real SQLite in-memory DB for testing
        import sqlite3
        self.db_path = ":memory:"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

        # Create minimal schema
        self.conn.execute("""CREATE TABLE IF NOT EXISTS extracted_records (
            record_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id VARCHAR(10) NOT NULL,
            week_number INT NOT NULL,
            disease VARCHAR(100),
            village VARCHAR(50),
            street VARCHAR(50),
            reporting_date DATE,
            extractor_type VARCHAR(20) NOT NULL DEFAULT 'rule',
            UNIQUE (person_id, week_number)
        )""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS quarantine (
            qw_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id VARCHAR(10),
            week_number INT NOT NULL,
            raw_text TEXT NOT NULL,
            failure_reason VARCHAR(200) NOT NULL,
            UNIQUE (person_id, week_number)
        )""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS processing_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_number INT NOT NULL,
            run_type VARCHAR(50) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'running',
            records_processed INT NOT NULL DEFAULT 0,
            records_quarantined INT NOT NULL DEFAULT 0,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            UNIQUE (week_number, run_type)
        )""")
        self.conn.commit()

        self.extractor = RuleBasedExtractor()
        self.pipeline = IngestionPipeline(extractor=self.extractor, db_conn=self.conn)

    def teardown_method(self):
        self.conn.close()

    def test_writes_valid_records_to_db(self):
        run = self.pipeline.process_week(1)
        assert run.records_processed > 0

        # Check DB
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM extracted_records WHERE week_number = ?", (1,))
        count = cur.fetchone()[0]
        cur.close()
        assert count == run.records_processed

    def test_writes_quarantine_to_db(self):
        run = self.pipeline.process_week(1)
        assert run.records_quarantined >= 0

        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM quarantine WHERE week_number = ?", (1,))
        count = cur.fetchone()[0]
        cur.close()
        assert count == run.records_quarantined

    def test_writes_processing_run_to_db(self):
        run = self.pipeline.process_week(1)
        assert run.status == "completed"

        cur = self.conn.cursor()
        cur.execute(
            "SELECT status, records_processed, records_quarantined FROM processing_runs WHERE week_number = ?",
            (1,),
        )
        row = cur.fetchone()
        cur.close()
        assert row is not None
        assert row["status"] == "completed"
        assert row["records_processed"] == run.records_processed
        assert row["records_quarantined"] == run.records_quarantined

    def test_idempotent_db_writes(self):
        # First run
        run1 = self.pipeline.process_week(3)
        # Second run should skip (already completed)
        run2 = self.pipeline.process_week(3)
        assert run2.status == "skipped"

        # Counts should not have doubled
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM extracted_records WHERE week_number = ?", (3,))
        count = cur.fetchone()[0]
        cur.close()
        assert count == run1.records_processed


class TestExtractionValidation:
    """LLM sampling validation produces output file."""

    def test_output_path_created(self, tmp_path):
        output = str(tmp_path / "validation.json")
        # Mock Ollama to be unavailable so we test the fallback path
        with patch("src.extraction.ingestion.OllamaExtractor") as mock_ollama:
            mock_instance = MagicMock()
            mock_instance.is_available = False
            mock_instance.name = "OllamaExtractor"
            mock_ollama.return_value = mock_instance

            results = run_extraction_validation(
                sample_size=10,
                seed=42,
                output_path=output,
            )

        assert Path(output).exists()
        with open(output) as f:
            data = json.load(f)
        assert "ollama_available" in data
        assert data["ollama_available"] is False
        assert "error" in data
