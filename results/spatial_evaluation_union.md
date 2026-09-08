# P8 Spatial Evaluation Report: union

## Configuration

- **Fusion mode:** union
- **Ground truth outbreaks:** 6
- **Total outbreak unit-weeks:** 145
- **Total evaluated unit-weeks:** 10920
- **Elapsed time:** 0.1s

## Detector Comparison (Spatial Granularity)

| Detector | TP | FP | FN | TN | Sensitivity | Specificity | PPV | F1 |
|----------|----|----|----|----|-------------|-------------|-----|----|
| Combined Status      |   77 | 1737 |   68 | 9038 | 0.531 | 0.839 | 0.042 | 0.079 |
| Cusum Signal         |   44 |  781 |  101 | 9994 | 0.303 | 0.928 | 0.053 | 0.091 |
| Ewma Signal          |   38 |  290 |  107 | 10485 | 0.262 | 0.973 | 0.116 | 0.161 |
| Trend Sustained      |    2 |    3 |  143 | 10772 | 0.014 | 1.000 | 0.400 | 0.027 |
| Baseline Deviation   |   57 | 1162 |   88 | 9613 | 0.393 | 0.892 | 0.047 | 0.084 |

## Over-Escalation vs Under-Escalation (Combined Status)

- **Over-escalation (FP):** 1737 false alarms
- **Under-escalation (FN):** 68 missed outbreak unit-weeks
- **Alert burden:** 1814 unit-weeks (16.6% of all unit-weeks)
- **True outbreak rate:** 1.33% of unit-weeks

## Timeliness by Outbreak

| Event | Disease | Village | True Start | First Detected | Lead Time | Unit |
|-------|---------|---------|------------|----------------|-----------|------|
| E001 | Dengue | Village A | 27 | 27 | 0w | Village A/Street 4 |
| E002 | Malaria | Village B | 40 | 40 | 0w | Village B/Street 4 |
| E003 | Influenza/ARI | Village C | 58 | 59 | 1w | Village C/Street 2 |
| E004 | Chikungunya | Village A | 72 | 72 | 0w | Village A/Street 2 |
| E005 | Acute Gastroenteritis | Village B | 82 | 82 | 0w | Village B/Street 6 |
| E006 | Dengue | Village C | 93 | 93 | 0w | Village C/Street 8 |

## Per-Outbreak Breakdown (Combined Status)

| Event | TP | FP | FN | TN | Sensitivity | Units | Unit-Weeks |
|-------|----|----|----|----|------------|-------|------------|
| E001 |    6 |   70 |    1 |  511 | 0.857 | 1 | 7 |
| E002 |   12 |  126 |   12 |  606 | 0.500 | 3 | 24 |
| E003 |   44 |  112 |   46 |  638 | 0.489 | 10 | 90 |
| E004 |    3 |   95 |    5 |  485 | 0.375 | 2 | 8 |
| E005 |    6 |   86 |    2 |  662 | 0.750 | 1 | 8 |
| E006 |    6 |  153 |    2 |  679 | 0.750 | 2 | 8 |

## Notes

1. **Spatial unit:** Each (village, street) pair is evaluated independently. 9 of 10 street names appear in multiple villages.
2. **Alert burden:** Percentage of unit-weeks that fire (TP+FP) vs true outbreak rate (1.3%).
3. **Runtime:** Metrics computation took 0.1s.