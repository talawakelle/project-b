from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def finite(value):
    return None if pd.isna(value) or not np.isfinite(value) else float(value)


def base_metrics(observed: pd.Series, modeled: pd.Series) -> dict:
    pair = pd.concat([observed, modeled], axis=1).dropna()
    pair.columns = ["station", "nasa"]
    if pair.empty:
        return {"n": 0}
    error = pair["nasa"] - pair["station"]
    return {
        "n": int(len(pair)),
        "station_mean": finite(pair["station"].mean()),
        "nasa_mean": finite(pair["nasa"].mean()),
        "bias_nasa_minus_station": finite(error.mean()),
        "mae": finite(error.abs().mean()),
        "rmse": finite(np.sqrt(np.mean(np.square(error)))),
        "pearson_r": finite(pair["station"].corr(pair["nasa"], method="pearson")),
        "spearman_r": finite(pair["station"].corr(pair["nasa"], method="spearman")),
    }


def event_metrics(observed: pd.Series, modeled: pd.Series, threshold: float = 1.0) -> dict:
    pair = pd.concat([observed, modeled], axis=1).dropna()
    pair.columns = ["station", "nasa"]
    station_wet = pair["station"] >= threshold
    nasa_wet = pair["nasa"] >= threshold
    tp = int((station_wet & nasa_wet).sum())
    fp = int((~station_wet & nasa_wet).sum())
    fn = int((station_wet & ~nasa_wet).sum())
    tn = int((~station_wet & ~nasa_wet).sum())
    return {
        "threshold_mm": threshold,
        "hits": tp,
        "false_alarms": fp,
        "misses": fn,
        "correct_dry": tn,
        "probability_of_detection": finite(tp / (tp + fn)) if tp + fn else None,
        "false_alarm_ratio": finite(fp / (tp + fp)) if tp + fp else None,
        "critical_success_index": finite(tp / (tp + fp + fn)) if tp + fp + fn else None,
        "wet_dry_accuracy": finite((tp + tn) / len(pair)) if len(pair) else None,
    }


def nearest_grid(xwalk: pd.DataFrame, parameter: str, latitude: float, longitude: float) -> dict:
    grids = (
        xwalk.loc[xwalk["parameter"].eq(parameter), [
            "source_grid_latitude", "source_grid_longitude", "estate_key"
        ]]
        .drop_duplicates()
        .copy()
    )
    if grids.empty:
        raise RuntimeError(f"No NASA grid found for {parameter}")
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
        "parameter": parameter,
        "grid_latitude": float(chosen["source_grid_latitude"]),
        "grid_longitude": float(chosen["source_grid_longitude"]),
        "representative_estate_key": str(chosen["estate_key"]),
        "distance_km": float(chosen["distance_km"]),
    }


def monthly_metrics(joined: pd.DataFrame, valid: pd.Series, observed: str, modeled: str, mode: str) -> dict:
    work = joined.loc[valid, ["date", observed, modeled]].copy()
    if work.empty:
        return {"n_months": 0}
    work["month"] = work["date"].dt.to_period("M")
    if mode == "sum":
        monthly = work.groupby("month").agg(
            station=(observed, "sum"), nasa=(modeled, "sum"), valid_days=("date", "size")
        )
        coverage_gate = 0.90
    else:
        monthly = work.groupby("month").agg(
            station=(observed, "mean"), nasa=(modeled, "mean"), valid_days=("date", "size")
        )
        coverage_gate = 0.80
    monthly["calendar_days"] = monthly.index.days_in_month
    monthly["valid_fraction"] = monthly["valid_days"] / monthly["calendar_days"]
    monthly = monthly.loc[monthly["valid_fraction"] >= coverage_gate]
    metrics = base_metrics(monthly["station"], monthly["nasa"])
    metrics["n_months"] = metrics.pop("n", 0)
    metrics["coverage_gate"] = coverage_gate
    return metrics


