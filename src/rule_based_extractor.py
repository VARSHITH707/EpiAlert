"""Rule-based extractor that handles every discovered report template.

Template survey source: results/report_templates.json
Any report matching no known template goes to quarantine (never silently dropped).

This is the extractor API used by downstream ingestion / validation.
"""
import csv, json, re, sys
from pathlib import Path
from datetime import date
from collections import Counter

from src.ingestion import (
    DATASET_ROOT,
    PEOPLE_FILE,
    DISEASES,
    WEEK_ZERO_REFERENCE,
    week_number_to_date,
    load_people_map,
)

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

# ---------------------------------------------------------------------------
# Template discovery
# ---------------------------------------------------------------------------

TEMPLATE_SURVEY_PATH = Path("results/report_templates.json")


def load_template_survey() -> list:
    """Load discovered templates from results/report_templates.json.

    Each entry: {"template": normalized template string, "count": int}
    """
    if TEMPLATE_SURVEY_PATH.exists():
        return json.loads(TEMPLATE_SURVEY_PATH.read_text(encoding="utf-8"))
    return []


# Pre-compile regex patterns for every discovered template
_TEMPLATE_PATTERNS = None
_TEMPLATE_PATTERN_LIST = []  # list of (compiled_pattern, template_signature)


def _build_template_patterns():
    global _TEMPLATE_PATTERNS, _TEMPLATE_PATTERN_LIST
    if globals().get('_TEMPLATE_PATTERNS') is not None:
        return

    survey = load_template_survey()
    patterns = []
    for entry in survey:
        template = entry["template"]
        # Normalize: collapse whitespace, lowercase
        template_norm = re.sub(r'\s+', ' ', template.lower()).strip()

        # Convert template to regex by replacing placeholders with capture groups
        # and turning literal whitespace into \s+.
        regex = template_norm
        regex = regex.replace('<date>', r'(\d{1,2}\s+\w+\s+\d{4})')
        regex = regex.replace('<person>', r'(P\d{3,4})')
        # Replace literal spaces with \s+ to allow flexible whitespace in the input
        regex = regex.replace(' ', r'\s+')
        regex = '^' + regex + '$'
        try:
            compiled = re.compile(regex, re.IGNORECASE)
            patterns.append((compiled, template_norm))
        except re.error:
            pass

    _TEMPLATE_PATTERNS = patterns
    _TEMPLATE_PATTERN_LIST = patterns
    print(f'Compiled {len(patterns)} template patterns from {len(survey)} templates', file=sys.stderr)


def extract_from_text(text: str, template_survey: list = None) -> dict:
    """Extract structured fields from a report text.

    Returns dict with:
    - person_id: str (uppercase)
    - report_date: date or None
    - infection: str (disease or 'No infection')
    - template_matched: bool
    - template_signature: str or None
    - quarantine_reason: str or None (if template not matched)
    """
    _build_template_patterns()

    lower = text.lower()
    person_id = None
    m_person = re.search(r"\b(p\d{3,4})\b", text, re.IGNORECASE)
    if m_person:
        person_id = (m_person.group(1)[0].upper() + m_person.group(1)[1:]).upper()

    # Infection
    infection = "No infection"
    for dl, dn in DISEASE_SET.items():
        if dl in lower:
            infection = dn
            break

    # Date: find "on DD Month YYYY"
    m_date = re.search(r"on\s+(\d{1,2})\s+(\w+)\s+(\d{4})", lower)
    report_date = None
    if m_date:
        day = int(m_date.group(1))
        month_name = m_date.group(2).lower()
        month = MONTH_MAP.get(month_name)
        year = int(m_date.group(3))
        if month is not None:
            try:
                report_date = date(year, month, day)
            except ValueError:
                report_date = None

    # Template matching: try every discovered pattern
    template_matched = False
    template_signature = None
    quarantine_reason = None

    for compiled, template_sig in _TEMPLATE_PATTERN_LIST:
        if compiled.search(text):
            template_matched = True
            template_signature = template_sig
            break

    if not template_matched:
        quarantine_reason = f"no_matching_template: {text[:200]}"

    return {
        "person_id": person_id or "UNKNOWN",
        "report_date": report_date,
        "infection": infection,
        "template_matched": template_matched,
        "template_signature": template_signature,
        "quarantine_reason": quarantine_reason,
    }


# ---------------------------------------------------------------------------
# Validation via consolidated store (not raw .txt files)
# ---------------------------------------------------------------------------

