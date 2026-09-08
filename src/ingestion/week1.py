"""EpiAlert Weekly Ingestion - Week 1 Module

Scaffold module for week 001 ingestion. Provides the foundation for
processing weekly report files and loading them into the database.
"""

from pathlib import Path
from datetime import date
import csv
from src.database.db import get_connection
from src.config import DB_CONFIG
from src.ingestion import (
    DATASET_ROOT, 
    PEOPLE_FILE, 
    DISEASES, 
    WEEK_ZERO_REFERENCE,
    week_number_to_date,
    load_people_map,
)


def parse_report(path: Path) -> tuple:
    """Parse a single week report .txt file.
    
    Expected format (from raw dataset):
    - "On 12 January 2025, P0001 from Street 1 in Village A was reported as having no infection."
    - "The health assessment on 12 January 2025 recorded P0001 of Street 1, Village A with no infection."
    - "No infection was reported for P0001 from Village A, Street 1 on 12 January 2025."
    
    Returns (person_id, report_date, infection_status, raw_text)
    """
    text = path.read_text(encoding="utf-8").strip()
    person = path.stem.upper()
    
    lower = text.lower()
    
    # Determine infection/disease status
    infection = None
    for d in DISEASES:
        if d.lower() in lower:
            infection = d
            break
    
    # If no specific disease found, default to "No infection"
    if infection is None:
        if "no infection" in lower or "without infection" in lower:
            infection = "No infection"
        else:
            infection = "No infection"
    
    # Extract date from text
    import re
    date_match = re.search(r'on\s+(\d{1,2}\s+\w+\s+\d{4})', lower)
    if date_match:
        date_str = date_match.group(1)
        month_map = {
            "january": "01", "february": "02", "march": "03", "april": "04",
            "may": "05", "june": "06", "july": "07", "august": "08",
            "september": "09", "october": "10", "november": "11", "december": "12"
        }
        try:
            month_num = month_map.get(date_str.split()[1].lower(), "01")
            day = date_str.split()[0]
            report_date = date(int(date_str.split()[2], 10), int(month_num), int(day))
        except (ValueError, IndexError):
            report_date = WEEK_ZERO_REFERENCE
    else:
        report_date = WEEK_ZERO_REFERENCE
    
    return person, report_date, infection, text


def ingest_week1() -> dict:
    """Ingest week 001 reports into the database.
    
    Processes all 3,000 report .txt files from data/EpiAlert_Phase1_Dataset/week_001/reports/
    and inserts them into the MySQL/SQLite reports table.
    
    Returns dict with ingestion statistics.
    """
    people = {}
    people_file = PEOPLE_FILE
    with open(people_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            people[row["person_id"]] = (row["village"], row["street"])
    
    conn = get_connection()
    cur = conn.cursor()
    reports_dir = DATASET_ROOT / "week_001" / "reports"
    
    # Clear existing week 001 data (for re-ingestion)
    cur.execute("DELETE FROM reports WHERE week_number = 1")
    
    sql = """INSERT INTO reports
    (person_id, village, street, report_date, infection, raw_text, week_number)
    VALUES (%s, %s, %s, %s, %s, %s, %s)"""
    
    count = 0
    for path in sorted(reports_dir.glob("*.txt")):
        person, report_date, infection, raw = parse_report(path)
        if person not in people:
            raise ValueError(f"Unknown person: {person}")
        village, street = people[person]
        cur.execute(sql, (
            person, village, street, report_date,
            infection, raw, 1
        ))
        count += 1
    
    conn.commit()
    cur.close()
    conn.close()
    print(f"Week 1: processed {count} reports.")
    return {"week": 1, "processed": count}


if __name__ == "__main__":
    result = ingest_week1()
    print(f"Result: {result}")