#!/usr/bin/env python3
"""Controlled NASA POWER acquisition for the 37-estate Project B scope.

This script downloads one regional response per parameter, maps every estate to
the nearest source grid point returned by NASA, preserves raw JSON, and writes
auditable estate-day CSV outputs. It never reads Project A, trains a model, or
executes an RL allocation.
"""

from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import hashlib
import json
import math
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path


VERSION = "1.0.0"
BASE_URL = "https://power.larc.nasa.gov/api/temporal/daily/regional"
EXPECTED_COMPANY_COUNTS = {"TTEL": 16, "KVPL": 13, "HPL": 8}
SYSTEM_COMPANY_CODE = {"TTEL": "TTEL", "KVPL": "KVPL", "HPL": "HRPL"}
PARAMETERS = {
    "PRECTOTCORR": {
        "column": "rain_power_corrected_mm_day",
        "time_standard": "LST",
        "valid_min": 0.0,
        "valid_max": 500.0,
        "role": "MERRA-2/GEOS-IT corrected precipitation; validate against estate gauges",
    },
    "T2M": {
        "column": "temperature_mean_c",
        "time_standard": "LST",
        "valid_min": 0.0,
        "valid_max": 45.0,
        "role": "2 m mean air temperature",
    },
    "T2M_MAX": {
        "column": "temperature_max_c",
        "time_standard": "LST",
        "valid_min": 0.0,
        "valid_max": 50.0,
        "role": "2 m maximum air temperature",
    },
    "T2M_MIN": {
        "column": "temperature_min_c",
        "time_standard": "LST",
        "valid_min": -5.0,
        "valid_max": 40.0,
        "role": "2 m minimum air temperature",
    },
    "RH2M": {
        "column": "relative_humidity_pct",
        "time_standard": "LST",
        "valid_min": 0.0,
        "valid_max": 100.0,
        "role": "2 m relative humidity",
    },
    "WS2M": {
        "column": "wind_speed_2m_m_s",
        "time_standard": "LST",
        "valid_min": 0.0,
        "valid_max": 60.0,
        "role": "2 m wind speed",
    },
    "ALLSKY_SFC_SW_DWN": {
        "column": "solar_allsky_kwh_m2_day",
        "time_standard": "LST",
        "valid_min": 0.0,
        "valid_max": 15.0,
        "role": "all-sky surface shortwave energy",
    },
}
FILL_VALUE = -999.0
OPERATIONAL_SAFE_LAG_DAYS = 8
NASA_DOCS = {
    "daily_api": "https://power.larc.nasa.gov/docs/services/api/temporal/daily/",
    "data_sources": "https://power.larc.nasa.gov/docs/methodology/data/sources/",
    "api_openapi": "https://power.larc.nasa.gov/api/temporal/daily/openapi.json",
}


class GateError(RuntimeError):
    pass


