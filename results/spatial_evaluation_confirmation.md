# P8 Spatial Evaluation Report: confirmation

## Configuration

- **Fusion mode:** confirmation
- **Ground truth outbreaks:** 6
- **Total outbreak unit-weeks:** 145
- **Total evaluated unit-weeks:** 10790
- **Elapsed time:** 0.1s

## Detector Comparison (Spatial Granularity)

| Detector | TP | FP | FN | TN | Sensitivity | Specificity | PPV | F1 |
|----------|----|----|----|----|-------------|-------------|-----|----|
| Combined Status      |   38 |  280 |  107 | 10365 | 0.262 | 0.974 | 0.119 | 0.164 |
| Cusum Signal         |   44 |  779 |  101 | 9866 | 0.303 | 0.927 | 0.053 | 0.091 |
| Ewma Signal          |   38 |  289 |  107 | 10356 | 0.262 | 0.973 | 0.116 | 0.161 |
| Trend Sustained      |    2 |    3 |  143 | 10642 | 0.014 | 1.000 | 0.400 | 0.027 |
| Baseline Deviation   |   57 | 1149 |   88 | 9496 | 0.393 | 0.892 | 0.047 | 0.084 |

## Over-Escalation vs Under-Escalation (Combined Status)

- **Over-escalation (FP):** 280 false alarms
- **Under-escalation (FN):** 107 missed outbreak unit-weeks
- **Alert burden:** 318 unit-weeks (2.9% of all unit-weeks)
- **True outbreak rate:** 1.34% of unit-weeks

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
| E001 |    6 |   66 |    1 |  480 | 0.857 | 1 | 7 |
| E002 |   12 |  126 |   12 |  606 | 0.500 | 3 | 24 |
| E003 |   44 |  112 |   46 |  638 | 0.489 | 10 | 90 |
| E004 |    3 |   95 |    5 |  485 | 0.375 | 2 | 8 |
| E005 |    6 |   86 |    2 |  662 | 0.750 | 1 | 8 |
| E006 |    6 |  150 |    2 |  632 | 0.750 | 2 | 8 |

## Notes

1. **Spatial unit:** Each (village, street) pair is evaluated independently. 9 of 10 street names appear in multiple villages.
2. **Alert burden:** Percentage of unit-weeks that fire (TP+FP) vs true outbreak rate (1.3%).
3. **Runtime:** Metrics computation took 0.1s.