"""P8 Spatial evaluation — detection per (disease, village, street) unit.

Wires the combined detector into the evaluation framework so results
are produced at spatial granularity, not population-wide.

Architecture:
- For each week in the evaluation range:
  - For each (disease, village, street) spatial unit:
    - Get baseline (mu, sigma) from historical weeks for that unit
    - Get current week count for that unit
    - Run CUSUM, EWMA, trend, baseline deviation
    - Run CombinedDetector to fuse into NORMAL/WATCH/ALERT/HIGH_ALERT
    - Persist to detection_results table
- Maintains SEPARATE stateful detector instances per spatial unit
  so CUSUM/EMWA state is not corrupted across units.
"""

from src.database.db import get_connection, init_schema
from src.detection.combined import CombinedDetector
from src.detection.cusum import cusum_compute, H_CUSUM, K_CUSUM
from src.detection.ewma import ewma_compute, ewma_ucl, LAMBDA_EWMA, L_EWMA
from src.ingestion import week_number_to_date, load_people_map
from collections import defaultdict
import json

# ---------------------------------------------------------------------------
# Spatial unit enumeration
# ---------------------------------------------------------------------------

def get_spatial_units(disease: str = None) -> list:
    """Return list of (village, street, population) for all spatial units.

    If disease is specified, only units that have had that disease
    in any week are returned.  Otherwise all units are returned.
    """
    people_map = load_people_map()
    spatial_pop = defaultdict(int)
    spatial_has_disease = defaultdict(bool)

    conn = get_connection()
    cur = conn.cursor()

    # Build population map
    for pid, (village, street) in people_map.items():
        spatial_pop[(village, street)] += 1

    if disease:
        # Only units that have had this disease
        cur.execute(
            "SELECT DISTINCT village, street FROM reports WHERE infection = ?",
            (disease,),
        )
        for row in cur.fetchall():
            spatial_has_disease[(row[0], row[1])] = True

    cur.close()
    conn.close()

    units = []
    for (village, street), pop in sorted(spatial_pop.items()):
        if disease is None or spatial_has_disease.get((village, street), False):
            units.append((village, street, pop))

    return units


def get_weekly_count(week_num: int, disease: str, village: str, street: str) -> int:
    """Return observed count for a specific (disease, village, street, week)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT COUNT(*) FROM reports
           WHERE week_number = ? AND infection = ? AND village = ? AND street = ?""",
        (week_num, disease, village, street),
    )
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count


def get_spatial_baseline(week_num: int, disease: str, village: str, street: str,
                         baseline_weeks: list) -> dict:
    """Compute baseline (mu, sigma) for a spatial unit from historical weeks.

    Returns dict with:
    - mu: expected rate (cases / population)
    - sigma: standard deviation of rate
    - Bt_size: number of historical weeks used
    - weekly_counts: list of (week_num, count) for the baseline window
    """
    counts = []
    conn = get_connection()
    cur = conn.cursor()

    for bw in baseline_weeks:
        cur.execute(
            """SELECT COUNT(*) FROM reports
               WHERE week_number = ? AND infection = ? AND village = ? AND street = ?""",
            (bw, disease, village, street),
        )
        count = cur.fetchone()[0]
        counts.append((bw, count))

    cur.close()
    conn.close()

    if not counts:
        return {"mu": 0.0, "sigma": 0.0, "Bt_size": 0, "weekly_counts": []}

    # Compute rate per week
    # Use the population of this spatial unit
    people_map = load_people_map()
    population = 0
    for pid, (v, s) in people_map.items():
        if v == village and s == street:
            population += 1

    if population == 0:
        return {"mu": 0.0, "sigma": 0.0, "Bt_size": 0, "weekly_counts": []}

    rates = [c / population for _, c in counts]
    mu = sum(rates) / len(rates)

    if len(rates) > 1:
        variance = sum((r - mu) ** 2 for r in rates) / (len(rates) - 1)
        sigma = variance ** 0.5
    else:
        sigma = 0.0

    return {
        "mu": mu,
        "sigma": sigma,
        "Bt_size": len(counts),
        "weekly_counts": counts,
    }


# ---------------------------------------------------------------------------
# Stateful detector registry — one CUSUM + one EWMA per spatial unit
# ---------------------------------------------------------------------------

