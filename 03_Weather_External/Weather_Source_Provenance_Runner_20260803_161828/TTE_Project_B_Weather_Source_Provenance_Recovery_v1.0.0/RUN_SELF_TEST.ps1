$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$program = Join-Path $scriptRoot 'weather_source_provenance_recovery.py'
$hashManifest = Join-Path $scriptRoot 'FILE_HASHES_SHA256.csv'

if (!(Test-Path -LiteralPath $program -PathType Leaf)) {
    throw "Self-test target missing: $program"
}
if (!(Test-Path -LiteralPath $hashManifest -PathType Leaf)) {
    throw "Package hash manifest missing: $hashManifest"
}

$hashFailures = @()
foreach ($entry in (Import-Csv -LiteralPath $hashManifest)) {
    $target = Join-Path $scriptRoot $entry.relative_path
    if (!(Test-Path -LiteralPath $target -PathType Leaf)) {
        $hashFailures += "MISSING: $($entry.relative_path)"
        continue
    }
    $actual = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $entry.sha256.ToLowerInvariant()) {
        $hashFailures += "HASH_MISMATCH: $($entry.relative_path)"
    }
}
if ($hashFailures.Count -ne 0) {
    $hashFailures | ForEach-Object { Write-Error $_ }
    throw 'Package integrity verification failed.'
}
Write-Host 'PACKAGE_HASHES_PASS'

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $program --self-test
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python $program --self-test
} else {
    throw 'Python 3 was not found. Install Python 3 or add it to PATH.'
}

if ($LASTEXITCODE -ne 0) {
    throw "Self-test failed with exit code $LASTEXITCODE."
}
