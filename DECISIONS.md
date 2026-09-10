# Detector decisions

Every number here came from running `compare_detectors.py` against
`ground_truth.csv`, which the detector never reads. Nothing is estimated.

**Operating rule agreed before any measurement:** maximise recall subject to
alert burden staying at or below **5%** of unit-weeks. A configuration above
that ceiling is rejected however good its recall looks, because burden above
roughly 5% against a 1.3% true outbreak rate is the alert-fatigue failure this
project exists to prevent.

Reproduce with:

```bash
python compare_detectors.py --disease Dengue
python compare_detectors.py            # all diseases, pooled and per-disease
```

---

## What was wrong before

The audit found that **two of the three mechanisms the project claims as novel
were not on the production path.**

`compute_adaptive_baseline` — the alert-excluding, guard-banded baseline — was
imported by **zero** production modules. It had passing tests and no callers.
What detection actually used was `src/evaluation_spatial.py:336`:

```python
baseline_weeks = list(range(max(1, week_num - 20), week_num))
```

A plain 20-week trailing window. No guard band, no exclusion of alert weeks.

`src/detection/confirmation.py` was also dead — zero production importers. The
two-of-two rule does exist, reimplemented inside `combined.py` as
`_determine_confirmation_status`, so behaviour was correct but duplicated.

Spatial localisation was real and working. No issue there.

---

## What was tried

All measured on Dengue, weeks 21-104, confirmation fusion, identical data.

| Configuration | recall | precision | burden | verdict |
|---|---|---|---|---|
| Plain 20-week window (old) | 0.400 | 0.097 | 2.8% | baseline |
| Guard band 1 | 0.400 | 0.081 | 3.4% | no recall gain, more cost |
| **Guard band 2** | **0.533** | **0.090** | **4.1%** | **accepted** |
| Guard band 3 | 0.533 | 0.080 | 4.6% | same recall, more cost |
| Exclude alert weeks, no floor | 0.667 | 0.034 | 13.3% | **rejected — over ceiling** |
| Exclude + minimum baseline 14 | 0.400 | 0.063 | 4.3% | worse than guard band alone |
| Exclude + minimum baseline 17 | 0.200 | 0.053 | 2.6% | recall collapses |

---

## Decision: guard band of 2, no alert exclusion

**Accepted**, with a caveat the pooled numbers hide.

### Pooled, all five diseases, weeks 21-104

| | old | new | change |
|---|---|---|---|
| Recall | 0.262 | **0.317** | +21% |
| Precision | 0.119 | 0.110 | −8% |
| Specificity | 0.974 | 0.965 | −1% |
| F1 | 0.164 | 0.163 | unchanged |
| Alert burden | 2.9% | 3.8% | within 5% ceiling |
| False alarms / location / year | 6.69 | 8.90 | +2.2 |
| Median detection delay | 3 weeks | 3 weeks | unchanged |
| Events found | 6/6 | 6/6 | unchanged |

### Per disease — the pooled figure hides two losses

| Disease | recall old -> new | precision old -> new | burden old -> new | verdict |
|---|---|---|---|---|
| Acute Gastroenteritis | 0.125 -> **0.375** | 0.045 -> **0.075** | 1.0% -> 1.8% | strong win, both metrics |
| Dengue | 0.400 -> **0.533** | 0.097 -> 0.090 | 2.8% -> 4.1% | win |
| Influenza/ARI | 0.278 -> **0.322** | 0.410 -> 0.341 | 2.8% -> 3.9% | win on recall, F1 flat |
| Malaria | 0.208 -> 0.208 | 0.076 -> 0.062 | 3.0% -> 3.7% | **no gain, pure cost** |
| Chikungunya | 0.125 -> 0.125 | 0.009 -> 0.008 | 4.9% -> **5.8%** | **no gain, breaches ceiling** |

Three diseases improve. Malaria pays more false alarms for no extra detection.
Chikungunya does the same and crosses the 5% ceiling.

Chikungunya was already marginal at 4.9% before the change. Its outbreak, E004,
is a four-week `temporary_spike` — over before evidence accumulates — and its
precision of 0.009 means roughly 99% of its alerts were already false. The guard
band neither causes nor fixes that.

**Judged against the agreed rule**, the change is accepted on the pooled result:
recall up 21%, burden 3.8% inside the ceiling, no event lost. The per-disease
losses are recorded rather than averaged away.

### Per location — one village breaches the ceiling

