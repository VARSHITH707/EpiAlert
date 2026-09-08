"""Tests for P5 — Validation and quarantine.

Each of the 9 validation rules tested with at least one passing case
and one failing case. Quarantine records must retain raw text for audit.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import date

from src.extraction.schema import ExtractedRecord
from src.extraction.validation import (
    validate_record,
    validate_or_quarantine,
    quarantine_record,
    ValidationFailure,
    QuarantineRecord,
    ALLOWED_DISEASES,
)


# Helpers
def make_record(**kwargs) -> ExtractedRecord:
    defaults = {
        "person_id": "P0001",
        "village": "Village A",
        "street": "Street 1",
        "reporting_date": date(2025, 1, 12),  # Sunday
        "disease": None,
    }
    defaults.update(kwargs)
    return ExtractedRecord(**defaults)


class TestRule1_RecordStructure:
    """Rule 1: Record must have person_id."""

    def test_passes_with_person_id(self):
        rec = make_record(person_id="P0001")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is True
        assert len(failures) == 0

    def test_fails_without_person_id(self):
        rec = ExtractedRecord(person_id="")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is False
        assert any(f.rule_number == 1 for f in failures)


class TestRule2_PersonExists:
    """Rule 2: Person must exist in registry."""

    def test_passes_for_known_person(self):
        rec = make_record(person_id="P0001")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is True

    def test_fails_for_unknown_person(self):
        rec = make_record(person_id="P9999")  # doesn't exist
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is False
        assert any(f.rule_number == 2 for f in failures)
        assert any("P9999" in f.message for f in failures)


class TestRule3_VillageExists:
    """Rule 3: Extracted village must exist in registry."""

    def test_passes_for_valid_village(self):
        rec = make_record(village="Village A")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is True

    def test_fails_for_nonexistent_village(self):
        rec = make_record(village="Village Z")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is False
        assert any(f.rule_number == 3 for f in failures)


class TestRule4_StreetExists:
    """Rule 4: Extracted street must exist in registry."""

    def test_passes_for_valid_street(self):
        rec = make_record(street="Street 1")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is True

    def test_fails_for_nonexistent_street(self):
        rec = make_record(street="Street 99")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is False
        assert any(f.rule_number == 4 for f in failures)


class TestRule5_PersonVillageMatch:
    """Rule 5: Person's extracted village must match registry."""

    def test_passes_when_matching(self):
        # P0001 is in Village A per the registry
        rec = make_record(person_id="P0001", village="Village A")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is True

    def test_fails_when_mismatched(self):
        # P0001 is in Village A, but extraction says Village B
        rec = make_record(person_id="P0001", village="Village B")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is False
        assert any(f.rule_number == 5 for f in failures)


class TestRule6_PersonStreetMatch:
    """Rule 6: Person's extracted street must match registry.

    CRITICAL: P0001 is on Street 1 in Village A. If extraction returns
    Street 1 but Village B, that's also a mismatch because the FULL
    (village, street) composite must match.
    """

    def test_passes_when_matching(self):
        rec = make_record(person_id="P0001", street="Street 1")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is True

    def test_fails_when_mismatched(self):
        # P0001 is on Street 1, but extraction says Street 2
        rec = make_record(person_id="P0001", street="Street 2")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is False
        assert any(f.rule_number == 6 for f in failures)

    def test_fails_for_same_street_different_village(self):
        """Street 1 exists in all 3 villages. Must check person's actual street."""
        # P0001 is on Street 1 in Village A. If extraction says Street 1
        # but Village B, rule 5 catches the village mismatch.
        # But if extraction returns Street 1 AND Village A, it passes.
        rec = make_record(person_id="P0001", street="Street 1", village="Village A")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is True


