#!/usr/bin/env python3
"""Auditable NASA POWER IMERG acquisition for the 37-estate Project B scope.

This is a rainfall-only companion to the existing NASA POWER meteorological
snapshot. It uses the current POWER IMERG parameters, UTC time, native 0.1
degree grid, annual request chunks, immutable raw responses, and strict gates.
It never trains a model, accesses Project A, or executes RL allocation.
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


VERSION = "1.1.0"
DAILY_REGIONAL_URL = "https://power.larc.nasa.gov/api/temporal/daily/regional"
DAILY_POINT_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
PARAMETER_CATALOG_URL = (
    "https://power.larc.nasa.gov/api/system/manager/parameters"
    "?community=AG&temporal=DAILY"
)
EXPECTED_COMPANY_COUNTS = {"TTEL": 16, "KVPL": 13, "HPL": 8}
SYSTEM_COMPANY_CODE = {"TTEL": "TTEL", "KVPL": "KVPL", "HPL": "HRPL"}
PARAMETERS = {
    "IMERG_PRECTOT": {
        "column": "rain_imerg_mm_day",
        "valid_min": 0.0,
        "valid_max": 1000.0,
        "units": "mm/day",
        "role": "NASA GPM IMERG total precipitation at native 0.1 degree resolution",
    },
    "IMERG_PRECTOT_COUNT": {
        "column": "imerg_wet_half_hour_retrieval_count",
        "valid_min": 0.0,
        "valid_max": 48.0,
        "units": "count",
        "role": "Count of half-hourly retrievals with precipitation at least 0.01 mm",
    },
}
FILL_VALUE = -999.0
TIME_STANDARD = "UTC"
OPERATIONAL_SAFE_LAG_DAYS = 2
# NASA documents IMERG Final Run at about 3.5 months behind near real time.
# This age flag is conservative metadata, not proof of the exact run loaded.
FINAL_RUN_AGE_DAYS = 107
NASA_DOCS = {
    "daily_api": "https://power.larc.nasa.gov/docs/services/api/temporal/daily/",
    "data_sources": "https://power.larc.nasa.gov/docs/methodology/data/sources/",
    "parameter_dictionary": "https://power.larc.nasa.gov/docs/tutorials/parameters/",
    "release_guide": "https://power.larc.nasa.gov/docs/release-guide/",
    "parameter_catalog_api": PARAMETER_CATALOG_URL,
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
            raise GateError(f"Duplicate estate key at row {index}: {company}/{estate}")
        seen.add(key)
        if company not in EXPECTED_COMPANY_COUNTS:
            raise GateError(f"Unexpected company at row {index}: {company}")
        try:
            latitude = float(row["latitude"])
            longitude = float(row["longitude"])
        except ValueError as exc:
            raise GateError(f"Invalid coordinate at row {index}") from exc
        if not (5.5 <= latitude <= 7.5 and 79.5 <= longitude <= 81.5):
            raise GateError(f"Coordinate outside controlled Sri Lanka box: {estate}")
        if row["status"].strip().upper() != "PROVISIONAL":
            raise GateError(f"Estate is not explicitly PROVISIONAL: {estate}")
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
        raise GateError(f"Coordinate confidence profile changed: {dict(confidence)}")
    return prepared


def regional_bounds(estates: list[dict]) -> dict[str, float]:
    # POWER regional requests require a two-degree span. This fixed box covers
    # all controlled estates and returns 400 native 0.1-degree IMERG cells.
    bounds = {
        "latitude-min": 5.5,
        "latitude-max": 7.5,
        "longitude-min": 79.5,
        "longitude-max": 81.5,
    }
    for estate in estates:
        if not (
            bounds["latitude-min"] <= estate["latitude"] <= bounds["latitude-max"]
            and bounds["longitude-min"] <= estate["longitude"] <= bounds["longitude-max"]
        ):
            raise GateError(f"Estate lies outside the controlled request box: {estate['estate']}")
    return bounds


def build_url(parameter: str, start: str, end: str, bounds: dict[str, float]) -> str:
    query = {
        **bounds,
        "parameters": parameter,
        "community": "AG",
        "start": start,
        "end": end,
        "time-standard": TIME_STANDARD,
        "format": "JSON",
    }
    return f"{DAILY_REGIONAL_URL}?{urllib.parse.urlencode(query)}"


def build_point_url(parameters: list[str], start: str, end: str) -> str:
    # This point is used only to discover the latest non-fill IMERG date before
    # the much larger regional acquisition starts. Regional/date gates still
    # validate every source cell used in the final dataset.
    query = {
        "parameters": ",".join(parameters),
        "community": "AG",
        "longitude": 80.75,
        "latitude": 6.95,
        "start": start,
        "end": end,
        "time-standard": TIME_STANDARD,
        "format": "JSON",
    }
    return f"{DAILY_POINT_URL}?{urllib.parse.urlencode(query)}"


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def download_json(url: str, destination: Path | None = None, force: bool = False) -> dict:
    if destination and destination.exists() and not force:
        try:
            return json.loads(destination.read_text(encoding="utf-8"))
        except Exception:
            destination.unlink()
    headers = {
        "User-Agent": "TTE-Project-B-NASA-POWER-IMERG-Audit/1.1",
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
                if destination:
                    atomic_write_bytes(destination, body)
                return payload
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:1200]
            last_error = GateError(f"NASA HTTP {exc.code}: {detail}")
            if exc.code not in {429, 500, 502, 503, 504}:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, GateError) as exc:
            last_error = exc
        if attempt < 4:
            time.sleep(min(2 ** attempt, 20))
    raise GateError(f"NASA request failed after retries: {last_error}")


def validate_catalog(catalog: dict) -> dict:
    selected = {}
    for parameter, spec in PARAMETERS.items():
        metadata = catalog.get(parameter)
        if not isinstance(metadata, dict):
            raise GateError(f"Current NASA parameter catalog does not contain {parameter}")
        if str(metadata.get("temporal", "")).upper() != "DAILY":
            raise GateError(f"{parameter} is not currently a DAILY parameter")
        if str(metadata.get("community", "")).upper() != "AG":
            raise GateError(f"{parameter} is not currently available to AG")
        if str(metadata.get("units", "")) != spec["units"]:
            raise GateError(
                f"{parameter} unit changed: expected {spec['units']!r}, "
                f"found {metadata.get('units')!r}"
            )
        selected[parameter] = metadata
    return selected


def latest_common_available_date(payload: dict, start: dt.date, end: dt.date) -> dt.date:
    if payload.get("type") != "Feature":
        raise GateError("IMERG availability probe did not return a point Feature")
    header = payload.get("header", {})
    if str(header.get("time_standard", "")).upper() != TIME_STANDARD:
        raise GateError("IMERG availability probe did not return UTC data")
    if "IMERG" not in {str(value).upper() for value in header.get("sources", [])}:
        raise GateError("IMERG availability probe does not declare IMERG as its source")
    series_by_parameter = payload.get("properties", {}).get("parameter", {})
    common_valid_dates = None
    for parameter in PARAMETERS:
        series = series_by_parameter.get(parameter)
        if not isinstance(series, dict):
            raise GateError(f"Availability probe lacks {parameter}")
        valid_dates = {
            parse_yyyymmdd(key)
            for key, value in series.items()
            if clean_value(value) is not None
        }
        common_valid_dates = (
            valid_dates if common_valid_dates is None
            else common_valid_dates & valid_dates
        )
    if not common_valid_dates:
        raise GateError("NASA returned no usable IMERG dates in the requested range")
    latest = max(common_valid_dates)
    if latest < start:
        raise GateError("NASA has no usable IMERG date on or after the requested start")
    expected_through_latest = set(iso_dates(start, latest))
    internal_gaps = sorted(expected_through_latest - common_valid_dates)
    if internal_gaps:
        raise GateError(
            "IMERG availability contains internal non-trailing gaps; first gaps: "
            + ", ".join(day.isoformat() for day in internal_gaps[:10])
        )
    if latest > end:
        raise GateError("Availability probe returned a date after the requested end")
    return latest


def grid_key(feature: dict) -> tuple[float, float]:
    coords = feature.get("geometry", {}).get("coordinates") or []
    if len(coords) < 2:
        raise GateError("NASA grid feature has no longitude/latitude")
    return round(float(coords[0]), 6), round(float(coords[1]), 6)


def validate_payload(payload: dict, parameter: str, start: str, end: str) -> None:
    if payload.get("type") != "FeatureCollection":
        raise GateError(f"{parameter}: expected FeatureCollection")
    features = payload.get("features")
    if not isinstance(features, list) or not features:
        raise GateError(f"{parameter}: response contains no grid features")
    header = payload.get("header", {})
    if str(header.get("start")) != start or str(header.get("end")) != end:
        raise GateError(f"{parameter}: response date range does not match request")
    if str(header.get("time_standard", "")).upper() != TIME_STANDARD:
        raise GateError(f"{parameter}: expected UTC, found {header.get('time_standard')!r}")
    if "IMERG" not in {str(value).upper() for value in header.get("sources", [])}:
        raise GateError(f"{parameter}: response does not declare IMERG as its source")
    expected_dates = {
        day.strftime("%Y%m%d")
        for day in iso_dates(parse_yyyymmdd(start), parse_yyyymmdd(end))
    }
    keys = [grid_key(feature) for feature in features]
    if len(keys) != len(set(keys)):
        raise GateError(f"{parameter}: duplicate source grid coordinates")
    for feature in features:
        series = feature.get("properties", {}).get("parameter", {}).get(parameter)
        if not isinstance(series, dict):
            raise GateError(f"{parameter}: grid feature lacks the requested series")
        if set(series) != expected_dates:
            raise GateError(f"{parameter}: a grid feature has missing or extra dates")


def merge_chunk_payloads(parameter: str, payloads: list[dict], start: str, end: str) -> dict:
    if not payloads:
        raise GateError(f"{parameter}: no chunk responses")
    merged = copy.deepcopy(payloads[0])
    merged_features = {grid_key(feature): feature for feature in merged["features"]}
    expected_grids = set(merged_features)
    sources = set(merged.get("header", {}).get("sources", []))
    api_versions = {merged.get("header", {}).get("api", {}).get("version")}
    for payload in payloads[1:]:
        current = {grid_key(feature): feature for feature in payload["features"]}
        if set(current) != expected_grids:
            raise GateError(f"{parameter}: source grid changed between annual chunks")
        sources.update(payload.get("header", {}).get("sources", []))
        api_versions.add(payload.get("header", {}).get("api", {}).get("version"))
        for key, feature in current.items():
            target_series = merged_features[key]["properties"]["parameter"][parameter]
            incoming = feature["properties"]["parameter"][parameter]
            overlap = set(target_series) & set(incoming)
            if overlap:
                raise GateError(f"{parameter}: duplicate dates across chunks")
            target_series.update(incoming)
    merged["features"] = [merged_features[key] for key in sorted(merged_features)]
    merged.setdefault("header", {})["start"] = start
    merged["header"]["end"] = end
    merged["header"]["sources"] = sorted(sources)
    merged["header"]["api_versions_seen"] = sorted(str(v) for v in api_versions)
    return merged


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def nearest_feature(estate: dict, payload: dict) -> tuple[int, dict, float]:
    candidates = []
    for index, feature in enumerate(payload["features"]):
        lon, lat = grid_key(feature)
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


def write_csv(path: Path, fieldnames: list[str], rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def value_at_lag(rows: list[dict], index: int, column: str, lag: int):
    source_index = index - lag
    return rows[source_index].get(column) if source_index >= 0 else None


def window_before(rows: list[dict], index: int, column: str, lag: int, width: int):
    end_index = index - lag + 1
    start_index = end_index - width
    if end_index <= 0:
        return []
    return [
        row.get(column)
        for row in rows[max(0, start_index):end_index]
        if row.get(column) is not None
    ]


def add_derived_features(rows_by_estate: dict[str, list[dict]]) -> None:
    column = "rain_imerg_mm_day"
    for rows in rows_by_estate.values():
        for index, row in enumerate(rows):
            row["rain_imerg_lag1_mm"] = value_at_lag(rows, index, column, 1)
            for width in (3, 7, 14, 30):
                values = window_before(rows, index, column, 1, width)
                row[f"rain_imerg_sum_{width}d_lag1_mm"] = sum(values) if values else None
            row["nasa_imerg_operational_safe_lag_days"] = OPERATIONAL_SAFE_LAG_DAYS
            row["rain_imerg_lag2_mm"] = value_at_lag(
                rows, index, column, OPERATIONAL_SAFE_LAG_DAYS
            )
            for width in (7, 14, 30):
                values = window_before(
                    rows, index, column, OPERATIONAL_SAFE_LAG_DAYS, width
                )
                row[f"rain_imerg_sum_{width}d_lag2_mm"] = sum(values) if values else None


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
    requested_end_day = parse_yyyymmdd(args.end)
    if requested_end_day < start_day:
        raise GateError("End date is before start date")
    today_utc = dt.datetime.now(dt.timezone.utc).date()
    if requested_end_day > today_utc - dt.timedelta(days=1):
        raise GateError("End date must be at least one complete UTC day behind today")

    estates = load_estates(coordinate_path)
    bounds = regional_bounds(estates)
    raw_dir = output_dir / "01_raw_nasa_json"
    processed_dir = output_dir / "02_processed"
    audit_dir = output_dir / "03_audit"
    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(coordinate_path, audit_dir / coordinate_path.name)

    probe_start = start_day.strftime("%Y%m%d")
    probe_end = requested_end_day.strftime("%Y%m%d")
    probe_url = build_point_url(list(PARAMETERS), probe_start, probe_end)
    probe_path = raw_dir / f"IMERG_AVAILABILITY_PROBE_{probe_start}_{probe_end}.json"
    availability_probe = download_json(probe_url, probe_path, args.force)
    latest_available_day = latest_common_available_date(
        availability_probe, start_day, requested_end_day
    )
    if latest_available_day < requested_end_day:
        message = (
            f"NASA's latest common non-fill IMERG date is {latest_available_day}; "
            f"requested end was {requested_end_day}."
        )
        if not args.allow_end_truncation:
            raise GateError(message + " Re-run with --allow-end-truncation or an earlier end.")
        print(f"WARNING: {message} The output will end at the latest available date.")
    end_day = min(requested_end_day, latest_available_day)
    dates = list(iso_dates(start_day, end_day))
    date_keys = [day.strftime("%Y%m%d") for day in dates]
    chunks = calendar_year_chunks(start_day, end_day)
    maturity_cutoff = today_utc - dt.timedelta(days=FINAL_RUN_AGE_DAYS)

    print(f"NASA POWER IMERG controlled acquisition v{VERSION}")
    print("Scope: 37 estates (TTEL 16, KVPL 13, HPL display / HRPL system 8)")
    print(
        f"Period: {start_day.isoformat()} to {end_day.isoformat()} ({len(dates)} days); "
        f"requested end={requested_end_day.isoformat()}"
    )
    print("Time standard: UTC | Native IMERG grid: 0.1 degree")
    print("Project A access: DISABLED | Retraining: DISABLED | RL allocation: DISABLED")

    catalog = download_json(PARAMETER_CATALOG_URL)
    selected_catalog = validate_catalog(catalog)
    catalog_path = audit_dir / "nasa_power_parameter_catalog_extract.json"
    catalog_path.write_text(
        json.dumps({
            "retrieved_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "catalog_url": PARAMETER_CATALOG_URL,
            "selected_parameters": selected_catalog,
        }, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    payloads = {}
    request_manifest = []
    request_count = len(PARAMETERS) * len(chunks)
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
                f"[{request_number}/{request_count}] {parameter}: "
                f"{chunk_start_day.isoformat()} to {chunk_end_day.isoformat()}"
            )
            payload = download_json(url, raw_path, args.force)
            validate_payload(payload, parameter, chunk_start, chunk_end)
            chunk_payloads.append(payload)
            request_manifest.append({
                "parameter": parameter,
                "chunk_start": chunk_start_day.isoformat(),
                "chunk_end": chunk_end_day.isoformat(),
                "time_standard": TIME_STANDARD,
                "api_url": url,
                "raw_file": str(raw_path.relative_to(output_dir)).replace("\\", "/"),
                "raw_sha256": sha256(raw_path),
                "api_version": payload.get("header", {}).get("api", {}).get("version"),
                "sources": payload.get("header", {}).get("sources", []),
                "grid_features_returned": len(payload["features"]),
                "units": (payload.get("parameters", {}).get(parameter) or {}).get("units"),
                "longname": (payload.get("parameters", {}).get(parameter) or {}).get("longname"),
            })
            time.sleep(0.2)
        payloads[parameter] = merge_chunk_payloads(
            parameter,
            chunk_payloads,
            start_day.strftime("%Y%m%d"),
            end_day.strftime("%Y%m%d"),
        )

    grid_sets = {parameter: {grid_key(f) for f in payload["features"]} for parameter, payload in payloads.items()}
    if len({frozenset(value) for value in grid_sets.values()}) != 1:
        raise GateError("IMERG precipitation and retrieval-count grids do not match")

    mappings = {}
    crosswalk_rows = []
    shared_groups = defaultdict(list)
    maximum_mapping_distance = 0.0
    for estate in estates:
        chosen_grid = None
        for parameter, payload in payloads.items():
            index, feature, distance = nearest_feature(estate, payload)
            lon, lat = grid_key(feature)
            if chosen_grid is None:
                chosen_grid = (lon, lat)
            elif chosen_grid != (lon, lat):
                raise GateError(f"Parameter grids disagree for {estate['estate_key']}")
            if distance > 10.0:
                raise GateError(
                    f"Nearest IMERG cell is unexpectedly far from {estate['estate_key']}: "
                    f"{distance:.3f} km"
                )
            maximum_mapping_distance = max(maximum_mapping_distance, distance)
            mappings[(estate["estate_key"], parameter)] = index
            group = f"{parameter}:{lat:.6f}:{lon:.6f}"
            shared_groups[group].append(estate["estate_key"])
            coords = feature.get("geometry", {}).get("coordinates") or []
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
                "time_standard": TIME_STANDARD,
                "source_grid_latitude": lat,
                "source_grid_longitude": lon,
                "source_grid_elevation_m": coords[2] if len(coords) > 2 else None,
                "distance_to_grid_point_km": round(distance, 3),
                "grid_group_id": group,
                "units": PARAMETERS[parameter]["units"],
                "coordinate_source_url": estate["source_url"],
            })
    write_csv(
        audit_dir / "estate_parameter_grid_crosswalk.csv",
        list(crosswalk_rows[0]),
        crosswalk_rows,
    )

    shared_rows = []
    for group, estate_keys in sorted(shared_groups.items()):
        parameter, lat, lon = group.split(":")
        shared_rows.append({
            "parameter": parameter,
            "source_grid_latitude": lat,
            "source_grid_longitude": lon,
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
    non_integer_count_values = 0
    extreme_rain_values = 0
    for estate in estates:
        estate_rows = []
        rain_feature = payloads["IMERG_PRECTOT"]["features"][
            mappings[(estate["estate_key"], "IMERG_PRECTOT")]
        ]
        source_lon, source_lat = grid_key(rain_feature)
        for day, date_key in zip(dates, date_keys):
            age_eligible = day <= maturity_cutoff
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
                "source_grid_latitude": source_lat,
                "source_grid_longitude": source_lon,
                "weather_data_kind": "NASA_POWER_GPM_IMERG_SATELLITE_PRECIPITATION_NOT_GAUGE",
                "time_standard": TIME_STANDARD,
                "imerg_run_maturity": (
                    "FINAL_RUN_ELIGIBLE_BY_AGE_NOT_PROVEN"
                    if age_eligible else "RECENT_FINAL_OR_LATE_RUN_MIXED"
                ),
                "imerg_training_eligible_by_age": "YES" if age_eligible else "NO",
                "same_day_weather_forecast_safe": "NO",
            }
            for parameter, spec in PARAMETERS.items():
                feature = payloads[parameter]["features"][
                    mappings[(estate["estate_key"], parameter)]
                ]
                value = clean_value(
                    feature["properties"]["parameter"][parameter].get(date_key)
                )
                row[spec["column"]] = value
                if value is None:
                    missing_counts[parameter] += 1
                elif value < spec["valid_min"] or value > spec["valid_max"]:
                    invalid_counts[parameter] += 1
                if parameter == "IMERG_PRECTOT" and value is not None and value > 500:
                    extreme_rain_values += 1
                if (
                    parameter == "IMERG_PRECTOT_COUNT"
                    and value is not None
                    and abs(value - round(value)) > 1e-9
                ):
                    non_integer_count_values += 1
            estate_rows.append(row)
        rows_by_estate[estate["estate_key"]] = estate_rows

    add_derived_features(rows_by_estate)
    all_rows = [row for estate in estates for row in rows_by_estate[estate["estate_key"]]]
    expected_rows = len(estates) * len(dates)
    keys = [(row["estate_key"], row["observation_date"]) for row in all_rows]
    duplicate_keys = len(keys) - len(set(keys))
    daily_path = processed_dir / (
        "nasa_power_imerg_37_estate_daily_"
        f"{start_day.strftime('%Y%m%d')}_{end_day.strftime('%Y%m%d')}.csv"
    )
    write_csv(daily_path, list(all_rows[0]), all_rows)

    distinct_cells = len({
        (row["source_grid_latitude"], row["source_grid_longitude"])
        for row in all_rows
    })
    quality_rows = []
    for parameter, spec in PARAMETERS.items():
        quality_rows.append({
            "check_type": "PARAMETER_PROFILE",
            "parameter": parameter,
            "output_column": spec["column"],
            "expected_values": expected_rows,
            "missing_values": missing_counts[parameter],
            "invalid_values": invalid_counts[parameter],
            "detail": spec["role"],
            "gate": "PASS" if missing_counts[parameter] == 0 and invalid_counts[parameter] == 0 else "FAIL",
        })
    quality_rows.extend([
        {
            "check_type": "ROW_COUNT", "parameter": "ALL", "output_column": "",
            "expected_values": expected_rows, "missing_values": expected_rows - len(all_rows),
            "invalid_values": 0, "detail": f"actual={len(all_rows)}", "gate": "PASS" if len(all_rows) == expected_rows else "FAIL",
        },
        {
            "check_type": "DUPLICATE_ESTATE_DATE_KEYS", "parameter": "ALL", "output_column": "",
            "expected_values": 0, "missing_values": 0, "invalid_values": duplicate_keys,
            "detail": "estate_key + observation_date", "gate": "PASS" if duplicate_keys == 0 else "FAIL",
        },
        {
            "check_type": "COUNT_INTEGER", "parameter": "IMERG_PRECTOT_COUNT", "output_column": "imerg_wet_half_hour_retrieval_count",
            "expected_values": expected_rows, "missing_values": 0, "invalid_values": non_integer_count_values,
            "detail": "Expected whole-number count between 0 and 48", "gate": "PASS" if non_integer_count_values == 0 else "FAIL",
        },
        {
            "check_type": "GRID_CONCORDANCE", "parameter": "IMERG_PRECTOT + IMERG_PRECTOT_COUNT", "output_column": "",
            "expected_values": len(grid_sets["IMERG_PRECTOT"]), "missing_values": 0, "invalid_values": 0,
            "detail": f"native_grid_features={len(grid_sets['IMERG_PRECTOT'])}", "gate": "PASS",
        },
        {
            "check_type": "ESTATE_MAPPING_DISTANCE", "parameter": "ALL", "output_column": "",
            "expected_values": 37, "missing_values": 0, "invalid_values": 0,
            "detail": f"max_distance_km={maximum_mapping_distance:.3f}; distinct_estate_cells={distinct_cells}", "gate": "PASS",
        },
    ])
    write_csv(
        audit_dir / "data_quality_summary.csv",
        list(quality_rows[0]),
        quality_rows,
    )

    hard_gate_pass = (
        len(estates) == 37
        and len(all_rows) == expected_rows
        and duplicate_keys == 0
        and non_integer_count_values == 0
        and all(missing_counts[p] == 0 and invalid_counts[p] == 0 for p in PARAMETERS)
    )
    decision = (
        "PROVISIONAL_IMERG_ACQUISITION_COMPLETED_NOT_APPROVED_FOR_TRAINING"
        if hard_gate_pass else "IMERG_ACQUISITION_FAILED_QUALITY_GATES"
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
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "requested_end": requested_end_day.isoformat(),
            "latest_available_date_from_probe": latest_available_day.isoformat(),
            "end_was_truncated_to_availability": end_day < requested_end_day,
            "days": len(dates),
            "expected_estate_day_rows": expected_rows,
            "actual_estate_day_rows": len(all_rows),
            "annual_chunks": len(chunks),
        },
        "product": {
            "source": "NASA POWER / GPM IMERG",
            "native_spatial_resolution_degrees": 0.1,
            "time_standard": TIME_STANDARD,
            "parameters": list(PARAMETERS),
            "distinct_grid_cells_used_by_estates": distinct_cells,
            "maximum_estate_to_grid_distance_km": round(maximum_mapping_distance, 3),
            "final_run_age_flag_days": FINAL_RUN_AGE_DAYS,
            "final_run_age_cutoff": maturity_cutoff.isoformat(),
            "recent_data_warning": "Recent dates may mix IMERG Late and Final Run data.",
        },
        "regional_bounds": bounds,
        "requests": request_manifest,
        "availability_probe": {
            "api_url": probe_url,
            "raw_file": str(probe_path.relative_to(output_dir)).replace("\\", "/"),
            "raw_sha256": sha256(probe_path),
            "requested_end": requested_end_day.isoformat(),
            "latest_common_non_fill_date": latest_available_day.isoformat(),
        },
        "quality": {
            "hard_gate_pass": hard_gate_pass,
            "duplicate_estate_date_keys": duplicate_keys,
            "missing_values_by_parameter": dict(missing_counts),
            "invalid_values_by_parameter": dict(invalid_counts),
            "non_integer_count_values": non_integer_count_values,
            "rain_values_above_500_mm": extreme_rain_values,
        },
        "interpretation": {
            "imerg_is_estate_gauge_data": False,
            "imerg_is_future_forecast": False,
            "same_day_observed_weather_allowed_in_operational_forecast": False,
            "operational_safe_lag_days": OPERATIONAL_SAFE_LAG_DAYS,
            "prectotcorr_replaced_or_deleted": False,
            "temperature_bias_repaired_by_imerg": False,
        },
        "controls": {
            "project_a_accessed": False,
            "project_b_retrained": False,
            "rl_allocation_executed": False,
            "training_authorized": False,
        },
        "sources": NASA_DOCS,
    }
    manifest_path = audit_dir / "acquisition_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    summary = "\n".join([
        "TTE PROJECT B NASA POWER IMERG 37-ESTATE ACQUISITION COMPLETED",
        f"Decision: {decision}",
        f"Period: {start_day.isoformat()} to {end_day.isoformat()}",
        f"Requested end: {requested_end_day.isoformat()}",
        f"Latest common non-fill IMERG date: {latest_available_day.isoformat()}",
        f"Estates: {len(estates)} (TTEL=16, KVPL=13, HPL display / HRPL system=8)",
        f"Estate-day rows: {len(all_rows):,} / {expected_rows:,}",
        f"Native IMERG cells used by estates: {distinct_cells}",
        f"Maximum estate-to-grid distance: {maximum_mapping_distance:.3f} km",
        f"Duplicate estate-date keys: {duplicate_keys}",
        f"Missing IMERG values: {sum(missing_counts.values())}",
        f"Invalid IMERG values: {sum(invalid_counts.values())}",
        "Rainfall product: IMERG_PRECTOT; UTC daily; native 0.1 degree grid",
        "Recent data may mix IMERG Late and Final Run data",
        "Coordinates remain PROVISIONAL; IMERG is not an estate rain gauge",
        "Project A accessed: NO",
        "Project B retrained: NO",
        "RL allocation executed: NO",
        "Training authorized: NO",
        f"Output: {output_dir}",
    ]) + "\n"
    (output_dir / "FINAL_SUMMARY.txt").write_text(summary, encoding="utf-8")

    hash_rows = []
    for path in sorted(
        p for p in output_dir.rglob("*")
        if p.is_file() and p.name != "file_hashes_sha256.csv"
    ):
        hash_rows.append({
            "relative_path": str(path.relative_to(output_dir)).replace("\\", "/"),
            "size_bytes": path.stat().st_size,
            "sha256": sha256(path),
        })
    write_csv(
        audit_dir / "file_hashes_sha256.csv",
        ["relative_path", "size_bytes", "sha256"],
        hash_rows,
    )
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
    synthetic = []
    start = dt.date(2021, 1, 1)
    for index in range(40):
        synthetic.append({
            "observation_date": (start + dt.timedelta(days=index)).isoformat(),
            "rain_imerg_mm_day": float(index + 1),
        })
    add_derived_features({"TEST": synthetic})
    assert synthetic[1]["rain_imerg_lag1_mm"] == 1.0
    assert synthetic[2]["rain_imerg_lag2_mm"] == 1.0
    assert synthetic[7]["rain_imerg_sum_7d_lag1_mm"] == sum(range(1, 8))
    assert synthetic[8]["rain_imerg_sum_7d_lag2_mm"] == sum(range(1, 8))
    print("SELF_TEST_PASS")
    print("Current IMERG parameter names: IMERG_PRECTOT, IMERG_PRECTOT_COUNT")
    print("No network, project, training, or RL action executed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coordinates", required=True, help="Controlled provisional coordinate CSV")
    parser.add_argument("--output", help="New empty output directory")
    parser.add_argument("--start", default="20210401", help="Start date YYYYMMDD")
    parser.add_argument("--end", help="End date YYYYMMDD")
    parser.add_argument("--force", action="store_true", help="Redownload raw JSON if present")
    parser.add_argument(
        "--allow-end-truncation",
        action="store_true",
        help="End at NASA's latest common non-fill IMERG date when the requested end is unavailable",
    )
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
