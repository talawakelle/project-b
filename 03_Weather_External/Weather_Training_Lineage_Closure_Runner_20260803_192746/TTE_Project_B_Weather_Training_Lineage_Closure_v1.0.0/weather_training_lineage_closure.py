#!/usr/bin/env python3
"""Read-only discovery of Project B cleaned weather artifacts and consumers."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
import tempfile
from pathlib import Path


TARGETS = {
    "tri_weather_daily.csv.gz": {
        "role": "TRI_CLEANED_DAILY",
        "expected_sha256": "d489af0aac8dbdb86ca38c8ea20565024bf7c132998835380732572ca8947d9f",
    },
    "pedro_weather_daily.csv.gz": {
        "role": "PEDRO_CLEANED_DAILY",
        "expected_sha256": "28be35ce5896b2837bf12c35e3db315b8bead666df3fdce263831a8eb9694a58",
    },
    "tri-thalawakele_2026_06.xlsx": {
        "role": "TRI_JUNE_SUPPLEMENT",
        "expected_sha256": "f1ee97a92e55111acf8518fe9cc20dddcb8af5945f5b78c08553da94555c3d45",
    },
    "tri_weather_daily_cache.csv": {
        "role": "TRI_LEGACY_CACHE",
        "expected_sha256": "0680e9b4d3f92520634f724b319c8f4c0fbed231194d9fef0f15398f4a6337e2",
    },
    "pedro_weather_daily_cache.csv": {
        "role": "PEDRO_LEGACY_CACHE",
        "expected_sha256": "df6fbe719bf254018e61a2b6f55bb883a07684d2711a658f6b7618a1f20ceb71",
    },
}

PATTERNS = {
    "tri_weather_daily.csv.gz": "CLEANED_DAILY_REFERENCE",
    "pedro_weather_daily.csv.gz": "CLEANED_DAILY_REFERENCE",
    "tri_weather_daily_cache.csv": "LEGACY_CACHE_REFERENCE",
    "pedro_weather_daily_cache.csv": "LEGACY_CACHE_REFERENCE",
    "tri-thalawakele_2026_06.xlsx": "SUPPLEMENT_REFERENCE",
    "daily_sha256": "HASH_OR_METADATA_REFERENCE",
    "training_prefix_sha256": "HASH_OR_METADATA_REFERENCE",
    "duplicate_timestamps_removed": "CLEANING_RULE_REFERENCE",
    "usable_day": "CLEANING_RULE_REFERENCE",
    "physical_ranges": "CLEANING_RULE_REFERENCE",
    "usable_for_training": "TRAINING_FLAG_REFERENCE",
    "d489af0aac8dbdb86ca38c8ea20565024bf7c132998835380732572ca8947d9f": "TRI_CLEANED_HASH_REFERENCE",
    "28be35ce5896b2837bf12c35e3db315b8bead666df3fdce263831a8eb9694a58": "PEDRO_CLEANED_HASH_REFERENCE",
}

TEXT_EXTENSIONS = {
    ".py", ".ps1", ".json", ".csv", ".txt", ".log", ".yaml", ".yml",
    ".toml", ".md", ".bat", ".cmd", ".ini", ".cfg",
}
EXECUTABLE_EXTENSIONS = {".py", ".ps1", ".bat", ".cmd"}
MAX_TEXT_BYTES = 8 * 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def read_text_file(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def scan_roots(
    search_roots: list[Path], output_root: Path, exclude_roots: list[Path] | None = None
) -> dict:
    artifacts: list[dict] = []
    references: list[dict] = []
    failures: list[dict] = []
    skipped_large: list[dict] = []
    files_seen = 0
    text_files_scanned = 0
    excluded = [output_root.resolve(), *[path.resolve() for path in (exclude_roots or [])]]

    def on_walk_error(error: OSError) -> None:
        failures.append({
            "stage": "directory_walk",
            "path": getattr(error, "filename", ""),
            "error": f"{type(error).__name__}: {error}",
        })

    for root in search_roots:
        for directory, dirnames, filenames in os.walk(root, onerror=on_walk_error):
            directory_path = Path(directory)
            dirnames[:] = [name for name in dirnames if not any(
                inside(directory_path / name, excluded_root)
                for excluded_root in excluded
            )]
            for filename in filenames:
                path = directory_path / filename
                if any(inside(path, excluded_root) for excluded_root in excluded):
                    continue
                files_seen += 1
                name_key = filename.casefold()
                target = TARGETS.get(name_key)
                try:
                    stat = path.stat()
                except (OSError, PermissionError) as error:
                    failures.append({
                        "stage": "stat",
                        "path": str(path),
                        "error": f"{type(error).__name__}: {error}",
                    })
                    continue

                if target:
                    try:
                        actual_hash = sha256_file(path)
                        artifacts.append({
                            "role": target["role"],
                            "file_name": filename,
                            "path": str(path),
                            "size_bytes": stat.st_size,
                            "modified_utc": datetime.fromtimestamp(
                                stat.st_mtime, tz=timezone.utc
                            ).isoformat(),
                            "expected_sha256": target["expected_sha256"],
                            "actual_sha256": actual_hash,
                            "expected_hash_match": actual_hash == target["expected_sha256"],
                        })
                    except (OSError, PermissionError) as error:
                        failures.append({
                            "stage": "hash",
                            "path": str(path),
                            "error": f"{type(error).__name__}: {error}",
                        })

                extension = path.suffix.casefold()
                if extension not in TEXT_EXTENSIONS:
                    continue
                if stat.st_size > MAX_TEXT_BYTES:
                    skipped_large.append({
                        "path": str(path),
                        "size_bytes": stat.st_size,
                        "reason": "TEXT_FILE_ABOVE_8_MIB",
                    })
                    continue
                try:
                    text = read_text_file(path)
                    text_files_scanned += 1
                except (OSError, PermissionError) as error:
                    failures.append({
                        "stage": "content_read",
                        "path": str(path),
                        "error": f"{type(error).__name__}: {error}",
                    })
                    continue
                for line_number, line in enumerate(text.splitlines(), 1):
                    folded = line.casefold()
                    for pattern, category in PATTERNS.items():
                        if pattern.casefold() not in folded:
                            continue
                        references.append({
                            "category": category,
                            "pattern": pattern,
                            "path": str(path),
                            "extension": extension,
                            "line_number": line_number,
                            "consumer_code_candidate": (
                                extension in EXECUTABLE_EXTENSIONS
                                and category in {
                                    "CLEANED_DAILY_REFERENCE",
                                    "LEGACY_CACHE_REFERENCE",
                                    "SUPPLEMENT_REFERENCE",
                                    "CLEANING_RULE_REFERENCE",
                                }
                            ),
                            "line_excerpt": line.strip()[:500],
                        })
    return {
        "artifacts": artifacts,
        "references": references,
        "failures": failures,
        "skipped_large": skipped_large,
        "files_seen": files_seen,
        "text_files_scanned": text_files_scanned,
    }


def build_decision(scan: dict, search_roots: list[Path]) -> dict:
    artifacts = scan["artifacts"]
    references = scan["references"]
    matched_roles = {
        row["role"] for row in artifacts if row["expected_hash_match"]
    }
    cleaned_roles = {"TRI_CLEANED_DAILY", "PEDRO_CLEANED_DAILY"}
    both_cleaned = cleaned_roles.issubset(matched_roles)
    cleaned_consumer_patterns = {
        row["pattern"] for row in references
        if row["consumer_code_candidate"]
        and row["category"] == "CLEANED_DAILY_REFERENCE"
    }
    both_cleaned_consumers = {
        "tri_weather_daily.csv.gz", "pedro_weather_daily.csv.gz"
    }.issubset(cleaned_consumer_patterns)
    supplement_found = "TRI_JUNE_SUPPLEMENT" in matched_roles

    if both_cleaned and both_cleaned_consumers:
        decision = "CLEANED_ARTIFACTS_AND_CONSUMER_CODE_CANDIDATES_FOUND_REPRODUCTION_REQUIRED"
    elif both_cleaned:
        decision = "CLEANED_ARTIFACTS_FOUND_CONSUMER_CODE_NOT_PROVEN"
    else:
        decision = "CLEANED_DAILY_ARTIFACTS_NOT_FULLY_FOUND"

    return {
        "decision": decision,
        "search_roots": [str(path) for path in search_roots],
        "files_seen": scan["files_seen"],
        "text_files_scanned": scan["text_files_scanned"],
        "artifact_candidates": len(artifacts),
        "expected_hash_matches": sum(row["expected_hash_match"] for row in artifacts),
        "content_references": len(references),
        "consumer_code_candidates": sum(
            row["consumer_code_candidate"] for row in references
        ),
        "scan_failures": len(scan["failures"]),
        "large_text_files_skipped": len(scan["skipped_large"]),
        "tri_cleaned_daily_exact_hash_found": "TRI_CLEANED_DAILY" in matched_roles,
        "pedro_cleaned_daily_exact_hash_found": "PEDRO_CLEANED_DAILY" in matched_roles,
        "both_cleaned_consumer_code_candidates_found": both_cleaned_consumers,
        "tri_june_supplement_exact_hash_found": supplement_found,
        "end_to_end_training_weather_lineage_verified": False,
        "weather_training_approved": False,
        "project_b_retraining_authorized": False,
        "rl_allocation_enabled": False,
        "next_gate": "REVIEW_MATCHED_ARTIFACTS_BUILD_CODE_AND_TRAINING_CONSUMPTION_REFERENCES",
    }


def save_outputs(output_root: Path, scan: dict, decision: dict) -> None:
    output_root.mkdir(parents=True, exist_ok=False)
    write_csv(
        output_root / "target_artifact_candidates.csv",
        scan["artifacts"],
        ["role", "file_name", "path", "size_bytes", "modified_utc",
         "expected_sha256", "actual_sha256", "expected_hash_match"],
    )
    write_csv(
        output_root / "content_references.csv",
        scan["references"],
        ["category", "pattern", "path", "extension", "line_number",
         "consumer_code_candidate", "line_excerpt"],
    )
    write_csv(
        output_root / "scan_failures.csv",
        scan["failures"],
        ["stage", "path", "error"],
    )
    write_csv(
        output_root / "large_text_files_skipped.csv",
        scan["skipped_large"],
        ["path", "size_bytes", "reason"],
    )
    (output_root / "lineage_closure_summary.json").write_text(
        json.dumps(decision, indent=2), encoding="utf-8"
    )
    lines = [
        "========== PROJECT B WEATHER TRAINING LINEAGE CLOSURE ==========",
        f"DECISION: {decision['decision']}",
        f"OUTPUT: {output_root}",
        f"FILES SEEN: {decision['files_seen']}",
        f"TARGET ARTIFACT CANDIDATES: {decision['artifact_candidates']}",
        f"EXPECTED HASH MATCHES: {decision['expected_hash_matches']}",
        f"CONTENT REFERENCES: {decision['content_references']}",
        f"CONSUMER CODE CANDIDATES: {decision['consumer_code_candidates']}",
        f"SCAN FAILURES: {decision['scan_failures']}",
        f"TRI CLEANED DAILY EXACT HASH FOUND: {decision['tri_cleaned_daily_exact_hash_found']}",
        f"PEDRO CLEANED DAILY EXACT HASH FOUND: {decision['pedro_cleaned_daily_exact_hash_found']}",
        f"TRI JUNE SUPPLEMENT EXACT HASH FOUND: {decision['tri_june_supplement_exact_hash_found']}",
        "END-TO-END TRAINING WEATHER LINEAGE VERIFIED: False",
        "WEATHER TRAINING APPROVED: False",
        "Project B retraining remains unauthorized.",
        "RL allocation remains disabled.",
        "No source file, model, training state, or RL setting was modified.",
    ]
    (output_root / "FINAL_SUMMARY.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="tte_weather_lineage_test_") as temp:
        root = Path(temp) / "source"
        output = Path(temp) / "output"
        root.mkdir()
        (root / "tri_weather_daily.csv.gz").write_bytes(b"synthetic-cleaned-daily")
        (root / "builder.py").write_text(
            "path = 'tri_weather_daily.csv.gz'\n"
            "legacy = 'tri_weather_daily_cache.csv'\n"
            "rule = 'physical_ranges'\n",
            encoding="utf-8",
        )
        scan = scan_roots([root], output)
        assert any(
            row["role"] == "TRI_CLEANED_DAILY" for row in scan["artifacts"]
        )
        assert any(
            row["pattern"] == "tri_weather_daily.csv.gz"
            and row["consumer_code_candidate"]
            for row in scan["references"]
        )
        assert any(
            row["pattern"] == "physical_ranges" for row in scan["references"]
        )
    print("SELF_TEST_PASS")
    print("Read-only artifact discovery and targeted consumer-reference scan passed")
    print("No project, model, training, network, or RL action executed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--search-root", type=Path, action="append", default=[])
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--exclude-root", type=Path, action="append", default=[])
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return
    if not args.project_root or not args.output_root:
        parser.error("--project-root and --output-root are required")

    roots = []
    for candidate in [args.project_root, *args.search_root]:
        resolved = candidate.resolve()
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    if not roots:
        raise SystemExit("No valid search roots were supplied")
    if args.output_root.exists():
        raise SystemExit(f"Output folder already exists: {args.output_root}")

    scan = scan_roots(roots, args.output_root, args.exclude_root)
    decision = build_decision(scan, roots)
    save_outputs(args.output_root, scan, decision)
    print((args.output_root / "FINAL_SUMMARY.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
