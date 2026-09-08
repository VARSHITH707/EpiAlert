"""OllamaExtractor — LLM-based extraction via local Ollama (phi3).

Uses the local Ollama service with phi3 for extraction. Strict JSON output,
temperature 0.1, system prompt constrains output to the schema.

Auto-discovers the Ollama service, prefers phi3, fails gracefully with a
clear message if the service is down. Never pretends Ollama worked when it
did not — falls back to explicit None fields.

IMPORTANT: the LLM receives ONLY the raw text and the verified schema.
It does NOT see any database state, ground truth, or other reports.
This prevents the LLM from inferring values it could not know.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from typing import List, Optional

import requests

from src.extraction.schema import ExtractedRecord, ExtractionResult
from src.extraction.interface import ExtractorInterface

# The three allowed test phone numbers are NEVER passed to the LLM.
# The LLM extracts only what is in the report text: person_id, village,
# street, date, disease. Phone numbers come from the enriched registry
# (P2), not from extraction.

OLLAMA_URL = "http://localhost:11434"
OLLAMA_TIMEOUT = 30  # seconds per request

# System prompt constrains the LLM to emit ONLY valid JSON matching the schema.
SYSTEM_PROMPT = """\
You extract structured surveillance data from a health report text.
Output ONLY a single JSON object. No explanation, no markdown, no other text.

The JSON must have exactly these fields:
- person_id: string, the person identifier (e.g., "P0001"), or null if absent
- village: string or null — the village name from the text, or null if not mentioned
- street: string or null — the street name from the text, or null if not mentioned
- reporting_date: string in "YYYY-MM-DD" format or null if not parseable
- disease: string or null — one of: Dengue, Malaria, Chikungunya, Influenza/ARI,
  Acute Gastroenteritis. If the report says "no infection", set to null.

Rules:
- Never invent values. If a field is not in the text, set it to null.
- village and street must come FROM THE TEXT. Do not guess or use common knowledge.
- If person_id appears in the text, extract it exactly. If not, null.
- reporting_date must be in YYYY-MM-DD format. If unparseable, null.
- disease must be one of the 5 allowed values above, or null.

