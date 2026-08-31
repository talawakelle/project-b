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
$program = Join-Path $scriptRoot 'weather_training_lineage_closure.py'
if (!(Test-Path -LiteralPath $program -PathType Leaf)) {
    throw "Lineage-closure program missing: $program"
}

$processedRoot = Join-Path $ProjectRoot '02_Processed_Data'
if (!(Test-Path -LiteralPath $processedRoot -PathType Container)) {
    throw "Processed-data folder not found: $processedRoot"
}

$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$outputRoot = Join-Path $processedRoot "Weather_Training_Lineage_Closure_$timestamp"

$roots = New-Object System.Collections.Generic.List[string]
$roots.Add($ProjectRoot)

$standardCandidates = @(
    'C:\TTE_Integrated_Platform',
    (Join-Path $env:USERPROFILE 'Desktop'),
    (Join-Path $env:USERPROFILE 'Documents'),
    (Join-Path $env:USERPROFILE 'Downloads')
)
foreach ($candidate in $standardCandidates) {
    if (Test-Path -LiteralPath $candidate -PathType Container) {
        $roots.Add($candidate)
    }
}
foreach ($candidate in $AdditionalSearchRoots) {
    if (!(Test-Path -LiteralPath $candidate -PathType Container)) {
        throw "Additional search root not found: $candidate"
    }
    $roots.Add($candidate)
}
$uniqueRoots = @($roots | Select-Object -Unique)

$python = Resolve-PythonCommand
$arguments = @(
    $program,
    '--project-root', $ProjectRoot,
    '--output-root', $outputRoot,
    '--exclude-root', $scriptRoot
)
foreach ($root in $uniqueRoots) {
    $arguments += @('--search-root', $root)
}

Write-Host '========== PROJECT B WEATHER TRAINING LINEAGE CLOSURE ==========' -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot"
Write-Host 'Search roots:'
$uniqueRoots | ForEach-Object { Write-Host "  $_" }
Write-Host 'Mode: READ ONLY for source files, code, models, training, and RL settings' -ForegroundColor Yellow

if ($python.Count -eq 2) {
    & $python[0] $python[1] @arguments
} else {
    & $python[0] @arguments
}
if ($LASTEXITCODE -ne 0) {
    throw "Weather training-lineage closure failed with exit code $LASTEXITCODE."
}

$summary = Join-Path $outputRoot 'FINAL_SUMMARY.txt'
if (!(Test-Path -LiteralPath $summary -PathType Leaf)) {
    throw "Audit finished without final summary: $summary"
}

Write-Host ''
Get-Content -LiteralPath $summary
Write-Host ''
Write-Host 'Required review files:' -ForegroundColor Cyan
Write-Host "  $(Join-Path $outputRoot 'target_artifact_candidates.csv')"
Write-Host "  $(Join-Path $outputRoot 'content_references.csv')"
Write-Host "  $(Join-Path $outputRoot 'scan_failures.csv')"
Write-Host "  $(Join-Path $outputRoot 'lineage_closure_summary.json')"
Write-Host ''

$reviewFiles = @(
    (Join-Path $outputRoot 'target_artifact_candidates.csv'),
    (Join-Path $outputRoot 'content_references.csv'),
    (Join-Path $outputRoot 'scan_failures.csv'),
    (Join-Path $outputRoot 'large_text_files_skipped.csv'),
    (Join-Path $outputRoot 'lineage_closure_summary.json'),
    (Join-Path $outputRoot 'FINAL_SUMMARY.txt')
)
foreach ($reviewFile in $reviewFiles) {
    if (!(Test-Path -LiteralPath $reviewFile -PathType Leaf)) {
        throw "Required review output missing: $reviewFile"
    }
}
$reviewZip = Join-Path `
    (Join-Path $env:USERPROFILE 'Downloads') `
    "Project_B_Weather_Training_Lineage_Closure_Review_$timestamp.zip"
Compress-Archive `
    -LiteralPath $reviewFiles `
    -DestinationPath $reviewZip `
    -CompressionLevel Optimal

Write-Host 'REVIEW PACKAGE CREATED' -ForegroundColor Green
Write-Host $reviewZip
Get-FileHash -LiteralPath $reviewZip -Algorithm SHA256
Write-Host ''
Write-Host 'Project B retraining remains unauthorized.' -ForegroundColor Yellow
Write-Host 'RL allocation remains disabled.' -ForegroundColor Yellow
