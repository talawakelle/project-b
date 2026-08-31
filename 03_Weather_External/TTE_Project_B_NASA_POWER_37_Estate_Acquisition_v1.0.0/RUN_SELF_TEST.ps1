$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptPath = Join-Path $scriptRoot 'download_nasa_power_37_estates.py'
$coordinatePath = Join-Path $scriptRoot 'estate_coordinates_provisional.csv'

$pythonExe = Get-Command python.exe -ErrorAction SilentlyContinue
$prefix = @()
if (!$pythonExe) {
    $pythonExe = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pythonExe) { $prefix = @('-3') }
}
if (!$pythonExe) { throw 'Python 3 was not found.' }

& $pythonExe.Source @prefix $scriptPath --coordinates $coordinatePath --self-test
if ($LASTEXITCODE -ne 0) { throw "Self-test failed with exit code $LASTEXITCODE" }
