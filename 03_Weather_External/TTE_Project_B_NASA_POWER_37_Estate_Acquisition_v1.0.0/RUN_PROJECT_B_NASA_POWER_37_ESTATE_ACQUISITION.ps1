[CmdletBinding()]
param(
    [ValidatePattern('^\d{8}$')]
    [string]$StartDate = '20210401',

    [ValidatePattern('^\d{8}$')]
    [string]$EndDate = '',

    [switch]$ForceRedownload
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Write-Host 'TTE PROJECT B NASA POWER 37-ESTATE ACQUISITION v1.0.0' -ForegroundColor Cyan
Write-Host 'Project A will not be accessed or restarted.' -ForegroundColor Yellow
Write-Host 'Project B retraining and RL allocation remain disabled.' -ForegroundColor Yellow
Write-Host 'NASA weather is gridded observed/reanalysis data, not an estate weather station or a future forecast.' -ForegroundColor Yellow

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptPath = Join-Path $scriptRoot 'download_nasa_power_37_estates.py'
$coordinatePath = Join-Path $scriptRoot 'estate_coordinates_provisional.csv'

if (!(Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "Python acquisition script not found: $scriptPath"
}
if (!(Test-Path -LiteralPath $coordinatePath -PathType Leaf)) {
    throw "Coordinate CSV not found: $coordinatePath"
}
if (!(Test-Path -LiteralPath 'H:\PROJECT B' -PathType Container)) {
    throw 'Project B root was not found at H:\PROJECT B. This runner will not guess another project path.'
}

if ([string]::IsNullOrWhiteSpace($EndDate)) {
    $EndDate = (Get-Date).ToUniversalTime().Date.AddDays(-8).ToString('yyyyMMdd')
}

$parsedStart = [datetime]::ParseExact($StartDate, 'yyyyMMdd', $null)
$parsedEnd = [datetime]::ParseExact($EndDate, 'yyyyMMdd', $null)
if ($parsedEnd -lt $parsedStart) {
    throw "EndDate $EndDate is before StartDate $StartDate"
}
$latestSafeEnd = (Get-Date).ToUniversalTime().Date.AddDays(-7)
if ($parsedEnd.Date -gt $latestSafeEnd) {
    throw "EndDate $EndDate is too recent. Use $($latestSafeEnd.ToString('yyyyMMdd')) or earlier because the included solar product can arrive 5-7 days late."
}

$pythonExe = Get-Command python.exe -ErrorAction SilentlyContinue
$pythonArgsPrefix = @()
if (!$pythonExe) {
    $pythonExe = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pythonExe) { $pythonArgsPrefix = @('-3') }
}
if (!$pythonExe) {
    throw 'Python 3 was not found. Install Python 3 or make python.exe/py.exe available in PATH.'
}

$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$weatherRoot = 'H:\PROJECT B\03_Weather_External'
$output = Join-Path $weatherRoot ("NASA_POWER_37_Estate_{0}_{1}_{2}" -f $StartDate, $EndDate, $timestamp)
if (Test-Path -LiteralPath $output) {
    throw "Refusing to reuse an existing output directory: $output"
}
New-Item -ItemType Directory -Path $output -Force | Out-Null

$arguments = @(
    $scriptPath,
    '--coordinates', $coordinatePath,
    '--output', $output,
    '--start', $StartDate,
    '--end', $EndDate
)
if ($ForceRedownload) { $arguments += '--force' }

& $pythonExe.Source @pythonArgsPrefix @arguments
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    throw "NASA acquisition failed with exit code $exitCode. Review the final error above; do not use the partial output for modelling."
}

$manifestPath = Join-Path $output '03_audit\acquisition_manifest.json'
if (!(Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Acquisition manifest was not created: $manifestPath"
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.status -ne 'PROVISIONAL_WEATHER_ACQUISITION_COMPLETED_NOT_APPROVED_FOR_TRAINING') {
    throw "Unexpected acquisition decision: $($manifest.status)"
}

$backupRoot = 'G:\TTE_Project_B_Handover'
$backup = $null
if (Test-Path -LiteralPath 'G:\' -PathType Container) {
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $backup = Join-Path $backupRoot (Split-Path -Leaf $output)
    if (Test-Path -LiteralPath $backup) {
        throw "Refusing to overwrite existing backup: $backup"
    }
    Copy-Item -LiteralPath $output -Destination $backup -Recurse

    $sourceHashes = Import-Csv (Join-Path $output '03_audit\file_hashes_sha256.csv')
    foreach ($entry in $sourceHashes) {
        $relativeWindowsPath = $entry.relative_path -replace '/', '\'
        $backupFile = Join-Path $backup $relativeWindowsPath
        if (!(Test-Path -LiteralPath $backupFile -PathType Leaf)) {
            throw "Backup file is missing: $backupFile"
        }
        $actualHash = (Get-FileHash -LiteralPath $backupFile -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $entry.sha256.ToLowerInvariant()) {
            throw "Backup hash mismatch: $backupFile"
        }
    }
}

Write-Host ''
Write-Host 'PROJECT B NASA POWER ACQUISITION COMPLETED' -ForegroundColor Green
Write-Host "Decision: $($manifest.status)" -ForegroundColor Green
Write-Host "Rows: $($manifest.period.actual_estate_day_rows)" -ForegroundColor Green
Write-Host "Output: $output" -ForegroundColor Green
if ($backup) { Write-Host "Verified backup: $backup" -ForegroundColor Green }
Write-Host 'Project A unchanged; retraining disabled; RL allocation disabled; training not authorized.' -ForegroundColor Yellow
