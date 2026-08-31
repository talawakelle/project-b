#!/usr/bin/env python3
"""Read-only Project B weather-source provenance recovery.

The program searches for the raw Pedro/TRI workbooks and other estate weather
records, profiles the two derived station caches, and writes an auditable
candidate manifest.  It never copies a source file, modifies Project B inputs,
trains a model, or allocates RL.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import statistics
import sys
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence


VERSION = "1.0.0"
EXPECTED_SOURCES = {
    "TRI-TW": ["TRI-Thalawakele.xlsx", "TRI-Thalawakele_2026_06.xlsx"],
    "PEDRO-NE": ["Pedro_Estate_Nuwaraeliya(2).xlsx"],
}
CACHE_FILES = {
    "PEDRO-NE": "pedro_weather_daily_cache.csv",
    "TRI-TW": "tri_weather_daily_cache.csv",
}
SUPPORTED_FILES = {
    ".xlsx", ".xlsm", ".xlsb", ".xls", ".csv", ".tsv",
    ".txt", ".json", ".xml", ".zip", ".7z", ".rar",
}
SPREADSHEETS = {".xlsx", ".xlsm", ".xlsb", ".xls"}
TEXT_FILES = {".csv", ".tsv", ".txt", ".json", ".xml"}
ARCHIVES = {".zip", ".7z", ".rar"}
SKIP_DIR_NAMES = {
    ".git", "__pycache__", "node_modules", "$recycle.bin",
    "system volume information",
}
WEATHER_PATTERNS = {
    "rainfall": re.compile(r"\b(rain\s*fall|rain\s*gauge|rainfall_mm|precipitation|pluviometer)\b", re.I),
    "temperature": re.compile(r"\b(air\s*temp(?:erature)?|temperature|temp_mean|temp_min|temp_max)\b", re.I),
    "humidity": re.compile(r"\b(humidity|relative\s*humidity)\b", re.I),
    "wind": re.compile(r"\b(wind\s*speed|gust\s*speed|anemometer)\b", re.I),
    "sunshine": re.compile(r"\b(sunshine|solar\s*radiation|uv_max|light_mean)\b", re.I),
    "soil": re.compile(r"\b(soil\s*moisture|soil\s*temp)\b", re.I),
    "records": re.compile(r"\b(weather_records|meteorological|weather\s*station)\b", re.I),
    "date": re.compile(r"\b(date|timestamp|observation\s*date)\b", re.I),
}
IDENTITY_PATTERNS = {
    "pedro-ne": re.compile(r"pedro(?:[-_ ]?ne|\s+estate|[_ ]estate[_ ]nuwaraeliya)", re.I),
    "tri-tw": re.compile(r"tri[-_ ]?tw|tri[-_ ]?thalawakele|st\.?\s*coombs", re.I),
}
NEGATIVE_PATTERNS = {
    "crop": re.compile(r"\b(green\s*leaf|made\s*tea|crop\s*forecast|harvest(?:ed|ing)?)\b", re.I),
    "weighment": re.compile(r"\b(weigh(?:ing|ment)|net\s*weight|weighing[_ ]id)\b", re.I),
    "hr": re.compile(r"\b(planter\s*trainee|employee|curriculum\s*vitae|interview)\b", re.I),
}


def utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


EXPECTED_BY_NORMALIZED = {
    norm_name(name): station
    for station, names in EXPECTED_SOURCES.items()
    for name in names
}


def safe_rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return str(path)


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_path(path: Path, project_root: Path) -> str:
    text = str(path).replace("/", "\\").casefold()
    project_text = str(project_root).replace("/", "\\").casefold()
    if "\\outputs\\" in text or "\\model" in text or "\\05_neural" in text:
        return "DERIVED_OUTPUT"
    if "\\02_processed_data\\" in text:
        return "PROCESSED_OR_AUDIT"
    if "\\03_weather_external\\" in text:
        return "EXTERNAL_REFERENCE"
    if "\\config\\" in text:
        return "CONFIG_OR_LINEAGE_CLUE"
    if "\\01_source_data_pack\\" in text:
        return "SOURCE_DATA_PACK"
    if text.startswith(project_text):
        return "PROJECT_OTHER"
    if "\\downloads\\" in text:
        return "USER_DOWNLOADS"
    if "\\desktop\\" in text:
        return "USER_DESKTOP"
    if "\\documents\\" in text:
        return "USER_DOCUMENTS"
    return "ADDITIONAL_ROOT"


def iter_files(root: Path, output_root: Path | None = None) -> Iterator[Path]:
    output_resolved = None
    if output_root:
        try:
            output_resolved = output_root.resolve()
        except Exception:
            output_resolved = output_root
    for current, dirs, files in os.walk(root, topdown=True, onerror=lambda _e: None):
        current_path = Path(current)
        kept = []
        for directory in dirs:
            if directory.casefold() in SKIP_DIR_NAMES:
                continue
            candidate = current_path / directory
            if output_resolved is not None:
                try:
                    if candidate.resolve() == output_resolved:
                        continue
                except Exception:
                    pass
            kept.append(directory)
        dirs[:] = kept
        for name in files:
            path = current_path / name
            if path.suffix.casefold() in SUPPORTED_FILES:
                yield path


def read_text_prefix(path: Path, limit: int = 2 * 1024 * 1024) -> str:
    with path.open("rb") as handle:
        data = handle.read(limit)
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def inspect_openxml(path: Path) -> tuple[str, list[str], str | None]:
    """Return searchable text, sheet names, and an error for xlsx/xlsm."""
    fragments: list[str] = []
    sheet_names: list[str] = []
    total_uncompressed = 0
    per_entry_limit = 12 * 1024 * 1024
    total_limit = 32 * 1024 * 1024
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            workbook_names = [n for n in names if n.casefold() == "xl/workbook.xml"]
            shared_names = [n for n in names if n.casefold() == "xl/sharedstrings.xml"]
            worksheet_names = [n for n in names if n.casefold().startswith("xl/worksheets/sheet") and n.casefold().endswith(".xml")]
            selected = workbook_names + shared_names + worksheet_names[:12]
            for name in selected:
                info = archive.getinfo(name)
                if info.file_size > per_entry_limit or total_uncompressed + info.file_size > total_limit:
                    continue
                raw = archive.read(name)
                total_uncompressed += len(raw)
                text = raw.decode("utf-8", errors="replace")
                fragments.append(text)
                if name.casefold() == "xl/workbook.xml":
                    sheet_names.extend(re.findall(r'<sheet[^>]+name="([^"]+)"', text, re.I))
        combined = " ".join(fragments)
        combined = re.sub(r"<[^>]+>", " ", combined)
        return combined[:40_000_000], sheet_names[:100], None
    except Exception as exc:
        return "", [], f"{type(exc).__name__}: {exc}"


def inspect_binary_workbook(path: Path) -> tuple[str, list[str], str | None]:
    try:
        return read_text_prefix(path, 4 * 1024 * 1024), [], None
    except Exception as exc:
        return "", [], f"{type(exc).__name__}: {exc}"


def matched_terms(text: str, patterns: dict[str, re.Pattern[str]]) -> list[str]:
    return [name for name, pattern in patterns.items() if pattern.search(text)]


def score_name(name: str) -> tuple[int, list[str], list[str]]:
    normalized = norm_name(name)
    identities = matched_terms(name, IDENTITY_PATTERNS)
    terms = matched_terms(name, WEATHER_PATTERNS)
    score = 0
    if normalized in EXPECTED_BY_NORMALIZED:
        score += 120
    score += 35 * len(identities)
    score += 12 * len(terms)
    if re.search(r"rain|weather|climat|meteo|gauge|pluvio", name, re.I):
        score += 20
    return score, identities, terms


def score_content(text: str) -> tuple[int, list[str], list[str], list[str]]:
    identities = matched_terms(text, IDENTITY_PATTERNS)
    weather = matched_terms(text, WEATHER_PATTERNS)
    negative = matched_terms(text, NEGATIVE_PATTERNS)
    score = 30 * len(identities) + 12 * len(weather) - 18 * len(negative)
    if "rainfall" in weather and "date" in weather:
        score += 25
    if len(weather) >= 4:
        score += 30
    return score, identities, weather, negative


def expected_station_for_name(name: str) -> str:
    return EXPECTED_BY_NORMALIZED.get(norm_name(name), "")


@dataclass
class Candidate:
    root: str
    full_path: str
    relative_path: str
    path_class: str
    extension: str
    size_bytes: int
    modified_utc: str
    exact_expected_source: bool
    expected_station: str
    name_score: int
    content_score: int
    total_score: int
    identity_terms: str
    weather_terms: str
    negative_terms: str
    sheet_names: str
    sha256: str
    hash_status: str
    classification: str
    inspection_status: str


@dataclass
class ArchiveMember:
    archive_path: str
    archive_path_class: str
    archive_sha256: str
    member_name: str
    member_size_bytes: int
    exact_expected_source: bool
    expected_station: str
    name_score: int
    classification: str


def candidate_classification(
    *, exact: bool, extension: str, total_score: int, weather_terms: Sequence[str],
    negative_terms: Sequence[str], path_class: str
) -> str:
    if exact:
        if path_class in {"DERIVED_OUTPUT", "PROCESSED_OR_AUDIT", "EXTERNAL_REFERENCE"}:
            return "EXACT_NAME_DERIVED_OR_COPIED_LOCATION_REVIEW_REQUIRED"
        return "EXPECTED_STATION_WORKBOOK_CANDIDATE"
    if extension in {".xlsx", ".xlsm", ".xlsb", ".xls"}:
        if "rainfall" in weather_terms and "date" in weather_terms and len(weather_terms) >= 3:
            if set(negative_terms) >= {"crop", "weighment"} and len(weather_terms) < 5:
                return "LIKELY_CROP_OR_WEIGHMENT_FALSE_POSITIVE"
            return "WEATHER_OR_RAIN_GAUGE_WORKBOOK_CANDIDATE"
        if total_score >= 55:
            return "WORKBOOK_NAME_OR_CONTENT_REVIEW_REQUIRED"
    if extension in TEXT_FILES and total_score >= 55:
        return "TEXT_LINEAGE_OR_WEATHER_CANDIDATE"
    return "LOW_CONFIDENCE_OR_FALSE_POSITIVE"


def inspect_archives(
    path: Path, path_class: str, scan_failures: list[dict[str, str]]
) -> list[ArchiveMember]:
    results: list[ArchiveMember] = []
    if path.suffix.casefold() != ".zip":
        name_score, _, _ = score_name(path.name)
        if name_score >= 20:
            results.append(ArchiveMember(
                archive_path=str(path), archive_path_class=path_class,
                archive_sha256="", member_name="<OPAQUE_ARCHIVE_NOT_INSPECTED>",
                member_size_bytes=0, exact_expected_source=False,
                expected_station="", name_score=name_score,
                classification="OPAQUE_ARCHIVE_NAME_CANDIDATE",
            ))
        return results
    interesting: list[tuple[zipfile.ZipInfo, int, bool, str]] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for index, info in enumerate(archive.infolist()):
                if index >= 250_000:
                    scan_failures.append({
                        "path": str(path), "stage": "archive_members",
                        "error": "Archive member inspection capped at 250000 entries",
                    })
                    break
                basename = Path(info.filename).name
                score, _, _ = score_name(basename)
                exact_station = expected_station_for_name(basename)
                exact = bool(exact_station)
                if exact or score >= 20:
                    interesting.append((info, score, exact, exact_station))
        if not interesting:
            return results
        archive_hash = ""
        hash_status = "SKIPPED_ARCHIVE_OVER_1_GIB"
        try:
            if path.stat().st_size <= 1024 * 1024 * 1024:
                archive_hash = sha256_file(path)
                hash_status = "HASHED"
        except Exception as exc:
            hash_status = f"HASH_FAILED:{type(exc).__name__}"
        for info, score, exact, station in interesting:
            results.append(ArchiveMember(
                archive_path=str(path), archive_path_class=path_class,
                archive_sha256=archive_hash or hash_status,
                member_name=info.filename, member_size_bytes=info.file_size,
                exact_expected_source=exact, expected_station=station,
                name_score=score,
                classification=("EXPECTED_SOURCE_ARCHIVE_MEMBER" if exact
                                else "WEATHER_NAMED_ARCHIVE_MEMBER_REVIEW_REQUIRED"),
            ))
    except Exception as exc:
        scan_failures.append({
            "path": str(path), "stage": "archive_open",
            "error": f"{type(exc).__name__}: {exc}",
        })
    return results


def should_inspect_content(path: Path, name_score: int) -> bool:
    suffix = path.suffix.casefold()
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if suffix in {".xlsx", ".xlsm"}:
        return size <= 128 * 1024 * 1024
    if suffix in {".xls", ".xlsb"}:
        return size <= 128 * 1024 * 1024
    if suffix in TEXT_FILES:
        return name_score >= 20 or size <= 8 * 1024 * 1024
    return False


def inspect_candidate(
    path: Path, root: Path, project_root: Path, scan_failures: list[dict[str, str]]
) -> Candidate | None:
    try:
        stat = path.stat()
    except Exception as exc:
        scan_failures.append({"path": str(path), "stage": "stat", "error": str(exc)})
        return None
    suffix = path.suffix.casefold()
    path_class = classify_path(path, project_root)
    name_score, name_identities, name_weather = score_name(path.name)
    station = expected_station_for_name(path.name)
    exact = bool(station)
    content_text = ""
    sheet_names: list[str] = []
    inspection_status = "NAME_ONLY"
    if should_inspect_content(path, name_score):
        if suffix in {".xlsx", ".xlsm"}:
            content_text, sheet_names, error = inspect_openxml(path)
        elif suffix in {".xls", ".xlsb"}:
            content_text, sheet_names, error = inspect_binary_workbook(path)
        else:
            try:
                content_text = read_text_prefix(path)
                error = None
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        if error:
            inspection_status = f"CONTENT_INSPECTION_FAILED:{error[:180]}"
            scan_failures.append({"path": str(path), "stage": "content", "error": error})
        else:
            inspection_status = "CONTENT_INSPECTED"
    content_score, content_identities, content_weather, negative = score_content(content_text)
    identities = sorted(set(name_identities + content_identities))
    weather = sorted(set(name_weather + content_weather))
    total = name_score + content_score
    # Retain all exact sources and high-signal candidates. Derived cache/config
    # files are handled separately and are not allowed to crowd this manifest.
    retain = exact or total >= 35 or (
        suffix in SPREADSHEETS and "rainfall" in weather and "date" in weather
    )
    if not retain:
        return None
    digest = ""
    hash_status = "NOT_REQUIRED_LOW_CONFIDENCE"
    if exact or total >= 70:
        if stat.st_size <= 2 * 1024 * 1024 * 1024:
            try:
                digest = sha256_file(path)
                hash_status = "HASHED"
            except Exception as exc:
                hash_status = f"HASH_FAILED:{type(exc).__name__}"
        else:
            hash_status = "SKIPPED_FILE_OVER_2_GIB"
    classification = candidate_classification(
        exact=exact, extension=suffix, total_score=total,
        weather_terms=weather, negative_terms=negative, path_class=path_class,
    )
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat()
    return Candidate(
        root=str(root), full_path=str(path), relative_path=safe_rel(path, root),
        path_class=path_class, extension=suffix, size_bytes=stat.st_size,
        modified_utc=modified, exact_expected_source=exact,
        expected_station=station, name_score=name_score,
        content_score=content_score, total_score=total,
        identity_terms="|".join(identities), weather_terms="|".join(weather),
        negative_terms="|".join(sorted(negative)),
        sheet_names="|".join(sheet_names), sha256=digest,
        hash_status=hash_status, classification=classification,
        inspection_status=inspection_status,
    )


def dedupe_roots(roots: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            if not root.exists() or not root.is_dir():
                continue
            key = os.path.normcase(os.path.abspath(str(root)))
        except Exception:
            continue
        if key in seen:
            continue
        # If an existing root already contains this one, the nested root does
        # not need a second scan.
        if any(key == existing or key.startswith(existing.rstrip("\\/") + os.sep)
               for existing in seen):
            continue
        seen.add(key)
        result.append(root)
    return result


def find_named_file(root: Path, basename: str) -> Path | None:
    target = norm_name(basename)
    for path in iter_files(root):
        if norm_name(path.name) == target:
            return path
    return None


def find_station_base(project_root: Path) -> Path | None:
    preferred = (
        project_root / "01_Source_Data_Pack" /
        "TTE_Project_B_Advanced_Source_Data_Pack" / "03_Crop_and_Field_Data"
    )
    if preferred.exists():
        return preferred
    for current, dirs, files in os.walk(project_root, topdown=True, onerror=lambda _e: None):
        dirs[:] = [d for d in dirs if d.casefold() not in SKIP_DIR_NAMES]
        if "weather_station_master.csv" in files:
            config = Path(current) / "weather_station_master.csv"
            if config.parent.name.casefold() == "config":
                return config.parent.parent
    return None


def profile_csv_cache(path: Path, station_code: str) -> dict[str, object]:
    result: dict[str, object] = {
        "station_code": station_code,
        "cache_path": str(path),
        "exists": path.exists(),
        "sha256": "",
        "rows": 0,
        "date_min": "",
        "date_max": "",
        "duplicate_dates": 0,
        "median_weather_records": "",
        "rainfall_invalid_rows": 0,
        "required_columns_present": False,
        "profile_status": "MISSING",
    }
    if not path.exists():
        return result
    try:
        result["sha256"] = sha256_file(path)
        dates: list[str] = []
        records: list[float] = []
        invalid_rain = 0
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or [])
            required = {"date", "weather_records", "rainfall_mm", "air_temp_mean", "humidity_mean"}
            result["required_columns_present"] = required.issubset(fields)
            for row in reader:
                date = (row.get("date") or "").strip()
                if date:
                    dates.append(date)
                try:
                    records.append(float(row.get("weather_records") or "nan"))
                except ValueError:
                    pass
                try:
                    rain = float(row.get("rainfall_mm") or "nan")
                    if not (-0.001 <= rain < 1000):
                        invalid_rain += 1
                except ValueError:
                    invalid_rain += 1
        result["rows"] = len(dates)
        result["date_min"] = min(dates) if dates else ""
        result["date_max"] = max(dates) if dates else ""
        result["duplicate_dates"] = len(dates) - len(set(dates))
        result["median_weather_records"] = statistics.median(records) if records else ""
        result["rainfall_invalid_rows"] = invalid_rain
        result["profile_status"] = "PROFILED"
    except Exception as exc:
        result["profile_status"] = f"FAILED:{type(exc).__name__}:{str(exc)[:150]}"
    return result


def read_station_config(station_base: Path | None) -> tuple[list[dict[str, str]], Path | None]:
    if station_base is None:
        return [], None
    path = station_base / "config" / "weather_station_master.csv"
    if not path.exists():
        return [], path
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    return rows, path


def locate_caches(station_base: Path | None) -> dict[str, Path]:
    if station_base is None:
        return {station: Path("<NOT_LOCATED>") / name for station, name in CACHE_FILES.items()}
    preferred = station_base / "outputs" / "gpu_candidates_20260722_181042"
    found: dict[str, Path] = {}
    for station, name in CACHE_FILES.items():
        path = preferred / name
        if path.exists():
            found[station] = path
            continue
        alternate = find_named_file(station_base, name)
        found[station] = alternate or path
    return found


def write_csv(path: Path, rows: Sequence[dict[str, object]], fields: Sequence[str] | None = None) -> None:
    if fields is None:
        fields = list(rows[0].keys()) if rows else ["status"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def scan_and_write(project_root: Path, search_roots: Sequence[Path], output_root: Path) -> dict[str, object]:
    output_root.mkdir(parents=True, exist_ok=False)
    roots = dedupe_roots(search_roots)
    scan_failures: list[dict[str, str]] = []
    candidates: list[Candidate] = []
    archive_members: list[ArchiveMember] = []
    scanned_files = 0
    supported_files = 0
    for root in roots:
        for path in iter_files(root, output_root):
            scanned_files += 1
            supported_files += 1
            path_class = classify_path(path, project_root)
            if path.suffix.casefold() in ARCHIVES:
                archive_members.extend(inspect_archives(path, path_class, scan_failures))
            candidate = inspect_candidate(path, root, project_root, scan_failures)
            if candidate is not None:
                candidates.append(candidate)

    candidates.sort(key=lambda row: (-row.total_score, row.full_path.casefold()))
    archive_members.sort(key=lambda row: (not row.exact_expected_source, -row.name_score, row.archive_path.casefold()))

    station_base = find_station_base(project_root)
    config_rows, config_path = read_station_config(station_base)
    cache_paths = locate_caches(station_base)
    cache_profiles = [profile_csv_cache(path, station) for station, path in cache_paths.items()]

    expected_rows: list[dict[str, object]] = []
    for station, names in EXPECTED_SOURCES.items():
        for name in names:
            physical = [c for c in candidates if c.exact_expected_source and c.expected_station == station and norm_name(Path(c.full_path).name) == norm_name(name)]
            archived = [m for m in archive_members if m.exact_expected_source and m.expected_station == station and norm_name(Path(m.member_name).name) == norm_name(name)]
            expected_rows.append({
                "station_code": station,
                "expected_filename": name,
                "physical_matches": len(physical),
                "archive_member_matches": len(archived),
                "physical_paths": " | ".join(c.full_path for c in physical),
                "archive_members": " | ".join(f"{m.archive_path}::{m.member_name}" for m in archived),
                "status": ("PHYSICAL_CANDIDATE_FOUND" if physical else
                           "ARCHIVE_MEMBER_FOUND" if archived else "NOT_FOUND"),
            })

    exact_physical = sum(int(row["physical_matches"]) for row in expected_rows)
    exact_archive = sum(int(row["archive_member_matches"]) for row in expected_rows)
    convincing_other = sum(
        1 for c in candidates
        if c.classification == "WEATHER_OR_RAIN_GAUGE_WORKBOOK_CANDIDATE"
    )
    if exact_physical or exact_archive:
        decision = "RAW_STATION_WORKBOOK_CANDIDATES_FOUND_REPRODUCTION_REQUIRED"
    elif convincing_other:
        decision = "NON_EXACT_WEATHER_CANDIDATES_FOUND_MANUAL_REVIEW_REQUIRED"
    else:
        decision = "RAW_STATION_WORKBOOK_CANDIDATES_NOT_FOUND_OWNER_EXPORT_REQUIRED"

    config_claims_training_usable = any(
        str(row.get("usable_for_training", "")).strip().casefold() in {"true", "1", "yes"}
        for row in config_rows
    )
    contradictions: list[dict[str, str]] = []
    if config_claims_training_usable:
        contradictions.append({
            "severity": "HIGH",
            "finding": "Station config marks source usable_for_training=TRUE, but raw-workbook lineage and cache reproduction are not verified.",
            "required_action": "Do not use the config flag as authorization. Locate raw workbooks and reproduce each cache byte-for-byte or value-for-value under a documented transformation.",
        })
    if not config_rows:
        contradictions.append({
            "severity": "HIGH",
            "finding": "weather_station_master.csv was not located or read.",
            "required_action": "Recover the station master before any weather feature approval.",
        })

    candidate_rows = [asdict(row) for row in candidates]
    archive_rows = [asdict(row) for row in archive_members]
    write_csv(output_root / "candidate_files.csv", candidate_rows, list(Candidate.__dataclass_fields__))
    write_csv(output_root / "archive_member_candidates.csv", archive_rows, list(ArchiveMember.__dataclass_fields__))
    write_csv(output_root / "expected_source_search.csv", expected_rows)
    write_csv(output_root / "station_cache_profiles.csv", cache_profiles)
    write_csv(output_root / "station_config_rows.csv", config_rows, list(config_rows[0].keys()) if config_rows else ["status"])
    write_csv(output_root / "governance_contradictions.csv", contradictions, ["severity", "finding", "required_action"])
    write_csv(output_root / "scan_failures.csv", scan_failures, ["path", "stage", "error"])

    summary: dict[str, object] = {
        "status": decision,
        "version": VERSION,
        "created_utc": utc_now_text(),
        "project_root": str(project_root),
        "search_roots": [str(root) for root in roots],
        "station_base": str(station_base) if station_base else None,
        "station_config_path": str(config_path) if config_path else None,
        "scan": {
            "supported_files_seen": supported_files,
            "candidate_files_retained": len(candidates),
            "archive_member_candidates": len(archive_members),
            "scan_failures": len(scan_failures),
        },
        "provenance": {
            "expected_source_files": sum(len(v) for v in EXPECTED_SOURCES.values()),
            "exact_physical_matches": exact_physical,
            "exact_archive_member_matches": exact_archive,
            "other_weather_or_rain_gauge_candidates": convincing_other,
            "raw_provenance_verified": False,
            "cache_reproduction_completed": False,
        },
        "governance": {
            "config_claims_training_usable": config_claims_training_usable,
            "contradictions": len(contradictions),
        },
        "controls": {
            "source_files_modified": False,
            "source_files_copied": False,
            "nasa_download_executed": False,
            "project_b_retrained": False,
            "training_authorized": False,
            "rl_allocation_executed": False,
        },
        "next_gate": (
            "Review candidates, identify authoritative owners, then run an isolated cache-reproduction audit."
            if exact_physical or exact_archive
            else "Request authoritative Pedro/TRI exports and estate rain-gauge records from their data owners."
        ),
    }
    with (output_root / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    final_lines = [
        "========== PROJECT B WEATHER SOURCE PROVENANCE RECOVERY ==========",
        f"Decision: {decision}",
        f"Search roots: {len(roots)}",
        f"Supported files seen: {supported_files}",
        f"Candidate files retained: {len(candidates)}",
        f"Archive member candidates: {len(archive_members)}",
        f"Exact physical expected-source matches: {exact_physical}",
        f"Exact expected-source archive members: {exact_archive}",
        f"Other weather/rain-gauge workbook candidates: {convincing_other}",
        f"Scan failures: {len(scan_failures)}",
        "Raw provenance verified: False",
        "Cache reproduction completed: False",
        f"Config claims usable_for_training: {config_claims_training_usable}",
        "Project B retraining authorized: False",
        "RL allocation executed: False",
        "No source files were copied or modified.",
        f"Output: {output_root}",
    ]
    (output_root / "FINAL_SUMMARY.txt").write_text("\n".join(final_lines) + "\n", encoding="utf-8")
    return summary


def create_mock_xlsx(path: Path, text: str, sheet_name: str = "Weather") -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", f'<workbook><sheets><sheet name="{sheet_name}"/></sheets></workbook>')
        archive.writestr("xl/sharedStrings.xml", f"<sst><si><t>{text}</t></si></sst>")
        archive.writestr("xl/worksheets/sheet1.xml", f"<worksheet><sheetData>{text}</sheetData></worksheet>")


def self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="project_b_weather_provenance_") as temp_text:
        temp = Path(temp_text)
        project = temp / "PROJECT B"
        base = project / "01_Source_Data_Pack" / "TTE_Project_B_Advanced_Source_Data_Pack" / "03_Crop_and_Field_Data"
        config = base / "config"
        cache = base / "outputs" / "gpu_candidates_20260722_181042"
        raw = base / "04_Weather_Data"
        config.mkdir(parents=True)
        cache.mkdir(parents=True)
        raw.mkdir(parents=True)
        (config / "weather_station_master.csv").write_text(
            "station_code,station_name,latitude,longitude,weather_file,file_status,usable_for_training,notes\n"
            "TRI-TW,TRI Thalawakele,6.9,80.7,TRI-Thalawakele.xlsx,valid,TRUE,test\n"
            "PEDRO-NE,Pedro Estate,6.9,80.8,Pedro_Estate_Nuwaraeliya(2).xlsx,valid,TRUE,test\n",
            encoding="utf-8",
        )
        cache_header = "date,weather_records,air_temp_mean,humidity_mean,rainfall_mm\n"
        cache_rows = "2026-01-01,80,18.2,91,2.5\n2026-01-02,81,18.0,90,0\n"
        (cache / "pedro_weather_daily_cache.csv").write_text(cache_header + cache_rows, encoding="utf-8")
        (cache / "tri_weather_daily_cache.csv").write_text(cache_header + cache_rows, encoding="utf-8")
        create_mock_xlsx(
            raw / "Pedro_Estate_Nuwaraeliya(2).xlsx",
            "date rainfall air temperature humidity wind speed sunshine soil moisture Pedro Estate",
        )
        create_mock_xlsx(
            raw / "estate_daily_register.xlsx",
            "date rainfall air temperature humidity wind speed sunshine St. Coombs",
        )
        archive_path = project / "source_backup.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("raw/TRI-Thalawakele_2026_06.xlsx", b"mock")
        output = project / "02_Processed_Data" / "test_output"
        summary = scan_and_write(project, [project], output)
        expected = list(csv.DictReader((output / "expected_source_search.csv").open(encoding="utf-8-sig")))
        assert summary["status"] == "RAW_STATION_WORKBOOK_CANDIDATES_FOUND_REPRODUCTION_REQUIRED"
        assert summary["controls"]["training_authorized"] is False
        assert summary["controls"]["rl_allocation_executed"] is False
        assert any(row["expected_filename"] == "Pedro_Estate_Nuwaraeliya(2).xlsx" and int(row["physical_matches"]) == 1 for row in expected)
        assert any(row["expected_filename"] == "TRI-Thalawakele_2026_06.xlsx" and int(row["archive_member_matches"]) == 1 for row in expected)
        profiles = list(csv.DictReader((output / "station_cache_profiles.csv").open(encoding="utf-8-sig")))
        assert len(profiles) == 2 and all(row["profile_status"] == "PROFILED" for row in profiles)
        assert (output / "governance_contradictions.csv").exists()
    print("SELF_TEST_PASS")
    print("Read-only discovery, archive inspection, cache profiling, and governance gates passed")
    print("No network, source-copy, project-training, or RL action executed")
    return 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--search-root", action="append", default=[], type=Path)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.self_test:
        return self_test()
    if args.project_root is None or args.output_root is None:
        raise SystemExit("--project-root and --output-root are required")
    if not args.project_root.exists():
        raise SystemExit(f"Project root does not exist: {args.project_root}")
    search_roots = args.search_root or [args.project_root]
    summary = scan_and_write(args.project_root, search_roots, args.output_root)
    print("========== PROJECT B WEATHER SOURCE PROVENANCE RECOVERY ==========")
    print(f"DECISION: {summary['status']}")
    print(f"OUTPUT: {args.output_root}")
    print(f"EXPECTED PHYSICAL MATCHES: {summary['provenance']['exact_physical_matches']}")
    print(f"EXPECTED ARCHIVE MEMBER MATCHES: {summary['provenance']['exact_archive_member_matches']}")
    print(f"OTHER WEATHER/RAIN-GAUGE CANDIDATES: {summary['provenance']['other_weather_or_rain_gauge_candidates']}")
    print(f"SCAN FAILURES: {summary['scan']['scan_failures']}")
    print("RAW PROVENANCE VERIFIED: False")
    print("CACHE REPRODUCTION COMPLETED: False")
    print("No source files were copied or modified.")
    print("Project B retraining remains unauthorized.")
    print("RL allocation remains disabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