def parse_yyyymmdd(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise GateError(f"Invalid date {value!r}; expected YYYYMMDD") from exc


def iso_dates(start: dt.date, end: dt.date):
    current = start
    while current <= end:
        yield current
        current += dt.timedelta(days=1)


def calendar_year_chunks(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    chunks = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, dt.date(cursor.year, 12, 31))
        if (chunk_end - cursor).days + 1 > 366:
            raise GateError("Internal chunk exceeds NASA's 366-day regional request limit")
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + dt.timedelta(days=1)
    return chunks


def financial_year(day: dt.date) -> str:
    start_year = day.year if day.month >= 4 else day.year - 1
    return f"{start_year}/{str(start_year + 1)[-2:]}"


def safe_slug(value: str) -> str:
    result = "".join(c.lower() if c.isalnum() else "_" for c in value)
    while "__" in result:
        result = result.replace("__", "_")
    return result.strip("_")


def load_estates(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    required = {
        "company", "region", "estate", "latitude", "longitude", "status",
        "confidence", "coordinate_basis", "source_url", "audit_note",
    }
    missing = required - set(rows[0] if rows else [])
    if missing:
        raise GateError(f"Coordinate CSV missing columns: {sorted(missing)}")
    if len(rows) != 37:
        raise GateError(f"Expected 37 estates, found {len(rows)}")

    seen = set()
    counts = Counter()
    confidence = Counter()
    prepared = []
    for index, row in enumerate(rows, 2):
        company = row["company"].strip().upper()
        estate = row["estate"].strip()
        key = (company, estate.casefold())
        if key in seen:
            raise GateError(f"Duplicate estate key at CSV row {index}: {company}/{estate}")
        seen.add(key)
        if company not in EXPECTED_COMPANY_COUNTS:
            raise GateError(f"Unexpected company at CSV row {index}: {company}")
        try:
            latitude = float(row["latitude"])
            longitude = float(row["longitude"])
        except ValueError as exc:
            raise GateError(f"Invalid coordinate at CSV row {index}") from exc
        if not (5.5 <= latitude <= 7.5 and 79.5 <= longitude <= 81.5):
            raise GateError(
                f"Coordinate outside controlled Sri Lanka box at row {index}: "
                f"{estate} ({latitude}, {longitude})"
            )
        if row["status"].strip().upper() != "PROVISIONAL":
            raise GateError(f"Estate is not explicitly PROVISIONAL at row {index}: {estate}")
        counts[company] += 1
        confidence[row["confidence"].strip().title()] += 1
        prepared.append({
            **row,
            "company": company,
            "company_system_code": SYSTEM_COMPANY_CODE[company],
            "latitude": latitude,
            "longitude": longitude,
            "estate_key": f"{SYSTEM_COMPANY_CODE[company]}::{estate}",
        })

    if dict(counts) != EXPECTED_COMPANY_COUNTS:
        raise GateError(
            f"Company scope mismatch: expected {EXPECTED_COMPANY_COUNTS}, found {dict(counts)}"
        )
    if confidence != Counter({"High": 30, "Medium": 6, "Low": 1}):
        raise GateError(f"Coordinate confidence profile changed unexpectedly: {dict(confidence)}")
    return prepared


def regional_bounds(estates: list[dict]) -> dict[str, float]:
    # NASA requires at least a 2-degree range for regional requests. This box
    # contains every controlled point and is fixed for reproducibility.
    bounds = {
        "latitude-min": 5.5,
        "latitude-max": 7.5,
        "longitude-min": 79.5,
        "longitude-max": 81.5,
    }
    for estate in estates:
        if not (bounds["latitude-min"] <= estate["latitude"] <= bounds["latitude-max"]):
            raise GateError(f"Estate latitude outside request box: {estate['estate']}")
        if not (bounds["longitude-min"] <= estate["longitude"] <= bounds["longitude-max"]):
            raise GateError(f"Estate longitude outside request box: {estate['estate']}")
    return bounds


def build_url(parameter: str, start: str, end: str, bounds: dict[str, float]) -> str:
    query = {
        **bounds,
        "parameters": parameter,
        "community": "AG",
        "start": start,
        "end": end,
        "time-standard": PARAMETERS[parameter]["time_standard"],
        "units": "metric",
        "format": "JSON",
    }
    return f"{BASE_URL}?{urllib.parse.urlencode(query)}"


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def download_json(url: str, destination: Path, force: bool) -> dict:
    if destination.exists() and not force:
        try:
            return json.loads(destination.read_text(encoding="utf-8"))
        except Exception:
            destination.unlink()

    headers = {
        "User-Agent": "TTE-Project-B-NASA-POWER-Audit/1.0 (+controlled-regional-acquisition)",
        "Accept": "application/json",
    }
    last_error = None
    for attempt in range(1, 5):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=240) as response:
                body = response.read()
                if response.status != 200:
                    raise GateError(f"NASA returned HTTP {response.status}")
                payload = json.loads(body.decode("utf-8"))
                atomic_write_bytes(destination, body)
                return payload
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:1000]
            last_error = GateError(f"NASA HTTP {exc.code}: {detail}")
            if exc.code not in {429, 500, 502, 503, 504}:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, GateError) as exc:
            last_error = exc
        if attempt < 4:
            time.sleep(min(2 ** attempt, 20))
    raise GateError(f"NASA request failed after retries: {last_error}")