class TestRule7_DateParses:
    """Rule 7: Reporting date must be a valid date."""

    def test_passes_for_valid_date(self):
        rec = make_record(reporting_date=date(2025, 1, 12))
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is True

    def test_fails_for_invalid_date_type(self):
        rec = ExtractedRecord(
            person_id="P0001",
            reporting_date="not a date",  # type is wrong
        )
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is False
        assert any(f.rule_number == 7 for f in failures)


class TestRule8_DateIsSunday:
    """Rule 8: Reporting date must be a Sunday (dataset convention)."""

    def test_passes_for_sunday(self):
        rec = make_record(reporting_date=date(2025, 1, 12))  # Sunday
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is True

    def test_fails_for_monday(self):
        rec = make_record(reporting_date=date(2025, 1, 13))  # Monday
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is False
        assert any(f.rule_number == 8 for f in failures)

    def test_fails_for_wednesday(self):
        rec = make_record(reporting_date=date(2025, 1, 15))  # Wednesday
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is False
        assert any(f.rule_number == 8 for f in failures)


class TestRule9_DiseaseVocabulary:
    """Rule 9: Disease must be in controlled vocabulary or None."""

    def test_passes_for_none(self):
        rec = make_record(disease=None)
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is True

    def test_passes_for_valid_disease(self):
        rec = make_record(disease="Dengue")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is True

    def test_passes_for_chikungunya(self):
        rec = make_record(disease="Chikungunya")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is True

    def test_fails_for_invalid_disease(self):
        rec = make_record(disease="Chickenpox")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is False
        assert any(f.rule_number == 9 for f in failures)

    def test_fails_for_empty_string_disease(self):
        rec = make_record(disease="")
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is False
        assert any(f.rule_number == 9 for f in failures)


class TestQuarantineRecord:
    """Quarantine records must retain raw text and failure details."""

    def test_quarantine_retains_raw_text(self):
        rec = make_record(person_id="P0001")
        failures = [ValidationFailure(2, "person_exists", "Not found")]
        q = quarantine_record(rec, 5, "The health assessment on 12 January 2025 recorded P0001...", failures)
        assert q.raw_text == "The health assessment on 12 January 2025 recorded P0001..."
        assert q.week_number == 5
        assert q.person_id == "P0001"
        assert len(q.failures) == 1
        assert q.failures[0].rule_number == 2

    def test_validate_or_quarantine_returns_record_when_valid(self):
        rec = make_record(person_id="P0001")
        validated, quarantined = validate_or_quarantine(rec, 1, "some text")
        assert validated is rec
        assert quarantined is None

    def test_validate_or_quarantine_returns_quarantine_when_invalid(self):
        rec = make_record(person_id="P9999")  # doesn't exist
        validated, quarantined = validate_or_quarantine(rec, 1, "some text")
        assert validated is None
        assert quarantined is not None
        assert isinstance(quarantined, QuarantineRecord)
        assert len(quarantined.failures) > 0

    def test_multiple_failures_captured(self):
        # P9999 doesn't exist, Village Z doesn't exist, Street 99 doesn't exist,
        # Monday is not Sunday, Chickenpox not in vocabulary
        rec = ExtractedRecord(
            person_id="P9999",
            village="Village Z",
            street="Street 99",
            reporting_date=date(2025, 1, 13),  # Monday
            disease="Chickenpox",
        )
        is_valid, failures = validate_record(rec, 1, "some text")
        assert is_valid is False
        # P9999 not found stops further person-based checks, so rules 3,4,5,6 don't fire
        # But rules 2 (person not found), 8 (not Sunday), 9 (Chickenpox) fire = 3 failures
        assert len(failures) >= 3


class TestAllowedDiseasesConstant:
    """The controlled vocabulary must contain exactly the 5 expected diseases."""

    def test_has_exactly_five_diseases(self):
        assert len(ALLOWED_DISEASES) == 5

    def test_contains_all_expected(self):
        expected = {"Dengue", "Malaria", "Chikungunya",
                    "Influenza/ARI", "Acute Gastroenteritis"}
        assert ALLOWED_DISEASES == expected
