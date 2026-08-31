# Project B Weather Source Provenance Recovery v1.0.0

This package performs the next controlled Project B gate after the matched
NASA rainfall benchmark. It searches for the original Pedro/TRI station
workbooks and other estate rain-gauge/weather records, including matching file
names inside ZIP archives.

The three expected station sources are taken from the existing station master
and its notes:

- `TRI-Thalawakele.xlsx`
- `TRI-Thalawakele_2026_06.xlsx`
- `Pedro_Estate_Nuwaraeliya(2).xlsx`

The audit also profiles and hashes the current derived caches:

- `pedro_weather_daily_cache.csv`
- `tri_weather_daily_cache.csv`

## Safety boundary

The runner is read-only with respect to source data. It does not copy raw
files, download NASA data, change Project A or Project B inputs, train a model,
or allocate RL. It writes only a new timestamped audit folder under:

```text
H:\PROJECT B\02_Processed_Data
```

Finding a file with the expected name is not proof of provenance. The runner
therefore keeps `raw_provenance_verified=false` and
`training_authorized=false`. A later isolated reproduction gate must establish
that an authoritative raw workbook and documented transformation reproduce the
existing cache.

## Run

From the extracted package folder:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\RUN_SELF_TEST.ps1
.\RUN_PROJECT_B_WEATHER_SOURCE_PROVENANCE_RECOVERY.ps1
```

By default, the runner searches `H:\PROJECT B` and the current Windows user's
Downloads, Desktop, and Documents folders. To include another controlled root:

```powershell
.\RUN_PROJECT_B_WEATHER_SOURCE_PROVENANCE_RECOVERY.ps1 `
    -AdditionalSearchRoots 'G:\Weather Backups'
```

Do not pass an entire drive unless there is a concrete reason; broad scanning
adds noise and may take hours.

## Primary outputs

- `expected_source_search.csv`: exact expected filenames and locations.
- `candidate_files.csv`: scored physical-file candidates with hashes where
  warranted.
- `archive_member_candidates.csv`: matching ZIP-member names without extraction.
- `station_cache_profiles.csv`: cache hashes, date ranges, row counts, and key
  validity checks.
- `station_config_rows.csv`: the station master claims used in the audit.
- `governance_contradictions.csv`: unsafe or internally inconsistent claims.
- `scan_failures.csv`: files that could not be inspected.
- `summary.json` and `FINAL_SUMMARY.txt`: decision and preserved controls.

## Possible decisions

- `RAW_STATION_WORKBOOK_CANDIDATES_FOUND_REPRODUCTION_REQUIRED`
- `NON_EXACT_WEATHER_CANDIDATES_FOUND_MANUAL_REVIEW_REQUIRED`
- `RAW_STATION_WORKBOOK_CANDIDATES_NOT_FOUND_OWNER_EXPORT_REQUIRED`

None of these decisions authorizes retraining.