def validate_payload(payload: dict, parameter: str, start: str, end: str) -> None:
    if payload.get("type") != "FeatureCollection":
        raise GateError(f"{parameter}: expected FeatureCollection, got {payload.get('type')!r}")
    features = payload.get("features")
    if not isinstance(features, list) or not features:
        raise GateError(f"{parameter}: NASA response contains no grid features")
    header = payload.get("header", {})
    if str(header.get("start")) != start or str(header.get("end")) != end:
        raise GateError(
            f"{parameter}: response range {header.get('start')}..{header.get('end')} "
            f"does not match {start}..{end}"
        )
    if str(header.get("time_standard", "")).upper() != PARAMETERS[parameter]["time_standard"]:
        raise GateError(f"{parameter}: unexpected time standard {header.get('time_standard')!r}")
    for feature in features:
        series = feature.get("properties", {}).get("parameter", {}).get(parameter)
        if not isinstance(series, dict):
            raise GateError(f"{parameter}: grid feature lacks the requested series")


def grid_key(feature: dict) -> tuple[float, float, float | None]:
    coordinates = feature.get("geometry", {}).get("coordinates") or []
    if len(coordinates) < 2:
        raise GateError("NASA grid feature has no longitude/latitude")
    elevation = None
    if len(coordinates) > 2 and coordinates[2] is not None:
        elevation = round(float(coordinates[2]), 6)
    return round(float(coordinates[0]), 6), round(float(coordinates[1]), 6), elevation


def merge_chunk_payloads(parameter: str, payloads: list[dict], start: str, end: str) -> dict:
    if not payloads:
        raise GateError(f"{parameter}: no chunk responses to merge")
    merged = copy.deepcopy(payloads[0])
    merged_features = {grid_key(feature): feature for feature in merged["features"]}
    expected_grid_keys = set(merged_features)
    all_sources = set(merged.get("header", {}).get("sources", []))

    for payload in payloads[1:]:
        current_features = {grid_key(feature): feature for feature in payload["features"]}
        if set(current_features) != expected_grid_keys:
            raise GateError(f"{parameter}: source grid changed between annual chunks")
        all_sources.update(payload.get("header", {}).get("sources", []))
        for key, current_feature in current_features.items():
            target_series = merged_features[key]["properties"]["parameter"][parameter]
            current_series = current_feature["properties"]["parameter"][parameter]
            overlap = set(target_series) & set(current_series)
            if overlap:
                raise GateError(f"{parameter}: duplicate dates across chunks: {sorted(overlap)[:3]}")
            target_series.update(current_series)

    merged["features"] = [merged_features[key] for key in sorted(merged_features)]
    merged.setdefault("header", {})["start"] = start
    merged["header"]["end"] = end
    merged["header"]["sources"] = sorted(all_sources)
    return merged


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def nearest_feature(estate: dict, payload: dict) -> tuple[int, dict, float]:
    candidates = []
    for index, feature in enumerate(payload["features"]):
        coordinates = feature.get("geometry", {}).get("coordinates") or []
        if len(coordinates) < 2:
            raise GateError("NASA grid feature has no longitude/latitude")
        lon, lat = float(coordinates[0]), float(coordinates[1])
        distance = haversine_km(estate["latitude"], estate["longitude"], lat, lon)
        candidates.append((distance, index, feature))
    distance, index, feature = min(candidates, key=lambda item: (item[0], item[1]))
    return index, feature, distance


def clean_value(value):
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric <= FILL_VALUE + 0.001:
        return None
    return numeric


def normalize_parameter_value(parameter: str, raw_value, source_units: str | None):
    value = clean_value(raw_value)
    if value is None:
        return None
    if parameter == "ALLSKY_SFC_SW_DWN":
        normalized_units = (source_units or "").replace(" ", "").lower()
        if normalized_units in {"mj/m^2/day", "mj/m2/day"}:
            return value / 3.6
        if normalized_units in {"kwh/m^2/day", "kwh/m2/day"}:
            return value
        raise GateError(
            f"ALLSKY_SFC_SW_DWN returned unsupported units {source_units!r}; "
            "refusing to label or convert the values"
        )
    return value


