# TTE Project B — NASA POWER 37-Estate Acquisition v1.0.0

## Purpose

Acquire daily NASA POWER weather for the controlled 37-estate crop-model scope:

- TTEL: 16 estates
- KVPL: 13 estates
- HPL display label / HRPL system code: 8 estates

The runner downloads regional responses in calendar-year chunks, because NASA caps each regional request at 366 days. It preserves every raw NASA JSON response, reconciles the grids and dates across chunks, maps each estate to the nearest grid point returned by NASA, reports shared grid cells, and creates one estate-day CSV.

## Controlled period

Default start: **1 April 2021**. The end date defaults to eight days before the run date because the included solar product can arrive 5–7 days late. An explicit end date may be supplied, but the runner rejects dates less than seven days behind UTC today.

## Parameters

| NASA code | Output meaning | Time standard |
|---|---|---|
| PRECTOTCORR | MERRA-2/GEOS-IT corrected precipitation (mm/day) | LST |
| T2M | Mean 2 m air temperature | LST |
| T2M_MAX | Maximum 2 m air temperature | LST |
| T2M_MIN | Minimum 2 m air temperature | LST |
| RH2M | Relative humidity at 2 m | LST |
| WS2M | Wind speed at 2 m | LST |
| ALLSKY_SFC_SW_DWN | All-sky surface shortwave energy; source MJ/m²/day is converted to kWh/m²/day | LST |

Important: this release does **not** label PRECTOTCORR as IMERG. The current POWER Daily API v2.9.5 rejected the legacy IMERG parameter during live validation. If 0.1° IMERG rainfall is later required, it needs a separate Earthdata/POWER-compatible acquisition and validation path.

## Run

Open PowerShell in the extracted folder and run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\RUN_SELF_TEST.ps1
.\RUN_PROJECT_B_NASA_POWER_37_ESTATE_ACQUISITION.ps1
```

Optional explicit dates:

```powershell
.\RUN_PROJECT_B_NASA_POWER_37_ESTATE_ACQUISITION.ps1 `
    -StartDate 20210401 `
    -EndDate 20260726
```

## Outputs

The runner creates a new timestamped folder under:

`H:\PROJECT B\03_Weather_External`

It also copies the completed folder to `G:\TTE_Project_B_Handover` when drive G is available.

Key files:

- `01_raw_nasa_json\`: immutable raw responses, one per parameter per calendar-year chunk
- `02_processed\nasa_power_37_estate_daily_*.csv`: daily estate dataset
- `03_audit\estate_parameter_grid_crosswalk.csv`: estate-to-NASA-grid mapping
- `03_audit\shared_grid_groups.csv`: estates sharing the same source cell
- `03_audit\data_quality_summary.csv`: completeness and validity gates
- `03_audit\acquisition_manifest.json`: controlling decision and safeguards
- `03_audit\file_hashes_sha256.csv`: file integrity inventory
- `FINAL_SUMMARY.txt`: concise final decision

## Non-negotiable interpretation rules

1. Coordinates are provisional public points, not verified estate weather stations.
2. NASA weather is gridded observed/reanalysis data, not future weather forecasts.
3. Shared values among nearby estates are expected and must not be presented as independent microclimates.
4. Same-day observed weather must not enter a forecast as though it were known at forecast issue time.
5. Lag-8 derived features are the conservative operational-safe features for this combined parameter set; lag-1 research features remain visibly separate.
6. NASA weather does not create missing crop targets for Deniyaya, Indola, Kiruwanaganga, or Moragalla.
7. Successful acquisition does not authorize retraining, P90 recalibration, RL allocation, or any Project A action.

## Official NASA references

- https://power.larc.nasa.gov/docs/services/api/temporal/daily/
- https://power.larc.nasa.gov/docs/methodology/data/sources/
- https://power.larc.nasa.gov/api/temporal/daily/openapi.json
