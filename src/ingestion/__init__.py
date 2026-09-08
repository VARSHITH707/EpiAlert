"""EpiAlert Weekly Ingestion Module (refactored to read consolidated store)

Handles loading, validating, and preprocessing weekly disease surveillance data.

Now reads from:
- Consolidated JSONL store: data/consolidated/week_XXX.jsonl (preferred)
- SQLite raw_reports table: epialert_demo.db.raw_reports (fallback)

Rules (unchanged from dataset README):
- 3,000 people per week, exactly one record per person per week
- Everyone in a week is checked on the SAME Sunday
- Week 001 = 12 January 2025
- Week 104 = 3 January 2027
- Week 105 = 10 January 2027
- Weekly reference/check-in date is Sunday
- Do NOT assign different check-in dates to different people within the skills
"""

from pathlib import Path
from datetime import date, timedelta
import csv
from collections import Counter, defaultdict
import json

from src.database.db import get_connection
from src.config import DB_CONFIG

# ---------------------------------------------------------------------------
# Date computation
# ---------------------------------------------------------------------------

WEEK_ZERO_REFERENCE = date(2025, 1, 12)  # Week 001 = 12 January 2025 (Sunday)


def week_number_to_date(week_num: int) -> date:
    """Convert week number (1-based) to its Sunday reference date.

    Week 001 -> 2025-01-12, Week 002 -> 2025-01-19, etc.
    """
    delta_weeks = week_num - 1
    return WEEK_ZERO_REFERENCE + timedelta(weeks=delta_weeks)


def week_date_to_number(dt: date) -> int:
    """Convert a Sunday date back to week number (1-based)."""
    delta_weeks = (dt - WEEK_ZERO_REFERENCE).days // 7
    return delta_weeks + 1


# ---------------------------------------------------------------------------
# Dataset structure
# ---------------------------------------------------------------------------

DATASET_ROOT = Path("data/EpiAlert_Phase1_Dataset")

PEOPLE_FILE = DATASET_ROOT.parent / "EpiAlert_Phase1_Dataset" / "people.csv"
CONSOLIDATED_JSONL_DIR = Path("data/consolidated")


