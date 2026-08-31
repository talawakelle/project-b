# TTE Project B — NASA POWER IMERG 37-Estate Upgrade v1.1.0

## What this package corrects

The earlier NASA snapshot used `PRECTOTCORR`, the bias-corrected MERRA-2/GEOS-IT precipitation product on the coarse meteorological grid. NASA POWER now exposes the current GPM IMERG parameters:

- `IMERG_PRECTOT` — daily total precipitation in mm/day
- `IMERG_PRECTOT_COUNT` — number of half-hourly retrievals with at least 0.01 mm precipitation

IMERG is available only as daily UTC data in POWER and has a native 0.1° grid (about 10 km). This release downloads it as a separate, auditable rainfall product. It does not delete or overwrite the existing NASA data.

## Scope and safeguards

- 37 estates: TTEL 16, KVPL 13, HPL display code / HRPL system code 8
- Default start: 1 April 2021
- Default requested end: two complete UTC days before the run date
- Automatic default-run truncation to NASA's latest common non-fill IMERG date, recorded in the manifest
- Annual request chunks to respect the POWER regional request limit
- Raw NASA JSON responses retained unchanged
- Live parameter-catalog validation before acquisition
- Source grid/date reconciliation across every annual chunk
- Estate-to-grid crosswalk and shared-cell audit
- SHA-256 inventory and optional verified backup to `G:\TTE_Project_B_Handover`
- No Project A access, model training, P90 recalibration, or RL allocation

Coordinates remain provisional public points. IMERG is satellite precipitation, not an estate rain gauge. Nearby estates can legitimately share the same 0.1° source cell.

## Run the future-update automation

Extract the package and open PowerShell inside its folder:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\RUN_SELF_TEST.ps1
.\RUN_PROJECT_B_NASA_POWER_IMERG_37_ESTATE_ACQUISITION.ps1
```

For the controlled historical period used by the supplied snapshot:

```powershell
.\RUN_PROJECT_B_NASA_POWER_IMERG_37_ESTATE_ACQUISITION.ps1 `
    -StartDate 20210401 `
    -EndDate 20260726
```

An explicit unavailable end date is rejected. To request "latest available," omit `-EndDate`; the runner probes the live API, records the requested and actual ends, and stops at the last common non-fill date. At release time, NASA POWER accepted requests through 26 July 2026 but returned real IMERG values only through 2 March 2026; later values were the `-999` fill sentinel and were excluded.

The output is written to a new timestamped folder under:

```text
H:\PROJECT B\03_Weather_External
```

## Compare IMERG with Pedro and TRI station caches

After a completed IMERG snapshot exists under the weather-external folder:

```powershell
.\RUN_PROJECT_B_IMERG_STATION_COMPARISON.ps1
```

The comparison produces daily, monthly, and 1 mm wet/dry event metrics twice:

1. all available IMERG dates;
2. only dates old enough to be conservatively eligible for the Final Run by age.

It still cannot authorize training because only two highland station caches exist and their original raw workbooks have not been located.

## Interpretation limits

1. The comparison against `PRECTOTCORR` showed that coarse NASA data cannot represent estate microclimates.
2. IMERG improves rainfall spatial resolution but does not fix the +3.8°C to +6.2°C temperature bias.
3. POWER documents IMERG Final Run at about 3.5 months behind near real time and Late Run at about 14 hours. Recent dates may therefore change later.
4. The field `imerg_training_eligible_by_age=YES` is an age-based screen, not proof that the exact API value is Final Run.
5. Same-day IMERG is not allowed as an operational forecast feature. Lag-1 is research-only; lag-2 is the conservative operational feature boundary in this package.
6. Successful acquisition or comparison does not authorize Project B retraining or RL allocation.

## Key outputs

- `01_raw_nasa_json\` — one immutable response per parameter and calendar-year chunk
- `02_processed\nasa_power_imerg_37_estate_daily_*.csv` — estate-day IMERG rainfall data and leakage-safe lags
- `03_audit\nasa_power_parameter_catalog_extract.json` — current NASA parameter metadata captured at run time
- `03_audit\estate_parameter_grid_crosswalk.csv` — estate-to-native-IMERG-cell mapping
- `03_audit\shared_grid_groups.csv` — estates sharing a rainfall cell
- `03_audit\data_quality_summary.csv` — acquisition gates
- `03_audit\acquisition_manifest.json` — provenance, maturity warning, and control decision
- `03_audit\file_hashes_sha256.csv` — integrity inventory
- `FINAL_SUMMARY.txt` — concise result

## Official NASA references

- https://power.larc.nasa.gov/docs/services/api/temporal/daily/
- https://power.larc.nasa.gov/docs/methodology/data/sources/
- https://power.larc.nasa.gov/docs/tutorials/parameters/
- https://power.larc.nasa.gov/docs/release-guide/
