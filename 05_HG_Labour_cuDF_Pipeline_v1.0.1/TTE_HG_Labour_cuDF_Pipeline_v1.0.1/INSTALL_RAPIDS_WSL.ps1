$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$WslDistro = if ($env:TTE_WSL_DISTRO) { $env:TTE_WSL_DISTRO } else { "Ubuntu-24.04" }
$InstallerWindows = Join-Path $PSScriptRoot "INSTALL_RAPIDS_WSL.sh"
$InstallerWsl = (& wsl.exe -d $WslDistro -e wslpath -a $InstallerWindows).Trim()
if ($LASTEXITCODE -ne 0 -or -not $InstallerWsl) {
    throw "Could not resolve the RAPIDS installer inside $WslDistro."
}

Write-Host "Installing RAPIDS 26.06 in an isolated WSL environment." -ForegroundColor Cyan
Write-Host "This can download several GB and may take 10-30 minutes." -ForegroundColor Yellow
& wsl.exe -d $WslDistro -e bash $InstallerWsl
if ($LASTEXITCODE -ne 0) {
    throw "RAPIDS installation failed. Copy the complete error output for review."
}