class SpatialCUSUMState:
    """Maintains CUSUM S_t state per spatial unit.

    CUSUM is stateful — the S statistic carries across weeks.
    Sharing one state across streets would corrupt every series.
    """

    def __init__(self):
        # Key: (disease, village, street) -> S_t (float)
        self._state = {}

    def get_prev_S(self, disease: str, village: str, street: str) -> float:
        """Get previous CUSUM statistic for a spatial unit."""
        return self._state.get((disease, village, street), 0.0)

    def set_S(self, disease: str, village: str, street: str, S_t: float):
        """Set current CUSUM statistic for a spatial unit."""
        self._state[(disease, village, street)] = S_t

    def reset(self):
        """Clear all state."""
        self._state.clear()


class SpatialEWMAState:
    """Maintains EWMA Z_t state per spatial unit.

    EWMA is stateful — the Z statistic carries across weeks.
    Sharing one state across streets would corrupt every series.
    """

    def __init__(self):
        # Key: (disease, village, street) -> Z_t (float)
        self._state = {}

    def get_prev_Z(self, disease: str, village: str, street: str) -> float:
        """Get previous EWMA statistic for a spatial unit."""
        return self._state.get((disease, village, street), 0.0)

    def set_Z(self, disease: str, village: str, street: str, Z_t: float):
        """Set current EWMA statistic for a spatial unit."""
        self._state[(disease, village, street)] = Z_t

    def reset(self):
        """Clear all state."""
        self._state.clear()


# ---------------------------------------------------------------------------
# Main spatial evaluation
# ---------------------------------------------------------------------------

