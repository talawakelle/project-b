$ErrorActionPreference = "Continue"

Write-Host "WINDOWS GPU" -ForegroundColor Cyan
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader

Write-Host "`nWSL STATUS" -ForegroundColor Cyan
wsl.exe --status
wsl.exe -l -v

Write-Host "`nWSL GPU" -ForegroundColor Cyan
wsl.exe -e bash -lc "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader"

Write-Host "`nRAPIDS / PYTHON" -ForegroundColor Cyan
wsl.exe -e bash -lc "python3 - <<'PY'
import sys
print('Python:', sys.version)
for name in ('cudf','pandas','pyarrow','openpyxl'):
    try:
        module=__import__(name)
        print(name, getattr(module,'__version__','installed'))
    except Exception as exc:
        print(name, 'MISSING', repr(exc))
try:
    import cudf
    print(cudf.Series([1,2,3]))
    print('CUDF GPU TEST PASSED')
except Exception as exc:
    print('CUDF GPU TEST FAILED', repr(exc))
PY"

