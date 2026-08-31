TTE PROJECT B - MATCHED RAINFALL SOURCE BENCHMARK v1.0.0
========================================================

PURPOSE
Compare NASA POWER PRECTOTCORR and NASA POWER/GPM IMERG against the Pedro and
TRI daily station caches using exactly the same valid station-days. The runner
also tests IMERG date shifts of -1, 0, and +1 day because IMERG is UTC daily,
PRECTOTCORR is LST daily, and the station caches appear to use local dates.

THIS IS A DIAGNOSTIC ONLY
- It does not train, score, replace, or modify any model.
- It does not authorize Project B retraining.
- It does not enable RL allocation.
- It does not declare either NASA product to be estate gauge truth.

RUN
1. Extract this ZIP anywhere.
2. Open PowerShell in the extracted folder.
3. Run:

   Set-ExecutionPolicy -Scope Process Bypass -Force
   .\RUN_SELF_TEST.ps1
   .\RUN_PROJECT_B_MATCHED_RAINFALL_SOURCE_BENCHMARK.ps1

The runner discovers the newest processed PRECTOTCORR and IMERG snapshots under:
H:\PROJECT B\03_Weather_External

OUTPUT
H:\PROJECT B\02_Processed_Data\NASA_Rainfall_Source_Matched_Benchmark_YYYYMMDD_HHMMSS

Key files:
- matched_source_metrics.csv
- matched_sample_exclusions.csv
- station_source_grid_assignment.csv
- matched_daily_detail.csv
- decision.json

INTERPRETATION WARNING
A whole-day IMERG shift is only a boundary-sensitivity test. UTC daily totals
cannot be reconstructed exactly into Sri Lanka local-day totals without the
underlying sub-daily observations. Only two highland station caches are
available, and raw station-workbook provenance remains unresolved.
