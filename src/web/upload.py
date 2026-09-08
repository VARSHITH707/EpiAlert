"""Accept a new week of reports uploaded through the web interface.

The historical weeks 1-104 are already ingested. This handles NEW incoming
weeks (105 onwards), which is the live path: a health worker uploads the week's
reports, the system extracts and validates them, runs detection, and shows what
it found.

Nothing here re-implements ingestion. The uploaded reports are written in the
same layout the existing pipeline already reads, and IngestionPipeline does the
rest -- so uploaded data goes through exactly the same extraction, validation
and quarantine rules as the historical data. A second parser would be a second
place for the two paths to disagree.

Only weeks above 104 can be written. The historical dataset is read-only.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from src.database.db import get_connection, get_db_path

logger = logging.getLogger("web.upload")

# Weeks 1-104 are the ingested historical record and must never be overwritten
# by an upload.
FIRST_UPLOADABLE_WEEK = 105
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB is ample for ~3,000 reports

DATASET_ROOT = Path(get_db_path()).parent / "data" / "EpiAlert_Phase1_Dataset"

_PERSON_ID = re.compile(r"\bP\d{4,}\b", re.IGNORECASE)


class UploadError(Exception):
    """Raised when an upload cannot be accepted. Message is shown to the user."""


def next_available_week() -> int:
    """Lowest week number not yet present in the database."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT MAX(week_number) FROM reports")
    row = cur.fetchone()
    cur.close()
    conn.close()
    highest = row[0] if row and row[0] else 0
    return max(highest + 1, FIRST_UPLOADABLE_WEEK)


def parse_upload(content: bytes) -> list[tuple[str, str]]:
    """Turn an uploaded file into (person_id, report_text) pairs.

    Accepts two shapes, because both are plausible from a field worker:
      - plain text, one report per line
      - JSONL with "person_id" and "raw_text" fields

    Raises UploadError with a readable message when the file cannot be used.
    """
    if len(content) > MAX_UPLOAD_BYTES:
        raise UploadError(
            f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UploadError("File must be UTF-8 text.") from exc

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise UploadError("File is empty.")

    reports: list[tuple[str, str]] = []
    for line in lines:
        person_id, raw_text = _parse_line(line)
        if person_id:
            reports.append((person_id, raw_text))

    if not reports:
        raise UploadError(
            "No usable reports found. Each line needs a person ID like P0001, "
            "either in the text or as a JSONL person_id field."
        )
    return reports


def _parse_line(line: str) -> tuple[Optional[str], str]:
    """Extract (person_id, text) from one line of either supported format."""
    if line.startswith("{"):
        import json
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return None, line
        person_id = str(row.get("person_id", "")).strip().upper()
        raw_text = str(row.get("raw_text", "")).strip()
        return (person_id or None), raw_text

    # Plain text: the person ID appears inside the sentence itself.
    match = _PERSON_ID.search(line)
    return (match.group(0).upper() if match else None), line


def stage_week(week_num: int, reports: list[tuple[str, str]]) -> Path:
    """Write reports where the existing pipeline expects to find them.

    Returns the directory written to.
    """
    if week_num < FIRST_UPLOADABLE_WEEK:
        raise UploadError(
            f"Week {week_num} is part of the historical record and is read-only. "
            f"Uploads start at week {FIRST_UPLOADABLE_WEEK}."
        )

    reports_dir = DATASET_ROOT / f"week_{week_num:03d}" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    for person_id, raw_text in reports:
        # person_id came from a strict pattern, so it cannot contain a path
        # separator -- but rebuild the name from scratch rather than trusting it.
        safe = re.sub(r"[^A-Z0-9]", "", person_id.upper())
        if not safe:
            continue
        (reports_dir / f"{safe}.txt").write_text(raw_text, encoding="utf-8")

    logger.info("Staged %d reports for week %d", len(reports), week_num)
    return reports_dir


def write_consolidated(week_num: int, reports: list[tuple[str, str]]) -> Path:
    """Extract each report and write the consolidated JSONL for this week.

    This is the file format the existing ingest path already reads, so an
    uploaded week travels the same route into the database as the historical
    weeks did. Producing a second ingest path would be a second place for the
    two to drift apart.
    """
    import json

    # parse_report_text is what built the historical consolidated files, so
    # uploaded reports are parsed by exactly the same code. It also produces
    # the `template` signature, which the ingest requires -- a record without
    # one is quarantined as unparseable.
    from src.consolidate import parse_report_text

    out_dir = Path(get_db_path()).parent / "data" / "consolidated"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"week_{week_num:03d}.jsonl"

    with path.open("w", encoding="utf-8") as fh:
        for person_id, raw_text in reports:
            parsed = parse_report_text(raw_text)
            report_date = parsed["report_date"]
            fh.write(json.dumps({
                "person_id": person_id,
                "week_number": week_num,
                "raw_text": raw_text,
                # None rather than a guess: a fabricated date is worse than a
                # missing one, and the ingest quarantines the gap.
                "parsed_date": report_date.isoformat() if report_date else None,
                "parsed_infection": parsed["infection"],
                "template": parsed["template"],
            }) + "\n")

    logger.info("Wrote %s (%d reports)", path, len(reports))
    return path


def process_uploaded_week(week_num: int, content: bytes) -> dict:
    """Full path for an uploaded week: parse, extract, ingest, detect.

    Returns a summary dict for display. Raises UploadError for anything the
    user can fix by uploading a different file.
    """
    from src.ingestion import ingest_week_to_database

    reports = parse_upload(content)
    stage_week(week_num, reports)      # keep the raw text on disk for audit
    write_consolidated(week_num, reports)

    conn = get_connection()
    try:
        stats = ingest_week_to_database(week_num, conn=conn)
        conn.commit()
    finally:
        conn.close()

    summary = {
        "week": week_num,
        "reports_uploaded": len(reports),
        "status": "completed",
        "valid": stats.get("inserted", 0),
        "quarantined": stats.get("quarantined", 0),
        "detection": None,
        "detection_error": None,
    }

    summary["detection"], summary["detection_error"] = _run_detection(week_num)
    return summary


def _run_detection(week_num: int) -> tuple[Optional[dict], Optional[str]]:
    """Run spatial detection for one week.

    Detection failing must not lose the upload: the reports are already stored,
    so the error is reported and the ingest still counts.
    """
    try:
        from src.evaluation_spatial import run_spatial_evaluation

        # Must match the mode the alert engine reads. run_spatial_evaluation
        # defaults to union, so leaving it implicit meant an uploaded week
        # could never produce an alert -- detection wrote union rows while
        # alert generation only ever looks at confirmation rows.
        result = run_spatial_evaluation(
            start_week=week_num, end_week=week_num,
            # disease=None evaluates all five, not just the "Dengue" default.
            # An uploaded week could carry an outbreak of any of them.
            disease=None,
            fusion_mode="confirmation",
        )
        return {
            "rows": result.get("total_rows", 0),
            "summary": result.get("summary", {}),
        }, None
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
        logger.warning("Detection failed for week %s: %s", week_num, exc)
        return None, str(exc)
