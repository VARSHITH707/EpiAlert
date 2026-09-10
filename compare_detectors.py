"""Compare detector configurations on identical data.

    python compare_detectors.py                 # all diseases, pooled + per-disease
    python compare_detectors.py --disease Dengue
    python compare_detectors.py --weeks 21 60

Runs each configuration over the same weeks and scores both against
ground_truth.csv, so a change can be judged on numbers rather than on the fact
that it produced more alerts. More alerts is not an improvement by itself.

Reports, for the ALERT/HIGH_ALERT decision:
    recall, precision, specificity, F1, false-positive rate,
    false alarms per location per year, median detection delay,
    median lead time, alert burden

Alert burden is the share of unit-weeks that fire. The agreed operating rule is
recall maximised subject to burden staying at or below 5%; a configuration
above that ceiling is rejected however good its recall looks.

Detection never sees ground truth. It is loaded here, after the fact, purely to
score what the detector already decided.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.evaluation_spatial import run_spatial_evaluation

GROUND_TRUTH = (
    Path(__file__).parent / "data" / "EpiAlert_Phase1_Dataset" / "ground_truth.csv"
)
WEEKS_PER_YEAR = 52.0
BURDEN_CEILING = 0.05  # agreed operating limit

CONFIGS = {
    "old": {
        "label": "OLD  plain 20-week window",
        "baseline_exclude_alerts": False,
        "baseline_guard_band": 0,
    },
    "new": {
        # Guard band only. Excluding alert weeks was tried and rejected: with
        # no floor it runs away (burden 13.3%), and with a floor high enough to
        # stop that, recall collapses below the old detector. Measured, not
        # assumed -- see DECISIONS.md.
        "label": "NEW  guard band 2",
        "baseline_exclude_alerts": False,
        "baseline_guard_band": 2,
    },
}


def load_events() -> list[dict]:
    """Read ground_truth.csv. Read-only, never modified."""
    if not GROUND_TRUTH.exists():
        sys.exit(f"Ground truth not found: {GROUND_TRUTH}")
    events = []
    with GROUND_TRUTH.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            events.append({
                "event_id": row["event_id"],
                "village": row["village"].strip(),
                "disease": row["disease"].strip(),
                "start": int(row["start_week"]),
                "end": int(row["end_week"]),
                "streets": [s.strip() for s in row["affected_streets"].split(";")],
            })
    return events


def outbreak_map(events: list[dict]) -> dict[tuple[str, str, str], set[int]]:
    """(disease, village, street) -> weeks under outbreak."""
    out: dict[tuple[str, str, str], set[int]] = {}
    for ev in events:
        weeks = set(range(ev["start"], ev["end"] + 1))
        for street in ev["streets"]:
            out.setdefault((ev["disease"], ev["village"], street), set()).update(weeks)
    return out


def flatten(result: dict) -> list[dict]:
    """Detection output as a flat list of rows."""
    rows_by_week = result.get("rows_by_week", {})
    if isinstance(rows_by_week, dict):
        return [r for week_rows in rows_by_week.values() for r in week_rows]
    return [r for week_rows in rows_by_week for r in week_rows]


def _timeliness(rows: list[dict], events: list[dict]) -> tuple[list, list, int]:
    """When each event was first alerted on.

    Returns (delays, leads, events_found). Delay is weeks from onset to the
    first alert; lead is weeks of the event still remaining at that point, so
    zero means the alert arrived as the event ended.
    """
    fired_at: dict[tuple[str, str, str], set[int]] = {}
    for row in rows:
        if row["status"] in ("ALERT", "HIGH_ALERT"):
            key = (row["disease"], row["village"], row["street"])
            fired_at.setdefault(key, set()).add(row["week_number"])

    delays, leads, found = [], [], 0
    for ev in events:
        hits = sorted(
            w
            for street in ev["streets"]
            for w in fired_at.get((ev["disease"], ev["village"], street), set())
            if ev["start"] <= w <= ev["end"]
        )
        if hits:
            found += 1
            delays.append(hits[0] - ev["start"])
            leads.append(ev["end"] - hits[0])
    return delays, leads, found


def score(rows: list[dict], truth: dict, events: list[dict],
          weeks_span: int, n_units: int) -> dict:
    """Score detector output against ground truth."""
    tp = fp = fn = tn = 0
    for row in rows:
        key = (row["disease"], row["village"], row["street"])
        is_outbreak = row["week_number"] in truth.get(key, set())
        fired = row["status"] in ("ALERT", "HIGH_ALERT")
        if is_outbreak and fired:
            tp += 1
        elif is_outbreak:
            fn += 1
        elif fired:
            fp += 1
        else:
            tn += 1

    total = tp + fp + fn + tn
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    specificity = tn / (tn + fp) if (tn + fp) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0
    burden = (tp + fp) / total if total else 0.0

    # False alarms per location per year, the number an operator actually feels.
    unit_years = (n_units * weeks_span / WEEKS_PER_YEAR) if weeks_span else 0
    fa_per_loc_year = (fp / unit_years) if unit_years else 0.0

    delays, leads, found = _timeliness(rows, events)

    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn, "total": total,
        "recall": recall, "precision": precision, "specificity": specificity,
        "f1": f1, "fpr": fpr, "burden": burden,
        "fa_per_loc_year": fa_per_loc_year,
        "events_found": found, "events_total": len(events),
        "median_delay": statistics.median(delays) if delays else None,
        "median_lead": statistics.median(leads) if leads else None,
    }


def print_row(name: str, m: dict) -> None:
    delay = "n/a" if m["median_delay"] is None else f"{m['median_delay']:.0f}"
    lead = "n/a" if m["median_lead"] is None else f"{m['median_lead']:.0f}"
    flag = "" if m["burden"] <= BURDEN_CEILING else "  OVER CEILING"
    print(f"  {name:<30} {m['recall']:>6.3f} {m['precision']:>7.3f} "
          f"{m['specificity']:>7.3f} {m['f1']:>6.3f} {m['fpr']:>6.3f} "
          f"{m['fa_per_loc_year']:>7.2f} {delay:>6} {lead:>6} "
          f"{m['burden']:>7.1%}{flag}")


def header() -> None:
    print(f"  {'configuration':<30} {'recall':>6} {'prec':>7} {'spec':>7} "
          f"{'F1':>6} {'FPR':>6} {'FA/loc/y':>7} {'delay':>6} {'lead':>6} "
          f"{'burden':>7}")
    print("  " + "-" * 104)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare detector configurations.")
    parser.add_argument("--disease", default=None,
                        help="Single disease. Default: all.")
    parser.add_argument("--weeks", nargs=2, type=int, default=[21, 104],
                        metavar=("START", "END"))
    args = parser.parse_args()

    start, end = args.weeks
    events = load_events()
    truth = outbreak_map(events)
    span = end - start + 1

    print("=" * 108)
    print(f"DETECTOR COMPARISON   weeks {start}-{end}   "
          f"burden ceiling {BURDEN_CEILING:.0%}")
    print("=" * 108)
    print("Scored against ground_truth.csv, which the detector never reads.\n")

    results = {}
    for key, cfg in CONFIGS.items():
        rows = flatten(run_spatial_evaluation(
            start_week=start, end_week=end,
            disease=args.disease, fusion_mode="confirmation", persist=False,
            baseline_exclude_alerts=cfg["baseline_exclude_alerts"],
            baseline_guard_band=cfg["baseline_guard_band"],
        ))
        n_units = len({(r["village"], r["street"]) for r in rows}) or 1
        relevant = [e for e in events
                    if args.disease is None or e["disease"] == args.disease]
        results[key] = (cfg["label"], rows, score(rows, truth, relevant, span, n_units))

    print("POOLED")
    header()
    for key in CONFIGS:
        label, _, m = results[key]
        print_row(label, m)

    # Pooled numbers hide a configuration that helps one disease or village
    # and wrecks another, so both breakdowns are always printed.
    _breakdown(results, "disease", events, truth, span, args.disease)
    _breakdown(results, "village", events, truth, span, args.disease)

    print("\nEVENTS FOUND")
    for key in CONFIGS:
        label, _, m = results[key]
        print(f"  {label:<30} {m['events_found']}/{m['events_total']}")

    _verdict(results["old"][2], results["new"][2])
    return 0


def _breakdown(results: dict, field: str, events: list[dict], truth: dict,
               span: int, disease_filter: str | None) -> None:
    """Print one table per distinct value of `field` (disease or village)."""
    values = sorted({r[field] for _, rows, _ in results.values() for r in rows})
    if len(values) <= 1:
        return

    for value in values:
        evs = [
            e for e in events
            if e[field] == value
            and (disease_filter is None or e["disease"] == disease_filter)
        ]
        if not evs:
            continue
        print(f"\n{value.upper()}")
        header()
        for key in CONFIGS:
            label, rows, _ = results[key]
            subset = [r for r in rows if r[field] == value]
            n_units = len({(r["village"], r["street"]) for r in subset}) or 1
            print_row(label, score(subset, truth, evs, span, n_units))


def _verdict(old: dict, new: dict) -> None:
    """State whether the new configuration is accepted, and on what grounds."""
    print("\nVERDICT")
    print(f"  recall     {old['recall']:.3f} -> {new['recall']:.3f}")
    print(f"  precision  {old['precision']:.3f} -> {new['precision']:.3f}")
    print(f"  burden     {old['burden']:.1%} -> {new['burden']:.1%}")

    if new["burden"] > BURDEN_CEILING:
        print(f"\n  NEW IS REJECTED: burden {new['burden']:.1%} exceeds the "
              f"{BURDEN_CEILING:.0%} ceiling, whatever its recall.")
    elif new["recall"] > old["recall"]:
        print("\n  NEW WINS: higher recall, burden within ceiling.")
    else:
        print("\n  NEW DOES NOT WIN: no recall gain.")


if __name__ == "__main__":
    sys.exit(main())
