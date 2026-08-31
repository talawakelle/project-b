$ErrorActionPreference = "Stop"
$Root = "H:\PROJECT B\02_Processed_Data"
$Run = Get-ChildItem -LiteralPath $Root -Directory -Filter "HG_Labour_cuDF_Run_*" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if (-not $Run) { throw "No cuDF pipeline run was found under $Root" }
$ManifestPath = Join-Path $Run.FullName "run_manifest.json"
if (-not (Test-Path -LiteralPath $ManifestPath)) {
    throw "Latest run has no manifest: $($Run.FullName)"
}

$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$Manifest | Select-Object status, version, engine_requested, engine_used, decision,
    labour_rows_before_cross_file_dedup, labour_rows_after_dedup,
    weighment_rows_before_cross_file_dedup, weighment_rows_after_dedup,
    division_day_kpi_rows, both_source_rows, source_join_rate,
    operational_use_authorized, why_not_operational | Format-List

Write-Host "GPU: $($Manifest.gpu.name) / $($Manifest.gpu.memory_mb) MB / driver $($Manifest.gpu.driver_version)"
Write-Host "Benchmark status: $($Manifest.benchmark.status)"
Write-Host "GPU compute seconds: $($Manifest.benchmark.gpu_compute_seconds)"
Write-Host "CPU compute seconds: $($Manifest.benchmark.cpu_compute_seconds)"
Write-Host "Compute speed-up: $($Manifest.benchmark.compute_speedup_x)x"
Write-Host "Equivalent result: $($Manifest.benchmark.result_equivalent)"
Write-Host "GPU justified: $($Manifest.benchmark.gpu_economically_justified)"
Write-Host "Report: $(Join-Path $Run.FullName 'RUN_REPORT.md')"

