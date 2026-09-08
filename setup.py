"""One-command setup for EpiAlert.

    python setup.py

Generates the dataset, builds the database, runs detection, creates the alerts
and the mock SMS, and creates a login. About 3-5 minutes on a normal laptop.

Each step is skipped if it has already been done, so re-running is safe and
quick. Pass --fresh to redo everything from scratch.

Every step prints what it actually produced, read back from the database rather
than reported by the step itself -- so a stage that silently did nothing shows
as zero instead of claiming success.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
PYTHON = sys.executable

DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "epialert2026"


def run(label: str, args: list[str], timeout: int = 1800) -> bool:
    """Run a step, showing its output. Returns True on success."""
    print(f"\n>>> {label}")
    print("-" * 62)
    try:
        result = subprocess.run(
            [PYTHON, *args], cwd=ROOT, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"!! Timed out after {timeout}s.")
        return False
    if result.returncode != 0:
        print(f"!! Failed with exit code {result.returncode}.")
        return False
    return True


def count(table: str) -> int:
    """Row count for a table, or 0 if unreadable."""
    sys.path.insert(0, str(ROOT))
    try:
        from src.database.db import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        n = cur.fetchone()[0]
        cur.close()
        conn.close()
        return n
    except Exception:
        return 0


def ensure_env() -> None:
    """Write .env if absent, with a random session key.

    Written as UTF-8 deliberately. PowerShell redirection (`echo x >> .env`)
    produces UTF-16, whose null bytes make python-dotenv fail to load.
    """
    env = ROOT / ".env"
    if env.exists():
        print("  .env already present, left alone")
        return
    import secrets
    env.write_text(
        "DB_TYPE=sqlite\n"
        "DB_PATH=epialert_demo.db\n"
        "SMS_MODE=mock\n"
        f"EPIALERT_SECRET_KEY={secrets.token_urlsafe(32)}\n",
        encoding="utf-8",
    )
    print("  wrote .env with a fresh session key")


def ensure_user() -> None:
    """Create the default login if no account exists yet."""
    sys.path.insert(0, str(ROOT))
    from src.web.auth import create_user, user_count

    if user_count() > 0:
        print(f"  {user_count()} login(s) already exist, none created")
        return
    create_user(DEFAULT_USER, DEFAULT_PASSWORD)
    print(f"  created login: {DEFAULT_USER} / {DEFAULT_PASSWORD}")
    print("  change it before putting this anywhere shared")


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up EpiAlert.")
    parser.add_argument(
        "--fresh", action="store_true",
        help="Redo every step even if it has already been done.",
    )
    parser.add_argument(
        "--weeks", type=int, default=104,
        help="Weeks of data to generate (default 104). Fewer is faster.",
    )
    args = parser.parse_args()

    started = time.time()
    print("=" * 62)
    print("EpiAlert setup")
    print("=" * 62)

    # 1. Configuration
    print("\n>>> Configuration")
    print("-" * 62)
    ensure_env()

    # 2. Dataset
    consolidated = ROOT / "data" / "consolidated"
    have_data = len(list(consolidated.glob("week_*.jsonl"))) >= args.weeks
    if have_data and not args.fresh:
        print("\n>>> Dataset\n" + "-" * 62)
        print(f"  already generated ({args.weeks} weeks present)")
    elif not run("Generating dataset", ["generate_dataset.py", "--weeks", str(args.weeks)]):
        return 1

    # 3. Database
    if count("reports") > 0 and not args.fresh:
        print("\n>>> Database\n" + "-" * 62)
        print(f"  already ingested ({count('reports'):,} reports)")
    elif not run("Loading reports into the database", ["-m", "src.cli", "ingest-all"]):
        return 1

    # 4. Detection
    if count("detection_results") > 0 and not args.fresh:
        print("\n>>> Detection\n" + "-" * 62)
        print(f"  already run ({count('detection_results'):,} results)")
    else:
        # `detect` is the street-level detection that fills detection_results.
        # `evaluate` is the whole-population comparison and writes nothing there,
        # so using it here left a fresh install with zero results and no alerts.
        if not run(
            "Running detection (this is the slow step)",
            ["-m", "src.cli", "detect", "--start-week", "21",
             "--end-week", str(args.weeks)],
        ):
            return 1

    # 5. Alerts. --no-ollama keeps setup fast and dependency-free; the LLM
    # phrasing is a refinement, not a requirement.
    if count("alerts") > 0 and not args.fresh:
        print("\n>>> Alerts\n" + "-" * 62)
        print(f"  already generated ({count('alerts'):,} alerts)")
    elif not run("Generating alerts", ["-m", "src.cli", "alerts", "--no-ollama"]):
        return 1

    # 6. SMS, always mock
    if count("sms_messages") > 0 and not args.fresh:
        print("\n>>> SMS\n" + "-" * 62)
        print(f"  already dispatched ({count('sms_messages'):,} messages)")
    elif not run("Dispatching SMS (mock mode, nothing is really sent)",
                 ["-m", "src.cli", "sms"]):
        return 1

    # 7. Login
    print("\n>>> Login")
    print("-" * 62)
    ensure_user()

    # Summary, read back from the database
    print("\n" + "=" * 62)
    print(f"Ready in {time.time() - started:.0f}s")
    print("=" * 62)
    print(f"  people            {count('people'):>9,}")
    print(f"  reports           {count('reports'):>9,}")
    print(f"  detection results {count('detection_results'):>9,}")
    print(f"  alerts            {count('alerts'):>9,}")
    print(f"  SMS messages      {count('sms_messages'):>9,}")
    print()
    print("Start the website:")
    print("  python -m uvicorn src.web.main:app --reload")
    print()
    print("Then open http://127.0.0.1:8000 and sign in:")
    print(f"  username  {DEFAULT_USER}")
    print(f"  password  {DEFAULT_PASSWORD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
