"""One-time consolidation: walk every week once, parse every report, write consolidated store.

Reads the 312,000 raw .txt files exactly once and writes:
- SQLite table raw_reports (person_id, week_number, raw_text, parsed_date, parsed_infection, template_id)
- JSONL per week under data/consolidated/week_XXX.jsonl

Resumable: tracks completed weeks in results/consolidation_progress.json.
"""
import sys, json, re, time
from pathlib import Path
from datetime import date
from collections import Counter

sys.path.insert(0, ".")

from src.ingestion import (
    DATASET_ROOT,
    PEOPLE_FILE,
    DISEASES,
    WEEK_ZERO_REFERENCE,
    week_number_to_date,
    load_people_map,
)

ROOT = Path.cwd().resolve()
DATA_ROOT = ROOT / "data"
RESULTS_ROOT = ROOT / "results"
RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
CONSOLIDATED_ROOT = DATA_ROOT / "consolidated"
CONSOLIDATED_ROOT.mkdir(parents=True, exist_ok=True)

DB_PATH = ROOT / "epialert_demo.db"

MONTH_MAP = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

DISEASE_SET = {d.lower(): d for d in DISEASES}


def parse_report_text(text: str) -> dict:
    """Parse a single report text into structured fields.

    Returns dict with:
    - report_date: date object (extracted from text if possible, else None)
    - infection: str (disease name or 'No infection')
    - template: normalized template signature (for survey)
    - template_id: short id for the template
    """
    lower = text.lower()

    # Infection status
    infection = "No infection"
    for dl, dn in DISEASE_SET.items():
        if dl in lower:
            infection = dn
            break

    # Date extraction: supports multiple positions of "on DD Month YYYY"
    m = re.search(r"on\s+(\d{1,2})\s+(\w+)\s+(\d{4})", lower)
    report_date = None
    if m:
        day = int(m.group(1))
        month_name = m.group(2).lower()
        month = MONTH_MAP.get(month_name)
        year = int(m.group(3))
        if month is not None:
            try:
                report_date = date(year, month, day)
            except ValueError:
                report_date = None

    # Template signature: lower-case, collapse whitespace, mask dates and person ids
    sanitized = lower
    sanitized = re.sub(r"\d{1,2}\s+\w+\s+\d{4}", "<DATE>", sanitized)
    sanitized = re.sub(r"\bp\d{3,4}\b", "<PERSON>", sanitized)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    template = sanitized

    return {
        "report_date": report_date,
        "infection": infection,
        "template": template,
    }


def load_progress() -> dict:
    p = RESULTS_ROOT / "consolidation_progress.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"completed_weeks": [], "template_counts": {}, "total_files": 0, "elapsed_seconds": 0.0}


def save_progress(prog: dict) -> None:
    RESULTS_ROOT / "consolidation_progress.json"
    p = RESULTS_ROOT / "consolidation_progress.json"
    p.write_text(json.dumps(prog, indent=2), encoding="utf-8")


