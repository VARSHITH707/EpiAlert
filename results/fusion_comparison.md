# P8 Fusion Mode Comparison: Union vs Confirmation

## Overview

This report compares two fusion rules for the combined detector at spatial granularity (disease, village, street):

- **Union mode:** Any 2 of 4 signals (CUSUM, EWMA, trend, baseline) fire -> ALERT. Errors accumulate because the union of four weak detectors' false alarms is wider than any one alone.
- **Confirmation mode:** CUSUM AND EWMA must BOTH fire for ALERT. Exactly one firing = PROVISIONAL (logged, no alert). Implements the Yi et al. 2025 concordance rule (>=2 concordant models gave Youden index 0.651, sens 0.739, spec 0.912).

## Side-by-Side Comparison

| Metric | Union Mode | Confirmation Mode | Delta |
|--------|------------|-------------------|-------|
### Combined Status

| Metric | Union | Confirmation |
|--------|-------|--------------|
| TP           |    77 |    38 |
| FP           |  1737 |   280 |
| FN           |    68 |   107 |
| TN           |  9038 | 10365 |
| sensitivity  | 0.531 | 0.262 |
| specificity  | 0.839 | 0.974 |
| PPV          | 0.042 | 0.119 |
| F1           | 0.079 | 0.164 |

### Cusum Signal

| Metric | Union | Confirmation |
|--------|-------|--------------|
| TP           |    44 |    44 |
| FP           |   781 |   779 |
| FN           |   101 |   101 |
| TN           |  9994 |  9866 |
| sensitivity  | 0.303 | 0.303 |
| specificity  | 0.928 | 0.927 |
| PPV          | 0.053 | 0.053 |
| F1           | 0.091 | 0.091 |

### Ewma Signal

| Metric | Union | Confirmation |
|--------|-------|--------------|
| TP           |    38 |    38 |
| FP           |   290 |   289 |
| FN           |   107 |   107 |
| TN           | 10485 | 10356 |
| sensitivity  | 0.262 | 0.262 |
| specificity  | 0.973 | 0.973 |
| PPV          | 0.116 | 0.116 |
| F1           | 0.161 | 0.161 |

### Trend Sustained

| Metric | Union | Confirmation |
|--------|-------|--------------|
| TP           |     2 |     2 |
| FP           |     3 |     3 |
| FN           |   143 |   143 |
| TN           | 10772 | 10642 |
| sensitivity  | 0.014 | 0.014 |
| specificity  | 1.000 | 1.000 |
| PPV          | 0.400 | 0.400 |
| F1           | 0.027 | 0.027 |

### Baseline Deviation

| Metric | Union | Confirmation |
|--------|-------|--------------|
| TP           |    57 |    57 |
| FP           |  1162 |  1149 |
| FN           |    88 |    88 |
| TN           |  9613 |  9496 |
| sensitivity  | 0.393 | 0.393 |
| specificity  | 0.892 | 0.892 |
| PPV          | 0.047 | 0.047 |
| F1           | 0.084 | 0.084 |

## Alert Burden Comparison

| Metric | Union Mode | Confirmation Mode |
|--------|------------|-------------------|
| Total unit-weeks evaluated | 10920 | 10790 |
| True outbreak unit-weeks | 145 | 145 |
| True outbreak rate | 1.33% | 1.33% |
| Alerts fired (TP+FP) | 1814 | 318 |
| Alert burden (% of unit-weeks) | 16.6% | 2.9% |
| Over-alerting ratio (burden / true rate) | 12.5x | 2.2x |

## Timeliness Comparison

| Event | Union First Detected | Union Lead Time | Confirmation First Detected | Confirmation Lead Time |
|-------|---------------------|----------------|---------------------------|------------------------|
| E001 | 27 | 0w | 27 | 0w |
| E002 | 40 | 0w | 40 | 0w |
| E003 | 59 | 1w | 59 | 1w |
| E004 | 72 | 0w | 72 | 0w |
| E005 | 82 | 0w | 82 | 0w |
| E006 | 93 | 0w | 93 | 0w |

## Notes

1. **Authoritative evaluation:** The spatial evaluation (`evaluation_spatial.py`) is the primary evaluation for the EpiAlert paper (small-area detection). Whole-population evaluation (`evaluation.py`) is a comparison only.
2. **Confirmation mode** requires CUSUM+EWMA concordance for ALERT; single-firing rows become PROVISIONAL and are NOT counted as alerts in the metrics above. The metrics count PROVISIONAL as non-alert (TN when no outbreak, FP when outbreak but provisional is still 'not an alert' for reporting purposes — see code for exact handling).
3. **Do not tune to make numbers look good.** The full sweep is reported in the parameter sweep document. If no configuration achieves usable precision, that is the finding.