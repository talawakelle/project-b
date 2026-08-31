from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd


def finite(value):
    return None if pd.isna(value) or not np.isfinite(value) else float(value)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def base_metrics(observed: pd.Series, modeled: pd.Series) -> dict:
    pair = pd.concat([observed, modeled], axis=1).dropna()
    pair.columns = ["station", "imerg"]
    if pair.empty:
        return {"n": 0}
    error = pair["imerg"] - pair["station"]
    return {
        "n": int(len(pair)),
        "station_mean": finite(pair["station"].mean()),
        "imerg_mean": finite(pair["imerg"].mean()),
        "bias_imerg_minus_station": finite(error.mean()),
        "mae": finite(error.abs().mean()),
        "rmse": finite(np.sqrt(np.mean(np.square(error)))),
        "pearson_r": finite(pair["station"].corr(pair["imerg"], method="pearson")),
        "spearman_r": finite(pair["station"].corr(pair["imerg"], method="spearman")),
    }


def event_metrics(observed: pd.Series, modeled: pd.Series, threshold: float = 1.0) -> dict:
    pair = pd.concat([observed, modeled], axis=1).dropna()
    pair.columns = ["station", "imerg"]
    station_wet = pair["station"] >= threshold
    imerg_wet = pair["imerg"] >= threshold
    hits = int((station_wet & imerg_wet).sum())
    false_alarms = int((~station_wet & imerg_wet).sum())
    misses = int((station_wet & ~imerg_wet).sum())
    correct_dry = int((~station_wet & ~imerg_wet).sum())
    return {
        "threshold_mm": threshold,
        "hits": hits,
        "false_alarms": false_alarms,
        "misses": misses,
        "correct_dry": correct_dry,
        "probability_of_detection": finite(hits / (hits + misses)) if hits + misses else None,
        "false_alarm_ratio": finite(false_alarms / (hits + false_alarms)) if hits + false_alarms else None,
        "critical_success_index": finite(hits / (hits + false_alarms + misses)) if hits + false_alarms + misses else None,
        "wet_dry_accuracy": finite((hits + correct_dry) / len(pair)) if len(pair) else None,
    }


def monthly_metrics(joined: pd.DataFrame, valid: pd.Series) -> dict:
    work = joined.loc[valid, ["date", "rainfall_mm", "rain_imerg_mm_day"]].copy()
    if work.empty:
        return {"n_months": 0}
    work["month"] = work["date"].dt.to_period("M")
    monthly = work.groupby("month").agg(
        station=("rainfall_mm", "sum"),
        imerg=("rain_imerg_mm_day", "sum"),
        valid_days=("date", "size"),
    )
    monthly["calendar_days"] = monthly.index.days_in_month
    monthly["valid_fraction"] = monthly["valid_days"] / monthly["calendar_days"]
    monthly = monthly.loc[monthly["valid_fraction"] >= 0.90]
    metrics = base_metrics(monthly["station"], monthly["imerg"])
    metrics["n_months"] = metrics.pop("n", 0)
    metrics["coverage_gate"] = 0.90
    return metrics


def nearest_grid(xwalk: pd.DataFrame, latitude: float, longitude: float) -> dict:
    grids = (
        xwalk.loc[xwalk["parameter"].eq("IMERG_PRECTOT"), [
            "source_grid_latitude", "source_grid_longitude", "estate_key"
        ]]
        .drop_duplicates()
        .copy()
    )
    if grids.empty:
        raise RuntimeError("No IMERG_PRECTOT grid found")
    grids["distance_km"] = grids.apply(
        lambda row: haversine_km(
            latitude,
            longitude,
            float(row["source_grid_latitude"]),
            float(row["source_grid_longitude"]),
        ),
        axis=1,
    )
    chosen = grids.sort_values(["distance_km", "estate_key"]).iloc[0]
    return {
        "parameter": "IMERG_PRECTOT",
        "grid_latitude": float(chosen["source_grid_latitude"]),
        "grid_longitude": float(chosen["source_grid_longitude"]),
        "representative_estate_key": str(chosen["estate_key"]),
        "distance_km": float(chosen["distance_km"]),
    }


