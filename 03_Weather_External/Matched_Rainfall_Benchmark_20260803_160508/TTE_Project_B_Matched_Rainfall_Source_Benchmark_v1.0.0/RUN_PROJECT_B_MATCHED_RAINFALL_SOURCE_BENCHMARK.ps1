[CmdletBinding()]
param(
    [string]$PRECTOTRoot = '',
    [string]$IMERGRoot = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$weatherExternal = 'H:\PROJECT B\03_Weather_External'
$weatherBase = Join-Path `
    'H:\PROJECT B\01_Source_Data_Pack\TTE_Project_B_Advanced_Source_Data_Pack' `
    '03_Crop_and_Field_Data'
$outputParent = 'H:\PROJECT B\02_Processed_Data'

function Find-SnapshotRoot {
    param([Parameter(Mandatory = $true)][string]$DailyPattern)
    $file = Get-ChildItem -LiteralPath $weatherExternal -Recurse -File `
        -Filter $DailyPattern -ErrorAction SilentlyContinue |
        Where-Object { $_.Directory.Name -eq '02_processed' } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $file) {
        throw "No snapshot containing $DailyPattern was found under $weatherExternal"
    }
    return $file.Directory.Parent.FullName
}

if ([string]::IsNullOrWhiteSpace($PRECTOTRoot)) {
    $PRECTOTRoot = Find-SnapshotRoot 'nasa_power_37_estate_daily_*.csv'
}
if ([string]::IsNullOrWhiteSpace($IMERGRoot)) {
    $IMERGRoot = Find-SnapshotRoot 'nasa_power_imerg_37_estate_daily_*.csv'
}

$required = @(
    (Join-Path $packageRoot 'benchmark_rainfall_sources.py'),
    (Join-Path $PRECTOTRoot '03_audit\estate_parameter_grid_crosswalk.csv'),
    (Join-Path $IMERGRoot '03_audit\estate_parameter_grid_crosswalk.csv'),
    (Join-Path $weatherBase 'config\weather_station_master.csv'),
    (Join-Path $weatherBase 'outputs\gpu_candidates_20260722_181042\pedro_weather_daily_cache.csv'),
    (Join-Path $weatherBase 'outputs\gpu_candidates_20260722_181042\tri_weather_daily_cache.csv')
)
foreach ($path in $required) {
    if (!(Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "MISSING REQUIRED FILE: $path"
    }
}

$prectotDaily = @(Get-ChildItem -LiteralPath (Join-Path $PRECTOTRoot '02_processed') `
    -File -Filter 'nasa_power_37_estate_daily_*.csv')
$imergDaily = @(Get-ChildItem -LiteralPath (Join-Path $IMERGRoot '02_processed') `
    -File -Filter 'nasa_power_imerg_37_estate_daily_*.csv')
if ($prectotDaily.Count -ne 1) {
    throw "Expected one PRECTOTCORR processed CSV; found $($prectotDaily.Count)."
}
if ($imergDaily.Count -ne 1) {
    throw "Expected one IMERG processed CSV; found $($imergDaily.Count)."
}

$python = Get-Command python.exe -ErrorAction SilentlyContinue
$prefix = @()
if (!$python) {
    $python = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($python) { $prefix = @('-3') }
}
if (!$python) { throw 'Python 3 was not found in PATH.' }

$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$benchmarkOutput = Join-Path $outputParent "NASA_Rainfall_Source_Matched_Benchmark_$timestamp"
$env:TTE_PRECTOT_ROOT = $PRECTOTRoot
$env:TTE_IMERG_ROOT = $IMERGRoot
$env:TTE_WEATHER_BASE = $weatherBase
$env:TTE_BENCH_OUT = $benchmarkOutput

Write-Host "PRECTOTCORR snapshot: $PRECTOTRoot" -ForegroundColor Cyan
Write-Host "IMERG snapshot: $IMERGRoot" -ForegroundColor Cyan
Write-Host 'Station-day basis: local cache dates; IMERG shifts -1, 0, +1 tested' -ForegroundColor Cyan

& $python.Source @prefix (Join-Path $packageRoot 'benchmark_rainfall_sources.py')
if ($LASTEXITCODE -ne 0) {
    throw "Matched rainfall benchmark failed with exit code $LASTEXITCODE."
}

Write-Host ''
Write-Host 'Matched rainfall benchmark completed.' -ForegroundColor Green
Write-Host "Output folder: $benchmarkOutput" -ForegroundColor Green
Write-Host 'No source files or models were modified.' -ForegroundColor Yellow
Write-Host 'Project B retraining and RL allocation remain disabled.' -ForegroundColor Yellow
