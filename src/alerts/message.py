"""Alert message generation.

Produces the human-readable `message` field for alert rows. Two modes:

1. DETERMINISTIC TEMPLATE (default, always available):
   A parameterized format string built from verified values only:
   disease, village, street, week, observed_count, expected_count, status.
   Nothing is invented.

2. OLLAMA (optional): sends the verified values to phi3 with an explicit
   instruction not to invent any value. If Ollama is unavailable or fails,
   falls back to the deterministic template. The alert NEVER fails because
   the LLM failed.

The diagnostic explanation string (CUSUM=ACTIVE ..., EWMA=ACTIVE ...) is
preserved verbatim in the `explanation` column.
"""

from typing import Optional

# ---------------------------------------------------------------------------
# Deterministic template — used when Ollama is unavailable or fails
# ---------------------------------------------------------------------------

DEFAULT_TEMPLATE = (
    "EpiAlert Warning: Increased {disease_lower} activity has been detected "
    "in {village}, {street}. "
    "Observed {observed} cases this week against an expected {expected} cases. "
    "Status: {status}. Please take appropriate precautions."
)


def _fmt_deterministic(
    disease: str,
    village: str,
    street: str,
    week_number: int,
    observed_count: int,
    expected_count: int,
    status: str,
) -> str:
    """Build the human-readable message from verified values using the template."""
    disease_lower = disease.lower().replace("/", " ").replace(" ", "_")
    # Keep the disease name readable for the message
    return DEFAULT_TEMPLATE.format(
        disease_lower=disease,
        village=village,
        street=street,
        week_number=week_number,
        observed=observed_count,
        expected=expected_count,
        status=status,
    )


# ---------------------------------------------------------------------------
# Ollama integration
# ---------------------------------------------------------------------------

_OLLAMA_BASE = "http://localhost:11434"
_OLLAMA_MODEL = "phi3:latest"


def _ollama_available() -> bool:
    """Check whether Ollama is reachable without throwing."""
    try:
        import requests
        r = requests.get(f"{_OLLAMA_BASE}/api/tags", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def generate_alert_message_ollama(
    disease: str,
    village: str,
    street: str,
    week_number: int,
    observed_count: int,
    expected_count: int,
    status: str,
    explanation: Optional[str] = None,
) -> Optional[str]:
    """Ask phi3 to phrase the alert from verified values.

    The prompt passes the EXACT disease, village, street, week, counts and
    status. The model is instructed NOT to invent any disease, place, count
    or statistic. If the response looks invented it is rejected and None
    is returned (caller falls back to the deterministic template).

    Returns None if Ollama is unavailable, the request fails, or the
    response fails validation.
    """
    if not _ollama_available():
        return None

    import requests

    disease_lower = disease.lower()
    prompt = (
        f"You are an epidemic alert message generator.\n\n"
        f"Write a SHORT public-health alert message (one or two sentences) using "
        f"ONLY the verified values below. Do NOT invent any disease name, place, "
        f"count, statistic, date, or recommendation that is not in the data.\n\n"
        f"VERIFIED VALUES:\n"
        f"  Disease: {disease}\n"
        f"  Village: {village}\n"
        f"  Street: {street}\n"
        f"  Week number: {week_number}\n"
        f"  Observed cases this week: {observed_count}\n"
        f"  Expected cases baseline: {expected_count}\n"
        f"  Alert status: {status}\n\n"
        f"Output ONLY the alert message text. Nothing else.\n"
        f"Example output: 'EpiAlert Warning: Increased dengue activity has been "
        f"detected in Village A, Street 6. Please take appropriate precautions.'\n"
    )

    try:
        resp = requests.post(
            f"{_OLLAMA_BASE}/api/generate",
            json={
                "model": _OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.3,
                    "top_p": 0.9,
                },
                "system": "You output a single short alert message. No explanations.",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        text = data.get("response", "").strip()
        if not text:
            return None
        # Sanity check: the message must mention the disease (case-insensitive)
        if disease_lower.split("/")[0] not in text.lower() and disease_lower not in text.lower():
            return None
        if village not in text:
            return None
        if street not in text:
            return None
        return text
    except Exception:
        return None


def generate_alert_message(
    disease: str,
    village: str,
    street: str,
    week_number: int,
    observed_count: int,
    expected_count: int,
    status: str,
    explanation: Optional[str] = None,
    use_ollama: bool = True,
) -> str:
    """Generate the human-readable alert message.

    Tries Ollama first (if use_ollama=True and Ollama is reachable). On any
    failure falls back to the deterministic template. The deterministic template
    is ALWAYS available and never fails.

    Parameters
    ----------
    disease, village, street, week_number, observed_count, expected_count,
    status: verified values from detection_results.
    explanation: the diagnostic string (CUSUM=ACTIVE ...). NOT used in the
        message; stored separately in the alert's explanation column.
    use_ollama: try Ollama first. Default True.

    Returns
    -------
    str : human-readable alert message.
    """
    if use_ollama:
        ollama_msg = generate_alert_message_ollama(
            disease=disease,
            village=village,
            street=street,
            week_number=week_number,
            observed_count=observed_count,
            expected_count=expected_count,
            status=status,
            explanation=explanation,
        )
        if ollama_msg:
            return ollama_msg

    return _fmt_deterministic(
        disease=disease,
        village=village,
        street=street,
        week_number=week_number,
        observed_count=observed_count,
        expected_count=expected_count,
        status=status,
    )
