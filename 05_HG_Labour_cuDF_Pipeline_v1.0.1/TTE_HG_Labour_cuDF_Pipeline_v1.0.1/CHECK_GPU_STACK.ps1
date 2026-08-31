$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "TTE_RAPIDS_HELPERS.ps1")
$WslDistro = Get-TteWslDistro

Write-Host "WINDOWS GPU" -ForegroundColor Cyan
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader

Write-Host "`nWSL STATUS" -ForegroundColor Cyan
wsl.exe --status
wsl.exe -l -v

Write-Host "`nWSL GPU" -ForegroundColor Cyan
wsl.exe -d $WslDistro -e nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader

Write-Host "`nRAPIDS / PYTHON" -ForegroundColor Cyan
try {
    $WslPython = Get-TteRapidsPython -WslDistro $WslDistro
    & wsl.exe -d $WslDistro -e $WslPython -c "import sys,cudf,pandas,pyarrow,openpyxl; print('Python:',sys.version); print('cudf',cudf.__version__); print('pandas',pandas.__version__); print('pyarrow',pyarrow.__version__); print('openpyxl',openpyxl.__version__); print(cudf.Series([1,2,3])); print('CUDF GPU TEST PASSED')"
}
catch {
    Write-Host "CUDF GPU TEST FAILED: $($_.Exception.Message)" -ForegroundColor Red
}
