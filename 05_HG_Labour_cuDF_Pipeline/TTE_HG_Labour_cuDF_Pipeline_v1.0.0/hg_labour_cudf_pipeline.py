#!/usr/bin/env python3
"""GPU-first TTE labour + weighment canonicalization and KPI pipeline.

Designed for RAPIDS cuDF under WSL2. Source files are read-only. Raw employee
identifiers are HMAC-pseudonymized and never written to outputs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

try:
    import cudf
except Exception:
    cudf = None


VERSION = "1.0.0"
SUPPORTED = {".csv", ".parquet", ".xlsx", ".xls", ".sqlite", ".db"}
EXCLUDED_PATH_MARKERS = (
    "workforce_planner", "daily_runs", "scenario_runs", "candidate_matrix",
    "assignments", "neural_challenger", "hg_labour_cudf_run_", "hg_labour_deep_audit_",
)
PII_COLUMN_PATTERNS = (
    r"(^|_)name($|_)", r"nic", r"national.*id", r"passport", r"bank", r"account",
    r"phone", r"mobile", r"address", r"medical", r"diagnos", r"birth", r"email",
)

LABOUR_ALIASES = {
    "work_date": ["work_date", "attendance_date", "date", "workdate", "att_date", "day"],
    "worker_id": ["worker_id", "employee_id", "employee_code", "emp_id", "emp_code", "worker_code", "employee_no", "worker_no"],
    "estate_id": ["estate_id", "estate_code", "estate", "estate_name", "home_estate"],
    "division_id": ["division_id", "division_code", "division", "division_name", "home_division"],
    "field_id": ["field_id", "field_code", "field", "field_name"],
    "attendance_status": ["attendance_status", "attendance", "status", "day_status", "presence"],
    "work_type": ["work_type", "task", "job", "activity", "work_category", "operation"],
    "normal_hours": ["normal_hours", "hours", "worked_hours", "regular_hours"],
    "overtime_hours": ["overtime_hours", "ot_hours", "overtime", "ot"],
    "wage_cost": ["wage_cost", "wage", "salary", "earnings", "gross_pay", "amount", "labour_cost"],
    "worker_output": ["worker_output", "output", "quantity", "plucked_weight", "leaf_weight", "kg"],
}

WEIGHMENT_ALIASES = {
    "weighing_id": ["weighing_id", "weighment_id", "weigh_id", "transaction_id", "record_id"],
    "weighted_datetime": ["weighted_datetime", "weighed_datetime", "weighing_datetime", "timestamp", "date_time", "datetime"],
    "work_date": ["work_date", "weighing_date", "weighment_date", "date", "weighted_date"],
    "estate_id": ["estate_id", "estate_code", "estate", "estate_name"],
    "division_id": ["division_id", "division_code", "division", "division_name"],
    "field_id": ["field_id", "field_code", "field", "field_name"],
    "net_weight": ["net_weight", "net_weight_kg", "actual_weight", "crop_weight", "weight", "quantity", "net"],
    "status": ["status", "record_status", "approval_status", "weighing_status"],
}

VALID_PRESENT = {"present", "p", "worked", "working", "on duty", "duty"}
VALID_ABSENT = {"absent", "a", "no pay", "nopay", "unauthorised absence", "unauthorized absence"}
VALID_LEAVE = {"leave", "annual leave", "casual leave", "sick leave", "medical leave", "holiday"}
INVALID_WEIGHMENT_STATUS = {"cancelled", "canceled", "void", "voided", "rejected", "deleted"}


class PipelineError(RuntimeError):
    pass


class SchemaError(PipelineError):
    pass


@dataclass
class Source:
    system: str
    path: Path
    priority: int
    official: bool


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def normalized_columns(columns: Iterable[Any]) -> list[str]:
    seen: dict[str, int] = {}
    result = []
    for column in columns:
        base = norm(column) or "unnamed"
        seen[base] = seen.get(base, 0) + 1
        result.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return result


def pii_columns(columns: Iterable[str]) -> list[str]:
    return [column for column in columns if any(re.search(pattern, norm(column)) for pattern in PII_COLUMN_PATTERNS)]


def choose_mapping(columns: Iterable[str], aliases: dict[str, list[str]]) -> dict[str, str]:
    available = {norm(column): str(column) for column in columns}
    mapping = {}
    for canonical, candidates in aliases.items():
        for candidate in candidates:
            if norm(candidate) in available:
                mapping[canonical] = available[norm(candidate)]
                break
    return mapping


@lru_cache(maxsize=512)
def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pseudonymize(value: Any, key: bytes) -> str:
    text = "" if value is None else str(value).strip()
    if not text or text.lower() in {"nan", "none", "<na>"}:
        return ""
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def gpu_metadata() -> dict[str, str]:
    try:
        command = [
            "nvidia-smi", "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ]
        line = subprocess.check_output(command, text=True, timeout=15).strip().splitlines()[0]
        name, memory, driver = [item.strip() for item in line.split(",", 2)]
        return {"name": name, "memory_mb": memory, "driver_version": driver}
    except Exception as error:
        return {"error": repr(error)}


def get_backend(engine: str):
    if engine == "gpu":
        if cudf is None:
            raise PipelineError("cuDF is unavailable. Run inside a supported RAPIDS WSL2 environment.")
        return cudf, "gpu"
    if engine == "cpu":
        return pd, "cpu"
    if cudf is not None:
        return cudf, "gpu"
    return pd, "cpu"


def series_to_datetime(series, backend):
    if backend == "gpu":
        return cudf.to_datetime(series, errors="coerce", dayfirst=True)
    return pd.to_datetime(series, errors="coerce", format="mixed", dayfirst=True)


def series_to_numeric(series, backend):
    if backend == "gpu":
        return cudf.to_numeric(series, errors="coerce")
    return pd.to_numeric(series, errors="coerce")


def to_string(series):
    return series.fillna("").astype("str").str.strip()


def read_tabular(path: Path, module, backend: str):
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return [(path.stem, module.read_csv(path, dtype="str"))]
    if suffix == ".parquet":
        return [(path.stem, module.read_parquet(path))]
    if suffix in {".xlsx", ".xls"}:
        sheets = pd.read_excel(path, sheet_name=None, dtype=object)
        if backend == "gpu":
            return [(name, cudf.from_pandas(frame)) for name, frame in sheets.items()]
        return list(sheets.items())
    if suffix in {".sqlite", ".db"}:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            result = []
            for table in tables:
                frame = pd.read_sql_query(f'SELECT * FROM "{table.replace(chr(34), chr(34)*2)}"', connection)
                result.append((table, cudf.from_pandas(frame) if backend == "gpu" else frame))
            return result
        finally:
            connection.close()
    raise PipelineError(f"Unsupported source format: {path}")


def frame_copy_empty(module):
    return module.DataFrame()


def count_true(series) -> int:
    value = series.sum()
    return int(value.item() if hasattr(value, "item") else value)


def scalar(value):
    return value.item() if hasattr(value, "item") else value


def canonical_labour(frame, source: Source, key: bytes, backend: str):
    frame = frame.copy()
    frame.columns = normalized_columns(frame.columns)
    removed_pii = pii_columns(frame.columns)
    mapping = choose_mapping(frame.columns, LABOUR_ALIASES)
    if not {"work_date", "worker_id"}.issubset(mapping) or not ({"estate_id", "division_id"} & set(mapping)):
        raise SchemaError(f"Labour schema missing date/worker/location roles. Found mapping: {mapping}")

    module = cudf if backend == "gpu" else pd
    out = module.DataFrame(index=frame.index)
    for canonical in LABOUR_ALIASES:
        if canonical in mapping:
            out[canonical] = frame[mapping[canonical]]

    out["work_date"] = series_to_datetime(out["work_date"], backend)
    out["worker_id"] = to_string(out["worker_id"])
    if backend == "gpu":
        unique_ids = out["worker_id"].drop_duplicates().to_pandas().tolist()
        lookup = cudf.DataFrame({
            "worker_id": unique_ids,
            "worker_uid": [pseudonymize(value, key) for value in unique_ids],
        })
        out = out.merge(lookup, on="worker_id", how="left").drop(columns=["worker_id"])
    else:
        out["worker_uid"] = out["worker_id"].map(lambda value: pseudonymize(value, key))
        out = out.drop(columns=["worker_id"])

    for column in ("estate_id", "division_id", "field_id", "attendance_status", "work_type"):
        if column not in out.columns:
            out[column] = ""
        out[column] = to_string(out[column])
    for column in ("estate_id", "division_id", "field_id"):
        out[column] = out[column].str.upper().str.replace(r"[^A-Z0-9]+", "", regex=True)
    for column in ("normal_hours", "overtime_hours", "wage_cost", "worker_output"):
        if column not in out.columns:
            out[column] = 0.0
        out[column] = series_to_numeric(out[column], backend).fillna(0.0)

    status_norm = out["attendance_status"].str.lower().str.strip()
    out["present_flag"] = status_norm.isin(VALID_PRESENT).astype("int8")
    out["absent_flag"] = status_norm.isin(VALID_ABSENT).astype("int8")
    out["leave_flag"] = status_norm.isin(VALID_LEAVE).astype("int8")
    mapped_statuses = list(VALID_PRESENT | VALID_ABSENT | VALID_LEAVE)
    unmapped_status = status_norm.ne("") & ~status_norm.isin(mapped_statuses)
    out["source_sha256"] = file_sha256(source.path)
    out["source_file"] = source.path.name

    today = pd.Timestamp.now(tz=None).normalize()
    invalid_date = out["work_date"].isna()
    future_date = out["work_date"] > today
    missing_worker = out["worker_uid"].eq("")
    missing_location = out["estate_id"].eq("") & out["division_id"].eq("")
    impossible_hours = (out["normal_hours"] < 0) | (out["overtime_hours"] < 0) | ((out["normal_hours"] + out["overtime_hours"]) > 24)
    negative_cost = out["wage_cost"] < 0
    duplicate_mask = out.duplicated(subset=["worker_uid", "work_date", "estate_id", "division_id", "work_type"], keep=False)
    quality = {
        "system": "LifeHRMS",
        "source_file": source.path.name,
        "source_sha256": scalar(out["source_sha256"].iloc[0]) if len(out) else file_sha256(source.path),
        "input_rows": len(frame),
        "invalid_date_rows": count_true(invalid_date),
        "future_date_rows": count_true(future_date),
        "missing_worker_rows": count_true(missing_worker),
        "missing_location_rows": count_true(missing_location),
        "impossible_hours_rows": count_true(impossible_hours),
        "negative_cost_rows": count_true(negative_cost),
        "unmapped_attendance_status_rows": count_true(unmapped_status),
        "duplicate_candidate_rows": count_true(duplicate_mask),
        "pii_columns_excluded": " | ".join(removed_pii),
        "official_export": source.official,
    }
    valid = ~(invalid_date | future_date | missing_worker | missing_location | impossible_hours)
    return out.loc[valid].reset_index(drop=True), quality


def canonical_weighment(frame, source: Source, backend: str):
    frame = frame.copy()
    frame.columns = normalized_columns(frame.columns)
    removed_pii = pii_columns(frame.columns)
    mapping = choose_mapping(frame.columns, WEIGHMENT_ALIASES)
    date_role = "work_date" if "work_date" in mapping else "weighted_datetime" if "weighted_datetime" in mapping else None
    if date_role is None or "net_weight" not in mapping or not ({"estate_id", "division_id"} & set(mapping)):
        raise SchemaError(f"Weighment schema missing date/weight/location roles. Found mapping: {mapping}")

    module = cudf if backend == "gpu" else pd
    out = module.DataFrame(index=frame.index)
    for canonical in WEIGHMENT_ALIASES:
        if canonical in mapping:
            out[canonical] = frame[mapping[canonical]]
    if "weighted_datetime" in out.columns:
        out["weighted_datetime"] = series_to_datetime(out["weighted_datetime"], backend)
    if "work_date" in out.columns:
        out["work_date"] = series_to_datetime(out["work_date"], backend)
    else:
        out["work_date"] = out["weighted_datetime"]

    for column in ("weighing_id", "estate_id", "division_id", "field_id", "status"):
        if column not in out.columns:
            out[column] = ""
        out[column] = to_string(out[column])
    for column in ("estate_id", "division_id", "field_id"):
        out[column] = out[column].str.upper().str.replace(r"[^A-Z0-9]+", "", regex=True)
    out["net_weight"] = series_to_numeric(out["net_weight"], backend)
    out["source_sha256"] = file_sha256(source.path)
    out["source_file"] = source.path.name

    today = pd.Timestamp.now(tz=None).normalize()
    invalid_date = out["work_date"].isna()
    future_date = out["work_date"] > today
    invalid_weight = out["net_weight"].isna() | (out["net_weight"] < 0)
    missing_location = out["estate_id"].eq("") & out["division_id"].eq("")
    cancelled = out["status"].str.lower().str.strip().isin(INVALID_WEIGHMENT_STATUS)
    id_present = not bool(out["weighing_id"].eq("").all())
    duplicate_mask = out.duplicated(subset=["weighing_id"], keep=False) if id_present else out.duplicated(
        subset=["work_date", "estate_id", "division_id", "field_id", "net_weight"], keep=False
    )
    quality = {
        "system": "PROMPT",
        "source_file": source.path.name,
        "source_sha256": scalar(out["source_sha256"].iloc[0]) if len(out) else file_sha256(source.path),
        "input_rows": len(frame),
        "invalid_date_rows": count_true(invalid_date),
        "future_date_rows": count_true(future_date),
        "invalid_weight_rows": count_true(invalid_weight),
        "missing_location_rows": count_true(missing_location),
        "cancelled_rows": count_true(cancelled),
        "duplicate_candidate_rows": count_true(duplicate_mask),
        "pii_columns_excluded": " | ".join(removed_pii),
        "official_export": source.official,
    }
    valid = ~(invalid_date | future_date | invalid_weight | missing_location | cancelled)
    return out.loc[valid].reset_index(drop=True), quality


def aggregate_labour(frame):
    keys = ["work_date", "estate_id", "division_id"]
    result = frame.groupby(keys).agg({
        "worker_uid": "nunique",
        "present_flag": "sum",
        "absent_flag": "sum",
        "leave_flag": "sum",
        "normal_hours": "sum",
        "overtime_hours": "sum",
        "wage_cost": "sum",
        "worker_output": "sum",
        "source_file": "count",
    }).reset_index()
    return result.rename(columns={
        "worker_uid": "distinct_workers",
        "source_file": "labour_records",
    })


def aggregate_weighment(frame):
    keys = ["work_date", "estate_id", "division_id"]
    result = frame.groupby(keys).agg({
        "net_weight": "sum",
        "weighing_id": "nunique",
        "field_id": "nunique",
        "source_file": "count",
    }).reset_index()
    return result.rename(columns={
        "net_weight": "net_weight_kg",
        "weighing_id": "distinct_weighings",
        "field_id": "distinct_fields",
        "source_file": "weighment_records",
    })


def merge_kpis(labour_summary, weighment_summary):
    keys = ["work_date", "estate_id", "division_id"]
    merged = labour_summary.merge(weighment_summary, on=keys, how="outer")
    numeric = [
        "distinct_workers", "present_flag", "absent_flag", "leave_flag", "normal_hours",
        "overtime_hours", "wage_cost", "worker_output", "labour_records", "net_weight_kg",
        "distinct_weighings", "distinct_fields", "weighment_records",
    ]
    for column in numeric:
        if column not in merged.columns:
            merged[column] = 0
        merged[column] = merged[column].fillna(0)
    present_denominator = merged["present_flag"].where(merged["present_flag"] > 0)
    status_denominator = (merged["present_flag"] + merged["absent_flag"] + merged["leave_flag"]).where(
        (merged["present_flag"] + merged["absent_flag"] + merged["leave_flag"]) > 0
    )
    merged["kg_per_present_worker"] = merged["net_weight_kg"] / present_denominator
    merged["kg_per_distinct_worker"] = merged["net_weight_kg"] / merged["distinct_workers"].where(merged["distinct_workers"] > 0)
    merged["attendance_rate"] = merged["present_flag"] / status_denominator
    merged["labour_cost_per_kg"] = merged["wage_cost"] / merged["net_weight_kg"].where(merged["net_weight_kg"] > 0)
    merged["source_alignment"] = "BOTH"
    merged.loc[(merged["labour_records"] > 0) & (merged["weighment_records"] == 0), "source_alignment"] = "LABOUR_ONLY"
    merged.loc[(merged["labour_records"] == 0) & (merged["weighment_records"] > 0), "source_alignment"] = "WEIGHMENT_ONLY"
    return merged


def discover(root: Path, system: str) -> list[Source]:
    official_dir = root / "00_Official_Exports" / system
    official = []
    if official_dir.exists():
        for path in official_dir.rglob("*"):
            if path.is_file() and path.suffix.lower() in SUPPORTED:
                official.append(Source(system, path, 100, True))
    if official:
        return sorted(official, key=lambda item: str(item.path).lower())

    candidates: list[Source] = []
    if system == "LifeHRMS":
        target_names = {"labour attendance ttel june 2026- v2.xlsx"}
        for path in root.rglob("*"):
            lower = str(path).lower()
            if path.is_file() and path.name.lower() in target_names and not any(marker in lower for marker in EXCLUDED_PATH_MARKERS):
                candidates.append(Source(system, path, 60, False))
    else:
        for filename, priority in (("unique_weighment_audit.csv", 70), ("weighment_staging.sqlite", 50)):
            matches = [path for path in root.rglob(filename) if not any(marker in str(path).lower() for marker in EXCLUDED_PATH_MARKERS)]
            if matches:
                newest = max(matches, key=lambda path: path.stat().st_mtime)
                candidates.append(Source(system, newest, priority, False))
                if filename == "unique_weighment_audit.csv":
                    break
    highest = max((item.priority for item in candidates), default=-1)
    return [item for item in candidates if item.priority == highest]


def write_csv_rows(path: Path, rows: list[dict[str, Any]]):
    columns = sorted({key for row in rows for key in row}) if rows else ["message"]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_frame(frame, path: Path, backend: str, csv_copy: bool = False):
    frame.to_parquet(path, index=False, compression="snappy")
    if csv_copy:
        frame.to_csv(path.with_suffix(".csv"), index=False)


def concat(frames, backend: str):
    module = cudf if backend == "gpu" else pd
    return module.concat(frames, ignore_index=True) if frames else module.DataFrame()


def benchmark(labour, weighment, backend: str, sample_rows: int = 5_000_000) -> dict[str, Any]:
    if backend != "gpu" or cudf is None:
        return {"status": "GPU_BENCHMARK_NOT_RUN", "reason": "GPU engine was not active."}
    labour_gpu = labour.head(sample_rows)
    weighment_gpu = weighment.head(sample_rows)
    _ = aggregate_labour(labour_gpu.head(min(1000, len(labour_gpu))))
    _ = aggregate_weighment(weighment_gpu.head(min(1000, len(weighment_gpu))))
    start = time.perf_counter()
    gpu_l = aggregate_labour(labour_gpu)
    gpu_w = aggregate_weighment(weighment_gpu)
    gpu_k = merge_kpis(gpu_l, gpu_w)
    gpu_seconds = time.perf_counter() - start

    transfer_start = time.perf_counter()
    labour_cpu = labour_gpu.to_pandas()
    weighment_cpu = weighment_gpu.to_pandas()
    transfer_seconds = time.perf_counter() - transfer_start
    start = time.perf_counter()
    cpu_k = merge_kpis(aggregate_labour(labour_cpu), aggregate_weighment(weighment_cpu))
    cpu_seconds = time.perf_counter() - start

    gpu_weight = float(scalar(gpu_k["net_weight_kg"].sum())) if len(gpu_k) else 0.0
    cpu_weight = float(cpu_k["net_weight_kg"].sum()) if len(cpu_k) else 0.0
    equivalent = abs(gpu_weight - cpu_weight) <= max(1e-6, abs(cpu_weight) * 1e-9) and len(gpu_k) == len(cpu_k)
    speedup = cpu_seconds / gpu_seconds if gpu_seconds > 0 else None
    total_rows = len(labour_gpu) + len(weighment_gpu)
    return {
        "status": "PASSED" if equivalent else "FAILED_EQUIVALENCE",
        "sample_rows": total_rows,
        "gpu_compute_seconds": round(gpu_seconds, 6),
        "cpu_compute_seconds": round(cpu_seconds, 6),
        "gpu_to_cpu_transfer_seconds_excluded": round(transfer_seconds, 6),
        "compute_speedup_x": round(speedup, 3) if speedup is not None else None,
        "result_equivalent": equivalent,
        "gpu_economically_justified": bool(equivalent and total_rows >= 1_000_000 and speedup is not None and speedup >= 1.2),
    }


def self_test() -> int:
    os.environ.setdefault("TTE_LABOUR_HASH_KEY", "self-test-key-not-for-production")
    key = os.environ["TTE_LABOUR_HASH_KEY"].encode()
    temp = Path.cwd() / "synthetic_source.csv"
    temp.write_text("x\n1\n", encoding="utf-8")
    source_l = Source("LifeHRMS", temp, 1, True)
    source_w = Source("PROMPT", temp, 1, True)
    labour = pd.DataFrame({
        "Date": ["2026-06-20", "2026-06-20", "2026-06-21"],
        "Employee Code": ["E1", "E2", "E1"],
        "Estate": ["Clarendon"] * 3,
        "Division": ["D1"] * 3,
        "Status": ["Present", "Absent", "Present"],
        "Hours": [8, 0, 8],
        "Employee Name": ["A", "B", "A"],
    })
    weighment = pd.DataFrame({
        "weighing_id": ["W1", "W2"],
        "date": ["2026-06-20", "2026-06-21"],
        "estate": ["Clarendon"] * 2,
        "division": ["D1"] * 2,
        "net_weight": [100.0, 120.0],
        "status": ["Approved", "Approved"],
    })
    try:
        cl, ql = canonical_labour(labour, source_l, key, "cpu")
        cw, qw = canonical_weighment(weighment, source_w, "cpu")
        kpi = merge_kpis(aggregate_labour(cl), aggregate_weighment(cw))
        assert "worker_id" not in cl.columns
        assert "employee_name" not in cl.columns
        assert len(kpi) == 2
        assert abs(float(kpi["net_weight_kg"].sum()) - 220.0) < 1e-9
        assert ql["pii_columns_excluded"] == "employee_name"
        assert qw["input_rows"] == 2
        print("SELF TEST PASSED")
        return 0
    finally:
        temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root")
    parser.add_argument("--output")
    parser.add_argument("--engine", choices=("gpu", "cpu", "auto"), default="gpu")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.project_root or not args.output:
        parser.error("--project-root and --output are required unless --self-test is used")

    key_text = os.environ.get("TTE_LABOUR_HASH_KEY", "")
    if len(key_text) < 16:
        raise PipelineError("TTE_LABOUR_HASH_KEY must contain at least 16 characters. It is used only for HMAC pseudonymization.")
    key = key_text.encode("utf-8")
    root = Path(args.project_root).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    module, backend = get_backend(args.engine)
    started = datetime.now(timezone.utc)
    timings: dict[str, float] = {}

    sources = discover(root, "LifeHRMS") + discover(root, "PROMPT")
    if not any(source.system == "LifeHRMS" for source in sources):
        raise PipelineError(f"No LifeHRMS source found. Export to: {root / '00_Official_Exports' / 'LifeHRMS'}")
    if not any(source.system == "PROMPT" for source in sources):
        raise PipelineError(f"No PROMPT source found. Export to: {root / '00_Official_Exports' / 'PROMPT'}")

    labour_frames = []
    weighment_frames = []
    quality_rows: list[dict[str, Any]] = []
    source_manifest = []
    schema_rejections = []
    stage_start = time.perf_counter()
    for source in sources:
        digest = file_sha256(source.path)
        source_manifest.append({
            "system": source.system,
            "path": str(source.path),
            "filename": source.path.name,
            "sha256": digest,
            "size_bytes": source.path.stat().st_size,
            "modified": datetime.fromtimestamp(source.path.stat().st_mtime, timezone.utc).isoformat(),
            "official_export": source.official,
            "priority": source.priority,
        })
        for sheet_or_table, frame in read_tabular(source.path, module, backend):
            if len(frame) == 0:
                continue
            try:
                if source.system == "LifeHRMS":
                    canonical, quality = canonical_labour(frame, source, key, backend)
                    labour_frames.append(canonical)
                else:
                    canonical, quality = canonical_weighment(frame, source, backend)
                    weighment_frames.append(canonical)
                quality["sheet_or_table"] = sheet_or_table
                quality["accepted_rows"] = len(canonical)
                quality_rows.append(quality)
            except SchemaError as error:
                schema_rejections.append({
                    "system": source.system,
                    "source_file": source.path.name,
                    "sheet_or_table": sheet_or_table,
                    "rows": len(frame),
                    "reason": str(error),
                })
    timings["ingest_and_canonicalize_seconds"] = round(time.perf_counter() - stage_start, 6)

    if not labour_frames:
        raise PipelineError("No LifeHRMS sheet/table passed the canonical labour schema gate.")
    if not weighment_frames:
        raise PipelineError("No PROMPT sheet/table passed the canonical weighment schema gate.")

    labour = concat(labour_frames, backend)
    weighment = concat(weighment_frames, backend)
    before_labour = len(labour)
    before_weighment = len(weighment)
    labour_business_columns = [
        "worker_uid", "work_date", "estate_id", "division_id", "field_id",
        "attendance_status", "work_type", "normal_hours", "overtime_hours",
        "wage_cost", "worker_output",
    ]
    labour = labour.drop_duplicates(subset=labour_business_columns, keep="last")
    if not bool(weighment["weighing_id"].eq("").all()):
        weighment = weighment.sort_values(["work_date"]).drop_duplicates(subset=["weighing_id"], keep="last")
    else:
        weighment = weighment.drop_duplicates(subset=["work_date", "estate_id", "division_id", "field_id", "net_weight"], keep="last")

    stage_start = time.perf_counter()
    labour_summary = aggregate_labour(labour)
    weighment_summary = aggregate_weighment(weighment)
    kpis = merge_kpis(labour_summary, weighment_summary)
    timings["aggregate_and_join_seconds"] = round(time.perf_counter() - stage_start, 6)

    stage_start = time.perf_counter()
    write_frame(labour, output / "canonical_labour_worker_day.parquet", backend)
    write_frame(weighment, output / "canonical_weighment_event.parquet", backend)
    write_frame(labour_summary, output / "labour_division_day.parquet", backend, csv_copy=True)
    write_frame(weighment_summary, output / "weighment_division_day.parquet", backend, csv_copy=True)
    write_frame(kpis, output / "management_labour_crop_kpis.parquet", backend, csv_copy=True)
    timings["write_outputs_seconds"] = round(time.perf_counter() - stage_start, 6)

    benchmark_result = benchmark(labour, weighment, backend)
    write_csv_rows(output / "data_quality_report.csv", quality_rows)
    write_csv_rows(output / "schema_rejections.csv", schema_rejections)
    write_csv_rows(output / "source_manifest.csv", source_manifest)
    (output / "benchmark.json").write_text(json.dumps(benchmark_result, indent=2), encoding="utf-8")

    finished = datetime.now(timezone.utc)
    official_systems = {source.system for source in sources if source.official}
    joined_both = count_true(kpis["source_alignment"].eq("BOTH")) if len(kpis) else 0
    join_rate = joined_both / len(kpis) if len(kpis) else 0.0
    decision = "GPU_PIPELINE_TECHNICALLY_COMPLETE"
    operational = False
    if official_systems == {"LifeHRMS", "PROMPT"} and join_rate >= 0.80 and benchmark_result.get("status") == "PASSED":
        decision = "READY_FOR_BUSINESS_RECONCILIATION_AND_SHADOW_KPI_REVIEW"
    elif official_systems != {"LifeHRMS", "PROMPT"}:
        decision = "PIPELINE_COMPLETE_USING_FALLBACK_EVIDENCE_OBTAIN_OFFICIAL_PORTAL_EXPORTS"

    manifest = {
        "status": "TTE_HG_LABOUR_CUDF_PIPELINE_COMPLETED",
        "version": VERSION,
        "started_utc": started.isoformat(),
        "finished_utc": finished.isoformat(),
        "engine_requested": args.engine,
        "engine_used": backend,
        "gpu": gpu_metadata(),
        "project_root_read_only": str(root),
        "output": str(output),
        "sources": source_manifest,
        "labour_rows_before_cross_file_dedup": before_labour,
        "labour_rows_after_dedup": len(labour),
        "weighment_rows_before_cross_file_dedup": before_weighment,
        "weighment_rows_after_dedup": len(weighment),
        "division_day_kpi_rows": len(kpis),
        "both_source_rows": joined_both,
        "source_join_rate": round(join_rate, 6),
        "timings": timings,
        "benchmark": benchmark_result,
        "decision": decision,
        "operational_use_authorized": operational,
        "why_not_operational": "Official source-owner reconciliation, privacy/ACL verification and shadow KPI review are still required.",
    }
    (output / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    report = f"""# TTE HG Labour cuDF Pipeline Result

Status: **completed**  
Engine: **{backend.upper()}**  
Decision: **{decision}**

- Labour rows after deduplication: **{len(labour):,}**
- Weighment rows after deduplication: **{len(weighment):,}**
- Division-day KPI rows: **{len(kpis):,}**
- Rows with both labour and weighment evidence: **{joined_both:,} ({join_rate:.1%})**
- GPU benchmark status: **{benchmark_result.get('status')}**
- Compute speed-up: **{benchmark_result.get('compute_speedup_x', 'not measured')}x**

Raw worker identifiers were HMAC-pseudonymized. PII columns were excluded. Generated workforce-planner files were not used as evidence.

Operational use remains disabled until LifeHRMS and PROMPT source owners reconcile totals, access controls are confirmed, and the KPI outputs complete a shadow review.
"""
    (output / "RUN_REPORT.md").write_text(report, encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PIPELINE FAILED: {error}", file=sys.stderr)
        raise
