"""Tests for P4 — Extraction layer.

Verifies: pydantic schema, ExtractorInterface Protocol, RuleBasedExtractor
parsing accuracy, OllamaExtractor discovery and graceful failure.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.extraction import (
    ExtractorInterface,
    ExtractedRecord,
    ExtractionResult,
    RuleBasedExtractor,
    OllamaExtractor,
    is_ollama_available,
)


# ---------------------------------------------------------------------------
# Real report examples from the dataset (P1 findings)
# ---------------------------------------------------------------------------

REPORTS = [
    # Format A — most common
    (
        "P0001",
        "The health assessment on 12 January 2025 recorded P0001 of Street 1, Village A with no infection.",
        {
            "person_id": "P0001",
            "village": "Village A",
            "street": "Street 1",
            "reporting_date": "2025-01-12",
            "disease": None,
        },
    ),
    # Format A — with disease
    (
        "P0100",
        "The health assessment on 12 January 2025 recorded P0100 of Street 3, Village B with Dengue.",
        {
            "person_id": "P0100",
            "village": "Village B",
            "street": "Street 3",
            "reporting_date": "2025-01-12",
            "disease": "Dengue",
        },
    ),
    # Format B
    (
        "P0200",
        "P0200, from Village C, Street 5, was recorded with no reported infection on 03 January 2027.",
        {
            "person_id": "P0200",
            "village": "Village C",
            "street": "Street 5",
            "reporting_date": "2027-01-03",
            "disease": None,
        },
    ),
    # Format C
    (
        "P0300",
        "On 19 February 2025, P0300 from Street 8 in Village B was reported as having Malaria.",
        {
            "person_id": "P0300",
            "village": "Village B",
            "street": "Street 8",
            "reporting_date": "2025-02-19",
            "disease": "Malaria",
        },
    ),
    # Ambiguous street name — "Street 1" exists in all 3 villages
    # Extractor must extract what's in the text, NOT disambiguate
    (
        "P0400",
        "The health assessment on 12 January 2025 recorded P0400 of Street 1, Village A with no infection.",
        {
            "person_id": "P0400",
            "village": "Village A",
            "street": "Street 1",
            "reporting_date": "2025-01-12",
            "disease": None,
        },
    ),
]


class TestExtractedRecordSchema:
    """The schema must have exactly the 5 required fields."""

    def test_person_id_required(self):
        r = ExtractedRecord(person_id="P0001")
        assert r.person_id == "P0001"

    def test_optional_fields_default_to_none(self):
        r = ExtractedRecord(person_id="P0001")
        assert r.village is None
        assert r.street is None
        assert r.reporting_date is None
        assert r.disease is None

    def test_disease_none_means_no_infection(self):
        r = ExtractedRecord(person_id="P0001")
        assert r.is_no_infection() is True
        assert r.has_disease() is False

    def test_disease_set_means_infection(self):
        r = ExtractedRecord(person_id="P0001", disease="Dengue")
        assert r.has_disease() is True
        assert r.is_no_infection() is False

    def test_frozen_immutable(self):
        r = ExtractedRecord(person_id="P0001")
        with pytest.raises(Exception):
            r.person_id = "P9999"


class TestRuleBasedExtractor:
    """RuleBasedExtractor must parse all three formats correctly."""

    def setup_method(self):
        self.extractor = RuleBasedExtractor()

    def test_extracts_format_a_no_infection(self):
        text = "The health assessment on 12 January 2025 recorded P0001 of Street 1, Village A with no infection."
        result = self.extractor.extract_from_text("P0001", text)
        assert result.record.person_id == "P0001"
        assert result.record.village == "Village A"
        assert result.record.street == "Street 1"
        assert result.record.reporting_date is not None
        assert result.record.reporting_date.year == 2025
        assert result.record.disease is None
        assert result.extractor_type == "rule"

    def test_extracts_format_a_with_disease(self):
        text = "The health assessment on 12 January 2025 recorded P0100 of Street 3, Village B with Dengue."
        result = self.extractor.extract_from_text("P0100", text)
        assert result.record.disease == "Dengue"
        assert result.record.village == "Village B"

    def test_extracts_format_b(self):
        text = "P0200, from Village C, Street 5, was recorded with no reported infection on 03 January 2027."
        result = self.extractor.extract_from_text("P0200", text)
        assert result.record.person_id == "P0200"
        assert result.record.village == "Village C"
        assert result.record.street == "Street 5"
        assert result.record.reporting_date is not None
        assert result.record.reporting_date == __import__('datetime').date(2027, 1, 3)
        assert result.record.disease is None

    def test_extracts_format_c(self):
        text = "On 19 February 2025, P0300 from Street 8 in Village B was reported as having Malaria."
        result = self.extractor.extract_from_text("P0300", text)
        assert result.record.disease == "Malaria"
        assert result.record.street == "Street 8"
        assert result.record.village == "Village B"

    def test_extract_batch(self):
        batch = [
            ("P0001", REPORTS[0][1]),
            ("P0100", REPORTS[1][1]),
        ]
        results = self.extractor.extract_batch(batch)
        assert len(results) == 2
        assert results[0].record.person_id == "P0001"
        assert results[1].record.disease == "Dengue"

    def test_gets_stats(self):
        stats = self.extractor.get_stats()
        assert "extract_count" in stats
        assert "disease_count" in stats
        assert "disease_rate" in stats
        assert stats["extract_count"] >= 0  # stats structure is correct


class TestOllamaExtractorDiscovery:
    """OllamaExtractor must discover the service and report availability.

    Ollama is an OPTIONAL dependency: the pipeline falls back to the
    deterministic rule-based extractor and to template alert text when it is
    not running. These tests therefore SKIP rather than fail when the service
    is down -- a failing suite on a machine without Ollama would be reporting
    a missing optional service as a broken build.

    Graceful degradation when Ollama is absent is covered separately by
    TestOllamaExtractorGracefulFailure, which does not require the service.
    """

    def test_checks_availability_on_init(self):
        if not is_ollama_available():
            pytest.skip("Ollama not running at localhost:11434 (optional)")
        ext = OllamaExtractor()
        assert ext.is_available is True, \
            "Ollama answered the probe but the extractor reported unavailable"

    def test_detects_phi3_model(self):
        if not is_ollama_available():
            pytest.skip("Ollama not running at localhost:11434 (optional)")
        ext = OllamaExtractor()
        assert ext.model_available is True, \
            "Ollama is running but the phi3 model is not pulled"

    def test_is_ollama_available_function(self):
        # Probe twice: the helper must agree with itself rather than depend on
        # transient state, and must return a bool either way.
        first = is_ollama_available()
        assert isinstance(first, bool)
        assert is_ollama_available() is first


class TestOllamaExtractorGracefulFailure:
    """When Ollama is unavailable, the extractor must fail gracefully."""

    def test_returns_explicit_nulls_when_unavailable(self):
        # Point to a closed port to simulate unavailability
        ext = OllamaExtractor(url="http://localhost:19999", timeout=2)
        assert ext.is_available is False

        result = ext.extract_from_text("P0001", "Some report text")
        assert result.record.person_id == "P0001"  # person_id passed in
        assert result.record.village is None  # explicit null, not fabricated
        assert result.record.street is None
        assert result.record.disease is None
        assert result.extractor_type == "ollama"


class TestOllamaExtractorSchemaConformance:
    """OllamaExtractor output must conform to the 5-field schema."""

    def test_response_is_valid_json(self):
        ext = OllamaExtractor()
        if not ext.is_available:
            pytest.skip("Ollama not available")

        text = "The health assessment on 12 January 2025 recorded P0001 of Street 1, Village A with Dengue."
        result = ext.extract_from_text("P0001", text)

        # Must have all 5 fields
        assert hasattr(result.record, 'person_id')
        assert hasattr(result.record, 'village')
        assert hasattr(result.record, 'street')
        assert hasattr(result.record, 'reporting_date')
        assert hasattr(result.record, 'disease')

    def test_disease_validated_against_vocabulary(self):
        ext = OllamaExtractor()
        if not ext.is_available:
            pytest.skip("Ollama not available")

        # Prompt that might produce an invalid disease name
        text = "P0001 was recorded with Chickenpox on 12 January 2025."
        result = ext.extract_from_text("P0001", text)
        # Chickenpox is NOT in the controlled vocabulary — must be None
        assert result.record.disease is None or result.record.disease in {
            "Dengue", "Malaria", "Chikungunya", "Influenza/ARI",
            "Acute Gastroenteritis"
        }


class TestExtractorInterfaceProtocol:
    """Both extractors must implement ExtractorInterface."""

    def test_rule_based_implements_interface(self):
        ext = RuleBasedExtractor()
        assert isinstance(ext, ExtractorInterface)

    def test_ollama_implements_interface(self):
        ext = OllamaExtractor()
        assert isinstance(ext, ExtractorInterface)

    def test_interface_methods_exist(self):
        ext = RuleBasedExtractor()
        assert hasattr(ext, 'extract_from_text')
        assert hasattr(ext, 'extract_batch')
        assert hasattr(ext, 'name')
        assert callable(ext.extract_from_text)
        assert callable(ext.extract_batch)
        assert isinstance(ext.name, str)
