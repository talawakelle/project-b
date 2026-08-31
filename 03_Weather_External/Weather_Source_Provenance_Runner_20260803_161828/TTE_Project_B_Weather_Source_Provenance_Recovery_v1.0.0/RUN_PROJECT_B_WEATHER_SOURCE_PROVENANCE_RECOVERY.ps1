[CmdletBinding()]
param(
    [string]$ProjectRoot = 'H:\PROJECT B',
    [string[]]$AdditionalSearchRoots = @()
)

$ErrorActionPreference = 'Stop'

function Resolve-PythonCommand {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @('py', '-3')
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @('python')
    }
    throw 'Python 3 was not found. Install Python 3 or add it to PATH.'
}

if (!(Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw "Project B root not found: $ProjectRoot"
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonScript = Join-Path $scriptRoot 'weather_source_provenance_recovery.py'
if (!(Test-Path -LiteralPath $pythonScript -PathType Leaf)) {
    throw "Recovery program missing: $pythonScript"
}

$processedRoot = Join-Path $ProjectRoot '02_Processed_Data'
if (!(Test-Path -LiteralPath $processedRoot -PathType Container)) {
    throw "Processed-data folder not found: $processedRoot"
}

$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$outputRoot = Join-Path $processedRoot "Weather_Source_Provenance_Recovery_$timestamp"

$searchRoots = New-Object System.Collections.Generic.List[string]
$searchRoots.Add($ProjectRoot)

$profileCandidates = @(
    (Join-Path $env:USERPROFILE 'Downloads'),
    (Join-Path $env:USERPROFILE 'Desktop'),
    (Join-Path $env:USERPROFILE 'Documents')
)
foreach ($candidate in $profileCandidates) {
    if (Test-Path -LiteralPath $candidate -PathType Container) {
        $searchRoots.Add($candidate)
    }
}
foreach ($candidate in $AdditionalSearchRoots) {
    if (!(Test-Path -LiteralPath $candidate -PathType Container)) {
        throw "Additional search root not found: $candidate"
    }
    $searchRoots.Add($candidate)
}

$uniqueRoots = @($searchRoots | Select-Object -Unique)
$python = Resolve-PythonCommand
$arguments = @(
    $pythonScript,
    '--project-root', $ProjectRoot,
    '--output-root', $outputRoot
)
foreach ($root in $uniqueRoots) {
    $arguments += @('--search-root', $root)
}

Write-Host '========== PROJECT B WEATHER SOURCE PROVENANCE RECOVERY ==========' -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot"
Write-Host 'Search roots:'
$uniqueRoots | ForEach-Object { Write-Host "  $_" }
Write-Host 'Mode: READ ONLY (source files are not copied or modified)' -ForegroundColor Yellow

if ($python.Count -eq 2) {
    & $python[0] $python[1] @arguments
} else {
    & $python[0] @arguments
}
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    throw "Weather-source provenance recovery failed with exit code $exitCode."
}

$summaryPath = Join-Path $outputRoot 'FINAL_SUMMARY.txt'
if (!(Test-Path -LiteralPath $summaryPath -PathType Leaf)) {
    throw "Recovery finished without a final summary: $summaryPath"
}

Write-Host ''
Get-Content -LiteralPath $summaryPath
Write-Host ''
Write-Host 'Required review files:' -ForegroundColor Cyan
Write-Host "  $(Join-Path $outputRoot 'expected_source_search.csv')"
Write-Host "  $(Join-Path $outputRoot 'candidate_files.csv')"
Write-Host "  $(Join-Path $outputRoot 'archive_member_candidates.csv')"
Write-Host "  $(Join-Path $outputRoot 'governance_contradictions.csv')"
Write-Host ''
Write-Host 'Project B retraining remains unauthorized.' -ForegroundColor Yellow
Write-Host 'RL allocation remains disabled.' -ForegroundColor Yellow
