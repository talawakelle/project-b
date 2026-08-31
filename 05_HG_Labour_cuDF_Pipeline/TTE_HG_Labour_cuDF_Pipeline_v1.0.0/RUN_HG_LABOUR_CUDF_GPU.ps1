$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = "H:\PROJECT B"
if (-not (Test-Path -LiteralPath $ProjectRoot)) {
    throw "Project root not found: $ProjectRoot"
}
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw "WSL2 is required. cuDF is not supported as a normal native-Windows Python package."
}

$Gpu = & wsl.exe -e nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "The NVIDIA GPU is not visible inside WSL2. Review CHECK_GPU_STACK.ps1. Details: $Gpu"
}

$CuDFCheck = & wsl.exe -e python3 -c "import cudf; print(cudf.__version__)" 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "cuDF is not installed in the default WSL2 Python environment. Use the official RAPIDS WSL2 installer/release selector, then rerun. Details: $CuDFCheck"
}

function Convert-ToWslPath([string]$WindowsPath) {
    $Resolved = (Resolve-Path -LiteralPath $WindowsPath).Path
    $Result = & wsl.exe -e wslpath -a $Resolved
    if ($LASTEXITCODE -ne 0 -or -not $Result) {
        throw "Could not convert to WSL path: $WindowsPath"
    }
    return ($Result | Select-Object -First 1).Trim()
}

if (-not $env:TTE_LABOUR_HASH_KEY -or $env:TTE_LABOUR_HASH_KEY.Length -lt 16) {
    $SecureKey = Read-Host "Enter the stable labour pseudonymization key (minimum 16 characters)" -AsSecureString
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)
    try {
        $env:TTE_LABOUR_HASH_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }
}
if ($env:TTE_LABOUR_HASH_KEY.Length -lt 16) {
    throw "The pseudonymization key must contain at least 16 characters. Use the same key for every run."
}

$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$OutputRoot = Join-Path $ProjectRoot "02_Processed_Data\HG_Labour_cuDF_Run_$Stamp"
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

$ScriptWsl = Convert-ToWslPath (Join-Path $PSScriptRoot "hg_labour_cudf_pipeline.py")
$ProjectWsl = Convert-ToWslPath $ProjectRoot
$OutputWsl = Convert-ToWslPath $OutputRoot
$WslEnvParts = @("TTE_LABOUR_HASH_KEY")
if ($env:WSLENV) { $WslEnvParts += $env:WSLENV }
$env:WSLENV = $WslEnvParts -join ":"

$LogPath = Join-Path $OutputRoot "pipeline_console.log"

Write-Host "Running TTE HG labour cuDF pipeline on: $Gpu" -ForegroundColor Cyan
Write-Host "Source files remain read-only." -ForegroundColor Yellow
& wsl.exe -e python3 $ScriptWsl --project-root $ProjectWsl --output $OutputWsl --engine gpu 2>&1 |
    Tee-Object -FilePath $LogPath
if ($LASTEXITCODE -ne 0) {
    throw "cuDF pipeline failed. Review: $LogPath"
}

$ManifestPath = Join-Path $OutputRoot "run_manifest.json"
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
if ($Manifest.status -ne "TTE_HG_LABOUR_CUDF_PIPELINE_COMPLETED" -or $Manifest.engine_used -ne "gpu") {
    throw "The completion marker or GPU-engine marker is missing."
}

$BackupPath = $null
if (Test-Path -LiteralPath "G:\") {
    $BackupRoot = "G:\TTE_Project_B_Handover"
    New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
    $BackupPath = Join-Path $BackupRoot (Split-Path $OutputRoot -Leaf)
    robocopy $OutputRoot $BackupPath /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /NFL /NDL /NP | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "Pipeline completed, but backup copy failed with robocopy exit code $LASTEXITCODE."
    }
    $SourceHashes = Get-ChildItem -LiteralPath $OutputRoot -Recurse -File | ForEach-Object {
        [pscustomobject]@{Relative=$_.FullName.Substring($OutputRoot.Length).TrimStart('\');Hash=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash}
    }
    $BackupHashes = Get-ChildItem -LiteralPath $BackupPath -Recurse -File | ForEach-Object {
        [pscustomobject]@{Relative=$_.FullName.Substring($BackupPath.Length).TrimStart('\');Hash=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash}
    }
    if (Compare-Object $SourceHashes $BackupHashes -Property Relative,Hash) {
        throw "Backup SHA-256 verification failed: $BackupPath"
    }
}

Remove-Item Env:TTE_LABOUR_HASH_KEY -ErrorAction SilentlyContinue

Write-Host "`nTTE HG LABOUR CUDF PIPELINE COMPLETED" -ForegroundColor Green
Write-Host "Engine: $($Manifest.engine_used)"
Write-Host "Decision: $($Manifest.decision)"
Write-Host "Labour rows: $($Manifest.labour_rows_after_dedup)"
Write-Host "Weighment rows: $($Manifest.weighment_rows_after_dedup)"
Write-Host "Source join rate: $([math]::Round([double]$Manifest.source_join_rate * 100, 2))%"
Write-Host "Compute speed-up: $($Manifest.benchmark.compute_speedup_x)x"
Write-Host "Output: $OutputRoot"
if ($BackupPath) { Write-Host "Verified backup: $BackupPath" }
