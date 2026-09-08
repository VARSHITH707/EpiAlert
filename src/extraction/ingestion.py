"""P6 Ingestion Pipeline.

Processes weekly reports through the extraction and validation pipeline.
Week-by-week, batch DB writes, idempotent (re-running a week doesn't
duplicate rows), records each run in processing_runs.

Flow per week:
  1. Check processing_runs for existing completed run (idempotency)
  2. Read all .txt files from week_NNN/reports/
  3. Extract each report with RuleBasedExtractor
  4. Validate each extracted record
  5. Valid records → extracted_records table (batch insert)
  6. Failed records → quarantine table (batch insert)
  7. Update processing_runs with counts

Then: LLM sampling validation (section 1 of web prompt) compares
OllamaExtractor vs RuleBasedExtractor on a random sample and writes
results/extraction_validation.json.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from src.extraction.interface import ExtractorInterface
from src.extraction.ollama_extractor import OllamaExtractor
from src.extraction.rule_based import RuleBasedExtractor
from src.extraction.schema import ExtractedRecord, ExtractionResult
from src.extraction.validation import (
    validate_or_quarantine,
    QuarantineRecord,
)

logger = logging.getLogger(__name__)

# Dataset paths
DATASET_ROOT = Path("data/EpiAlert_Phase1_Dataset")

# Batch size for DB writes
BATCH_SIZE = 500


class ProcessingRun:
    """Tracks a single ingestion run for one week."""

    def __init__(
        self,
        week_number: int,
        run_type: str = "extraction",
    ):
        self.week_number = week_number
        self.run_type = run_type
        self.status = "pending"
        self.records_processed = 0
        self.records_quarantined = 0
        self.started_at: Optional[date] = None
        self.completed_at: Optional[date] = None

    def start(self):
        self.status = "running"
        self.started_at = date.today()

    def complete(self):
        self.status = "completed"
        self.completed_at = date.today()

    def to_dict(self) -> dict:
        return {
            "week_number": self.week_number,
            "run_type": self.run_type,
            "status": self.status,
            "records_processed": self.records_processed,
            "records_quarantined": self.records_quarantined,
            "started_at": str(self.started_at) if self.started_at else None,
            "completed_at": str(self.completed_at) if self.completed_at else None,
        }


def get_raw_reports_for_week(week_num: int) -> List[Tuple[str, str]]:
    """Read all raw report texts for a week.

    Returns list of (person_id, raw_text) tuples.
    Does NOT load all into memory at once for huge weeks — but for
    3,000 reports the memory footprint is negligible.
    """
    reports_dir = DATASET_ROOT / f"week_{week_num:03d}" / "reports"
    if not reports_dir.exists():
        return []

    results = []
    for path in sorted(reports_dir.glob("*.txt")):
        person_id = path.stem.strip().upper()
        raw_text = path.read_text(encoding="utf-8")
        results.append((person_id, raw_text))
    return results


def compute_cache_key(raw_text: str) -> str:
    """Compute a cache key for an extracted result.

    Hash of the raw text so re-runs with the same input produce
    the same cache key.
    """
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16]


class IngestionPipeline:
    """Main ingestion pipeline for P6.

    Processes weeks through extraction → validation → storage.
    Uses RuleBasedExtractor by default. Selectable via extractor param.
    """

    def __init__(
        self,
        extractor: Optional[ExtractorInterface] = None,
        db_conn=None,
    ):
        self.extractor = extractor or RuleBasedExtractor()
        self.db_conn = db_conn
        self.cache: dict = {}  # cache_key -> ExtractionResult (for LLM sampling)
        self._completed_weeks: set = set()  # in-memory idempotency when no DB

    def process_week(
        self,
        week_num: int,
        *,
        force: bool = False,
    ) -> ProcessingRun:
        """Process a single week through the full pipeline.

        Parameters
        ----------
        week_num : int
            Week number (1-based).
        force : bool
            If True, re-process even if already completed.

        Returns
        -------
        ProcessingRun with final status and counts.
        """
        run = ProcessingRun(week_num)
        run.start()
        logger.info(f"Starting ingestion for week {week_num:03d}...")

        # Check idempotency
        if not force and self._run_already_completed(week_num):
            logger.info(f"Week {week_num:03d} already processed, skipping.")
            run.status = "skipped"
            run.completed_at = date.today()
            self._completed_weeks.add(week_num)
            return run

        # Read raw reports
        raw_reports = get_raw_reports_for_week(week_num)
        total = len(raw_reports)
        logger.info(f"Found {total} reports for week {week_num:03d}.")

        if total == 0:
            run.status = "completed"
            run.completed_at = date.today()
            self._completed_weeks.add(week_num)
            return run

        # Extract and validate in batches
        valid_records: List[ExtractionResult] = []
        quarantine_records: List[QuarantineRecord] = []

        extracted = self.extractor.extract_batch(raw_reports)

        for i, (person_id, raw_text) in enumerate(raw_reports):
            result = extracted[i]
            record = result.record

            validated, quarantined = validate_or_quarantine(
                record, week_num, raw_text,
            )

            if validated is not None:
                valid_records.append(result)
            if quarantined is not None:
                quarantine_records.append(quarantined)

            if (i + 1) % 500 == 0:
                logger.info(
                    f"  Week {week_num:03d}: processed {i+1}/{total}, "
                    f"{len(valid_records)} valid, {len(quarantine_records)} quarantined"
                )

        run.records_processed = len(valid_records)
        run.records_quarantined = len(quarantine_records)

        # Finalize status BEFORE writing to DB so the row reflects the correct status
        run.complete()
        self._completed_weeks.add(week_num)

        # Batch write to DB if connection available
        if self.db_conn is not None:
            self._write_valid_records(valid_records, week_num)
            self._write_quarantine_records(quarantine_records, week_num)
            self._update_processing_run(run)
        logger.info(
            f"Week {week_num:03d} complete: "
            f"{run.records_processed} extracted, {run.records_quarantined} quarantined"
        )

        return run

    def _run_already_completed(self, week_num: int) -> bool:
        """Check if a week has already been processed (idempotency)."""
        if self.db_conn is not None:
            # Check processing_runs table
            cur = self.db_conn.cursor()
            cur.execute(
                "SELECT status FROM processing_runs WHERE week_number = ? AND run_type = 'extraction'",
                (week_num,),
            )
            row = cur.fetchone()
            cur.close()
            if row and row[0] == "completed":
                return True
            return False
        # In-memory fallback: track completed weeks when no DB
        return week_num in self._completed_weeks

    def _write_valid_records(
        self,
        records: List[ExtractionResult],
        week_num: int,
    ):
        """Batch insert valid extracted records into extracted_records table."""
        if not records:
            return
        if self.db_conn is None:
            # Silently returning here made an upload report "completed" after
            # storing zero rows. Extraction without persistence is almost never
            # what a caller wants, so say so loudly.
            logger.warning(
                "No database connection: discarding %d validated records for "
                "week %d. Construct IngestionPipeline(db_conn=get_connection()) "
                "to persist them.",
                len(records), week_num,
            )
            return

        cur = self.db_conn.cursor()
        inserted = 0
        for result in records:
            # Check for existing record (idempotency)
            cur.execute(
                "SELECT record_id FROM extracted_records WHERE person_id = ? AND week_number = ?",
                (result.record.person_id, week_num),
            )
            if cur.fetchone():
                continue  # already exists

            disease = result.record.disease or None
            village = result.record.village or None
            street = result.record.street or None
            reporting_date = result.record.reporting_date

            cur.execute(
                """INSERT INTO extracted_records
                   (person_id, week_number, disease, village, street,
                    reporting_date, extractor_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (result.record.person_id, week_num, disease,
                 village, street,
                 reporting_date.isoformat() if reporting_date else None,
                 result.extractor_type),
            )
            inserted += 1

            if inserted % BATCH_SIZE == 0:
                self.db_conn.commit()

        self.db_conn.commit()
        cur.close()

    def _write_quarantine_records(
        self,
        records: List[QuarantineRecord],
        week_num: int,
    ):
        """Batch insert quarantined records into quarantine table."""
        if not records or self.db_conn is None:
            return

        cur = self.db_conn.cursor()
        inserted = 0
        for q in records:
            # Check for existing (idempotency)
            cur.execute(
                "SELECT qw_id FROM quarantine WHERE person_id = ? AND week_number = ?",
                (q.person_id, week_num),
            )
            if cur.fetchone():
                continue

            failure_reasons = "; ".join(
                f"R{qf.rule_number}({qf.rule_name})" for qf in q.failures
            )

            cur.execute(
                """INSERT INTO quarantine
                   (person_id, week_number, raw_text, failure_reason)
                   VALUES (?, ?, ?, ?)""",
                (q.person_id, week_num, q.raw_text, failure_reasons),
            )
            inserted += 1

            if inserted % BATCH_SIZE == 0:
                self.db_conn.commit()

        self.db_conn.commit()
        cur.close()

    def _update_processing_run(self, run: ProcessingRun):
        """Write or update the processing_runs record."""
        if self.db_conn is None:
            return

        cur = self.db_conn.cursor()
        cur.execute(
            """INSERT INTO processing_runs
               (week_number, run_type, status, records_processed,
                records_quarantined, started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(week_number, run_type) DO UPDATE SET
                   status = excluded.status,
                   records_processed = excluded.records_processed,
                   records_quarantined = excluded.records_quarantined,
                   started_at = excluded.started_at,
                   completed_at = excluded.completed_at""",
            (run.week_number, run.run_type, run.status,
             run.records_processed, run.records_quarantined,
             run.started_at.isoformat() if run.started_at else None,
             run.completed_at.isoformat() if run.completed_at else None),
        )
        self.db_conn.commit()
        cur.close()

    def process_all_weeks(
        self,
        start_week: int = 1,
        end_week: int = 104,
        *,
        force: bool = False,
        callback=None,
    ) -> dict:
        """Process all weeks in range.

        Returns dict with summary statistics.
        """
        total_processed = 0
        total_quarantined = 0
        total_weeks = 0
        failed_weeks = []

        for week_num in range(start_week, end_week + 1):
            try:
                run = self.process_week(week_num, force=force)
                total_processed += run.records_processed
                total_quarantined += run.records_quarantined
                if run.status == "completed" or run.status == "skipped":
                    total_weeks += 1
                if callback:
                    callback(week_num, end_week, run)
            except Exception as e:
                logger.error(f"Week {week_num:03d} failed: {e}")
                failed_weeks.append((week_num, str(e)))

        return {
            "total_weeks_requested": end_week - start_week + 1,
            "total_weeks_processed": total_weeks,
            "total_records_processed": total_processed,
            "total_records_quarantined": total_quarantined,
            "failed_weeks": failed_weeks,
        }


