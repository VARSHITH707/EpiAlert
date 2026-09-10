"""End-to-end test: upload through to an SMS audit row.

Covers the whole chain in one test, because every stage in this project has at
some point been individually correct while the chain was broken -- code with
passing tests that nothing called, detection writing to a table nothing read,
alerts generated in a fusion mode the SMS layer never queried. A test per stage
would have passed through all of it.

    upload -> extraction -> validation -> database -> detection
           -> alert -> message -> recipients -> SMS -> audit row

A seeded outbreak on one street is planted, and the test asserts the system
finds that street and only that street.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.alerts.service import AlertService, SMSDispatchService
from src.database.db import get_connection
from src.sms import MockSMSProvider
from src.web.upload import process_uploaded_week

# Well clear of the historical range and of other tests' weeks.
E2E_WEEK = 175
OUTBREAK_VILLAGE = "Village A"
OUTBREAK_STREET = "Street 4"
OUTBREAK_DISEASE = "dengue"
OUTBREAK_CASES = 14


def _query(sql: str, params: tuple = ()) -> list[tuple]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = [tuple(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def _build_week() -> bytes:
    """A week of reports with one street clearly in outbreak."""
    people = _query(
        "SELECT person_id, village, street FROM people WHERE village = ? AND street = ?",
        (OUTBREAK_VILLAGE, OUTBREAK_STREET),
    )
    others = _query(
        """SELECT person_id, village, street FROM people
           WHERE NOT (village = ? AND street = ?) LIMIT 400""",
        (OUTBREAK_VILLAGE, OUTBREAK_STREET),
    )
    if not people:
        pytest.skip("population not loaded; run setup.py first")

    lines = []
    for i, (pid, village, street) in enumerate(people):
        state = OUTBREAK_DISEASE if i < OUTBREAK_CASES else "no infection"
        lines.append(
            f"On 11 January 2027, {pid} from {street} in {village} "
            f"was reported as having {state}."
        )
    for pid, village, street in others:
        lines.append(
            f"On 11 January 2027, {pid} from {street} in {village} "
            f"was reported as having no infection."
        )
    return ("\n".join(lines)).encode("utf-8")


@pytest.fixture(scope="module")
def uploaded() -> dict:
    """Run the upload once; every assertion below reads from the same run."""
    return process_uploaded_week(E2E_WEEK, _build_week())


# ---------------------------------------------------------------------------
# Upload -> extraction -> validation -> database
# ---------------------------------------------------------------------------

def test_reports_were_stored(uploaded: dict):
    assert uploaded["valid"] > 0, "reports parsed but none stored"
    assert uploaded["quarantined"] == 0, "valid reports were wrongly quarantined"

    stored = _query(
        "SELECT COUNT(*) FROM reports WHERE week_number = ?", (E2E_WEEK,)
    )[0][0]
    assert stored == uploaded["valid"], (
        "the reported count does not match what is actually in the database"
    )


def test_cases_landed_on_the_right_street(uploaded: dict):
    """Extraction must not misplace a case onto a neighbouring street."""
    rows = _query(
        """SELECT village, street, COUNT(*) FROM reports
           WHERE week_number = ? AND infection = 'Dengue'
           GROUP BY village, street""",
        (E2E_WEEK,),
    )
    assert rows, "no dengue cases stored at all"
    for village, street, count in rows:
        assert (village, street) == (OUTBREAK_VILLAGE, OUTBREAK_STREET), (
            f"{count} dengue case(s) placed on {village}/{street}, "
            f"but they were reported on {OUTBREAK_VILLAGE}/{OUTBREAK_STREET}"
        )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_detection_flagged_the_outbreak_street(uploaded: dict):
    assert uploaded["detection"] is not None, uploaded["detection_error"]

    flagged = _query(
        """SELECT disease, village, street, observed_count, status
           FROM detection_results
           WHERE week_number = ? AND status IN ('ALERT','HIGH_ALERT')""",
        (E2E_WEEK,),
    )
    assert flagged, "the seeded outbreak produced no alert"

    streets = {(row[1], row[2]) for row in flagged}
    assert (OUTBREAK_VILLAGE, OUTBREAK_STREET) in streets, (
        f"outbreak street not flagged; flagged instead: {streets}"
    )
    # Street names repeat across villages, so an alert must not spill into a
    # same-named street elsewhere.
    assert streets == {(OUTBREAK_VILLAGE, OUTBREAK_STREET)}, (
        f"alert spilled onto unaffected streets: {streets}"
    )


# ---------------------------------------------------------------------------
# Alert
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def alert_id(uploaded: dict) -> int:
    """Generate alerts and return the one for the seeded outbreak."""
    AlertService().generate_alerts(fusion_mode="confirmation", use_ollama=False)
    rows = _query(
        """SELECT alert_id FROM alerts
           WHERE week_number = ? AND village = ? AND street = ?""",
        (E2E_WEEK, OUTBREAK_VILLAGE, OUTBREAK_STREET),
    )
    if not rows:
        pytest.fail("detection flagged the street but no alert row was created")
    return rows[0][0]


def test_alert_carries_a_readable_message(alert_id: int):
    """The message is what a resident receives; it must not be diagnostics."""
    message, status, eligible = _query(
        "SELECT message, status, sms_eligible FROM alerts WHERE alert_id = ?",
        (alert_id,),
    )[0]

    assert message, "alert has no message"
    assert status in ("ALERT", "HIGH_ALERT")
    assert eligible == 1, "confirmation-mode alert is not marked sms_eligible"

    assert OUTBREAK_VILLAGE in message and OUTBREAK_STREET in message, (
        f"message does not name the affected area: {message!r}"
    )
    assert "CUSUM" not in message and "EWMA" not in message, (
        f"detector diagnostics leaked into the resident-facing message: {message!r}"
    )


# ---------------------------------------------------------------------------
# SMS dispatch and audit
# ---------------------------------------------------------------------------

def test_alert_writes_sms_audit_rows(alert_id: int):
    """Requirement: a triggered alert must write an SMS dispatch row."""
    service = AlertService()
    alert = service.get_alert(alert_id)
    SMSDispatchService(service, provider=MockSMSProvider()).dispatch_alert(alert)

    rows = _query(
        """SELECT phone_number, delivery_status, provider
           FROM sms_messages WHERE alert_id = ?""",
        (alert_id,),
    )
    assert rows, "alert produced no SMS audit row"
    assert all(r[2] == "mock" for r in rows), (
        "test dispatched through a non-mock provider"
    )
    assert all(r[1] in ("sent", "skipped") for r in rows)


def test_dispatch_is_deduplicated(alert_id: int):
    """Many residents, few numbers: the point of the SMS layer."""
    residents = _query(
        "SELECT COUNT(*) FROM people WHERE village = ? AND street = ?",
        (OUTBREAK_VILLAGE, OUTBREAK_STREET),
    )[0][0]
    distinct_numbers = _query(
        """SELECT COUNT(DISTINCT phone_number) FROM people
           WHERE village = ? AND street = ?""",
        (OUTBREAK_VILLAGE, OUTBREAK_STREET),
    )[0][0]
    sent = _query(
        "SELECT COUNT(*) FROM sms_messages WHERE alert_id = ?", (alert_id,)
    )[0][0]

    assert residents > distinct_numbers, (
        "test is meaningless unless residents outnumber phone numbers"
    )
    assert sent <= distinct_numbers, (
        f"{sent} messages sent to {distinct_numbers} distinct numbers "
        f"for {residents} residents - deduplication failed"
    )


def test_redispatch_sends_nothing_further(alert_id: int):
    """Re-running must not message anyone twice."""
    before = _query(
        "SELECT COUNT(*) FROM sms_messages WHERE alert_id = ?", (alert_id,)
    )[0][0]

    service = AlertService()
    results = SMSDispatchService(
        service, provider=MockSMSProvider()
    ).dispatch_alert(service.get_alert(alert_id))

    after = _query(
        "SELECT COUNT(*) FROM sms_messages WHERE alert_id = ?", (alert_id,)
    )[0][0]

    assert after == before, "a repeat dispatch created new audit rows"
    assert all(r["delivery_status"] == "skipped" for r in results), (
        "the provider was contacted again for recipients already messaged"
    )
