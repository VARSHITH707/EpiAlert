"""Validation and quarantine for extracted records.

Each extracted record passes through 7 validation rules. Any failure
quarantines the record with its raw text retained for audit, and excludes
it from detection until a human resolves it.

Rules:
  1. Record has required structure (person_id present)
  2. Person exists in the people registry
  3. Village exists in the registry
  4. Street exists in the registry
  5. Person-village relationship matches the registry
  6. Person-street relationship matches the registry
  7. Reporting date parses and is a valid date
  8. Reporting date is a Sunday (per dataset convention)
  9. Disease is in the controlled vocabulary (or None for no-infection)

CRITICAL: 9 of 10 street names appear in multiple villages. Therefore
validation rule 6 must check (person_id, street) against the registry
using the FULL composite key, never street alone.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from src.extraction.schema import ExtractedRecord

# Dataset paths
DATASET_ROOT = Path("data/EpiAlert_Phase1_Dataset")
PEOPLE_FILE = DATASET_ROOT.parent / "EpiAlert_Phase1_Dataset" / "people.csv"
PEOPLE_ENRICHED_FILE = DATASET_ROOT.parent / "EpiAlert_Phase1_Dataset" / "people_enriched.csv"

# Controlled vocabulary of disease terms
ALLOWED_DISEASES = {
    "Dengue", "Malaria", "Chikungunya",
    "Influenza/ARI", "Acute Gastroenteritis",
}

# Cache for registry lookups (loaded once)
_registry: Optional[dict] = None


def _load_registry() -> dict:
    """Load the people registry into memory.

    Returns dict mapping person_id -> (village, street, phone_number).
    Uses enriched file if available, falls back to original.
    """
    global _registry
    if _registry is not None:
        return _registry

    source = PEOPLE_ENRICHED_FILE if PEOPLE_ENRICHED_FILE.exists() else PEOPLE_FILE
    registry = {}
    with open(source, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pid = row["person_id"].strip().upper()
            village = row["village"].strip()
            street = row["street"].strip()
            phone = row.get("phone_number", "").strip() or None
            registry[pid] = (village, street, phone)

    _registry = registry
    return _registry


def _load_spatial_lookup() -> dict:
    """Build lookup structures for validation.

    Returns:
      villages: set of valid village names
      streets: set of valid street names
      person_locations: dict person_id -> (village, street)
      street_villages: dict street -> set of villages it appears in
    """
    registry = _load_registry()
    villages = set()
    streets = set()
    person_locations = {}
    street_villages = {}

    for pid, (village, street, _) in registry.items():
        villages.add(village)
        streets.add(street)
        person_locations[pid] = (village, street)
        street_villages.setdefault(street, set()).add(village)

    return villages, streets, person_locations, street_villages


class ValidationFailure:
    """Represents a single validation failure."""

    def __init__(
        self,
        rule_number: int,
        rule_name: str,
        message: str,
        field: Optional[str] = None,
    ):
        self.rule_number = rule_number
        self.rule_name = rule_name
        self.message = message
        self.field = field

    def __repr__(self):
        return f"ValidationFailure(rule={self.rule_number}, {self.rule_name}: {self.message})"


class QuarantineRecord:
    """A record that failed validation and was quarantined.

    Retained with raw text for audit until human resolution.
    """

    def __init__(
        self,
        person_id: str,
        week_number: int,
        raw_text: str,
        extracted_record: ExtractedRecord,
        failures: List[ValidationFailure],
    ):
        self.person_id = person_id
        self.week_number = week_number
        self.raw_text = raw_text
        self.extracted_record = extracted_record
        self.failures = failures
        self.quarantined_at = date.today()
        self.resolved = False
        self.resolution = None

    def __repr__(self):
        return (
            f"QuarantineRecord(pid={self.person_id}, week={self.week_number}, "
            f"failures={len(self.failures)})"
        )


# ---------------------------------------------------------------------------
# Validation rules
# ---------------------------------------------------------------------------

def validate_record(
    record: ExtractedRecord,
    week_number: int,
    raw_text: str,
) -> Tuple[bool, List[ValidationFailure]]:
    """Run all validation rules on an extracted record.

    Parameters
    ----------
    record : ExtractedRecord
        The extracted record to validate.
    week_number : int
        Week number (for quarantine tracking).
    raw_text : str
        Original raw text (retained for quarantine audit).

    Returns
    -------
    Tuple of (is_valid, list_of_failures).
    If is_valid is True, the record passes all rules and can be used
    for detection. If False, the record must be quarantined.
    """
    failures = []

    registry = _load_registry()
    villages, streets, person_locations, street_villages = _load_spatial_lookup()

    # Rule 1: Record has required structure
    if not record.person_id:
        failures.append(ValidationFailure(
            1, "required_structure",
            "Record missing person_id",
            field="person_id",
        ))
        # Can't do further person-based checks without person_id
        if failures:
            return False, failures

    pid = record.person_id

    # Rule 2: Person exists in registry
    if pid not in registry:
        failures.append(ValidationFailure(
            2, "person_exists",
            f"Person {pid} not found in people registry",
            field="person_id",
        ))
    else:
        registered_village, registered_street, _ = registry[pid]

        # Rule 3: Village exists in registry (if extracted)
        if record.village is not None:
            if record.village not in villages:
                failures.append(ValidationFailure(
                    3, "village_exists",
                    f"Village '{record.village}' not found in registry",
                    field="village",
                ))
            else:
                # Rule 5: Person-village relationship matches
                if registered_village != record.village:
                    failures.append(ValidationFailure(
                        5, "person_village_match",
                        f"Person {pid} belongs to {registered_village} in registry, "
                        f"but extraction returned {record.village}",
                        field="village",
                    ))

        # Rule 4: Street exists in registry (if extracted)
        if record.street is not None:
            if record.street not in streets:
                failures.append(ValidationFailure(
                    4, "street_exists",
                    f"Street '{record.street}' not found in registry",
                    field="street",
                ))
            else:
                # Rule 6: Person-street relationship matches
                if registered_street != record.street:
                    failures.append(ValidationFailure(
                        6, "person_street_match",
                        f"Person {pid} belongs to {registered_street} in registry, "
                        f"but extraction returned {record.street}",
                        field="street",
                    ))

    # Rule 7: Date parses (already parsed by extractor, but verify)
    if record.reporting_date is not None:
        if not isinstance(record.reporting_date, date):
            failures.append(ValidationFailure(
                7, "date_valid",
                f"Reporting date is not a valid date: {record.reporting_date}",
                field="reporting_date",
            ))

    # Rule 8: Date is a Sunday
    if record.reporting_date is not None and isinstance(record.reporting_date, date):
        if record.reporting_date.weekday() != 6:  # Sunday = 6
            failures.append(ValidationFailure(
                8, "date_is_sunday",
                f"Reporting date {record.reporting_date} is a "
                f"{record.reporting_date.strftime('%A')}, not Sunday",
                field="reporting_date",
            ))

    # Rule 9: Disease in controlled vocabulary (or None for no-infection)
    if record.disease is not None:
        if record.disease not in ALLOWED_DISEASES:
            failures.append(ValidationFailure(
                9, "disease_vocabulary",
                f"Disease '{record.disease}' not in controlled vocabulary. "
                f"Allowed: {sorted(ALLOWED_DISEASES)}",
                field="disease",
            ))

    is_valid = len(failures) == 0
    return is_valid, failures


def quarantine_record(
    record: ExtractedRecord,
    week_number: int,
    raw_text: str,
    failures: List[ValidationFailure],
) -> QuarantineRecord:
    """Create a quarantine record for a failed validation.

    In production, this would write to the quarantine table in the database.
    For now, returns the quarantine record object.
    """
    return QuarantineRecord(
        person_id=record.person_id,
        week_number=week_number,
        raw_text=raw_text,
        extracted_record=record,
        failures=failures,
    )


# Convenience function: validate and quarantine in one call
def validate_or_quarantine(
    record: ExtractedRecord,
    week_number: int,
    raw_text: str,
) -> Tuple[Optional[ExtractedRecord], Optional[QuarantineRecord]]:
    """Validate a record. Returns (record, None) if valid, (None, quarantine) if not.

    This is the main entry point for the ingestion pipeline (P6).
    """
    is_valid, failures = validate_record(record, week_number, raw_text)
    if is_valid:
        return record, None
    else:
        return None, quarantine_record(record, week_number, raw_text, failures)
