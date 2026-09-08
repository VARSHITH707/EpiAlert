"""Generate the EpiAlert synthetic dataset.

The full dataset is 312,000 weekly reports (~370 MB), far too large for a git
repository. This script regenerates it from the two small files that ARE in the
repo -- `people.csv` (who lives where) and `ground_truth.csv` (the outbreaks to
plant) -- so a fresh clone can run the whole system.

    python generate_dataset.py

The seed is fixed, so everyone who runs this gets byte-identical data and the
same detection results.

What it produces
----------------
104 weekly files in `data/consolidated/`, one JSON object per person per week:

    {"person_id": "P0001", "week_number": 1, "raw_text": "...",
     "parsed_date": "2025-01-12", "parsed_infection": "No infection",
     "template": "..."}

Reports are natural-language sentences in several phrasings, exactly as a
community health worker might write them. They contain only person, place,
date and infection status -- never case counts, statistics or alerts. Those are
what EpiAlert is supposed to work out for itself.

Dates
-----
Week 1 is Sunday 12 January 2025 and every week is the following Sunday.
Real calendar arithmetic, so leap years and month lengths are handled.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
DATASET_DIR = ROOT / "data" / "EpiAlert_Phase1_Dataset"
OUT_DIR = ROOT / "data" / "consolidated"

# Matches the original dataset so regenerated data reproduces its results.
SEED = 20260829
WEEK_ONE = date(2025, 1, 12)  # a Sunday
TOTAL_WEEKS = 104

# Background illness in a normal week, measured from the original data: 2.12%
# of people report some infection. Split by disease in the same proportions.
BASELINE_RATE = 0.0212
DISEASE_WEIGHTS = {
    "Influenza/ARI": 494,
    "Acute Gastroenteritis": 353,
    "Dengue": 240,
    "Malaria": 132,
    "Chikungunya": 56,
}

# During an outbreak, this fraction of an affected street reports the disease.
# Well above the ~2% background, so the rise is real but not absurd.
OUTBREAK_RATE = 0.09

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Several phrasings, so extraction is exercised against varied wording rather
# than one rigid format.
TEMPLATES_INFECTED = [
    "On {date}, {pid} from {street} in {village} was reported as having {disease}.",
    "The health assessment on {date} recorded {pid} of {street}, {village} with {disease}.",
    "{disease} was reported for {pid} from {village}, {street}, on {date}.",
    "On {date}, {pid} from {street} in {village} had {disease} reported.",
    "{pid} of {street}, {village} was assessed on {date} and found to have {disease}.",
]
TEMPLATES_HEALTHY = [
    "On {date}, {pid} from {street} in {village} was reported as having no infection.",
    "The health assessment on {date} recorded {pid} of {street}, {village} with no infection.",
    "No infection was reported for {pid} from {village}, {street}, on {date}.",
    "On {date}, {pid} from {street} in {village} had no infection reported.",
    "{pid} of {street}, {village} was assessed on {date} and found to have no infection.",
]


def week_date(week_num: int) -> date:
    """Sunday for a given week number. Week 1 is 12 January 2025."""
    return WEEK_ONE + timedelta(weeks=week_num - 1)


def format_date(d: date) -> str:
    """Render as '12 January 2025', the phrasing the reports use."""
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def make_template(text: str) -> str:
    """Template signature: lower-cased with the date and person id masked.

    Mirrors what src/consolidate.py produces, so generated data carries the
    same field the ingest expects.
    """
    import re

    s = text.lower()
    s = re.sub(r"\d{1,2}\s+\w+\s+\d{4}", "<DATE>", s)
    s = re.sub(r"\bp\d{3,4}\b", "<PERSON>", s)
    return re.sub(r"\s+", " ", s).strip()


def load_people() -> list[dict]:
    """Read people.csv, which is small enough to live in the repository."""
    path = DATASET_DIR / "people.csv"
    if not path.exists():
        sys.exit(
            f"Cannot find {path}.\n"
            "people.csv ships with the repository -- if it is missing, the "
            "clone is incomplete."
        )
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_outbreaks() -> list[dict]:
    """Read ground_truth.csv: the outbreaks to plant.

    EpiAlert itself never reads this file. It exists so the generator knows
    what to create and so evaluation can score detections afterwards.
    """
    path = DATASET_DIR / "ground_truth.csv"
    if not path.exists():
        sys.exit(f"Cannot find {path}. The clone is incomplete.")

    events = []
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            events.append({
                "village": row["village"].strip(),
                "disease": row["disease"].strip(),
                "start": int(row["start_week"]),
                "end": int(row["end_week"]),
                "streets": {s.strip() for s in row["affected_streets"].split(";")},
            })
    return events


def outbreak_for(events: list[dict], week: int, village: str, street: str):
    """Return the disease affecting this street this week, or None."""
    for ev in events:
        if (ev["start"] <= week <= ev["end"]
                and ev["village"] == village
                and street in ev["streets"]):
            return ev["disease"]
    return None


def pick_background_disease(rng: random.Random) -> str:
    """Pick a disease for ordinary background illness."""
    return rng.choices(
        list(DISEASE_WEIGHTS), weights=list(DISEASE_WEIGHTS.values())
    )[0]


def build_week(week: int, people: list[dict], events: list[dict],
               rng: random.Random) -> list[dict]:
    """Build one week of reports, one per person."""
    d = week_date(week)
    date_text = format_date(d)
    rows = []

    for person in people:
        pid = person["person_id"]
        village = person["village"]
        street = person["street"]

        outbreak_disease = outbreak_for(events, week, village, street)
        if outbreak_disease and rng.random() < OUTBREAK_RATE:
            disease = outbreak_disease
        elif rng.random() < BASELINE_RATE:
            disease = pick_background_disease(rng)
        else:
            disease = None

        if disease:
            text = rng.choice(TEMPLATES_INFECTED).format(
                date=date_text, pid=pid, street=street,
                village=village, disease=disease.lower(),
            )
        else:
            text = rng.choice(TEMPLATES_HEALTHY).format(
                date=date_text, pid=pid, street=street, village=village,
            )

        rows.append({
            "person_id": pid,
            "week_number": week,
            "raw_text": text,
            "parsed_date": d.isoformat(),
            "parsed_infection": disease or "No infection",
            "template": make_template(text),
        })

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the EpiAlert synthetic dataset."
    )
    parser.add_argument(
        "--weeks", type=int, default=TOTAL_WEEKS,
        help=f"How many weeks to generate (default {TOTAL_WEEKS}).",
    )
    args = parser.parse_args()

    if not 1 <= args.weeks <= 520:
        sys.exit("--weeks must be between 1 and 520.")

    rng = random.Random(SEED)
    people = load_people()
    events = load_outbreaks()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Generating {args.weeks} weeks for {len(people):,} people")
    print(f"  seed        : {SEED} (fixed, so everyone gets identical data)")
    print(f"  outbreaks   : {len(events)} planted from ground_truth.csv")
    print(f"  output      : {OUT_DIR}")
    print()

    total = 0
    infected = 0
    for week in range(1, args.weeks + 1):
        rows = build_week(week, people, events, rng)
        path = OUT_DIR / f"week_{week:03d}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        total += len(rows)
        infected += sum(
            1 for r in rows if r["parsed_infection"] != "No infection"
        )
        if week % 10 == 0 or week == args.weeks:
            print(f"  week {week:3d}/{args.weeks}  ({total:,} reports)")

    print()
    print(f"Done. {total:,} reports, {infected:,} reporting infection "
          f"({100 * infected / total:.2f}%).")
    print()
    print("Next:")
    print("  python -m src.cli ingest-all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