def consolidate(resume: bool = True) -> dict:
    people_map = load_people_map()

    if resume and (RESULTS_ROOT / "consolidation_progress.json").exists():
        prog = load_progress()
    else:
        prog = {"completed_weeks": [], "template_counts": {}, "total_files": 0, "elapsed_seconds": 0.0}
    completed = set(prog["completed_weeks"])
    template_counts = dict(prog.get("template_counts", {}))
    total_files = prog.get("total_files", 0)

    # Ensure schema + consolidated table
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("DROP TABLE IF EXISTS raw_reports")
    conn.execute(
        """
        CREATE TABLE raw_reports (
            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id VARCHAR(10) NOT NULL,
            week_number INT NOT NULL,
            raw_text TEXT NOT NULL,
            parsed_date DATE,
            parsed_infection VARCHAR(100),
            template_id VARCHAR(60),
            consolidated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(person_id, week_number)
        )
        """
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_person_week ON raw_reports(person_id, week_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_week ON raw_reports(week_number)")
    conn.commit()

    t0 = time.time()
    if prog.get("elapsed_seconds"):
        t0 -= prog["elapsed_seconds"]

    batch = []
    BATCH_SIZE = 2000

    week_dirs = sorted([d for d in DATASET_ROOT.iterdir() if d.name.startswith("week_") and d.is_dir()])
    for week_dir in week_dirs:
        try:
            week_num = int(week_dir.name.split("_")[1])
        except ValueError:
            continue
        if week_num < 1 or week_num > 105:
            continue
        if week_num in completed:
            continue

        reports_dir = week_dir / "reports"
        if not reports_dir.exists():
            continue

        jsonl_path = CONSOLIDATED_ROOT / f"week_{week_num:03d}.jsonl"
        file_count = 0
        parsed_count = 0
        quarantine_count = 0
        seen_this_week = set()

        with jsonl_path.open("a", encoding="utf-8") as jf, conn:
            for path in sorted(reports_dir.glob("*.txt")):
                total_files += 1
                file_count += 1
                text = path.read_text(encoding="utf-8").strip()
                person_id = path.stem.upper()
                parsed = parse_report_text(text)

                if person_id not in people_map:
                    # Quarantine unknown persons
                    quarantine_count += 1
                    continue

                parsed_date = parsed["report_date"].isoformat() if parsed["report_date"] else None
                template = parsed["template"]
                template_counts[template] = template_counts.get(template, 0) + 1

                # Write JSONL (avoid duplicate person entries per week)
                rec = {
                    "person_id": person_id,
                    "week_number": week_num,
                    "raw_text": text,
                    "parsed_date": parsed_date,
                    "parsed_infection": parsed["infection"],
                    "template": template,
                }
                if person_id in seen_this_week:
                    continue
                seen_this_week.add(person_id)
                jf.write(json.dumps(rec, ensure_ascii=False) + "\n")

                # DB batch

                # DB batch
                batch.append(
                    (
                        person_id,
                        week_num,
                        text,
                        parsed_date,
                        parsed["infection"],
                        template,
                    )
                )
                if len(batch) >= BATCH_SIZE:
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO raw_reports
                            (person_id, week_number, raw_text, parsed_date, parsed_infection, template_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        batch,
                    )
                    conn.commit()
                    batch.clear()

        # Flush remaining batch for this week
        if batch:
            conn.executemany(
                """
                INSERT OR IGNORE INTO raw_reports
                    (person_id, week_number, raw_text, parsed_date, parsed_infection, template_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                batch,
            )
            conn.commit()
            batch.clear()

        # Update progress file per week
        prog["completed_weeks"] = sorted(completed)
        prog["template_counts"] = template_counts
        prog["total_files"] = total_files
        save_progress(prog)

        completed.add(week_num)

        print(
            f"week {week_num:03d}: files={file_count} parsed={parsed_count or file_count} "
            f"quarantine={quarantine_count} total_files={total_files}"
        )

    elapsed = time.time() - t0
    prog["elapsed_seconds"] = elapsed
    save_progress(prog)

    # Write template survey report
    template_survey = []
    for template, count in sorted(template_counts.items(), key=lambda kv: -kv[1]):
        template_survey.append({"template": template, "count": count})
    (RESULTS_ROOT / "report_templates.json").write_text(
        json.dumps(template_survey, indent=2), encoding="utf-8"
    )

    print("consolidation complete")
    return {
        "total_files": total_files,
        "weeks_completed": len(completed),
        "distinct_templates": len(template_counts),
        "elapsed_seconds": round(elapsed, 2),
        "template_counts_top": sorted(template_counts.items(), key=lambda kv: -kv[1])[:10],
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", action="store_true", default=True)
    ap.add_argument("--fresh", action="store_true", default=False)
    args = ap.parse_args()

    if args.fresh:
        # Delete existing consolidated JSONL files to start fresh
        for p in CONSOLIDATED_ROOT.glob("*.jsonl"):
            p.unlink()
        print("fresh consolidation started")
    else:
        print("resumable consolidation started")

    result = consolidate(resume=not args.fresh)
    print(json.dumps(result, indent=2))
