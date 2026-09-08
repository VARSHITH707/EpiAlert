EpiAlert Phase-1 Synthetic Dataset
Seed: 20260829
3 villages; 1,000 people each; 3,000 people total.
Village A=7 streets, Village B=9 streets, Village C=10 streets.
104 weekly datasets; 3,000 individual reports per week; 312,000 reports total.

IMPORTANT DATE RULE:
Every person in the SAME week is checked on the SAME Sunday.
Week 001 = 12 January 2025.
Week 002 = 19 January 2025.
...
Week 104 = 27 December 2026.

Each week contains:
week_xxx/reports/P0001.txt ... P3000.txt

Each person has exactly one report in every week.
Raw reports contain only person, village, street, date, and infection/no-infection.
No case counts, baseline, CUSUM, EWMA, spread, localization, outbreak labels,
or alerts are included in the raw input.

Weeks 1-20: baseline initialization.
Weeks 21-104: detection/testing.
ground_truth.csv: hidden evaluation file; never provide it to EpiAlert.
