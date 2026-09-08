"""RuleBasedExtractor — deterministic, fast extraction for bulk historical data.

Parses the three report text formats found in the Phase 1 dataset:

  Format A (most common): "The health assessment on DD Month YYYY recorded
      PERSON of STREET, VILLAGE with [no] infection."

  Format B: "PERSON, from VILLAGE, STREET, was recorded with [no] reported
      infection on DD Month YYYY."

  Format C: "On DD Month YYYY, PERSON from STREET in VILLAGE was reported
      as having [no] infection."

All extraction is deterministic. No API calls, no model download, no API key.
Runs offline on all 312,000 reports in minutes.

Emits explicit None for any field that cannot be parsed — never fabricates.
"""

from __future__ import annotations

import re
import time
from datetime import date
from typing import List, Optional

from src.extraction.schema import ExtractedRecord, ExtractionResult
from src.extraction.interface import ExtractorInterface

# Controlled vocabulary of disease terms (including variants found in data)
DISEASE_TERMS = {
    "Dengue": ["dengue"],
    "Malaria": ["malaria"],
    "Chikungunya": ["chikungunya"],
    "Influenza/ARI": ["influenza", "ari", "acute respiratory infection",
                     "acute respiratory infections"],
    "Acute Gastroenteritis": ["gastroenteritis", "acute gastroenteritis",
                               "acute gastro-enteritis"],
}

NO_INFECTION_TERMS = ["no infection", "no reported infection", "no infections",
                      "without infection", "no infectious disease"]

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# Pre-compiled patterns for the three known formats
# Format A: "... recorded PERSON of STREET, VILLAGE with ..."
_PATTERN_A = re.compile(
    r"(?:recorded|records|recording)\s+"
    r"(P\w+)\s+of\s+"
    r"(?:Street\s*(\d+)|([^,]+?)),\s*"
    r"(Village\s*\w+)",
    re.IGNORECASE,
)

# Format B: "PERSON, from VILLAGE, STREET, ..."
_PATTERN_B = re.compile(
    r"(P\w+)\s*,\s*from\s+"
    r"(Village\s*\w+)\s*,\s*"
    r"(?:Street\s*(\d+)|([^,]+?))\s*,",
    re.IGNORECASE,
)

# Format C: "PERSON from STREET in VILLAGE"
_PATTERN_C = re.compile(
    r"(?:from|of)\s+"
    r"(?:Street\s*(\d+)|([^,]+?))\s+"
    r"(?:in|of)\s+"
    r"(Village\s*\w+)",
    re.IGNORECASE,
)

# Date patterns
_DATE_PATTERN = re.compile(
    r"(?:on|on\s+)\s+(\d{1,2})\s+(\w+)\s+(\d{4})",
    re.IGNORECASE,
)

# Disease detection (checked after location extraction)
_DISEASE_PATTERN = re.compile(
    r"\b(" + "|".join(
        re.escape(term) for terms in DISEASE_TERMS.values() for term in terms
    ) + r")\b",
    re.IGNORECASE,
)


def _parse_date(text: str) -> Optional[date]:
    """Extract and parse a date from report text.

    Returns the first matching date, or None if unparseable.
    """
    m = _DATE_PATTERN.search(text)
    if not m:
        return None
    day_s, month_s, year_s = m.groups()
    try:
        month = MONTH_MAP.get(month_s.strip().lower())
        if month is None:
            return None
        return date(int(year_s), month, int(day_s))
    except (ValueError, OverflowError):
        return None


def _extract_disease(text: str) -> Optional[str]:
    """Return the disease name if found, None for no-infection reports."""
    lower = text.lower()

    # Check for no-infection first
    for term in NO_INFECTION_TERMS:
        if term in lower:
            return None

    # Check for specific disease
    m = _DISEASE_PATTERN.search(text)
    if m:
        found_term = m.group(1).lower()
        for disease_name, variants in DISEASE_TERMS.items():
            if found_term in [v.lower() for v in variants]:
                return disease_name
    return None


def _normalise_street(street_match: Optional[str], fallback: Optional[str]) -> Optional[str]:
    """Normalise street name to 'Street N' format."""
    if street_match:
        return f"Street {street_match.strip()}"
    if fallback:
        fb = fallback.strip()
        if fb.lower().startswith("street "):
            return fb
        return fb
    return None


def _normalise_village(village_match: Optional[str]) -> Optional[str]:
    """Normalise village name."""
    if village_match:
        v = village_match.strip()
        if v.lower().startswith("village "):
            return v
        return f"Village {v}"
    return None


class RuleBasedExtractor(ExtractorInterface):
    """Deterministic rule-based extractor.

    Parses the known report formats using regex. Fast enough for all
    312,000 historical reports in minutes. No external dependencies.

    Implements ExtractorInterface.
    """

    def __init__(self):
        self._extract_count = 0
        self._disease_count = 0

    @property
    def name(self) -> str:
        return "rule"

    def extract_from_text(
        self,
        person_id: str,
        raw_text: str,
        week_number: int = 0,
    ) -> ExtractionResult:
        """Extract from a single report using regex patterns."""
        t0 = time.perf_counter()

        person_id = person_id.strip().upper()
        text = raw_text.strip()

        village: Optional[str] = None
        street: Optional[str] = None
        reporting_date: Optional[date] = _parse_date(text)
        disease: Optional[str] = _extract_disease(text)

        # Try Format A first (most common in dataset)
        m = _PATTERN_A.search(text)
        if m:
            person_a = m.group(1)
            street_num = m.group(2)
            street_text = m.group(3)
            village_text = m.group(4)
            if person_a.upper() == person_id or not person_id:
                street = _normalise_street(street_num or street_text, None)
                village = _normalise_village(village_text)
        else:
            # Try Format B
            m = _PATTERN_B.search(text)
            if m:
                person_b = m.group(1)
                village_text = m.group(2)
                street_num = m.group(3)
                street_text = m.group(4)
                if person_b.upper() == person_id or not person_id:
                    village = _normalise_village(village_text)
                    street = _normalise_street(street_num or street_text, None)
            else:
                # Try Format C — look for "from STREET in VILLAGE"
                m = _PATTERN_C.search(text)
                if m:
                    street_num = m.group(1)
                    street_text = m.group(2)
                    village_text = m.group(3)
                    street = _normalise_street(street_num or street_text, None)
                    village = _normalise_village(village_text)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        self._extract_count += 1
        if disease:
            self._disease_count += 1

        record = ExtractedRecord(
            person_id=person_id,
            village=village,
            street=street,
            reporting_date=reporting_date,
            disease=disease,
        )

        return ExtractionResult(
            record=record,
            extractor_type="rule",
            raw_text=text,
            extraction_time_ms=elapsed_ms,
        )

    def extract_batch(
        self,
        records: List[tuple],
        *,
        callback=None,
    ) -> List[ExtractionResult]:
        """Extract from a batch of (person_id, raw_text) pairs."""
        results = []
        for i, (person_id, raw_text) in enumerate(records):
            result = self.extract_from_text(person_id, raw_text)
            results.append(result)
            if callback:
                callback(i + 1, len(records), result)
        return results

    def get_stats(self) -> dict:
        """Return extraction statistics."""
        return {
            "extract_count": self._extract_count,
            "disease_count": self._disease_count,
            "disease_rate": self._disease_count / max(1, self._extract_count),
        }

    def reset_stats(self):
        """Reset extraction counters."""
        self._extract_count = 0
        self._disease_count = 0
