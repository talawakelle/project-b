$ErrorActionPreference = "Stop"

$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

$nasaRoot = Join-Path `
    "H:\PROJECT B\03_Weather_External" `
    "TTE_Project_B_NASA_POWER_37_Estate_Snapshot_20210401_20260726_validated"

$weatherBase = Join-Path `
    "H:\PROJECT B\01_Source_Data_Pack\TTE_Project_B_Advanced_Source_Data_Pack" `
    "03_Crop_and_Field_Data"

$outputParent = "H:\PROJECT B\02_Processed_Data"
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$comparisonOutput = Join-Path `
    $outputParent `
    "NASA_Station_Provisional_Comparison_$timestamp"

$required = @(
    "$packageRoot\nasa_station_compare.py",
    "$nasaRoot\02_processed\nasa_power_37_estate_daily_20210401_20260726.csv",
    "$nasaRoot\03_audit\estate_parameter_grid_crosswalk.csv",
    "$weatherBase\config\weather_station_master.csv",
    "$weatherBase\outputs\gpu_candidates_20260722_181042\pedro_weather_daily_cache.csv",
    "$weatherBase\outputs\gpu_candidates_20260722_181042\tri_weather_daily_cache.csv"
)

foreach ($path in $required) {
    if (!(Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "MISSING REQUIRED FILE: $path"
    }
}

if (!(Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python is not available in PATH."
}

$env:TTE_NASA_ROOT = $nasaRoot
$env:TTE_WEATHER_BASE = $weatherBase
$env:TTE_COMPARE_OUT = $comparisonOutput

& python "$packageRoot\nasa_station_compare.py"

if ($LASTEXITCODE -ne 0) {
    throw "NASA-station comparison failed with exit code $LASTEXITCODE."
}

"`nComparison completed."
"Output folder: $comparisonOutput"
"No source files or models were modified."
"Project B retraining remains unauthorized."
"RL allocation remains disabled."

