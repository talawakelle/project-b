TTE PROJECT B - NASA VS STATION PROVISIONAL COMPARISON v1.0.0
================================================================

PURPOSE
Compare the validated NASA POWER snapshot against the Pedro and TRI daily
weather caches. This is a diagnostic source comparison only. It does not train,
retrain, score, select, allocate, or modify any model.

WHY THE RESULT REMAINS PROVISIONAL
- The named raw station workbooks were not found in the controlled source pack.
- Only two highland stations are available.
- Both stations map to the same coarse NASA meteorological grid cell.
- The comparison cannot validate low-country estates or all 37 estates.

HOW TO RUN
1. Extract this package to any local folder.
2. Open PowerShell in the extracted folder.
3. Run:

   Set-ExecutionPolicy -Scope Process Bypass -Force
   .\RUN_PROJECT_B_NASA_STATION_COMPARISON.ps1

OUTPUT
A new timestamped folder is created under:

H:\PROJECT B\02_Processed_Data\NASA_Station_Provisional_Comparison_YYYYMMDD_HHMMSS

The folder contains:
- comparison_metrics.csv
- exclusion_summary.csv
- station_nasa_daily_join_and_quality_flags.csv
- station_to_nasa_grid_assignment.csv
- decision.json

QUALITY RULES
- Days with less than 50% of the station's median daily record count are
  excluded from the relevant comparison.
- Rainfall must be between 0 and 500 mm/day.
- Temperature order must be minimum <= mean <= maximum.
- Exact-zero temperatures are treated as sensor sentinels.
- Rainfall zeros remain valid dry days.
- Rainfall and temperature are filtered independently; a humidity defect does
  not automatically invalidate an otherwise usable rainfall reading.

CONTROL STATUS
Project A is not accessed.
Project B is not retrained.
RL allocation is not executed.
Training remains unauthorized after this diagnostic.
