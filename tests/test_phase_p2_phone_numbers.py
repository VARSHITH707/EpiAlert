"""Tests for the people registry and its phone numbers.

Checks the enriched registry exists, carries a phone number for every person,
uses only the configured numbers, and spreads them reasonably.

Two things this file deliberately does NOT do:

- hardcode real phone numbers. They come from src.config, which reads .env.
  A real number committed to a repository is a real person who can be called
  by anyone who clones it.
- use absolute paths. Paths are derived from this file's location so the tests
  run on any machine, not just the one they were written on.
"""

from __future__ import annotations

import csv
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.config import ALERT_PHONE_NUMBERS

PROJECT_ROOT = Path(__file__).parent.parent
PEOPLE_ENRICHED = (
    PROJECT_ROOT / "data" / "EpiAlert_Phase1_Dataset" / "people_enriched.csv"
)
PEOPLE_SOURCE = PROJECT_ROOT / "data" / "EpiAlert_Phase1_Dataset" / "people.csv"


def _read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def enriched() -> list[dict]:
    if not PEOPLE_ENRICHED.exists():
        pytest.skip(
            f"{PEOPLE_ENRICHED.name} not generated yet — run `python setup.py`"
        )
    return _read(PEOPLE_ENRICHED)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_phone_numbers_are_configured():
    """At least one number must be configured for alerts to go anywhere."""
    assert ALERT_PHONE_NUMBERS, "ALERT_PHONE_NUMBERS is empty"
    for number in ALERT_PHONE_NUMBERS:
        assert number.startswith("+"), f"{number} is not in E.164 form"


def test_numbers_are_distinct():
    """Duplicates would silently weaken the deduplication test elsewhere."""
    assert len(ALERT_PHONE_NUMBERS) == len(set(ALERT_PHONE_NUMBERS))


# ---------------------------------------------------------------------------
# The enriched registry
# ---------------------------------------------------------------------------

def test_has_phone_number_column(enriched: list[dict]):
    assert "phone_number" in enriched[0], "phone_number column missing"


def test_every_person_has_a_number(enriched: list[dict]):
    missing = [r["person_id"] for r in enriched if not r.get("phone_number")]
    assert not missing, f"{len(missing)} people have no phone number"


def test_only_configured_numbers_are_used(enriched: list[dict]):
    """No invented numbers: an unknown number could be a real stranger."""
    used = {r["phone_number"] for r in enriched}
    unexpected = used - set(ALERT_PHONE_NUMBERS)
    assert not unexpected, f"unconfigured numbers in registry: {unexpected}"


def test_numbers_are_reasonably_balanced(enriched: list[dict]):
    """Each number should carry a fair share, not 90% of the population."""
    counts = Counter(r["phone_number"] for r in enriched)
    if len(counts) < 2:
        pytest.skip("only one number configured, nothing to balance")
    expected = len(enriched) / len(counts)
    for number, n in counts.items():
        assert 0.5 * expected <= n <= 1.5 * expected, (
            f"{number} has {n} people, expected roughly {expected:.0f}"
        )


def test_location_data_is_unchanged(enriched: list[dict]):
    """Adding a phone number must not disturb who lives where.

    The village and street assignments are the authoritative record that
    detection depends on.
    """
    if not PEOPLE_SOURCE.exists():
        pytest.skip("people.csv not present")

    original = {r["person_id"]: (r["village"], r["street"]) for r in _read(PEOPLE_SOURCE)}
    for row in enriched:
        pid = row["person_id"]
        assert pid in original, f"{pid} is not in the source registry"
        assert (row["village"], row["street"]) == original[pid], (
            f"{pid} moved from {original[pid]} to "
            f"({row['village']}, {row['street']})"
        )