Example input: "The health assessment on 12 January 2025 recorded P0001 of Street 1, Village A with no infection."
Example output: {"person_id": "P0001", "village": "Village A", "street": "Street 1", "reporting_date": "2025-01-12", "disease": null}
"""

# User prompt template — only the raw text is passed, nothing else.
USER_PROMPT_TEMPLATE = "Extract the surveillance data from this report:\n\n{text}\n"


class OllamaExtractor(ExtractorInterface):
    """LLM-based extractor using local Ollama phi3.

    Auto-discovers the Ollama service. Requires phi3 to be pulled.
    Fails gracefully if Ollama is unavailable — returns records with
    explicit None fields rather than raising or fabricating.

    Implements ExtractorInterface.
    """

    def __init__(
        self,
        model: str = "phi3",
        temperature: float = 0.1,
        url: str = OLLAMA_URL,
        timeout: float = OLLAMA_TIMEOUT,
    ):
        self._model = model
        self._temperature = temperature
        self._url = url.rstrip("/")
        self._timeout = timeout
        self._available = False
        self._model_available = False
        self._extract_count = 0
        self._fail_count = 0
        self._check_availability()

    def _check_availability(self):
        """Check if Ollama service and model are available."""
        try:
            resp = requests.get(
                f"{self._url}/api/tags",
                timeout=5,
            )
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                self._available = True
                for m in models:
                    if m.get("name", "").startswith(self._model):
                        self._model_available = True
                        break
        except Exception:
            self._available = False
            self._model_available = False

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def is_available(self) -> bool:
        """Return True if Ollama service is reachable."""
        return self._available

    @property
    def model_available(self) -> bool:
        """Return True if the preferred model is pulled."""
        return self._model_available

    def _generate(self, prompt: str) -> Optional[str]:
        """Call Ollama and return the response text, or None on failure."""
        if not self._available:
            self._fail_count += 1
            return None

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {
                "temperature": self._temperature,
                "num_predict": 512,
            },
        }

        try:
            resp = requests.post(
                f"{self._url}/api/chat",
                json=payload,
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                self._fail_count += 1
                return None

            data = resp.json()
            message = data.get("message", {})
            return message.get("content", "")
        except Exception:
            self._fail_count += 1
            return None

    def _parse_llm_response(self, text: str) -> Optional[dict]:
        """Parse the LLM JSON output. Returns None if unparseable."""
        if not text:
            return None

        # Try to find JSON in the response (LLM may add surrounding text)
        text = text.strip()
        if text.startswith("```"):
            # Markdown code block — extract JSON inside
            m = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
            if m:
                text = m.group(1).strip()

        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                return None
            return data
        except json.JSONDecodeError:
            return None

    def _dict_to_record(self, data: dict) -> ExtractedRecord:
        """Convert LLM JSON output to ExtractedRecord, with explicit None for missing."""
        person_id = data.get("person_id")
        if person_id and isinstance(person_id, str):
            person_id = person_id.strip().upper()
        else:
            person_id = None

        village = data.get("village")
        if village and isinstance(village, str):
            village = village.strip()
        else:
            village = None

        street = data.get("street")
        if street and isinstance(street, str):
            street = street.strip()
        else:
            street = None

        reporting_date = data.get("reporting_date")
        parsed_date = None
        if reporting_date and isinstance(reporting_date, str):
            try:
                parsed_date = date.fromisoformat(reporting_date.strip())
            except (ValueError, TypeError):
                parsed_date = None

        disease = data.get("disease")
        if disease and isinstance(disease, str):
            disease = disease.strip()
            # Validate against controlled vocabulary
            allowed = {"Dengue", "Malaria", "Chikungunya", "Influenza/ARI",
                       "Acute Gastroenteritis"}
            if disease not in allowed:
                disease = None
        else:
            disease = None

        return ExtractedRecord(
            person_id=person_id or "",
            village=village,
            street=street,
            reporting_date=parsed_date,
            disease=disease,
        )

    def extract_from_text(
        self,
        person_id: str,
        raw_text: str,
        week_number: int = 0,
    ) -> ExtractionResult:
        """Extract from a single report using phi3 via Ollama."""
        t0 = time.perf_counter()

        prompt = USER_PROMPT_TEMPLATE.format(text=raw_text)
        response = self._generate(prompt)

        if response is None:
            # Ollama unavailable — return record with explicit nulls
            self._fail_count += 1
            elapsed_ms = (time.perf_counter() - t0) * 1000
            record = ExtractedRecord(
                person_id=person_id.strip().upper() if person_id else "",
                village=None,
                street=None,
                reporting_date=None,
                disease=None,
            )
            return ExtractionResult(
                record=record,
                extractor_type="ollama",
                raw_text=raw_text,
                extraction_time_ms=elapsed_ms,
            )

        self._extract_count += 1
        parsed = self._parse_llm_response(response)

        if parsed is None:
            # Unparseable response — return explicit nulls
            elapsed_ms = (time.perf_counter() - t0) * 1000
            record = ExtractedRecord(
                person_id=person_id.strip().upper() if person_id else "",
                village=None,
                street=None,
                reporting_date=None,
                disease=None,
            )
            return ExtractionResult(
                record=record,
                extractor_type="ollama",
                raw_text=raw_text,
                extraction_time_ms=elapsed_ms,
            )

        record = self._dict_to_record(parsed)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        return ExtractionResult(
            record=record,
            extractor_type="ollama",
            raw_text=raw_text,
            extraction_time_ms=elapsed_ms,
        )

    def extract_batch(
        self,
        records: List[tuple],
        *,
        callback=None,
    ) -> List[ExtractionResult]:
        """Extract from a batch. For Ollama, processes one at a time."""
        results = []
        for i, (person_id, raw_text) in enumerate(records):
            result = self.extract_from_text(person_id, raw_text)
            results.append(result)
            if callback:
                callback(i + 1, len(records), result)
        return results

    def get_stats(self) -> dict:
        """Return extraction statistics."""
        total = self._extract_count + self._fail_count
        return {
            "extract_count": self._extract_count,
            "fail_count": self._fail_count,
            "total_attempts": total,
            "success_rate": self._extract_count / max(1, total),
            "model_available": self._model_available,
            "service_available": self._available,
        }

    def reset_stats(self):
        """Reset extraction counters."""
        self._extract_count = 0
        self._fail_count = 0
        self._check_availability()


def is_ollama_available(url: str = OLLAMA_URL, timeout: float = 5.0) -> bool:
    """Check if the Ollama service is reachable.

    Returns True if the service responds to /api/tags.
    """
    try:
        resp = requests.get(f"{url.rstrip('/')}/api/tags", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False