def write_csv(path: Path, fieldnames: list[str], rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def mean(values: list[float | None]):
    valid = [v for v in values if v is not None]
    return sum(valid) / len(valid) if valid else None


def total(values: list[float | None]):
    valid = [v for v in values if v is not None]
    return sum(valid) if valid else None


def value_at_lag(rows: list[dict], index: int, column: str, lag: int):
    source_index = index - lag
    return rows[source_index].get(column) if source_index >= 0 else None


def window_before(rows: list[dict], index: int, column: str, lag: int, width: int):
    end_index = index - lag + 1
    start_index = end_index - width
    if end_index <= 0:
        return []
    return [row.get(column) for row in rows[max(0, start_index):end_index]]


def add_derived_features(rows_by_estate: dict[str, list[dict]]) -> None:
    for estate_rows in rows_by_estate.values():
        for index, row in enumerate(estate_rows):
            rain_col = "rain_power_corrected_mm_day"
            temp_col = "temperature_mean_c"
            humidity_col = "relative_humidity_pct"
            wind_col = "wind_speed_2m_m_s"
            solar_col = "solar_allsky_kwh_m2_day"

            row["rain_lag1_mm"] = value_at_lag(estate_rows, index, rain_col, 1)
            for width in (3, 7, 14, 30):
                row[f"rain_sum_{width}d_lag1_mm"] = total(
                    window_before(estate_rows, index, rain_col, 1, width)
                )

            # Lag 8 is the conservative operational feature boundary because
            # the slowest included POWER product can arrive 5-7 days late.
            row["nasa_operational_safe_lag_days"] = OPERATIONAL_SAFE_LAG_DAYS
            row["rain_lag8_mm"] = value_at_lag(
                estate_rows, index, rain_col, OPERATIONAL_SAFE_LAG_DAYS
            )
            row["rain_sum_7d_lag8_mm"] = total(
                window_before(estate_rows, index, rain_col, OPERATIONAL_SAFE_LAG_DAYS, 7)
            )
            row["rain_sum_14d_lag8_mm"] = total(
                window_before(estate_rows, index, rain_col, OPERATIONAL_SAFE_LAG_DAYS, 14)
            )
            row["temperature_mean_7d_lag8_c"] = mean(
                window_before(estate_rows, index, temp_col, OPERATIONAL_SAFE_LAG_DAYS, 7)
            )
            row["relative_humidity_mean_7d_lag8_pct"] = mean(
                window_before(estate_rows, index, humidity_col, OPERATIONAL_SAFE_LAG_DAYS, 7)
            )
            row["wind_speed_mean_7d_lag8_m_s"] = mean(
                window_before(estate_rows, index, wind_col, OPERATIONAL_SAFE_LAG_DAYS, 7)
            )
            row["solar_sum_7d_lag8_kwh_m2"] = total(
                window_before(estate_rows, index, solar_col, OPERATIONAL_SAFE_LAG_DAYS, 7)
            )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def acquire(args) -> int:
    coordinate_path = Path(args.coordinates).resolve()
    output_dir = Path(args.output).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise GateError(f"Output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    start_day = parse_yyyymmdd(args.start)
    end_day = parse_yyyymmdd(args.end)
    if end_day < start_day:
        raise GateError("End date is before start date")
    today_utc = dt.datetime.now(dt.timezone.utc).date()
    if end_day > today_utc - dt.timedelta(days=7):
        raise GateError(
            f"End date {end_day} is too recent for the solar latency gate; "
            f"use {today_utc - dt.timedelta(days=7)} or earlier"
        )

    estates = load_estates(coordinate_path)
    bounds = regional_bounds(estates)
    expected_dates = list(iso_dates(start_day, end_day))
    expected_date_keys = [day.strftime("%Y%m%d") for day in expected_dates]
    chunks = calendar_year_chunks(start_day, end_day)
    raw_dir = output_dir / "01_raw_nasa_json"
    processed_dir = output_dir / "02_processed"
    audit_dir = output_dir / "03_audit"
    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(coordinate_path, audit_dir / coordinate_path.name)

    payloads = {}
    request_manifest = []
    print(f"NASA POWER controlled regional acquisition v{VERSION}")
    print(f"Scope: 37 estates (TTEL 16, KVPL 13, HPL display / HRPL system 8)")
    print(f"Period: {start_day.isoformat()} to {end_day.isoformat()} ({len(expected_dates)} days)")
    print("Project A access: DISABLED | Retraining: DISABLED | RL allocation: DISABLED")

    total_requests = len(PARAMETERS) * len(chunks)
    request_number = 0
    for parameter in PARAMETERS:
        chunk_payloads = []
        for chunk_start_day, chunk_end_day in chunks:
            request_number += 1
            chunk_start = chunk_start_day.strftime("%Y%m%d")
            chunk_end = chunk_end_day.strftime("%Y%m%d")
            raw_path = raw_dir / f"{parameter}_{chunk_start}_{chunk_end}.json"
            url = build_url(parameter, chunk_start, chunk_end, bounds)
            print(
                f"[{request_number}/{total_requests}] {parameter}: "
                f"{chunk_start_day.isoformat()} to {chunk_end_day.isoformat()}"
            )
            payload = download_json(url, raw_path, args.force)
            validate_payload(payload, parameter, chunk_start, chunk_end)
            chunk_payloads.append(payload)
            request_manifest.append({
                "parameter": parameter,
                "chunk_start": chunk_start_day.isoformat(),
                "chunk_end": chunk_end_day.isoformat(),
                "time_standard": PARAMETERS[parameter]["time_standard"],
                "api_url": url,
                "raw_file": str(raw_path.relative_to(output_dir)),
                "raw_sha256": sha256(raw_path),
                "api_version": payload.get("header", {}).get("api", {}).get("version"),
                "sources": payload.get("header", {}).get("sources", []),
                "grid_features_returned": len(payload["features"]),
                "units": (payload.get("parameters", {}).get(parameter) or {}).get("units"),
                "longname": (payload.get("parameters", {}).get(parameter) or {}).get("longname"),
            })
            time.sleep(0.15)
        payloads[parameter] = merge_chunk_payloads(
            parameter, chunk_payloads, args.start, args.end
        )

    mappings = {}
    crosswalk_rows = []
    shared_groups = defaultdict(list)
    for estate in estates:
        for parameter, payload in payloads.items():
            feature_index, feature, distance = nearest_feature(estate, payload)
            coords = feature["geometry"]["coordinates"]
            grid_lon, grid_lat = float(coords[0]), float(coords[1])
            grid_elevation = float(coords[2]) if len(coords) > 2 and coords[2] is not None else None
            grid_key = f"{parameter}:{grid_lat:.6f}:{grid_lon:.6f}"
            mappings[(estate["estate_key"], parameter)] = feature_index
            shared_groups[grid_key].append(estate["estate_key"])
            meta = payload.get("parameters", {}).get(parameter) or {}
            crosswalk_rows.append({
                "company_display_code": estate["company"],
                "company_system_code": estate["company_system_code"],
                "region": estate["region"],
                "estate": estate["estate"],
                "estate_key": estate["estate_key"],
                "coordinate_status": estate["status"],
                "coordinate_confidence": estate["confidence"],
                "estate_latitude": estate["latitude"],
                "estate_longitude": estate["longitude"],
                "parameter": parameter,
                "output_column": PARAMETERS[parameter]["column"],
                "time_standard": PARAMETERS[parameter]["time_standard"],
                "source_grid_latitude": grid_lat,
                "source_grid_longitude": grid_lon,
                "source_grid_elevation_m": grid_elevation,
                "distance_to_grid_point_km": round(distance, 3),
                "grid_group_id": grid_key,
                "units": meta.get("units"),
                "longname": meta.get("longname"),
                "coordinate_source_url": estate["source_url"],
            })

    crosswalk_fields = list(crosswalk_rows[0])
    write_csv(audit_dir / "estate_parameter_grid_crosswalk.csv", crosswalk_fields, crosswalk_rows)

    shared_rows = []
    for grid_key, estate_keys in sorted(shared_groups.items()):
        parameter, grid_lat, grid_lon = grid_key.split(":")
        shared_rows.append({
            "parameter": parameter,
            "source_grid_latitude": grid_lat,
            "source_grid_longitude": grid_lon,
            "estate_count": len(estate_keys),
            "is_shared_grid": "YES" if len(estate_keys) > 1 else "NO",
            "estate_keys": " | ".join(sorted(estate_keys)),
        })
    write_csv(
        audit_dir / "shared_grid_groups.csv",
        ["parameter", "source_grid_latitude", "source_grid_longitude", "estate_count", "is_shared_grid", "estate_keys"],
        shared_rows,
    )

    rows_by_estate = {}
    missing_counts = Counter()
    invalid_counts = Counter()
    for estate in estates:
        estate_rows = []
        for day, date_key in zip(expected_dates, expected_date_keys):
            row = {
                "company_display_code": estate["company"],
                "company_system_code": estate["company_system_code"],
                "region": estate["region"],
                "estate": estate["estate"],
                "estate_key": estate["estate_key"],
                "observation_date": day.isoformat(),
                "year": day.year,
                "month": day.month,
                "day": day.day,
                "day_of_year": day.timetuple().tm_yday,
                "financial_year_apr_mar": financial_year(day),
                "coordinate_status": estate["status"],
                "coordinate_confidence": estate["confidence"],
                "weather_data_kind": "NASA_POWER_OBSERVED_OR_REANALYSIS_NOT_FORECAST",
                "same_day_weather_forecast_safe": "NO",
            }
            for parameter, spec in PARAMETERS.items():
                feature = payloads[parameter]["features"][mappings[(estate["estate_key"], parameter)]]
                raw_value = feature["properties"]["parameter"][parameter].get(date_key)
                source_units = (payloads[parameter].get("parameters", {}).get(parameter) or {}).get("units")
                value = normalize_parameter_value(parameter, raw_value, source_units)
                row[spec["column"]] = value
                if value is None:
                    missing_counts[parameter] += 1
                elif value < spec["valid_min"] or value > spec["valid_max"]:
                    invalid_counts[parameter] += 1
            estate_rows.append(row)
        rows_by_estate[estate["estate_key"]] = estate_rows

    add_derived_features(rows_by_estate)
    all_rows = [row for estate in estates for row in rows_by_estate[estate["estate_key"]]]
    expected_rows = len(estates) * len(expected_dates)
    keys = [(row["estate_key"], row["observation_date"]) for row in all_rows]
    duplicate_keys = len(keys) - len(set(keys))
    cross_field_failures = sum(
        1 for row in all_rows
        if row["temperature_min_c"] is not None
        and row["temperature_mean_c"] is not None
        and row["temperature_max_c"] is not None
        and not (row["temperature_min_c"] <= row["temperature_mean_c"] <= row["temperature_max_c"])
    )

    daily_fields = list(all_rows[0])
    daily_path = processed_dir / f"nasa_power_37_estate_daily_{args.start}_{args.end}.csv"
    write_csv(daily_path, daily_fields, all_rows)

    quality_rows = []
    for parameter, spec in PARAMETERS.items():
        total_cells = expected_rows
        missing = missing_counts[parameter]
        invalid = invalid_counts[parameter]
        quality_rows.append({
            "check_type": "PARAMETER_PROFILE",
            "parameter": parameter,
            "output_column": spec["column"],
            "expected_values": total_cells,
            "missing_values": missing,
            "missing_pct": round(100.0 * missing / total_cells, 6),
            "invalid_range_values": invalid,
            "distinct_source_grid_cells": len({
                r["grid_group_id"] for r in crosswalk_rows if r["parameter"] == parameter
            }),
            "gate": "PASS" if missing == 0 and invalid == 0 else "FAIL",
        })
    quality_rows.extend([
        {
            "check_type": "ROW_COUNT", "parameter": "ALL", "output_column": "",
            "expected_values": expected_rows, "missing_values": expected_rows - len(all_rows),
            "missing_pct": 0 if expected_rows == len(all_rows) else None,
            "invalid_range_values": 0, "distinct_source_grid_cells": "",
            "gate": "PASS" if expected_rows == len(all_rows) else "FAIL",
        },
        {
            "check_type": "DUPLICATE_ESTATE_DATE_KEYS", "parameter": "ALL", "output_column": "",
            "expected_values": 0, "missing_values": "", "missing_pct": "",
            "invalid_range_values": duplicate_keys, "distinct_source_grid_cells": "",
            "gate": "PASS" if duplicate_keys == 0 else "FAIL",
        },
        {
            "check_type": "TEMPERATURE_ORDER", "parameter": "T2M_MIN<=T2M<=T2M_MAX", "output_column": "",
            "expected_values": expected_rows, "missing_values": "", "missing_pct": "",
            "invalid_range_values": cross_field_failures, "distinct_source_grid_cells": "",
            "gate": "PASS" if cross_field_failures == 0 else "FAIL",
        },
    ])
    quality_fields = list(quality_rows[0])
    write_csv(audit_dir / "data_quality_summary.csv", quality_fields, quality_rows)

    hard_gate_pass = (
        len(estates) == 37
        and len(all_rows) == expected_rows
        and duplicate_keys == 0
        and cross_field_failures == 0
        and all(missing_counts[p] == 0 and invalid_counts[p] == 0 for p in PARAMETERS)
    )
    decision = (
        "PROVISIONAL_WEATHER_ACQUISITION_COMPLETED_NOT_APPROVED_FOR_TRAINING"
        if hard_gate_pass else "WEATHER_ACQUISITION_FAILED_QUALITY_GATES"
    )
    manifest = {
        "status": decision,
        "version": VERSION,
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": {
            "estate_count": len(estates),
            "display_company_counts": dict(Counter(e["company"] for e in estates)),
            "system_company_counts": dict(Counter(e["company_system_code"] for e in estates)),
            "coordinate_status": "PROVISIONAL",
            "coordinate_confidence": dict(Counter(e["confidence"] for e in estates)),
        },
        "period": {
            "start": start_day.isoformat(), "end": end_day.isoformat(),
            "days": len(expected_dates), "expected_estate_day_rows": expected_rows,
            "actual_estate_day_rows": len(all_rows),
            "annual_chunks": len(chunks),
        },
        "regional_bounds": bounds,
        "requests": request_manifest,
        "quality": {
            "hard_gate_pass": hard_gate_pass,
            "duplicate_estate_date_keys": duplicate_keys,
            "temperature_order_failures": cross_field_failures,
            "missing_values_by_parameter": dict(missing_counts),
            "invalid_values_by_parameter": dict(invalid_counts),
        },
        "interpretation": {
            "nasa_weather_is_estate_station_data": False,
            "nasa_weather_is_future_forecast": False,
            "same_day_observed_weather_allowed_in_operational_forecast": False,
            "operational_safe_lag_days_for_included_products": OPERATIONAL_SAFE_LAG_DAYS,
            "imerg_included": False,
            "imerg_note": "Current POWER Daily API v2.9.5 rejected the legacy IMERG parameter; PRECTOTCORR is used and must be gauge-validated.",
        },
        "controls": {
            "project_a_accessed": False,
            "project_b_retrained": False,
            "rl_allocation_executed": False,
            "training_authorized": False,
            "four_missing_ttel_crop_target_estates_repaired_by_weather": False,
        },
        "sources": NASA_DOCS,
    }
    manifest_path = audit_dir / "acquisition_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    summary = "\n".join([
        "TTE PROJECT B NASA POWER 37-ESTATE ACQUISITION COMPLETED",
        f"Decision: {decision}",
        f"Period: {start_day.isoformat()} to {end_day.isoformat()}",
        f"Estates: {len(estates)} (TTEL=16, KVPL=13, HPL display / HRPL system=8)",
        f"Estate-day rows: {len(all_rows):,} / {expected_rows:,}",
        f"Duplicate estate-date keys: {duplicate_keys}",
        f"Temperature order failures: {cross_field_failures}",
        "Rainfall product: PRECTOTCORR (MERRA-2/GEOS-IT corrected precipitation), not IMERG",
        "Coordinates: PROVISIONAL; NASA values are gridded, not estate weather-station observations",
        "Project A accessed: NO",
        "Project B retrained: NO",
        "RL allocation executed: NO",
        "Training authorized: NO",
        f"Output: {output_dir}",
    ]) + "\n"
    (output_dir / "FINAL_SUMMARY.txt").write_text(summary, encoding="utf-8")

    hash_rows = []
    for path in sorted(p for p in output_dir.rglob("*") if p.is_file() and p.name != "file_hashes_sha256.csv"):
        hash_rows.append({
            "relative_path": str(path.relative_to(output_dir)).replace("\\", "/"),
            "size_bytes": path.stat().st_size,
            "sha256": sha256(path),
        })
    write_csv(audit_dir / "file_hashes_sha256.csv", ["relative_path", "size_bytes", "sha256"], hash_rows)
    print("\n" + summary)
    return 0 if hard_gate_pass else 2