def run_spatial_evaluation(
    start_week: int = 21,
    end_week: int = 25,
    disease: str = "Dengue",
    baseline_method: str = "simple_mean",
    baseline_threshold: float = 2.0,
    cusum_h: float = None,
    cusum_k: float = None,
    ewma_alpha: float = None,
    ewma_l: float = None,
    persist: bool = True,
    fusion_mode: str = None,
) -> dict:
    """Run all four detectors per spatial unit per week.

    Parameters
    ----------
    start_week : int
        First week to evaluate.
    end_week : int
        Last week to evaluate.
    disease : str
        Disease to evaluate (default: "Dengue").
    baseline_method : str
        Not used directly — always uses historical count mean.
    baseline_threshold : float
        Not used in spatial mode — baseline deviation is handled by
        CombinedDetector's config-driven threshold.
    cusum_h : float
        CUSUM decision threshold h. Defaults to config CUSUM_H.
    cusum_k : float
        CUSUM reference value k. Defaults to config CUSUM_K.
    ewma_alpha : float
        EWMA smoothing parameter. Defaults to config LAMBDA_EWMA.
    ewma_l : float
        EWMA control limit L. Defaults to config L_EWMA.
    persist : bool
        If True, write results to detection_results table.

    Returns
    -------
    dict with:
    - spatial_units: list of (village, street) evaluated
    - total_rows: total result rows produced
    - rows_by_week: dict week -> list of result dicts
    - summary: counts by status
    """
    from src.detection import config as det_config

    if cusum_h is None:
        cusum_h = det_config.CUSUM_H
    if cusum_k is None:
        cusum_k = det_config.CUSUM_K
    if ewma_alpha is None:
        ewma_alpha = det_config.EWMA_LAMBDA
    if ewma_l is None:
        ewma_l = det_config.EWMA_L
    if fusion_mode is None:
        fusion_mode = det_config.COMBINED_FUSION_MODE

    # Set fusion mode in config for the CombinedDetector to read
    # (We do this by setting an env var that the config reads)
    import os
    os.environ["COMBINED_FUSION_MODE"] = fusion_mode
    # Re-import to pick up the change
    import importlib
    import src.detection.config as cfg_mod
    importlib.reload(cfg_mod)
    from src.detection import config as det_config2
    # Update our local refs
    cusum_h = det_config2.CUSUM_H
    cusum_k = det_config2.CUSUM_K
    ewma_alpha = det_config2.EWMA_LAMBDA
    ewma_l = det_config2.EWMA_L

    # If disease is None, evaluate all diseases separately and merge
    if disease is None:
        from src.ingestion import DISEASES
        all_results = {
            "disease": "ALL",
            "spatial_units": [],
            "total_rows": 0,
            "rows_by_week": {},
            "summary": {"NORMAL": 0, "PROVISIONAL": 0, "WATCH": 0, "ALERT": 0, "HIGH_ALERT": 0},
        }
        for dis in DISEASES:
            disease_result = run_spatial_evaluation(
                start_week=start_week,
                end_week=end_week,
                disease=dis,
                baseline_method=baseline_method,
                baseline_threshold=baseline_threshold,
                cusum_h=cusum_h,
                cusum_k=cusum_k,
                ewma_alpha=ewma_alpha,
                ewma_l=ewma_l,
                persist=persist,
            )
            all_results["spatial_units"].extend(disease_result["spatial_units"])
            all_results["total_rows"] += disease_result["total_rows"]
            for week_key, rows in disease_result["rows_by_week"].items():
                if week_key not in all_results["rows_by_week"]:
                    all_results["rows_by_week"][week_key] = []
                all_results["rows_by_week"][week_key].extend(rows)
                for row in rows:
                    all_results["summary"][row["status"]] += 1
        # Deduplicate spatial units
        seen = set()
        unique_units = []
        for v, s in all_results["spatial_units"]:
            if (v, s) not in seen:
                seen.add((v, s))
                unique_units.append((v, s))
        all_results["spatial_units"] = sorted(unique_units)
        return all_results

    spatial_units = get_spatial_units(disease=disease)
    cusum_state = SpatialCUSUMState()
    ewma_state = SpatialEWMAState()

    results = {
        "disease": disease,
        "spatial_units": [(v, s) for v, s, _ in spatial_units],
        "total_rows": 0,
        "rows_by_week": {},
        "summary": {
            "NORMAL": 0,
            "PROVISIONAL": 0,
            "WATCH": 0,
            "ALERT": 0,
            "HIGH_ALERT": 0,
        },
    }

    if persist:
        init_schema()
        conn = get_connection()
        cur = conn.cursor()

    for week_num in range(start_week, end_week + 1):
        week_results = []
        baseline_weeks = list(range(max(1, week_num - 20), week_num))

        for village, street, population in spatial_units:
            # --- Baseline ---
            baseline = get_spatial_baseline(
                week_num, disease, village, street, baseline_weeks
            )
            mu = baseline["mu"]
            sigma = baseline["sigma"]

            # --- Current week count ---
            observed_count = get_weekly_count(week_num, disease, village, street)
            observed_rate = observed_count / population if population > 0 else 0.0

            # --- CUSUM (using per-spatial-unit data, not whole-population) ---
            prev_S = cusum_state.get_prev_S(disease, village, street)
            n_population = population

            # Binomial SE for this spatial unit
            if 0 < mu < 1 and n_population > 0:
                sigma_cusum = (mu * (1 - mu) / n_population) ** 0.5
            else:
                sigma_cusum = 0.001

            cusum_result = cusum_compute(
                week_num=week_num,
                x_t=observed_rate,
                mu_t=mu,
                sigma_t=sigma_cusum,
                prev_S=prev_S,
                k=cusum_k,
                h=cusum_h,
            )
            cusum_signal = 1 if cusum_result["alert"] else 0
            cusum_S = cusum_result["S_t"]
            cusum_state.set_S(disease, village, street, cusum_S)

            # --- EWMA (using per-spatial-unit data, not whole-population) ---
            prev_Z = ewma_state.get_prev_Z(disease, village, street)

            # Convert to z-score space for EWMA
            if sigma_cusum > 0:
                z_t = (observed_rate - mu) / sigma_cusum
            else:
                z_t = 0.0

            ewma_result = ewma_compute(
                week_num=week_num,
                z_t=z_t,
                prev_Z=prev_Z,
                lambda_=ewma_alpha,
                L=ewma_l,
            )
            ewma_signal = 1 if ewma_result["alert"] else 0
            ewma_Z = ewma_result["Z_t"]
            ewma_UCL = ewma_result["UCL_t"]
            ewma_state.set_Z(disease, village, street, ewma_Z)

            # --- Trend ---
            # Get recent weekly counts for this spatial unit
            recent_counts = []
            for bw in range(week_num - 3, week_num + 1):
                c = get_weekly_count(bw, disease, village, street)
                recent_counts.append(c)
            # Pad if not enough history
            while len(recent_counts) < 4:
                recent_counts.insert(0, 0)

            # --- Baseline deviation ---
            baseline_deviation_sigma = 0.0
            if sigma > 0 and mu > 0:
                baseline_deviation_sigma = (observed_rate - mu) / sigma

            # --- Combined detector ---
            combined = CombinedDetector(
                disease=disease,
                village=village,
                street=street,
                baseline_expected=mu,
                n_population=population,
            )
            combined_result = combined.detect(
                week_num=week_num,
                observed_count=observed_count,
                cusum_S=cusum_S,
                ewma_Z=ewma_Z,
                cusum_h=cusum_h,
                ewma_UCL=ewma_UCL,
                baseline_mu=mu,
                baseline_sigma=sigma,
                weekly_counts=recent_counts,
                cusum_k=cusum_k,
                ewma_lambda=ewma_alpha,
                ewma_l=ewma_l,            )

            row = {
                "week_number": week_num,
                    "disease": disease,
                    "village": village,
                    "street": street,
                    "observed_count": observed_count,
                    "expected_count": round(mu * population, 2),
                    "cusum_signal": cusum_signal,
                    "ewma_signal": ewma_signal,
                    "trend_sustained": combined_result["trend_sustained"],
                    "baseline_deviation": round(baseline_deviation_sigma, 4),
                    "status": combined_result["status"],
                    "severity": combined_result["severity"],
                    "explanation": combined_result["explanation"],
                    # Also store individual detector statuses for audit
                    "cusum_status": "ALERT" if cusum_signal else "NORMAL",
                    "ewma_status": "ALERT" if ewma_signal else "NORMAL",
                    "baseline_status": "ALERT" if (sigma > 0 and abs(baseline_deviation_sigma) >= 1.5) else "NORMAL",
                }
            week_results.append(row)
            results["summary"][row["status"]] += 1

            if persist:
                cur.execute(
                    """INSERT INTO detection_results
                       (week_number, disease, village, street, observed_count,
                        expected_count, cusum_signal, ewma_signal, trend_sustained,
                        baseline_deviation, status, severity, explanation, fusion_mode)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(week_number, disease, village, street, fusion_mode)
                       DO UPDATE SET
                           observed_count = excluded.observed_count,
                           expected_count = excluded.expected_count,
                           cusum_signal = excluded.cusum_signal,
                           ewma_signal = excluded.ewma_signal,
                           trend_sustained = excluded.trend_sustained,
                           baseline_deviation = excluded.baseline_deviation,
                           status = excluded.status,
                           severity = excluded.severity,
                           explanation = excluded.explanation,
                           detected_at = CURRENT_TIMESTAMP""",
                    (
                        row["week_number"], row["disease"], row["village"],
                        row["street"], row["observed_count"], row["expected_count"],
                        row["cusum_signal"], row["ewma_signal"],
                        row["trend_sustained"], row["baseline_deviation"],
                        row["status"], row["severity"], row["explanation"],
                        fusion_mode,
                    ),
                )

        results["rows_by_week"][f"week_{week_num:03d}"] = week_results
        results["total_rows"] += len(week_results)

    if persist:
        conn.commit()
        cur.close()
        conn.close()

    return results


