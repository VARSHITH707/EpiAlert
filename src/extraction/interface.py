"""ExtractorInterface Protocol.

Both RuleBasedExtractor and OllamaExtractor implement this Protocol
so they are interchangeable. The pipeline selects the extractor per run.
"""

from __future__ import annotations

from typing import List, Optional, Protocol, runtime_checkable

from src.extraction.schema import ExtractedRecord, ExtractionResult


@runtime_checkable
class ExtractorInterface(Protocol):
    """Protocol for extraction backends.

    Implementations:
    - RuleBasedExtractor: deterministic regex parsing
    - OllamaExtractor: local LLM via Ollama API (phi3)
    """

    def extract_from_text(
        self,
        person_id: str,
        raw_text: str,
        week_number: int = 0,
    ) -> ExtractionResult:
        """Extract structured data from a single raw report text.

        Parameters
        ----------
        person_id : str
            Person identifier from the filename/record.
        raw_text : str
            The full raw text of the report.
        week_number : int
            Week number (1-based). Optional context for the extractor.

        Returns
        -------
        ExtractionResult with the extracted record, or null fields
        if the information could not be extracted. Never fabricates
        values — missing data is explicit None.
        """
        ...

    def extract_batch(
        self,
        records: List[tuple],
        *,
        callback=None,
    ) -> List[ExtractionResult]:
        """Extract from multiple (person_id, raw_text) pairs.

        Parameters
        ----------
        records : List[tuple]
            List of (person_id, raw_text) tuples.
        callback : callable, optional
            Called as callback(i, total, result) for progress reporting.

        Returns
        -------
        List of ExtractionResult, one per input record, in order.
        """
        ...

    @property
    def name(self) -> str:
        """Human-readable name of this extractor."""
        ...
