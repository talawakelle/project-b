[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

$python = Get-Command python.exe -ErrorAction SilentlyContinue
$prefix = @()
if (!$python) {
    $python = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($python) { $prefix = @('-3') }
}
if (!$python) { throw 'Python 3 was not found in PATH.' }

& $python.Source @prefix (Join-Path $packageRoot 'benchmark_rainfall_sources.py') --self-test
if ($LASTEXITCODE -ne 0) { throw "Self-test failed with exit code $LASTEXITCODE." }