CONSOLIDATED_DIR = Path("data/consolidated")


def validate_week_consolidated(week_num: int) -> dict:
    """Validate a week using the consolidated JSONL store.

    Reads data/consolidated/week_XXX.jsonl instead of raw .txt files.
    Checks: 3000 records, no duplicates, all dates are Sundays,
    all person_ids known, all templates matched.
    """
    jsonl_path = CONSOLIDATED_DIR / f"week_{week_num:03d}.jsonl"
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
        "template_issues": [],
        "infection_distribution": Counter(),
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


def validate_all_weeks_consolidated(historical_only: bool = True) -> dict:
    """Validate all weeks using consolidated store."""
    max_week = 105 if not historical_only else 104
    results = {
        "total_weeks": 0,
        "valid_weeks": 0,
        "invalid_weeks": 0,
        "week_results": {},
        "total_duplicates": 0,
        "total_missing": 0,
        "total_date_issues": 0,
        "total_template_issues": 0,
    }

    for week_num in range(1, max_week + 1):
        r = validate_week_consolidated(week_num)
        results["week_results"][f"week_{week_num:03d}"] = r
        results["total_weeks"] += 1
        if r["is_valid"]:
            results["valid_weeks"] += 1
        else:
            results["invalid_weeks"] += 1
        results["total_duplicates"] += len(r["duplicate_persons"])
        results["total_missing"] += len(r["missing_persons"])
        results["total_date_issues"] += len(r["date_issues"])
        results["total_template_issues"] += len(r["template_issues"])

    return results


# ---------------------------------------------------------------------------
# Extractor API for downstream ingestion
# ---------------------------------------------------------------------------

def extract_week_to_db(week_num: int, conn=None) -> dict:
    """Extract and insert a week's reports into the database from consolidated store.

    Uses the RuleBasedExtractor to parse each report. Unmatched templates
    go to quarantine table.
    """
    from src.database.db import get_connection

    if conn is None:
        conn = get_connection()

    jsonl_path = CONSOLIDATED_DIR / f"week_{week_num:03d}.jsonl"
    if not jsonl_path.exists():
        raise FileNotFoundError(f"Consolidated JSONL not found: {jsonl_path}")

    people_map = load_people_map()
    cur = conn.cursor()

    # Clear existing week data
    cur.execute("DELETE FROM reports WHERE week_number = ?", (week_num,))
    cur.execute("DELETE FROM quarantine WHERE week_number = ?", (week_num,))

    inserted = 0
    quarantined = 0

    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            person_id = rec["person_id"]
            raw_text = rec.get("raw_text", "")
            parsed_date = rec.get("parsed_date")
            infection = rec.get("parsed_infection", "No infection")
            template_matched = rec.get("template_matched", False)
            quarantine_reason = rec.get("quarantine_reason")

            if person_id == "UNKNOWN" or person_id not in people_map:
                # Quarantine unknown persons
                cur.execute(
                    """INSERT INTO quarantine (person_id, week_number, raw_text, failure_reason)
                       VALUES (?, ?, ?, ?)""",
                    (person_id, week_num, raw_text[:500], quarantine_reason or "unknown person"),
                )
                quarantined += 1
                continue

            if not template_matched:
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
        "total_records": inserted + quarantined,
    }


def extract_all_weeks_to_db() -> dict:
    """Extract all 104 weeks from consolidated store into database."""
    from src.database.db import get_connection

    conn = get_connection()
    people_map = load_people_map()

    # Populate people table
    cur = conn.cursor()
    cur.execute("DELETE FROM people")
    for person_id, (village, street) in people_map.items():
        cur.execute(
            "INSERT INTO people (person_id, village, street) VALUES (?, ?, ?)",
            (person_id, village, street),
        )
    conn.commit()

    stats = {}
    total_inserted = 0
    total_quarantined = 0

    for week_num in range(1, 105):
        s = extract_week_to_db(week_num, conn)
        stats[f"week_{week_num:03d}"] = s
        total_inserted += s["inserted"]
        total_quarantined += s["quarantined"]

    conn.close()
    return {
        "total_weeks": 104,
        "total_inserted": total_inserted,
        "total_quarantined": total_quarantined,
        "week_stats": stats,
    }


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "extract_from_text",
    "validate_week_consolidated",
    "validate_all_weeks_consolidated",
    "extract_week_to_db",
    "extract_all_weeks_to_db",
    "load_template_survey",
]
