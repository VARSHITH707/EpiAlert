"""EpiAlert Preprocessing Module

Handles data aggregation and feature extraction from the database
for use by detection algorithms (Baseline, CUSUM, EWMA).
"""

from src.database.db import get_connection, init_schema
from src.ingestion import week_number_to_date, WEEK_ZERO_REFERENCE
from collections import Counter, defaultdict
import numpy as np


# ---------------------------------------------------------------------------
# Weekly aggregation from database
# ---------------------------------------------------------------------------

def get_weekly_aggregation(week_number: int, disease: str = None) -> dict:
    """Aggregate weekly surveillance data from the database.

    Returns aggregated data for a specific week, optionally filtered by disease.

    Structure:
    {
        "week_number": week,
        "date": reference_Sunday_date,
        "total_people": count,
        "disease_counts": {disease: count, ...},
        "infection_rate": float,  # proportion with infection
        "people": [
            {
                "person_id": str,
                "village": str,
                "street": str,
                "infected": bool,  # True if has specific disease
                "disease": str | None,
            }, ...
        ]
    }
    """
    conn = get_connection()
    cur = conn.cursor()

    # Get the Sunday date for this week
    week_date = week_number_to_date(week_number)

    # Build the disease filter
    if disease and disease != "No infection":
        cur.execute(
            "SELECT person_id, infection FROM reports WHERE week_number = ? AND infection = ?",
            (week_number, disease),
        )
    else:
        cur.execute("SELECT person_id, infection FROM reports WHERE week_number = ?", (week_number,))

    rows = cur.fetchall()

    people = []
    disease_counts = Counter()
    total_infected = 0

    for row in rows:
        person_id = row[0]
        infection = row[1] or "No infection"

        is_infected = infection != "No infection"
        if is_infected:
            total_infected += 1
            disease_counts[infection] += 1

        people.append({
            "person_id": person_id,
            "infected": is_infected,
            "disease": infection,
        })

    infection_rate = total_infected / len(people) if people else 0.0

    # Sort people by disease status then by person_id for deterministic order
    people.sort(key=lambda p: (not p["infected"], p["person_id"]))

    cur.close()
    conn.close()

    return {
        "week_number": week_number,
        "date": week_date,
        "total_people": len(people),
        "disease_counts": dict(disease_counts),
        "infection_rate": infection_rate,
        "people": people,
    }


def get_all_historical_aggregation(historical_only: bool = True) -> dict:
    """Aggregate data for all historical weeks.

    Returns dict keyed by week_number with weekly aggregation results.
    """
    max_week = 104 if historical_only else 105
    results = {}

    for week_num in range(1, max_week + 1):
        results[week_num] = get_weekly_aggregation(week_num)

    return results


def get_all_weeks_disease_distribution(historical_only: bool = True) -> dict:
    """Get disease distribution across all weeks.

    Returns dict: {week_number: {disease: count}}
    """
    max_week = 104 if historical_only else 105
    distribution = {}

    for week_num in range(1, max_week + 1):
        agg = get_weekly_aggregation(week_num)
        distribution[week_num] = agg["disease_counts"]

    return distribution


# ---------------------------------------------------------------------------
# Baseline calculation helpers
# ---------------------------------------------------------------------------

def compute_baseline_expected(
    historical_weeks: list,
    method: str = "simple_mean",
    alpha: float = None,
    **kwargs
) -> dict:
    """Compute baseline expected values from historical weeks.

    Methods:
    - "simple_mean": Overall mean across all historical weeks
    - "weighted_mean": Mean with optional weighting parameters
    - "moving_average": Moving average over a window

    Returns dict with baseline parameters.
    """
    # Get historical data
    if not historical_weeks:
        historical_weeks = list(range(1, 105))  # weeks 1-104

    weekly_data = {}
    for week_num in historical_weeks:
        agg = get_weekly_aggregation(week_num)
        weekly_data[week_num] = agg["infection_rate"]

    if method == "simple_mean":
        # Simple average across all historical weeks
        expected = np.mean([weekly_data[w] for w in historical_weeks])
        return {
            "method": "simple_mean",
            "expected": expected,
            "weeks_used": len(historical_weeks),
            "per_week": {w: weekly_data[w] for w in historical_weeks},
        }

    elif method == "moving_average":
        # Moving average over a window size
        window = kwargs.get("window", 4)  # default 4 weeks
        expected_per_week = {}
        for week_num in historical_weeks:
            # Average of this week and previous window-1 weeks
            relevant = [
                weekly_data[w] for w in historical_weeks
                if historical_weeks.index(w) <= historical_weeks.index(week_num)
                and historical_weeks.index(week_num) - historical_weeks.index(w) < window
            ]
            if relevant:
                expected_per_week[week_num] = np.mean(relevant)
            else:
                expected_per_week[week_num] = weekly_data[week_num]

        overall_expected = np.mean(list(expected_per_week.values()))
        return {
            "method": f"moving_average_window_{window}",
            "expected": overall_expected,
            "weeks_used": len(historical_weeks),
            "per_week": expected_per_week,
        }

    else:
        raise ValueError(f"Unknown baseline method: {method}")