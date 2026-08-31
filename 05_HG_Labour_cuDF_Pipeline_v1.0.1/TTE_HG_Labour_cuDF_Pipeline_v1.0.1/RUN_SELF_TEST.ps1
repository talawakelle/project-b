$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "TTE_RAPIDS_HELPERS.ps1")
$WslDistro = Get-TteWslDistro
$WslPython = Get-TteRapidsPython -WslDistro $WslDistro

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw "WSL2 is required."
}

$ScriptWsl = Convert-ToTteWslPath -WindowsPath (Join-Path $PSScriptRoot "hg_labour_cudf_pipeline.py") -WslDistro $WslDistro
$env:TTE_LABOUR_HASH_KEY = "self-test-key-not-for-production"
$WslEnvParts = @("TTE_LABOUR_HASH_KEY")
if ($env:WSLENV) { $WslEnvParts += $env:WSLENV }
$env:WSLENV = $WslEnvParts -join ":"
& wsl.exe -d $WslDistro -e $WslPython $ScriptWsl --self-test
if ($LASTEXITCODE -ne 0) { throw "Self-test failed." }
Remove-Item Env:TTE_LABOUR_HASH_KEY -ErrorAction SilentlyContinue
