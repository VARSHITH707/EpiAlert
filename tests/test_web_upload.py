"""Tests for uploading a new week of reports through the web interface.

The important test is test_end_to_end_detects_seeded_outbreak: it plants a known
outbreak on one street, uploads it, and checks the system finds that street and
no other. Testing the parser alone would not catch the failure this feature
actually had -- reports were parsed correctly but written to a table detection
never reads, so everything "passed" while nothing was detected.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.database.db import get_connection
from src.web.upload import (
    FIRST_UPLOADABLE_WEEK,
    MAX_UPLOAD_BYTES,
    UploadError,
    next_available_week,
    parse_upload,
    process_uploaded_week,
)

TEST_WEEK = 150  # well clear of both the historical range and week 105


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parses_plain_text_lines():
    content = (
        b"On 12 January 2025, P0001 from Street 1 in Village A was reported "
        b"as having dengue.\n"
        b"P0002 of Street 2, Village B with no infection.\n"
    )
    reports = parse_upload(content)
    assert len(reports) == 2
    assert reports[0][0] == "P0001"
    assert reports[1][0] == "P0002"


def test_parses_jsonl():
    content = (
        b'{"person_id": "P0009", "raw_text": "P0009 of Street 3, Village C '
        b'with no infection."}\n'
    )
    reports = parse_upload(content)
    assert reports == [
        ("P0009", "P0009 of Street 3, Village C with no infection.")
    ]


def test_empty_file_rejected():
    with pytest.raises(UploadError, match="empty"):
        parse_upload(b"")


def test_file_without_person_ids_rejected():
    """A file of prose with no IDs cannot be attributed to anyone."""
    with pytest.raises(UploadError, match="No usable reports"):
        parse_upload(b"Everyone on the street seems fine this week.\n")


def test_oversized_file_rejected():
    with pytest.raises(UploadError, match="larger than"):
        parse_upload(b"x" * (MAX_UPLOAD_BYTES + 1))


def test_non_utf8_rejected():
    with pytest.raises(UploadError, match="UTF-8"):
        parse_upload(b"\xff\xfe\x00invalid")


# ---------------------------------------------------------------------------
# The historical record is protected
# ---------------------------------------------------------------------------

def test_cannot_overwrite_historical_week():
    """Weeks 1-104 are the ingested record and must be refused."""
    content = b"P0001 of Street 1, Village A with no infection.\n"
    with pytest.raises(UploadError, match="read-only"):
        process_uploaded_week(50, content)


def test_next_available_week_is_beyond_history():
    assert next_available_week() >= FIRST_UPLOADABLE_WEEK


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

def _seeded_week(disease_cases: int = 12) -> bytes:
    """Build a week where one street has an outbreak and the rest do not."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT person_id FROM people WHERE village = ? AND street = ?",
        ("Village A", "Street 4"),
    )
    outbreak_people = [r[0] for r in cur.fetchall()]
    cur.execute(
        """SELECT person_id, village, street FROM people
           WHERE NOT (village = ? AND street = ?) LIMIT 300""",
        ("Village A", "Street 4"),
    )
    others = [tuple(r) for r in cur.fetchall()]
    cur.close()
    conn.close()

    lines = []
    for i, pid in enumerate(outbreak_people):
        state = "dengue" if i < disease_cases else "no infection"
        lines.append(
            f"On 11 January 2027, {pid} from Street 4 in Village A "
            f"was reported as having {state}."
        )
    for pid, village, street in others:
        lines.append(
            f"On 11 January 2027, {pid} from {street} in {village} "
            f"was reported as having no infection."
        )
    return ("\n".join(lines)).encode("utf-8")


def test_end_to_end_detects_seeded_outbreak():
    """Upload a week with a known outbreak and check it is found.

    This is the test that matters. It fails if reports are parsed but not
    stored, stored but not detected, or detected on the wrong street.
    """
    result = process_uploaded_week(TEST_WEEK, _seeded_week())

    assert result["valid"] > 0, "reports were parsed but none were stored"
    assert result["quarantined"] == 0, "valid reports were wrongly quarantined"
    assert result["detection"] is not None, result["detection_error"]

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT disease, village, street, observed_count, status
           FROM detection_results
           WHERE week_number = ? AND status != 'NORMAL'""",
        (TEST_WEEK,),
    )
    flagged = [tuple(r) for r in cur.fetchall()]
    cur.close()
    conn.close()

    assert flagged, "the seeded outbreak was not detected at all"

    streets = {(row[1], row[2]) for row in flagged}
    assert ("Village A", "Street 4") in streets, (
        f"outbreak street not flagged; flagged instead: {streets}"
    )
    # Street names repeat across villages, so an alert must not spill into
    # Street 4 of a different village.
    assert streets == {("Village A", "Street 4")}, (
        f"alert spilled onto unaffected streets: {streets}"
    )


def test_reupload_is_idempotent():
    """Uploading the same week twice must not duplicate stored reports."""
    content = _seeded_week()
    first = process_uploaded_week(TEST_WEEK, content)
    second = process_uploaded_week(TEST_WEEK, content)
    assert second["valid"] == first["valid"], (
        "re-upload changed the stored row count"
    )