def _ensure_people_map_loaded():
    """Lazily load people map once and cache it."""
    global _PEOPLE_MAP_CACHE, _PEOPLE_MAP_CACHE_LOADED_FROM
    if _PEOPLE_MAP_CACHE is not None and _PEOPLE_MAP_CACHE_LOADED_FROM == PEOPLE_FILE:
        return
    people = {}
    with open(PEOPLE_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            people[row["person_id"]] = (row["village"], row["street"])
    _PEOPLE_MAP_CACHE = people
    _PEOPLE_MAP_CACHE_LOADED_FROM = PEOPLE_FILE


_PEOPLE_MAP_CACHE = None
_PEOPLE_MAP_CACHE_LOADED_FROM = None


def load_people_map() -> dict:
    """Load the people.csv mapping person_id -> (village, street).

    Returns dict mapping person_id -> (village, street).
    This is the same for every week since all people are in the population.
    Caches after first load for performance.
    """
    _ensure_people_map_loaded()
    return _PEOPLE_MAP_CACHE


# ---------------------------------------------------------------------------
# Weekly report parsing (now from consolidated store)
# ---------------------------------------------------------------------------

DISEASES = ["Dengue", "Malaria", "Chikungunya", "Influenza/ARI", "Acute Gastroenteritis"]


def parse_report_txt(text: str) -> tuple:
    """Parse a single report text (extracted from consolidated store).

    Expected format (from raw dataset):
    - "On 12 January 2025, P0001 from Street 1 in Village A was reported as having no infection."
    - "The health assessment on 12 January 2025 recorded P0001 of Street 1, Village A with no infection."
    - "No infection was reported for P0001 from Village A, Street 1 on 12 January 2025."
    - "P0001, from Village A, Street 1, was recorded with no reported infection on 03 January 2027."

    Returns (person_id, report_date, infection_status, raw_text)
    """
    text = text.strip()
    person = None
    import re
    m_person = re.search(r"\b(P\d{3,4})\b", text, re.IGNORECASE)
    if m_person:
        person = m_person.group(1).upper()

    lower = text.lower()

    infection = None
    for d in DISEASES:
        if d.lower() in lower:
            infection = d
            break

    if infection is None:
        if "no infection" in lower or "without infection" in lower:
            infection = "No infection"
        else:
            infection = "No infection"

    # Extract date
    date_match = re.search(r"on\s+(\d{1,2}\s+\w+\s+\d{4})", lower)
    if date_match:
        date_str = date_match.group(1)
        month_map = {
            "january": "01",
            "february": "02",
            "march": "03",
            "april": "04",
            "may": "05",
            "june": "06",
            "july": "07",
            "august": "08",
            "september": "09",
            "october": "10",
            "november": "11",
            "december": "12",
        }
        try:
            month_num = month_map.get(date_str.split()[1].lower(), "01")
            day = date_str.split()[0]
            report_date = date(int(date_str.split()[2]), int(month_num), int(day))
        except (ValueError, IndexError):
            report_date = WEEK_ZERO_REFERENCE
    else:
        report_date = WEEK_ZERO_REFERENCE

    return person, report_date, infection, text


def load_week_from_jsonl(week_num: int) -> dict:
    """Load a week's data from the consolidated JSONL store.

    Returns dict with:
    - week_number: int
    - date: reference Sunday date
    - people: list of dicts with person_id, village, street, infection, raw_text
    - infection_distribution: Counter of infection statuses
    - person_count: int
    """
    jsonl_path = CONSOLIDATED_JSONL_DIR / f"week_{week_num:03d}.jsonl"
    if not jsonl_path.exists():
        raise FileNotFoundError(f"Consolidated JSONL not found: {jsonl_path}")

    people_map = load_people_map()
    people = []
    infection_dist = Counter()
    seen = set()

    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            person_id = rec.get("person_id")
            if person_id in seen:
                continue  # skip duplicates
            seen.add(person_id)

            raw_text = rec.get("raw_text", "")
            parsed_date_str = rec.get("parsed_date")
            infection = rec.get("parsed_infection", "No infection")
            template = rec.get("template", "")

            # Validate person exists
            if person_id not in people_map:
                continue  # unknown person, skip

            village, street = people_map[person_id]
            report_date = date.fromisoformat(parsed_date_str) if parsed_date_str else week_number_to_date(week_num)

            people.append({
                "person_id": person_id,
                "village": village,
                "street": street,
                "infection": infection,
                "raw_text": raw_text,
                "report_date": report_date,
            })
            infection_dist[infection] += 1

    total_people = len(people)
    infection_rate = sum(1 for p in people if p["infection"] != "No infection") / total_people if total_people else 0.0

    return {
        "week_number": week_num,
        "date": week_number_to_date(week_num),
        "people": people,
        "infection_distribution": dict(infection_dist),
        "infection_rate": infection_rate,
        "person_count": total_people,
    }


def load_week_from_db(week_num: int, disease: str = None) -> dict:
    """Load a week's data from the SQLite database (raw_reports table).

    Returns aggregated data for a specific week, optionally filtered by disease.

    Structure:
    {
        "week_number": week,
        "date": reference_Sunday_date,
        "total_people": count,
        "disease_counts": {disease: count, ...},
        "infection_rate": float,  # proportion with infection
        "people": [
            {
                "person_id": str,
                "village": str,
                "street": str,
                "infected": bool,  # True if has specific disease
                "disease": str | None,
            }, ...
        ],
    }
    """
    conn = get_connection()
    cur = conn.cursor()

    week_date = week_number_to_date(week_num)

    if disease and disease != "No infection":
        cur.execute(
            "SELECT person_id, infection FROM reports WHERE week_number = ? AND infection = ?",
            (week_num, disease),
        )
    else:
        cur.execute("SELECT person_id, infection FROM reports WHERE week_number = ?", (week_num,))

    rows = cur.fetchall()

    people = []
    disease_counts = Counter()
    total_infected = 0

    for row in rows:
        person_id = row[0]
        infection = row[1] or "No infection"

        is_infected = infection != "No infection"
        if is_infected:
            total_infected += 1
            disease_counts[infection] += 1

        people.append({
            "person_id": person_id,
            "infected": is_infected,
            "disease": infection,
        })

    infection_rate = total_infected / len(people) if people else 0.0

    # Sort people by disease status then by person_id for deterministic order
    people.sort(key=lambda p: (not p["infected"], p["person_id"]))

    cur.close()
    conn.close()

    return {
        "week_number": week_num,
        "date": week_date,
        "total_people": len(people),
        "disease_counts": dict(disease_counts),
        "infection_rate": infection_rate,
        "people": people,
    }


def get_weekly_aggregation(week_number: int, disease: str = None) -> dict:
    """Aggregate weekly surveillance data.

    Tries consolidated JSONL store first, falls back to database.
    """
    try:
        return load_week_from_jsonl(week_number)
    except FileNotFoundError:
        return load_week_from_db(week_number, disease)


# ---------------------------------------------------------------------------
# Core validation functions (now using consolidated store)
# ---------------------------------------------------------------------------

def validate_week_data(
    week_num: int,
    expected_people: int = 3000,
    tolerance: int = 0
) -> dict:
    """Validate data for a single week using consolidated store.

    Checks:
    1. Expected number of report files exist (in JSONL)
    2. Every person has exactly one record
    3. No duplicate records for same person
    4. All dates are valid Sundays
    5. Week identifier is correct
    6. All required fields present

    Returns dict with validation results including any errors.
    """
    jsonl_path = CONSOLIDATED_JSONL_DIR / f"week_{week_num:03d}.jsonl"

    result = {
        "week_num": week_num,
        "is_valid": True,
        "errors": [],
        "warnings": [],
        "person_count": 0,
        "duplicate_persons": [],
        "missing_persons": [],
        "unknown_person_files": [],
        "date_issues": [],
        "infection_distribution": Counter(),
        "template_issues": [],
    }

    if not jsonl_path.exists():
        result["is_valid"] = False
        result["errors"].append(f"Consolidated JSONL not found: {jsonl_path}")
        return result

    people_map = load_people_map()
    seen = {}
    expected_date = week_number_to_date(week_num)
    person_count = 0

    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            person_id = rec.get("person_id")
            raw_text = rec.get("raw_text", "")
            parsed_date_str = rec.get("parsed_date")
            template = rec.get("template", "")
            infection = rec.get("parsed_infection", "No infection")

            # The consolidated store saved the normalized template signature.
            # We treat a non-empty template field as matched.
            template_matched = bool(template)
            template_sig = template if template else None
            parsed_date = parsed_date_str

            person_count += 1

            if not template_matched:
                result["template_issues"].append({
                    "person_id": person_id,
                    "week": week_num,
                    "reason": "no_template_in_consolidated_store",
                })

            if person_id in seen:
                result["duplicate_persons"].append(person_id)
                continue

            seen[person_id] = True

            # Date validation: parsed date must be Sunday and match expected week
            if parsed_date:
                try:
                    rd = date.fromisoformat(parsed_date)
                    if rd.weekday() != 6:
                        result["date_issues"].append({
                            "person_id": person_id,
                            "date": parsed_date,
                            "weekday": rd.strftime("%A"),
                        })
                    if rd != expected_date:
                        result["date_issues"].append({
                            "person_id": person_id,
                            "date": parsed_date,
                            "expected": expected_date.isoformat(),
                        })
                except ValueError:
                    result["date_issues"].append({
                        "person_id": person_id,
                        "date": parsed_date,
                        "reason": "invalid date format",
                    })
            else:
                result["date_issues"].append({
                    "person_id": person_id,
                    "date": None,
                    "reason": "missing parsed date",
                })

            # Person validation
            if person_id not in people_map:
                result["unknown_person_files"].append(person_id)
            else:
                # Track infection distribution from parsed infection
                if infection:
                    result["infection_distribution"][infection] += 1

    result["person_count"] = person_count
    expected_person_ids = set(people_map.keys())
    reported_person_ids = set(seen.keys())
    missing = expected_person_ids - reported_person_ids
    result["missing_persons"] = sorted(missing)
    if missing:
        result["warnings"].append(f"Missing {len(missing)} people from consolidated store")

    if result["duplicate_persons"]:
        result["is_valid"] = False
        result["errors"].append(f"Found {len(result['duplicate_persons'])} duplicate persons")

    if result["template_issues"]:
        result["is_valid"] = False
        result["errors"].append(f"Found {len(result['template_issues'])} template issues")

    if result["date_issues"]:
        result["warnings"].append(f"{len(result['date_issues'])} date issues")

    return result


def validate_all_weeks(
    historical_only: bool = True,
    expected_people: int = 3000
) -> dict:
    """Validate all historical weeks (or up to week_105 if demo mode).

    Returns aggregated validation results.
    """
    results = {
        "total_weeks": 0,
        "valid_weeks": 0,
        "invalid_weeks": 0,
        "week_results": {},
        "total_duplicates": 0,
        "total_missing": 0,
        "total_date_issues": 0,
    }

    max_week = 105 if not historical_only else 104

    for week_num in range(1, max_week + 1):
        result = validate_week_data(week_num, expected_people)
        results["week_results"][f"week_{week_num:03d}"] = result
        results["total_weeks"] += 1

        if result["is_valid"]:
            results["valid_weeks"] += 1
        else:
            results["invalid_weeks"] += 1

        results["total_duplicates"] += len(result["duplicate_persons"])
        results["total_missing"] += len(result["missing_persons"])
        results["total_date_issues"] += len(result["date_issues"])

    return results


# ---------------------------------------------------------------------------
# Ingestion pipeline (now reads from consolidated store)
# ---------------------------------------------------------------------------

def ingest_week_to_database(week_num: int, conn=None) -> dict:
    """Ingest a single week's data into the database from consolidated store.

    Processes the consolidated JSONL file for the given week and inserts
    into the reports table.

    Returns dict with ingestion statistics.
    """
    if conn is None:
        conn = get_connection()

    jsonl_path = CONSOLIDATED_JSONL_DIR / f"week_{week_num:03d}.jsonl"
    if not jsonl_path.exists():
        raise FileNotFoundError(f"Consolidated JSONL not found: {jsonl_path}")

    people_map = load_people_map()
    cur = conn.cursor()

    # Clear existing week data
    cur.execute("DELETE FROM reports WHERE week_number = ?", (week_num,))
    cur.execute("DELETE FROM quarantine WHERE week_number = ?", (week_num,))

    inserted = 0
    quarantined = 0
    seen = set()

    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            person_id = rec.get("person_id")
            raw_text = rec.get("raw_text", "")
            parsed_date = rec.get("parsed_date")
            infection = rec.get("parsed_infection", "No infection")
            template = rec.get("template", "")
            quarantine_reason = rec.get("quarantine_reason")

            # Skip duplicates
            if person_id in seen:
                continue
            seen.add(person_id)

            # Validate person exists
            if person_id not in people_map:
                cur.execute(
                    """INSERT INTO quarantine (person_id, week_number, raw_text, failure_reason)
                       VALUES (?, ?, ?, ?)""",
                    (person_id, week_num, raw_text[:500], quarantine_reason or "unknown person"),
                )
                quarantined += 1
                continue

            # Check template matched (consolidated store always has template if parsing succeeded)
            if not template:
                cur.execute(
                    """INSERT INTO quarantine (person_id, week_number, raw_text, failure_reason)
                       VALUES (?, ?, ?, ?)""",
                    (person_id, week_num, raw_text[:500], quarantine_reason or "no matching template"),
                )
                quarantined += 1
                continue

            village, street = people_map[person_id]
            report_date = parsed_date if parsed_date else week_number_to_date(week_num).isoformat()

            cur.execute(
                """INSERT INTO reports (person_id, village, street, report_date, infection, raw_text, week_number)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (person_id, village, street, report_date, infection, raw_text, week_num),
            )
            inserted += 1

    conn.commit()
    cur.close()

    return {
        "week": week_num,
        "inserted": inserted,
        "quarantined": quarantined,
        "total_files": len(list(jsonl_path.read_text().splitlines())),
    }


def ingest_all_historical() -> dict:
    """Ingest all 104 historical weeks from consolidated store into database.

    Returns aggregate statistics.
    """
    # Create the tables first. On a fresh clone nothing has built the schema
    # yet, and this used to fail with "no such table: reports" -- an opaque
    # error for the very first command a new user runs. init_schema uses
    # CREATE TABLE IF NOT EXISTS throughout, so calling it is safe every time.
    from src.database.db import init_schema
    init_schema()

    conn = get_connection()

    # Ensure people table is populated
    people_map = load_people_map()
    cur = conn.cursor()

    # Clear existing data for fresh ingestion
    cur.execute("DELETE FROM reports")
    cur.execute("DELETE FROM people")

    # Insert all people, each with a phone number.
    #
    # Assigning the number here rather than in a separate step means the people
    # table is never left half-built: without a number, SMS dispatch finds no
    # recipients and silently sends nothing, which looks like a broken alert
    # engine rather than a missing column.
    #
    # The seed is fixed, so the same person always gets the same number and
    # results are reproducible. Numbers come from configuration, never from
    # source -- see ALERT_PHONE_NUMBERS in src/config.py.
    import random as _random

    from src.config import ALERT_PHONE_NUMBERS

    rng = _random.Random(42)
    person_sql = (
        "INSERT INTO people (person_id, village, street, phone_number) "
        "VALUES (?, ?, ?, ?)"
    )
    for person_id, (village, street) in people_map.items():
        phone = rng.choice(ALERT_PHONE_NUMBERS) if ALERT_PHONE_NUMBERS else None
        cur.execute(person_sql, (person_id, village, street, phone))

    conn.commit()

    # Ingest whatever weeks actually exist rather than assuming 104. Hardcoding
    # the count made a smaller dataset fail with "Consolidated JSONL not found:
    # week_031.jsonl" -- which reads like corruption when in fact the data is
    # simply shorter, as it is when someone runs `setup.py --weeks 30`.
    available = sorted(
        int(p.stem.split("_")[1])
        for p in CONSOLIDATED_JSONL_DIR.glob("week_*.jsonl")
    )
    if not available:
        raise FileNotFoundError(
            f"No consolidated weeks found in {CONSOLIDATED_JSONL_DIR}. "
            "Run `python generate_dataset.py` first."
        )

    all_stats = {}
    total_inserted = 0
    total_quarantined = 0

    for week_num in available:
        stats = ingest_week_to_database(week_num, conn)
        all_stats[f"week_{week_num:03d}"] = stats
        total_inserted += stats["inserted"]
        total_quarantined += stats["quarantined"]

    conn.commit()
    cur.close()
    conn.close()

    return {
        # The number of weeks actually ingested, not a hardcoded 104. Reporting
        # 104 after ingesting 30 is a false report of what happened.
        "total_weeks": len(available),
        "weeks": available,
        "total_inserted": total_inserted,
        "total_quarantined": total_quarantined,
        "week_stats": all_stats,
    }


# ---------------------------------------------------------------------------
# Week 105 demo ingestion (from consolidated store if available)
# ---------------------------------------------------------------------------

def ingest_week_105() -> dict:
    """Ingest week_105 as new incoming data for live/demo mode.

    Week 105 is NOT part of the 104-week historical evaluation.
    It represents new incoming data that the system processes
    to generate current alerts.

    Reads from consolidated store if available, otherwise creates placeholder.
    """
    jsonl_path = CONSOLIDATED_JSONL_DIR / "week_105.jsonl"

    if not jsonl_path.exists():
        # Create placeholder week_105 data
        week_105_dir = DATASET_ROOT.parent / "EpiAlert_Phase1_Dataset" / "week_105"
        week_105_reports = week_105_dir / "reports"
        week_105_reports.mkdir(parents=True, exist_ok=True)

        people_map = load_people_map()
        for person_id in sorted(people_map.keys()):
            village, street = people_map[person_id]
            report_text = (
                f"The health assessment on 10 January 2027 recorded "
                f"{person_id} of {street}, {village} with no infection.\n"
            )
            report_path = week_105_reports / f"{person_id}.txt"
            if not report_path.exists():
                report_path.write_text(report_text, encoding="utf-8")

    # Ingest from database
    stats = ingest_week_to_database(105)
    stats["week_105_demo"] = True

    return stats


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    'week_number_to_date',
    'week_date_to_number',
    'validate_week_data',
    'validate_all_weeks',
    'ingest_week_to_database',
    'ingest_all_historical',
    'ingest_week_105',
    'parse_report_txt',
    'load_people_map',
    'load_week_from_jsonl',
    'load_week_from_db',
    'get_weekly_aggregation',
    'DISEASES',
    'WEEK_ZERO_REFERENCE',
    'CONSOLIDATED_JSONL_DIR',
]