def self_test(coordinates: Path) -> int:
    estates = load_estates(coordinates)
    assert len(estates) == 37
    assert Counter(e["company_system_code"] for e in estates) == Counter({"TTEL": 16, "KVPL": 13, "HRPL": 8})
    assert financial_year(dt.date(2021, 4, 1)) == "2021/22"
    assert financial_year(dt.date(2022, 3, 31)) == "2021/22"
    assert calendar_year_chunks(dt.date(2021, 4, 1), dt.date(2023, 2, 1)) == [
        (dt.date(2021, 4, 1), dt.date(2021, 12, 31)),
        (dt.date(2022, 1, 1), dt.date(2022, 12, 31)),
        (dt.date(2023, 1, 1), dt.date(2023, 2, 1)),
    ]
    assert abs(haversine_km(6.9, 80.7, 6.9, 80.7)) < 1e-9

    feature_a = {"geometry": {"coordinates": [80.5, 6.5, 100]}, "properties": {"parameter": {"T2M": {"20210101": 20}}}}
    feature_b = {"geometry": {"coordinates": [81.0, 7.0, 200]}, "properties": {"parameter": {"T2M": {"20210101": 22}}}}
    payload = {"features": [feature_a, feature_b]}
    index, _, distance = nearest_feature({"latitude": 6.51, "longitude": 80.51}, payload)
    assert index == 0 and distance < 2

    synthetic = []
    start = dt.date(2021, 1, 1)
    for i in range(20):
        synthetic.append({
            "observation_date": (start + dt.timedelta(days=i)).isoformat(),
            "rain_power_corrected_mm_day": float(i + 1),
            "temperature_mean_c": 20.0 + i,
            "relative_humidity_pct": 70.0,
            "wind_speed_2m_m_s": 2.0,
            "solar_allsky_kwh_m2_day": 5.0,
        })
    add_derived_features({"TEST": synthetic})
    assert synthetic[8]["rain_lag8_mm"] == 1.0
    assert synthetic[14]["rain_sum_7d_lag8_mm"] == sum(range(1, 8))
    assert synthetic[1]["rain_lag1_mm"] == 1.0
    assert normalize_parameter_value("ALLSKY_SFC_SW_DWN", 18.0, "MJ/m^2/day") == 5.0
    print("SELF_TEST_PASS")
    print("Scope: 37 estates; system code mapping HPL -> HRPL confirmed")
    print("No network, project, training, or RL action executed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coordinates", required=True, help="Controlled provisional coordinate CSV")
    parser.add_argument("--output", help="New empty output directory")
    parser.add_argument("--start", default="20210401", help="Start date YYYYMMDD")
    parser.add_argument("--end", help="End date YYYYMMDD; must be at least 7 days behind UTC today")
    parser.add_argument("--force", action="store_true", help="Redownload raw JSON if already present")
    parser.add_argument("--self-test", action="store_true", help="Run offline package tests")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    coordinates = Path(args.coordinates).resolve()
    try:
        if args.self_test:
            return self_test(coordinates)
        if not args.output or not args.end:
            parser.error("--output and --end are required unless --self-test is used")
        return acquire(args)
    except GateError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
