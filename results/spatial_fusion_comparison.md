# P8 Fusion Mode Comparison: UNION vs CONFIRMATION

## Background

The original P8 combined detector uses a **UNION** fusion rule: any 2 of 4
signals (CUSUM, EWMA, trend, baseline deviation) → ALERT. This aggregates
false alarms from all four components, so errors accumulate.

Yi et al. (2025) found that requiring **>=2 concordant models** gave the best
Youden index (0.651, sensitivity 0.739, specificity 0.912), while requiring
>=1 model raised sensitivity to 0.902 but dropped specificity to 0.788.

This report implements a **CONFIRMATION** fusion rule as an alternative:
- ALERT requires CUSUM **AND** EWMA to both fire (concordant)
- Exactly one of {CUSUM, EWMA} firing = PROVISIONAL (logged, not an alert)
- Trend + baseline alone = PROVISIONAL unless both fire

---

**Precomputation:** 10,920 cells (observed count + baseline per unit-week)
**Evaluation period:** weeks 21-104 (84 weeks)
**Spatial units by disease:** Dengue: 26, Malaria: 26, Chikungunya: 26, Influenza/ARI: 26, Acute Gastroenteritis: 26
**Outbreak unit-weeks in period:** 145

## Part 1: Default Parameters (h=5.0, k=0.5, lambda=0.2, L=3.0)

| Metric | UNION (original) | CONFIRMATION (new) |
|--------|-------------------|---------------------|
| True Positives                 | 77 | 59 |
| False Positives (over-escalation) | 1737 | 941 |
| False Negatives (under-escalation) | 68 | 86 |
| True Negatives                 | 9038 | 9834 |
| Sensitivity (Recall)           | 0.531 | 0.407 |
| Specificity                    | 0.839 | 0.913 |
| PPV (Precision)                | 0.042 | 0.059 |
| F1 Score                       | 0.079 | 0.103 |
| Total Alerts Fired             | 1814 | 1000 |
| Alert Burden (% of unit-weeks) | 16.612 | 9.158 |

**True outbreak rate:** 145/10920 = 1.33% of unit-weeks
**UNION alert rate:** 16.6% (1814 alerts)
**CONFIRMATION alert rate:** 9.2% (1000 alerts)

> **WARNING:** UNION mode fires alerts on 16.6% of unit-weeks
> against a 1.33% true outbreak rate — a 12.5x over-alerting ratio.
> PPV=0.042 means 95.8% of alerts are FALSE.

---

## Part 2: Parameter Sweep

Grid: h ∈ {3.0,5.0,7.0} × k ∈ {0.25,0.50}
lambda = 0.2 (fixed), L = 3.0 (fixed)
Total: 6 configurations per mode  (12 total runs)

### UNION Mode — Parameter Sweep Results

| h | k | TP | FP | FN | TN | Sens | Spec | PPV | F1 | Alerts | Alert% |
|---|----|----|----|----|-----|------|------|-----|-----|--------|--------|
| 3.0 | 0.25 |  105 | 3756 |   40 |  7019 | 0.724 | 0.651 | 0.027 | 0.052 |   3861 |  35.4% |
| 3.0 | 0.50 |   89 | 2275 |   56 |  8500 | 0.614 | 0.789 | 0.038 | 0.071 |   2364 |  21.6% |
| 5.0 | 0.25 |   87 | 2648 |   58 |  8127 | 0.600 | 0.754 | 0.032 | 0.060 |   2735 |  25.0% |
| 5.0 | 0.50 |   77 | 1737 |   68 |  9038 | 0.531 | 0.839 | 0.042 | 0.079 |   1814 |  16.6% |
| 7.0 | 0.25 |   80 | 1992 |   65 |  8783 | 0.552 | 0.815 | 0.039 | 0.072 |   2072 |  19.0% |
| 7.0 | 0.50 |   75 | 1494 |   70 |  9281 | 0.517 | 0.861 | 0.048 | 0.088 |   1569 |  14.4% |

