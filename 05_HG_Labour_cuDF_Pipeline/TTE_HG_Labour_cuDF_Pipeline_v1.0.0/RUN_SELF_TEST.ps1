$ErrorActionPreference = "Stop"

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw "WSL2 is required."
}

function Convert-ToWslPath([string]$WindowsPath) {
    $Resolved = (Resolve-Path -LiteralPath $WindowsPath).Path
    (& wsl.exe -e wslpath -a $Resolved).Trim()
}

$ScriptWsl = Convert-ToWslPath (Join-Path $PSScriptRoot "hg_labour_cudf_pipeline.py")
$env:TTE_LABOUR_HASH_KEY = "self-test-key-not-for-production"
$WslEnvParts = @("TTE_LABOUR_HASH_KEY")
if ($env:WSLENV) { $WslEnvParts += $env:WSLENV }
$env:WSLENV = $WslEnvParts -join ":"
& wsl.exe -e python3 $ScriptWsl --self-test
if ($LASTEXITCODE -ne 0) { throw "Self-test failed." }
Remove-Item Env:TTE_LABOUR_HASH_KEY -ErrorAction SilentlyContinue
