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

Write-Host 'TTE PROJECT B NASA POWER IMERG 37-ESTATE ACQUISITION v1.1.0' -ForegroundColor Cyan
Write-Host 'This adds native 0.1-degree IMERG rainfall; it does not overwrite the existing NASA snapshot.' -ForegroundColor Yellow
Write-Host 'Project A access, Project B retraining, and RL allocation remain disabled.' -ForegroundColor Yellow

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptPath = Join-Path $scriptRoot 'download_nasa_power_imerg_37_estates.py'
$coordinatePath = Join-Path $scriptRoot 'estate_coordinates_provisional.csv'

foreach ($path in @($scriptPath, $coordinatePath)) {
    if (!(Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required package file not found: $path"
    }
}
if (!(Test-Path -LiteralPath 'H:\PROJECT B' -PathType Container)) {
    throw 'Project B root was not found at H:\PROJECT B. The runner will not guess another path.'
}
$useLatestAvailable = [string]::IsNullOrWhiteSpace($EndDate)
if ($useLatestAvailable) {
    $EndDate = (Get-Date).ToUniversalTime().Date.AddDays(-2).ToString('yyyyMMdd')
}
$parsedStart = [datetime]::ParseExact($StartDate, 'yyyyMMdd', $null)
$parsedEnd = [datetime]::ParseExact($EndDate, 'yyyyMMdd', $null)
if ($parsedEnd -lt $parsedStart) {
    throw "EndDate $EndDate is before StartDate $StartDate"
}
$latestAllowed = (Get-Date).ToUniversalTime().Date.AddDays(-1)
if ($parsedEnd.Date -gt $latestAllowed) {
    throw "EndDate must be $($latestAllowed.ToString('yyyyMMdd')) or earlier."
}

$pythonExe = Get-Command python.exe -ErrorAction SilentlyContinue
$pythonPrefix = @()
if (!$pythonExe) {
    $pythonExe = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pythonExe) { $pythonPrefix = @('-3') }
}
if (!$pythonExe) {
    throw 'Python 3 was not found in PATH.'
}

$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$weatherRoot = 'H:\PROJECT B\03_Weather_External'
if (!(Test-Path -LiteralPath $weatherRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $weatherRoot -Force | Out-Null
}
$output = Join-Path $weatherRoot (
    'NASA_POWER_IMERG_37_Estate_{0}_{1}_{2}' -f $StartDate, $EndDate, $timestamp
)
if (Test-Path -LiteralPath $output) {
    throw "Refusing to reuse output directory: $output"
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
if ($useLatestAvailable) { $arguments += '--allow-end-truncation' }

& $pythonExe.Source @pythonPrefix @arguments
if ($LASTEXITCODE -ne 0) {
    throw "IMERG acquisition failed with exit code $LASTEXITCODE. Do not use the partial output."
}

$manifestPath = Join-Path $output '03_audit\acquisition_manifest.json'
if (!(Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Acquisition manifest was not created: $manifestPath"
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.status -ne 'PROVISIONAL_IMERG_ACQUISITION_COMPLETED_NOT_APPROVED_FOR_TRAINING') {
    throw "Unexpected IMERG acquisition decision: $($manifest.status)"
}

$backup = $null
if (Test-Path -LiteralPath 'G:\' -PathType Container) {
    $backupRoot = 'G:\TTE_Project_B_Handover'
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $backup = Join-Path $backupRoot (Split-Path -Leaf $output)
    if (Test-Path -LiteralPath $backup) {
        throw "Refusing to overwrite backup: $backup"
    }
    Copy-Item -LiteralPath $output -Destination $backup -Recurse

    $hashes = Import-Csv (Join-Path $output '03_audit\file_hashes_sha256.csv')
    foreach ($entry in $hashes) {
        $relative = $entry.relative_path -replace '/', '\'
        $backupFile = Join-Path $backup $relative
        if (!(Test-Path -LiteralPath $backupFile -PathType Leaf)) {
            throw "Backup file is missing: $backupFile"
        }
        $actual = (Get-FileHash -LiteralPath $backupFile -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $entry.sha256.ToLowerInvariant()) {
            throw "Backup hash mismatch: $backupFile"
        }
    }
}

Write-Host ''
Write-Host 'PROJECT B NASA POWER IMERG ACQUISITION COMPLETED' -ForegroundColor Green
Write-Host "Decision: $($manifest.status)" -ForegroundColor Green
Write-Host "Rows: $($manifest.period.actual_estate_day_rows)" -ForegroundColor Green
Write-Host "Distinct estate IMERG cells: $($manifest.product.distinct_grid_cells_used_by_estates)" -ForegroundColor Green
Write-Host "Output: $output" -ForegroundColor Green
if ($backup) { Write-Host "Verified backup: $backup" -ForegroundColor Green }
Write-Host 'Training remains unauthorized; Project A and RL remain unchanged.' -ForegroundColor Yellow