**Best PPV:** h=7.0, k=0.5 → PPV=0.048, Sens=0.517, F1=0.088, Alerts=1569 (14.4%)
**Best F1:** h=7.0, k=0.5 → PPV=0.048, Sens=0.517, F1=0.088, Alerts=1569 (14.4%)

### CONFIRMATION Mode — Parameter Sweep Results

| h | k | TP | FP | FN | TN | Sens | Spec | PPV | F1 | Alerts | Alert% |
|---|----|----|----|----|-----|------|------|-----|-----|--------|--------|
| 3.0 | 0.25 |   68 | 1069 |   77 |  9706 | 0.469 | 0.901 | 0.060 | 0.106 |   1137 |  10.4% |
| 3.0 | 0.50 |   63 |  982 |   82 |  9793 | 0.434 | 0.909 | 0.060 | 0.106 |   1045 |   9.6% |
| 5.0 | 0.25 |   63 |  995 |   82 |  9780 | 0.434 | 0.908 | 0.060 | 0.105 |   1058 |   9.7% |
| 5.0 | 0.50 |   59 |  941 |   86 |  9834 | 0.407 | 0.913 | 0.059 | 0.103 |   1000 |   9.2% |
| 7.0 | 0.25 |   59 |  948 |   86 |  9827 | 0.407 | 0.912 | 0.059 | 0.102 |   1007 |   9.2% |
| 7.0 | 0.50 |   51 |  890 |   94 |  9885 | 0.352 | 0.917 | 0.054 | 0.094 |    941 |   8.6% |

**Best PPV:** h=3.0, k=0.5 → PPV=0.060, Sens=0.434, F1=0.106, Alerts=1045 (9.6%)
**Best F1:** h=3.0, k=0.25 → PPV=0.060, Sens=0.469, F1=0.106, Alerts=1137 (10.4%)

---

## Part 3: Findings

### Key Observations

1. **Union mode over-alerts badly:** At default parameters, the union rule fires
   on 16.6% of unit-weeks vs 1.33% true outbreak rate.
   PPV=0.042 means 95.8% of alerts are false.

2. **Confirmation mode improves PPV:** 0.059 vs 0.042 — a 39% relative improvement in precision.
   However, sensitivity drops to 0.407 from 0.531.

3. **PPV > 0.3 threshold:**
   - UNION mode: 0/6 configs reach PPV > 0.3
   - CONFIRMATION mode: 0/6 configs reach PPV > 0.3

   - **No UNION configuration reaches PPV > 0.3.**
   - **No CONFIRMATION configuration reaches PPV > 0.3.**

### Alert Burden

The true outbreak rate is 1.33% of unit-weeks (145 outbreak unit-weeks / 10,920 total).
An acceptable alert burden should be within an order of magnitude of this — say 2-10%.

- UNION configs with 2-10% alert burden AND PPV > 0.1: 0
- CONFIRMATION configs with 2-10% alert burden AND PPV > 0.1: 0

> **Finding:** No parameter configuration achieves both acceptable alert burden
> (2-10%) AND usable precision (PPV > 0.1) at spatial granularity with these
> four signals. The combined detector as specified is not working at this granularity.

---

## Part 4: Recommendation

The current combined detector (UNION, any-2-of-4) is **not suitable** for spatial
granularity detection without major rework. The confirmation mode (CUSUM+EWMA
concordance) provides better precision but still falls short of usable PPV at
reasonable sensitivity. Options:

1. **Tune for confirmation mode** — increase h and L to reduce false alarms.
2. **Add spatial smoothing** — borrow strength across nearby streets to reduce
   per-unit variance and false alarms.
3. **Use a different fusion rule** — e.g. CUSUM-only or EWMA-only with tuned
   thresholds may outperform the combined rule at this granularity.
4. **Accept the current PPV** if the system is used for screening (high sensitivity)
   rather than confirmation, and follow up all alerts with manual review.
