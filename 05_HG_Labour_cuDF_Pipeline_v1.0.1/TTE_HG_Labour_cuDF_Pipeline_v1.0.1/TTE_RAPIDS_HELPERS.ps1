function Get-TteWslDistro {
    if ($env:TTE_WSL_DISTRO) { return $env:TTE_WSL_DISTRO }
    return "Ubuntu-24.04"
}

function Get-TteRapidsPython {
    param([string]$WslDistro = (Get-TteWslDistro))

    if ($env:TTE_RAPIDS_PYTHON) {
        $Candidate = $env:TTE_RAPIDS_PYTHON
    }
    else {
        $WslHome = (& wsl.exe -d $WslDistro -e bash -lc 'printf "%s" "$HOME"').Trim()
        if ($LASTEXITCODE -ne 0 -or -not $WslHome) {
            throw "Could not determine the Linux home directory in $WslDistro."
        }
        $Candidate = "$WslHome/miniforge3/envs/tte-rapids/bin/python"
    }

    & wsl.exe -d $WslDistro -e test -x $Candidate
    if ($LASTEXITCODE -ne 0) {
        throw "RAPIDS Python was not found at $Candidate. Run INSTALL_RAPIDS_WSL.ps1 first."
    }
    return $Candidate
}

function Convert-ToTteWslPath {
    param(
        [Parameter(Mandatory=$true)][string]$WindowsPath,
        [string]$WslDistro = (Get-TteWslDistro)
    )
    $Resolved = (Resolve-Path -LiteralPath $WindowsPath).Path
    $Result = & wsl.exe -d $WslDistro -e wslpath -a $Resolved
    if ($LASTEXITCODE -ne 0 -or -not $Result) {
        throw "Could not convert to a WSL path: $WindowsPath"
    }
    return ($Result | Select-Object -First 1).Trim()
}
