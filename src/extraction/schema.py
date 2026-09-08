"""Schema-constrained extraction model.

Exactly five fields per the P4 specification. The extractor must emit
explicit None rather than inferring — a fabricated location is worse
than a missing one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class ExtractedRecord:
    """A single extracted surveillance record.

    Attributes
    ----------
    person_id : str
        Person identifier (e.g., "P0001"). Never None — every report
        names the person.
    village : Optional[str]
        Village name. None if unresolvable — must NOT be fabricated.
    street : Optional[str]
        Street name. None if unresolvable — must NOT be fabricated.
    reporting_date : Optional[date]
        Date the report was made. None if unparseable.
    disease : Optional[str]
        Disease/syndrome name from controlled vocabulary, or None if
        "no infection" or unrecognised.
    """

    person_id: str
    village: Optional[str] = None
    street: Optional[str] = None
    reporting_date: Optional[date] = None
    disease: Optional[str] = None

    def has_disease(self) -> bool:
        """Return True if a specific disease was extracted (not 'no infection')."""
        return self.disease is not None

    def is_no_infection(self) -> bool:
        """Return True if the record indicates no infection."""
        return self.disease is None and self.person_id is not None


@dataclass(frozen=True)
class ExtractionResult:
    """Result of extracting one raw report.

    Attributes
    ----------
    record : ExtractedRecord
        The extracted structured data.
    extractor_type : str
        Which extractor produced this: "rule" or "ollama".
    raw_text : str
        The original raw text (retained for audit and quarantine).
    extraction_time_ms : Optional[float]
        Time taken to extract, in milliseconds. None if not measured.
    """

    record: ExtractedRecord
    extractor_type: str = "rule"
    raw_text: str = ""
    extraction_time_ms: Optional[float] = None