def main() -> None:
    imerg_root = Path(os.environ["TTE_IMERG_ROOT"])
    source_base = Path(os.environ["TTE_WEATHER_BASE"])
    out_root = Path(os.environ["TTE_COMPARE_OUT"])
    out_root.mkdir(parents=True, exist_ok=False)

    daily_files = list((imerg_root / "02_processed").glob("nasa_power_imerg_37_estate_daily_*.csv"))
    if len(daily_files) != 1:
        raise RuntimeError(f"Expected one IMERG daily CSV, found {len(daily_files)}")
    imerg_path = daily_files[0]
    xwalk_path = imerg_root / "03_audit" / "estate_parameter_grid_crosswalk.csv"
    master_path = source_base / "config" / "weather_station_master.csv"
    caches = {
        "PEDRO-NE": source_base / "outputs" / "gpu_candidates_20260722_181042" / "pedro_weather_daily_cache.csv",
        "TRI-TW": source_base / "outputs" / "gpu_candidates_20260722_181042" / "tri_weather_daily_cache.csv",
    }
    required = [imerg_path, xwalk_path, master_path, *caches.values()]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing required files:\n" + "\n".join(missing))

    imerg = pd.read_csv(imerg_path)
    imerg["observation_date"] = pd.to_datetime(imerg["observation_date"], errors="raise")
    xwalk = pd.read_csv(xwalk_path)
    master = pd.read_csv(master_path)

    all_details = []
    metrics_rows = []
    grid_rows = []
    exclusion_rows = []

    for station_code, cache_path in caches.items():
        station_meta = master.loc[master["station_code"].eq(station_code)]
        if len(station_meta) != 1:
            raise RuntimeError(f"Expected one station row for {station_code}; found {len(station_meta)}")
        station_meta = station_meta.iloc[0]
        grid = nearest_grid(xwalk, float(station_meta["latitude"]), float(station_meta["longitude"]))
        grid_rows.append({"station_code": station_code, **grid})

        station = pd.read_csv(cache_path)
        station["date"] = pd.to_datetime(station["date"], errors="coerce")
        if station["date"].isna().any() or station["date"].duplicated().any():
            raise RuntimeError(f"Invalid or duplicate station dates in {cache_path}")
        station[["weather_records", "rainfall_mm"]] = station[[
            "weather_records", "rainfall_mm"
        ]].apply(pd.to_numeric, errors="coerce")
        median_records = float(station.loc[station["weather_records"] > 0, "weather_records"].median())

        representative = imerg.loc[
            imerg["estate_key"].eq(grid["representative_estate_key"]),
            [
                "observation_date", "rain_imerg_mm_day",
                "imerg_wet_half_hour_retrieval_count", "imerg_run_maturity",
                "imerg_training_eligible_by_age",
            ],
        ].rename(columns={"observation_date": "date"})
        joined = station.merge(representative, on="date", how="inner", validate="one_to_one")
        records_ok = joined["weather_records"] >= median_records * 0.50
        rain_valid = records_ok & joined["rainfall_mm"].between(0, 500, inclusive="both")
        mature = joined["imerg_training_eligible_by_age"].eq("YES")
        mature_valid = rain_valid & mature
        joined.insert(0, "station_code", station_code)
        joined["records_quality_pass"] = records_ok
        joined["station_rain_quality_pass"] = rain_valid
        joined["imerg_age_maturity_pass"] = mature
        all_details.append(joined)

        exclusion_rows.append({
            "station_code": station_code,
            "cache_rows": int(len(station)),
            "imerg_overlap_rows": int(len(joined)),
            "median_records_per_day": median_records,
            "low_record_rows": int((~records_ok).sum()),
            "station_rain_invalid_or_low_record_rows": int((~rain_valid).sum()),
            "recent_imerg_rows_excluded_from_mature_metrics": int((~mature).sum()),
            "raw_workbook_provenance_found": False,
        })

        comparisons = {
            "daily_rainfall_all_available": base_metrics(
                joined.loc[rain_valid, "rainfall_mm"],
                joined.loc[rain_valid, "rain_imerg_mm_day"],
            ),
            "monthly_matched_day_rainfall_all_available": monthly_metrics(joined, rain_valid),
            "rain_event_1mm_all_available": event_metrics(
                joined.loc[rain_valid, "rainfall_mm"],
                joined.loc[rain_valid, "rain_imerg_mm_day"],
            ),
            "daily_rainfall_mature_by_age": base_metrics(
                joined.loc[mature_valid, "rainfall_mm"],
                joined.loc[mature_valid, "rain_imerg_mm_day"],
            ),
            "monthly_matched_day_rainfall_mature_by_age": monthly_metrics(joined, mature_valid),
            "rain_event_1mm_mature_by_age": event_metrics(
                joined.loc[mature_valid, "rainfall_mm"],
                joined.loc[mature_valid, "rain_imerg_mm_day"],
            ),
        }
        for metric, values in comparisons.items():
            metrics_rows.append({"station_code": station_code, "metric": metric, **values})

    details = pd.concat(all_details, ignore_index=True)
    metrics = pd.DataFrame(metrics_rows)
    grids = pd.DataFrame(grid_rows)
    exclusions = pd.DataFrame(exclusion_rows)
    details.to_csv(out_root / "station_imerg_daily_join_and_quality_flags.csv", index=False)
    metrics.to_csv(out_root / "comparison_metrics.csv", index=False)
    grids.to_csv(out_root / "station_to_imerg_grid_assignment.csv", index=False)
    exclusions.to_csv(out_root / "exclusion_summary.csv", index=False)

    decision = {
        "status": "PROVISIONAL_IMERG_DIAGNOSTIC_COMPLETED_NOT_APPROVED_FOR_TRAINING",
        "reason": (
            "Only two highland caches are available; raw station workbooks remain missing; "
            "IMERG is satellite precipitation rather than gauge ground truth."
        ),
        "training_authorized": False,
        "project_a_accessed": False,
        "project_b_retrained": False,
        "rl_allocation_executed": False,
    }
    (out_root / "decision.json").write_text(json.dumps(decision, indent=2), encoding="utf-8")

    print("========== NASA IMERG VS STATION PROVISIONAL COMPARISON ==========")
    print("\nGRID ASSIGNMENT")
    print(grids.to_string(index=False))
    print("\nEXCLUSIONS")
    print(exclusions.to_string(index=False))
    print("\nMETRICS")
    with pd.option_context("display.max_columns", None, "display.width", 260):
        print(metrics.to_string(index=False))
    print(f"\nOUTPUT: {out_root}")
    print("DECISION: PROVISIONAL_IMERG_DIAGNOSTIC_COMPLETED_NOT_APPROVED_FOR_TRAINING")
    print("No source files or models were modified.")
    print("Project B retraining remains unauthorized.")
    print("RL allocation remains disabled.")


if __name__ == "__main__":
    main()

