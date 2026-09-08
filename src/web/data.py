"""Read-only queries backing the EpiAlert web pages.

Routes stay thin: every database access lives here. All values shown in the UI
come from the live database or results/ on disk -- nothing is hardcoded, so a
page cannot display a number the system did not actually compute.

All SQL is parameterised. Filter values arrive from query strings and must
never be interpolated into a statement.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from src.database.db import get_connection, get_db_path

logger = logging.getLogger("web.data")

RESULTS_DIR = Path(get_db_path()).parent / "results"


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    """Run a query and return a list of plain dicts."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(sql, params)
    out = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return out


def _scalar(sql: str, params: tuple = ()) -> Any:
    """Run a query returning a single value."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def dashboard_counts() -> dict:
    """Headline counts for the dashboard, read live from the database."""
    return {
        "people": _scalar("SELECT COUNT(*) FROM people") or 0,
        "reports": _scalar("SELECT COUNT(*) FROM reports") or 0,
        "detection_results": _scalar("SELECT COUNT(*) FROM detection_results") or 0,
        "alerts": _scalar("SELECT COUNT(*) FROM alerts") or 0,
        "alerts_high": _scalar(
            "SELECT COUNT(*) FROM alerts WHERE status = 'HIGH_ALERT'"
        ) or 0,
        "sms": _scalar("SELECT COUNT(*) FROM sms_messages") or 0,
        "sms_phones": _scalar(
            "SELECT COUNT(DISTINCT phone_number) FROM sms_messages"
        ) or 0,
        "spatial_units": _scalar(
            "SELECT COUNT(*) FROM (SELECT DISTINCT village, street FROM people)"
        ) or 0,
    }


def recent_alerts(limit: int = 10) -> list[dict]:
    """Most recent alerts for the dashboard panel."""
    return _rows(
        """SELECT alert_id, disease, village, street, week_number, status,
                  observed_count, expected_count, message
           FROM alerts
           ORDER BY week_number DESC, alert_id DESC
           LIMIT ?""",
        (limit,),
    )


def status_breakdown() -> list[dict]:
    """Detection result counts per combined status, per fusion mode."""
    return _rows(
        """SELECT fusion_mode, status, COUNT(*) AS n
           FROM detection_results
           GROUP BY fusion_mode, status
           ORDER BY fusion_mode, status"""
    )


# ---------------------------------------------------------------------------
# Detection results
# ---------------------------------------------------------------------------

def filter_options() -> dict:
    """Distinct values for the results page filter dropdowns."""
    return {
        "diseases": [r["disease"] for r in _rows(
            "SELECT DISTINCT disease FROM detection_results ORDER BY disease")],
        "villages": [r["village"] for r in _rows(
            "SELECT DISTINCT village FROM detection_results ORDER BY village")],
        "streets": [r["street"] for r in _rows(
            "SELECT DISTINCT street FROM detection_results ORDER BY street")],
    }


def detection_results(
    disease: Optional[str] = None,
    village: Optional[str] = None,
    street: Optional[str] = None,
    week: Optional[int] = None,
    status: Optional[str] = None,
    fusion_mode: str = "confirmation",
    limit: int = 200,
) -> list[dict]:
    """Detection results with optional filters.

    Defaults to confirmation mode: union fires on 16.6% of unit-weeks against
    a 1.3% true outbreak rate, so it is kept for comparison but is not the
    sensible default view.
    """
    where = ["fusion_mode = ?"]
    params: list = [fusion_mode]
    for column, value in (
        ("disease", disease), ("village", village),
        ("street", street), ("status", status),
    ):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if week:
        where.append("week_number = ?")
        params.append(week)
    params.append(limit)

    return _rows(
        f"""SELECT week_number, disease, village, street, observed_count,
                   expected_count, cusum_signal, ewma_signal, status, severity
            FROM detection_results
            WHERE {' AND '.join(where)}
            ORDER BY week_number DESC, village, street
            LIMIT ?""",
        tuple(params),
    )


# ---------------------------------------------------------------------------
# Alerts and SMS
# ---------------------------------------------------------------------------

def alerts(status: Optional[str] = None, limit: int = 200) -> list[dict]:
    """Alert list, newest week first."""
    where = ["sms_eligible = 1"]
    params: list = []
    if status:
        where.append("status = ?")
        params.append(status)
    params.append(limit)
    return _rows(
        f"""SELECT alert_id, disease, village, street, week_number, status,
                   severity, observed_count, expected_count, message
            FROM alerts
            WHERE {' AND '.join(where)}
            ORDER BY week_number DESC, alert_id DESC
            LIMIT ?""",
        tuple(params),
    )


def alert_detail(alert_id: int) -> Optional[dict]:
    """One alert plus its SMS audit rows, or None if not found."""
    found = _rows("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,))
    if not found:
        return None
    alert = found[0]
    alert["sms"] = _rows(
        """SELECT phone_number, delivery_status, provider, sent_at,
                  provider_response, error_text
           FROM sms_messages WHERE alert_id = ?
           ORDER BY phone_number""",
        (alert_id,),
    )
    return alert


def sms_messages(limit: int = 200) -> list[dict]:
    """SMS audit rows, newest first."""
    return _rows(
        """SELECT s.sms_id, s.alert_id, s.phone_number, s.delivery_status,
                  s.provider, s.sent_at, a.disease, a.village, a.street
           FROM sms_messages s
           LEFT JOIN alerts a ON a.alert_id = s.alert_id
           ORDER BY s.sms_id DESC
           LIMIT ?""",
        (limit,),
    )


def sms_summary() -> dict:
    """Counts proving the deduplication actually happened."""
    per_alert_max = _scalar(
        """SELECT MAX(n) FROM (
               SELECT COUNT(*) AS n FROM sms_messages GROUP BY alert_id
           )"""
    )
    return {
        "total": _scalar("SELECT COUNT(*) FROM sms_messages") or 0,
        "distinct_phones": _scalar(
            "SELECT COUNT(DISTINCT phone_number) FROM sms_messages") or 0,
        "alerts_covered": _scalar(
            "SELECT COUNT(DISTINCT alert_id) FROM sms_messages") or 0,
        "max_per_alert": per_alert_max or 0,
        "people_total": _scalar("SELECT COUNT(*) FROM people") or 0,
    }


# ---------------------------------------------------------------------------
# Evaluation artefacts on disk
# ---------------------------------------------------------------------------

def load_result_file(name: str) -> Optional[dict]:
    """Load a JSON file from results/, or None when it does not exist.

    Missing files render as NOT_COMPUTED in the UI rather than as zeros, so a
    metric that was never produced can never be mistaken for a measured one.
    """
    path = RESULTS_DIR / name
    if not path.exists():
        logger.info("Result file %s not present", path)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return None


def evaluation_comparison() -> dict:
    """Union vs confirmation metrics for the evaluation page."""
    return {
        "union": load_result_file("spatial_evaluation_union.json"),
        "confirmation": load_result_file("spatial_evaluation_confirmation.json"),
    }
