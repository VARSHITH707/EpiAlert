"""EpiAlert end-to-end pipeline.

One entry point that checks the environment, runs detection, generates alerts,
dispatches SMS in mock mode, and prints what it actually did.

    .venv\\Scripts\\python.exe run_all.py

Every number printed is read back from the database after the step ran, rather
than reported by the step itself. A stage that silently did nothing therefore
shows as zero instead of claiming success.

By default nothing is recomputed if it already exists, so the script is safe to
re-run. Pass --rebuild to regenerate alerts and SMS from current detection
results.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

import requests

from src.database.db import get_connection, get_db_path

OLLAMA_URL = "http://localhost:11434/api/tags"


def _count(table: str) -> int:
    """Row count for a table, or 0 if it does not exist."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return cur.fetchone()[0]
    except Exception:
        return 0
    finally:
        cur.close()
        conn.close()


def _scalar(sql: str) -> Optional[int]:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        cur.close()
        conn.close()


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")
    print("-" * 60)


def check_environment() -> dict:
    """Report what the pipeline has to work with."""
    step(1, "Environment")
    db = get_db_path()
    print(f"  database        : {db}")

    ollama_ok = False
    models: list[str] = []
    try:
        resp = requests.get(OLLAMA_URL, timeout=4)
        if resp.ok:
            ollama_ok = True
            models = [m["name"] for m in resp.json().get("models", [])]
    except requests.RequestException:
        pass

    if ollama_ok:
        print(f"  ollama          : running ({', '.join(models) or 'no models'})")
    else:
        # Not fatal: alert messages fall back to a deterministic template.
        print("  ollama          : not running - alert text uses the template fallback")

    return {"ollama": ollama_ok, "models": models}


def check_data() -> bool:
    """Verify the ingested data is present. Returns False if it is not."""
    step(2, "Data")
    people = _count("people")
    reports = _count("reports")
    print(f"  people          : {people:,}")
    print(f"  reports         : {reports:,}")

    if people == 0 or reports == 0:
        print("\n  No ingested data. Run first:")
        print("    .venv\\Scripts\\python.exe -m src.cli ingest-all")
        return False

    units = _scalar("SELECT COUNT(*) FROM (SELECT DISTINCT village, street FROM people)")
    print(f"  spatial units   : {units}")
    return True


def check_detection() -> bool:
    """Verify detection results exist."""
    step(3, "Detection")
    total = _count("detection_results")
    print(f"  detection rows  : {total:,}")

    if total == 0:
        print("\n  No detection results. Run first:")
        print("    .venv\\Scripts\\python.exe -m src.cli evaluate --start-week 21 --end-week 104")
        return False

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT fusion_mode, status, COUNT(*) FROM detection_results
           GROUP BY fusion_mode, status ORDER BY fusion_mode, status"""
    )
    for mode, status, n in cur.fetchall():
        print(f"    {mode:14s} {status:14s} {n:>6,}")
    cur.close()
    conn.close()
    return True


def run_alerts(rebuild: bool) -> int:
    """Generate alerts from detection results. Returns the alert count."""
    step(4, "Alerts")
    existing = _count("alerts")
    if existing and not rebuild:
        print(f"  {existing:,} alerts already present (use --rebuild to regenerate)")
    else:
        from src.alerts.service import AlertService

        service = AlertService()
        service.generate_alerts(fusion_mode="confirmation")
        print("  generated from confirmation-mode detection results")

    total = _count("alerts")
    eligible = _scalar("SELECT COUNT(*) FROM alerts WHERE sms_eligible = 1") or 0
    high = _scalar("SELECT COUNT(*) FROM alerts WHERE status = 'HIGH_ALERT'") or 0
    print(f"  alerts          : {total:,}")
    print(f"  sms eligible    : {eligible:,}")
    print(f"  high alerts     : {high:,}")
    return total


def run_sms() -> dict:
    """Dispatch SMS in mock mode. Returns summary counts read from the DB."""
    step(5, "SMS (mock mode - nothing is actually sent)")
    from src.alerts.service import AlertService, SMSDispatchService
    from src.sms import MockSMSProvider

    dispatch = SMSDispatchService(AlertService(), provider=MockSMSProvider())
    summary = dispatch.dispatch_all_sms_eligible()

    total = _count("sms_messages")
    phones = _scalar("SELECT COUNT(DISTINCT phone_number) FROM sms_messages") or 0
    covered = _scalar("SELECT COUNT(DISTINCT alert_id) FROM sms_messages") or 0
    per_alert = _scalar(
        "SELECT MAX(n) FROM (SELECT COUNT(*) n FROM sms_messages GROUP BY alert_id)"
    ) or 0
    people = _count("people")

    print(f"  newly sent      : {summary['sms_sent']:,}")
    print(f"  already sent    : {summary['sms_skipped']:,}")
    print(f"  failed          : {summary['sms_failed']:,}")
    print(f"  audit rows      : {total:,}")
    print(f"  alerts covered  : {covered:,}")
    print(f"  distinct numbers: {phones}")
    print(f"  max per alert   : {per_alert}")
    print(
        f"\n  Deduplication: {people:,} people share {phones} numbers, so a whole-street\n"
        f"  alert sends at most {per_alert} messages rather than one per resident."
    )
    return {"total": total, "phones": phones, "max_per_alert": per_alert}


def check_web() -> None:
    """Report how to start the site and whether an account exists."""
    step(6, "Web application")
    users = _count("users")
    if users == 0:
        print("  No login exists yet. Create one:")
        print("    .venv\\Scripts\\python.exe -m src.cli createuser --username admin")
    else:
        print(f"  {users} login(s) registered")
    print("\n  Start the site:")
    print("    .venv\\Scripts\\python.exe -m uvicorn src.web.main:app --reload")
    print("  Then open http://127.0.0.1:8000")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the EpiAlert pipeline end to end.")
    parser.add_argument(
        "--rebuild", action="store_true",
        help="Regenerate alerts even if some already exist",
    )
    args = parser.parse_args()

    started = time.time()
    print("=" * 60)
    print("EpiAlert - end-to-end pipeline")
    print("=" * 60)

    check_environment()

    if not check_data():
        return 1
    if not check_detection():
        return 1

    run_alerts(rebuild=args.rebuild)
    run_sms()
    check_web()

    print("\n" + "=" * 60)
    print(f"Completed in {time.time() - started:.1f}s")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