def main() -> None:
    nasa_root = Path(os.environ["TTE_NASA_ROOT"])
    source_base = Path(os.environ["TTE_WEATHER_BASE"])
    out_root = Path(os.environ["TTE_COMPARE_OUT"])
    out_root.mkdir(parents=True, exist_ok=False)

    nasa_path = nasa_root / "02_processed" / "nasa_power_37_estate_daily_20210401_20260726.csv"
    xwalk_path = nasa_root / "03_audit" / "estate_parameter_grid_crosswalk.csv"
    master_path = source_base / "config" / "weather_station_master.csv"
    caches = {
        "PEDRO-NE": source_base / "outputs" / "gpu_candidates_20260722_181042" / "pedro_weather_daily_cache.csv",
        "TRI-TW": source_base / "outputs" / "gpu_candidates_20260722_181042" / "tri_weather_daily_cache.csv",
    }
    required = [nasa_path, xwalk_path, master_path, *caches.values()]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing required files:\n" + "\n".join(missing))

    nasa = pd.read_csv(nasa_path)
    nasa["observation_date"] = pd.to_datetime(nasa["observation_date"], errors="raise")
    xwalk = pd.read_csv(xwalk_path)
    master = pd.read_csv(master_path)

    all_details = []
    metric_rows = []
    grid_rows = []
    exclusion_rows = []

    for station_code, cache_path in caches.items():
        station_row = master.loc[master["station_code"].eq(station_code)]
        if len(station_row) != 1:
            raise RuntimeError(f"Expected one station-master row for {station_code}; found {len(station_row)}")
        station_row = station_row.iloc[0]
        station_lat = float(station_row["latitude"])
        station_lon = float(station_row["longitude"])

        rain_grid = nearest_grid(xwalk, "PRECTOTCORR", station_lat, station_lon)
        temp_grid = nearest_grid(xwalk, "T2M", station_lat, station_lon)
        for grid in (rain_grid, temp_grid):
            grid_rows.append({"station_code": station_code, **grid})

        station = pd.read_csv(cache_path)
        station["date"] = pd.to_datetime(station["date"], errors="coerce")
        if station["date"].isna().any() or station["date"].duplicated().any():
            raise RuntimeError(f"Invalid or duplicate dates in {cache_path}")
        numeric = [
            "weather_records", "air_temp_mean", "air_temp_min", "air_temp_max", "rainfall_mm"
        ]
        station[numeric] = station[numeric].apply(pd.to_numeric, errors="coerce")
        positive_records = station.loc[station["weather_records"] > 0, "weather_records"]
        median_records = float(positive_records.median())

        rain_nasa = nasa.loc[
            nasa["estate_key"].eq(rain_grid["representative_estate_key"]),
            ["observation_date", "rain_power_corrected_mm_day"],
        ].rename(columns={"observation_date": "date"})
        temp_nasa = nasa.loc[
            nasa["estate_key"].eq(temp_grid["representative_estate_key"]),
            ["observation_date", "temperature_mean_c"],
        ].rename(columns={"observation_date": "date"})

        joined = station.merge(rain_nasa, on="date", how="inner", validate="one_to_one")
        joined = joined.merge(temp_nasa, on="date", how="inner", validate="one_to_one")
        records_ok = joined["weather_records"] >= median_records * 0.50
        rain_valid = records_ok & joined["rainfall_mm"].between(0, 500, inclusive="both")
        temp_valid = (
            records_ok
            & joined["air_temp_mean"].gt(0)
            & joined["air_temp_mean"].le(40)
            & joined["air_temp_min"].gt(0)  # exact zero is treated as a sensor sentinel
            & joined["air_temp_min"].le(40)
            & joined["air_temp_max"].gt(0)
            & joined["air_temp_max"].le(45)
            & joined["air_temp_min"].le(joined["air_temp_mean"])
            & joined["air_temp_mean"].le(joined["air_temp_max"])
        )
        joined.insert(0, "station_code", station_code)
        joined["records_quality_pass"] = records_ok
        joined["rain_quality_pass"] = rain_valid
        joined["temperature_quality_pass"] = temp_valid
        all_details.append(joined)

        exclusion_rows.append({
            "station_code": station_code,
            "cache_rows": int(len(station)),
            "nasa_overlap_rows": int(len(joined)),
            "median_records_per_day": median_records,
            "low_record_rows": int((~records_ok).sum()),
            "rain_invalid_or_low_record_rows": int((~rain_valid).sum()),
            "temperature_invalid_or_low_record_rows": int((~temp_valid).sum()),
            "raw_workbook_provenance_found": False,
        })

        comparisons = {
            "daily_temperature_mean_c": base_metrics(
                joined.loc[temp_valid, "air_temp_mean"], joined.loc[temp_valid, "temperature_mean_c"]
            ),
            "daily_rainfall_mm": base_metrics(
                joined.loc[rain_valid, "rainfall_mm"], joined.loc[rain_valid, "rain_power_corrected_mm_day"]
            ),
            "monthly_temperature_mean_c": monthly_metrics(
                joined, temp_valid, "air_temp_mean", "temperature_mean_c", "mean"
            ),
            "monthly_matched_day_rainfall_mm": monthly_metrics(
                joined, rain_valid, "rainfall_mm", "rain_power_corrected_mm_day", "sum"
            ),
            "rain_event_1mm": event_metrics(
                joined.loc[rain_valid, "rainfall_mm"], joined.loc[rain_valid, "rain_power_corrected_mm_day"]
            ),
        }
        for metric, values in comparisons.items():
            metric_rows.append({"station_code": station_code, "metric": metric, **values})

    details = pd.concat(all_details, ignore_index=True)
    metrics = pd.DataFrame(metric_rows)
    grids = pd.DataFrame(grid_rows)
    exclusions = pd.DataFrame(exclusion_rows)
    details.to_csv(out_root / "station_nasa_daily_join_and_quality_flags.csv", index=False)
    metrics.to_csv(out_root / "comparison_metrics.csv", index=False)
    grids.to_csv(out_root / "station_to_nasa_grid_assignment.csv", index=False)
    exclusions.to_csv(out_root / "exclusion_summary.csv", index=False)

    decision = {
        "status": "PROVISIONAL_DIAGNOSTIC_COMPLETED_NOT_APPROVED_FOR_TRAINING",
        "reason": "Only two highland caches are available and their named raw workbooks were not found.",
        "training_authorized": False,
        "project_a_accessed": False,
        "project_b_retrained": False,
        "rl_allocation_executed": False,
    }
    (out_root / "decision.json").write_text(json.dumps(decision, indent=2), encoding="utf-8")

    print("========== NASA VS STATION PROVISIONAL COMPARISON ==========")
    print("\nGRID ASSIGNMENT")
    print(grids.to_string(index=False))
    print("\nEXCLUSIONS")
    print(exclusions.to_string(index=False))
    print("\nMETRICS")
    with pd.option_context("display.max_columns", None, "display.width", 240):
        print(metrics.to_string(index=False))
    print(f"\nOUTPUT: {out_root}")
    print("DECISION: PROVISIONAL_DIAGNOSTIC_COMPLETED_NOT_APPROVED_FOR_TRAINING")
    print("No source files or models were modified.")
    print("Project B retraining remains unauthorized.")
    print("RL allocation remains disabled.")


if __name__ == "__main__":
    main()