| Village | recall old -> new | precision old -> new | burden old -> new | verdict |
|---|---|---|---|---|
| Village A | 0.200 -> **0.267** | 0.058 -> 0.058 | 1.8% -> 2.3% | win, well inside ceiling |
| Village B | 0.188 -> **0.250** | 0.064 -> 0.058 | 2.5% -> 3.6% | win, inside ceiling |
| Village C | 0.296 -> **0.347** | 0.168 -> 0.159 | 4.1% -> **5.1%** | recall up, **over ceiling** |

Village C carries E003, a ten-street `village_wide_increase`, so it has by far
the most outbreak weeks and the highest alert load to begin with. At 4.1% it was
already close to the limit; the guard band pushes it just past.

**Open option, not taken:** applying the guard band per disease or per village,
enabling it only where it helps. That would keep the wins and drop the losses,
at the cost of a configuration table to justify and maintain. Left out for now
as tuning that would need its own validation to avoid fitting the six events in
this dataset.

**Why the guard band works.** An outbreak contaminates the most recent weeks
first. A window running right up to the decision week absorbs the start of the
rise into what counts as normal, so the rise has to be larger before it looks
unusual. Skipping the two weeks before the decision keeps the comparison
against genuinely quiet weeks.

---

## Rejected: excluding alert weeks from the baseline

This is the mechanism the project describes as its central contribution. **It
does not work on this data, in either form.**

**Without a floor it runs away.** Burden 13.3%, nearly three times the ceiling.
The feedback is self-reinforcing: a unit alerts, that week leaves the window,
the mean drops, the next week is more likely to alert, that week leaves too.
Recall does rise to 0.667, but precision falls to 0.034 — 97% of alerts false.

**With a floor, recall collapses.** A minimum baseline stops the runaway, but
the floor that stops it also suppresses the detections the exclusion was meant
to produce. At minimum 14 the result is worse than the old detector on both
recall and precision; at 17, recall falls to 0.200, half the old value.

There is no setting between those where it beats a plain guard band.

**One implementation trap found along the way.** The first version returned
`mu = 0.0` when the baseline was insufficient. A mean of zero makes any single
case look infinitely unusual, so "insufficient evidence" silently became
"guaranteed alert" — burden rose to 21.3%. Insufficient baselines now return
status `INSUFFICIENT_BASELINE` and make no decision at all. This is the failure
the design warned about, inverted: not silently in-control, but silently
alarming.

**What this means for the write-up.** The claim that excluding alerting weeks
makes the baseline contamination-resistant is not supported by this dataset.
The mechanism is implemented, tested and available behind
`baseline_exclude_alerts=True`, and it is off by default because it measurably
makes the system worse. That is a finding, not a failure — it is the kind of
result that only appears when the mechanism is actually wired in and measured
rather than described.

---

## Kept for the academic comparison, off the production path

- `baseline_exclude_alerts=True` and `baseline_min` remain implemented and
  reachable, so the comparison can be reproduced.
- `src/detection/baseline.py:compute_adaptive_baseline` is still present with
  its tests, cited in the write-up as the mechanism that was evaluated.
- The four single-signal detectors (CUSUM alone, EWMA alone, trend, baseline
  deviation) remain in the evaluation for the four-method comparison.

---

## Leakage

`tests/test_no_leakage.py` — 8 tests, all passing. They fail if the guard is
removed rather than merely passing today.

- Baseline window is strictly before the decision week
- Guard band removes exactly the intended weeks
- Alert exclusion removes weeks from the divisor, not only the sum
- Alert history is append-only and isolated per `(disease, village, street)`
- Detection modules do not reference `ground_truth.csv` at all

Verified independently: `baseline_weeks = range(max(1, week_num - 20), week_num)`
is strictly bounded below the decision week. No leakage was found in the
original code either — this makes it enforced rather than incidental.

---

## A second defect found while measuring

`run_spatial_evaluation` communicated the fusion mode by writing an environment
variable and reloading the config module:

```python
os.environ["COMBINED_FUSION_MODE"] = fusion_mode
importlib.reload(cfg_mod)
```

Process-wide state carrying a per-call argument. One evaluation silently
changed the mode of every later evaluation in the same process, so results
depended on the order calls happened to run in. It surfaced as a test that
passed alone and failed in the suite: a union-mode run returned `PROVISIONAL`,
a status that only exists in confirmation mode.

This matters beyond the test. Any before/after comparison run in one process
was at risk of the second run inheriting the first run's mode. The comparison
in this document is unaffected -- both configurations were run with
`fusion_mode="confirmation"` passed explicitly -- but the defect made that a
matter of luck rather than design.

`fusion_mode` is now an argument to `CombinedDetector.detect()`. The global
mutation is gone, and `test_fusion_mode_does_not_leak_between_calls` fails if
it returns.