def run_spatial_evaluation_comparison(
    start_week: int = 21,
    end_week: int = 104,
    disease: str = None,
) -> dict:
    """Run spatial evaluation under BOTH fusion modes and compare.

    Returns dict with results for both 'union' and 'confirmation' modes.
    """
    results = {}
    for mode in ["union", "confirmation"]:
        print(f"Running spatial evaluation with fusion_mode={mode}...")
        result = run_spatial_evaluation(
            start_week=start_week,
            end_week=end_week,
            disease=disease,
            persist=False,
            fusion_mode=mode,
        )
        results[mode] = result
    return results


def get_detection_results_summary() -> dict:
    """Query detection_results table for summary statistics."""
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM detection_results")
    total = cur.fetchone()[0]

    cur.execute("SELECT status, COUNT(*) FROM detection_results GROUP BY status")
    by_status = {row[0]: row[1] for row in cur.fetchall()}

    cur.execute(
        "SELECT COUNT(DISTINCT village || '|' || street) FROM detection_results"
    )
    distinct_units = cur.fetchone()[0]

    cur.execute(
        "SELECT DISTINCT week_number FROM detection_results ORDER BY week_number"
    )
    weeks = [row[0] for row in cur.fetchall()]

    cur.close()
    conn.close()

    return {
        "total_rows": total,
        "by_status": by_status,
        "distinct_spatial_units": distinct_units,
        "weeks_covered": weeks,
    }