# ---------------------------------------------------------------------------
# LLM Sampling Validation (section 1 of web prompt)
# ---------------------------------------------------------------------------

def run_extraction_validation(
    sample_size: int = 300,
    seed: int = 42,
    output_path: str = "results/extraction_validation.json",
) -> dict:
    """Compare RuleBasedExtractor vs OllamaExtractor on a random sample.

    Picks `sample_size` random reports from the historical dataset,
    extracts with both extractors, and computes per-field agreement
    with confidence intervals.

    Writes results to output_path as JSON.
    """
    random.seed(seed)

    # Collect report files — sample from a random subset of weeks first so
    # this function is fast even for small sample_size. Walking all 312,000
    # files is what made this slow; we avoid it by picking weeks at random.
    week_dirs = [d for d in sorted(DATASET_ROOT.iterdir())
                 if d.name.startswith("week_") and (d / "reports").exists()]
    if not week_dirs:
        return {"error": "No report weeks found"}

    # Pick enough random weeks to cover sample_size
    weeks_needed = min(len(week_dirs), max(1, sample_size // 3000 + 1))
    chosen_weeks = random.sample(week_dirs, weeks_needed)

    all_reports = []
    for week_dir in chosen_weeks:
        reports_dir = week_dir / "reports"
        for path in reports_dir.glob("*.txt"):
            person_id = path.stem.strip().upper()
            raw_text = path.read_text(encoding="utf-8")
            all_reports.append((week_dir.name, person_id, raw_text))

    if len(all_reports) == 0:
        return {"error": "No reports found"}

    # Sample
    sample = random.sample(all_reports, min(sample_size, len(all_reports)))

    rule_extractor = RuleBasedExtractor()
    ollama_extractor = OllamaExtractor()

    results = {
        "sample_size": len(sample),
        "seed": seed,
        "rule_extractor": rule_extractor.name,
        "ollama_extractor": ollama_extractor.name,
        "ollama_available": ollama_extractor.is_available,
        "per_field_agreement": {},
        "per_record_details": [],
    }

    if not ollama_extractor.is_available:
        results["error"] = "Ollama not available — skipping LLM comparison"
        _write_validation_results(results, output_path)
        return results

    field_counts = {"person_id": 0, "village": 0, "street": 0,
                    "reporting_date": 0, "disease": 0}
    field_agreements = {"person_id": 0, "village": 0, "street": 0,
                        "reporting_date": 0, "disease": 0}
    total = len(sample)

    for week_name, person_id, raw_text in sample:
        rule_result = rule_extractor.extract_from_text(person_id, raw_text)
        ollama_result = ollama_extractor.extract_from_text(person_id, raw_text)

        rule_rec = rule_result.record
        ollama_rec = ollama_result.record

        detail = {
            "week": week_name,
            "person_id": person_id,
            "raw_text_preview": raw_text[:100],
            "rule": {
                "person_id": rule_rec.person_id,
                "village": rule_rec.village,
                "street": rule_rec.street,
                "reporting_date": str(rule_rec.reporting_date) if rule_rec.reporting_date else None,
                "disease": rule_rec.disease,
            },
            "ollama": {
                "person_id": ollama_rec.person_id,
                "village": ollama_rec.village,
                "street": ollama_rec.street,
                "reporting_date": str(ollama_rec.reporting_date) if ollama_rec.reporting_date else None,
                "disease": ollama_rec.disease,
            },
            "agreements": {},
        }

        for field in ["person_id", "village", "street", "reporting_date", "disease"]:
            rule_val = getattr(rule_rec, field)
            ollama_val = getattr(ollama_rec, field)

            # Normalise for comparison
            if field == "reporting_date":
                rule_val = str(rule_val) if rule_val else None
                ollama_val = str(ollama_val) if ollama_val else None

            both_none = rule_val is None and ollama_val is None
            both_equal = rule_val == ollama_val

            agreed = both_none or both_equal
            detail["agreements"][field] = agreed

            field_counts[field] += 1
            if agreed:
                field_agreements[field] += 1

        results["per_record_details"].append(detail)

    # Compute agreement rates
    for field in field_counts:
        if field_counts[field] > 0:
            results["per_field_agreement"][field] = {
                "compared": field_counts[field],
                "agreed": field_agreements[field],
                "agreement_rate": field_agreements[field] / field_counts[field],
            }
        else:
            results["per_field_agreement"][field] = {
                "compared": 0,
                "agreed": 0,
                "agreement_rate": None,
            }

    _write_validation_results(results, output_path)
    return results


def _write_validation_results(results: dict, output_path: str):
    """Write validation results to JSON file."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    logger.info(f"Extraction validation results written to {output_path}")