---

## A third defect: fusion mode was dropped on the all-diseases path

`run_spatial_evaluation` handles `disease=None` by recursing once per disease.
That recursion forwarded most parameters but **not `fusion_mode`**, so each
recursive call fell back to the config default. Every all-diseases run in
confirmation mode was therefore scored as union.

It surfaced when the pooled figures moved from recall 0.262 to 0.407 and burden
2.9% to 9.2% after unrelated work — union-mode numbers wearing a
confirmation-mode label. Both modes now differ as they should:

```
union          recall=0.407  burden=9.2%   statuses include WATCH
confirmation   recall=0.262  burden=2.9%   statuses include PROVISIONAL
```

Confirmation at guard band 0 reproduces 0.262 / 2.9% exactly, which is the
baseline recorded above, so the comparison in this document stands.

---

## Detection speed: 35x faster

Detection ran at about 65 rows/second. `get_weekly_count` and
`get_spatial_baseline` each opened a fresh database connection per street per
week per disease — roughly 65,000 connections for a full run — and the unit
population was recounted from all 3,000 people on every cell.

Both are now loaded once per run: `WeeklyCounts` holds every
`(week, disease, village, street)` count from a single grouped query, and
`_unit_population` builds its table once per process.

| | before | after |
|---|---|---|
| Throughput | 65 rows/s | **5,689 rows/s** |
| Dengue, 84 weeks | 53s | **0.38s** |
| Dengue, 1 week | 0.6s | **0.10s** |
| Full comparison, 5 diseases, both configs | ~5 min | **~3s** |

The first version of the cache loaded every week in the table, which made the
test suite roughly twice as slow -- most tests evaluate a single week, so a
full-table scan replaced a handful of cheap queries with one expensive one. The
load is now bounded to the week range being evaluated plus the baseline
lookback, so the fixed cost is proportional to the run. A cache with a fixed
cost is only a speedup for large runs.

Verified behaviour-neutral: Dengue recall 0.400 -> 0.533, precision
0.097 -> 0.090, burden 2.8% -> 4.1%, identical to three decimals before and
after. An optimisation that changes results is a bug, not a speedup.

The uncached query path is kept as a fallback for callers that pass no cache,
so existing callers and tests are unaffected.

### A second hot spot: the whole-population evaluation

`get_observed_rate_series` called `get_weekly_aggregation` once per week, and
that function builds a dict for every person in the week -- roughly 3,000
objects -- of which the detectors read nothing. Only the dashboard and the
report writer use the person list. Since the series is rebuilt for each
evaluated week, the cost was quadratic: about 312,000 discarded objects per
call, 84 calls.

Replaced with one grouped SQL query. Values are identical to the old path on
every week checked, and the rate is computed the same way: anyone whose
infection is not "No infection", over all people reporting that week.

| | before | after |
|---|---|---|
| `test_outbreak_detection_metrics` | **161s** | **25.5s** |
| 40-week rate series | 0.16s | 0.036s |

That single test was 42% of the whole suite. `get_weekly_aggregation` is
unchanged, so the dashboard and reports still get their per-person data.

---

## Code quality of the changed files

Checked against the project standard of functions under 50 lines and files
under 800.

Split during this work: `compare_detectors.score` and `main` (into `_timeliness`,
`_breakdown`, `_verdict`), and `get_spatial_baseline` (into `_eligible_weeks`
and `_counts_for_weeks`). All are now inside the limit, and the comparison
output is byte-identical before and after each split.

**`run_spatial_evaluation` is 351 lines and I made it longer**, from about 295,
by adding the alert history, the guard band, the counts cache and the
insufficient-baseline branch. It is the core detection loop. Splitting it now,
after several behavioural changes in one sitting, adds regression risk to the
one function every measurement in this document depends on, without adding
measured value. Recorded here as the top remaining cleanup rather than churned
at the end of a session. The natural extraction is the per-unit body of the
week loop.

`src/evaluation_spatial.py` is 731 lines, inside the 800 limit but close to it.

---

## Still open

- **Detection speed.** 65 rows/second, ~168s for a full run. `get_weekly_count`
  and `get_spatial_baseline` each open a new database connection per street per
  week — roughly 22,000 connections for one run. Loading weekly counts once
  into memory should give a 10-50x speedup. Not yet done.
- **Precision remains low** at 0.090. The ceiling appears to be the data:
  a street sees 0-4 cases in a normal week, so the gap between quiet and
  outbreak sits inside ordinary variation. A parameter sweep over k, h, lambda
  and L found nothing above 0.3.
