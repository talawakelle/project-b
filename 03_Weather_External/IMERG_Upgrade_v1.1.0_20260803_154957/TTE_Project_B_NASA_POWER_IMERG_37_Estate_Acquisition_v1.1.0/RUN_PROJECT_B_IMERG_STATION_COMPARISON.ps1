[CmdletBinding()]
param(
    [string]$IMERGRoot = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$weatherExternal = 'H:\PROJECT B\03_Weather_External'

if ([string]::IsNullOrWhiteSpace($IMERGRoot)) {
    $candidates = Get-ChildItem -LiteralPath $weatherExternal -Directory -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -like 'TTE_Project_B_NASA_POWER_IMERG_37_Estate_Snapshot_*_validated' -or
            $_.Name -like 'NASA_POWER_IMERG_37_Estate_*'
        } |
        Where-Object {
            Test-Path -LiteralPath (Join-Path $_.FullName '03_audit\acquisition_manifest.json')
        } |
        Sort-Object LastWriteTime -Descending
    $chosen = $candidates | Select-Object -First 1
    if ($null -eq $chosen) {
        throw 'No completed IMERG snapshot was found under H:\PROJECT B\03_Weather_External.'
    }
    $IMERGRoot = $chosen.FullName
}

$weatherBase = Join-Path `
    'H:\PROJECT B\01_Source_Data_Pack\TTE_Project_B_Advanced_Source_Data_Pack' `
    '03_Crop_and_Field_Data'
$outputParent = 'H:\PROJECT B\02_Processed_Data'
$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$comparisonOutput = Join-Path $outputParent "NASA_IMERG_Station_Comparison_$timestamp"

$required = @(
    (Join-Path $packageRoot 'compare_imerg_station.py'),
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

$dailyFiles = @(Get-ChildItem -LiteralPath (Join-Path $IMERGRoot '02_processed') -File -Filter 'nasa_power_imerg_37_estate_daily_*.csv')
if ($dailyFiles.Count -ne 1) {
    throw "Expected exactly one IMERG daily CSV; found $($dailyFiles.Count)."
}

$pythonExe = Get-Command python.exe -ErrorAction SilentlyContinue
$pythonPrefix = @()
if (!$pythonExe) {
    $pythonExe = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pythonExe) { $pythonPrefix = @('-3') }
}
if (!$pythonExe) { throw 'Python 3 was not found in PATH.' }

$env:TTE_IMERG_ROOT = $IMERGRoot
$env:TTE_WEATHER_BASE = $weatherBase
$env:TTE_COMPARE_OUT = $comparisonOutput

Write-Host "IMERG snapshot: $IMERGRoot" -ForegroundColor Cyan
& $pythonExe.Source @pythonPrefix (Join-Path $packageRoot 'compare_imerg_station.py')
if ($LASTEXITCODE -ne 0) {
    throw "IMERG-station comparison failed with exit code $LASTEXITCODE."
}

Write-Host ''
Write-Host 'IMERG comparison completed.' -ForegroundColor Green
Write-Host "Output folder: $comparisonOutput" -ForegroundColor Green
Write-Host 'No source files or models were modified.' -ForegroundColor Yellow
Write-Host 'Project B retraining and RL allocation remain disabled.' -ForegroundColor Yellow

