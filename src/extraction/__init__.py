"""EpiAlert Extraction Layer.

Provides schema-constrained extraction of disease surveillance data
from raw text reports. Two extraction paths behind one interface:

  - RuleBasedExtractor: deterministic parsing, fast, used for bulk historical
  - OllamaExtractor: local LLM (phi3), used for weeks 105+ and accuracy sampling
"""

from src.extraction.schema import ExtractedRecord, ExtractionResult
from src.extraction.interface import ExtractorInterface
from src.extraction.rule_based import RuleBasedExtractor
from src.extraction.ollama_extractor import OllamaExtractor, is_ollama_available

__all__ = [
    "ExtractorInterface",
    "ExtractedRecord",
    "ExtractionResult",
    "RuleBasedExtractor",
    "OllamaExtractor",
    "is_ollama_available",
]
