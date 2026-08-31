from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd


VERSION = "1.0.0"
STATIONS = {
    "PEDRO-NE": "pedro_weather_daily_cache.csv",
    "TRI-TW": "tri_weather_daily_cache.csv",
}


def finite(value):
    return None if pd.isna(value) or not np.isfinite(value) else float(value)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def base_metrics(observed: pd.Series, modeled: pd.Series) -> dict:
    pair = pd.concat([observed, modeled], axis=1).dropna()
    pair.columns = ["station", "source"]
    if pair.empty:
        return {"n": 0}
    error = pair["source"] - pair["station"]
    return {
        "n": int(len(pair)),
        "station_mean": finite(pair["station"].mean()),
        "source_mean": finite(pair["source"].mean()),
        "bias_source_minus_station": finite(error.mean()),
        "mae": finite(error.abs().mean()),
        "rmse": finite(np.sqrt(np.mean(np.square(error)))),
        "pearson_r": finite(pair["station"].corr(pair["source"], method="pearson")),
        "spearman_r": finite(pair["station"].corr(pair["source"], method="spearman")),
    }


def event_metrics(
    observed: pd.Series, modeled: pd.Series, threshold: float = 1.0
) -> dict:
    pair = pd.concat([observed, modeled], axis=1).dropna()
    pair.columns = ["station", "source"]
    station_wet = pair["station"] >= threshold
    source_wet = pair["source"] >= threshold
    hits = int((station_wet & source_wet).sum())
    false_alarms = int((~station_wet & source_wet).sum())
    misses = int((station_wet & ~source_wet).sum())
    correct_dry = int((~station_wet & ~source_wet).sum())
    return {
        "n": int(len(pair)),
        "threshold_mm": threshold,
        "hits": hits,
        "false_alarms": false_alarms,
        "misses": misses,
        "correct_dry": correct_dry,
        "probability_of_detection": finite(hits / (hits + misses))
        if hits + misses
        else None,
        "false_alarm_ratio": finite(false_alarms / (hits + false_alarms))
        if hits + false_alarms
        else None,
        "critical_success_index": finite(hits / (hits + false_alarms + misses))
        if hits + false_alarms + misses
        else None,
        "wet_dry_accuracy": finite((hits + correct_dry) / len(pair))
        if len(pair)
        else None,
    }


def monthly_metrics(
    frame: pd.DataFrame, source_column: str, coverage_gate: float = 0.90
) -> dict:
    work = frame[["date", "rainfall_mm", source_column]].dropna().copy()
    if work.empty:
        return {"n": 0, "n_months": 0, "coverage_gate": coverage_gate}
    work["month"] = work["date"].dt.to_period("M")
    monthly = work.groupby("month").agg(
        station=("rainfall_mm", "sum"),
        source=(source_column, "sum"),
        valid_days=("date", "size"),
    )
    monthly["calendar_days"] = monthly.index.days_in_month
    monthly["valid_fraction"] = monthly["valid_days"] / monthly["calendar_days"]
    monthly = monthly.loc[monthly["valid_fraction"] >= coverage_gate]
    result = base_metrics(monthly["station"], monthly["source"])
    result["n_months"] = result.get("n", 0)
    result["coverage_gate"] = coverage_gate
    return result


def nearest_grid(
    xwalk: pd.DataFrame, parameter: str, latitude: float, longitude: float
) -> dict:
    grids = (
        xwalk.loc[
            xwalk["parameter"].eq(parameter),
            ["source_grid_latitude", "source_grid_longitude", "estate_key"],
        ]
        .drop_duplicates()
        .copy()
    )
    if grids.empty:
        raise RuntimeError(f"No grid found for {parameter}")
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


def one_daily_csv(root: Path, pattern: str) -> Path:
    files = list((root / "02_processed").glob(pattern))
    if len(files) != 1:
        raise RuntimeError(
            f"Expected exactly one processed file matching {pattern!r} under {root}; "
            f"found {len(files)}"
        )
    return files[0]


def add_metric_rows(
    rows: list[dict],
    station_code: str,
    shift_days: int,
    source: str,
    valid: pd.DataFrame,
    source_column: str,
) -> None:
    metrics = {
        "daily_rainfall": base_metrics(valid["rainfall_mm"], valid[source_column]),
        "monthly_matched_day_rainfall": monthly_metrics(valid, source_column),
        "rain_event_1mm": event_metrics(valid["rainfall_mm"], valid[source_column]),
    }
    for metric_name, values in metrics.items():
        rows.append(
            {
                "station_code": station_code,
                "imerg_station_date_shift_days": shift_days,
                "source": source,
                "metric": metric_name,
                **values,
            }
        )


def run_self_test() -> None:
    observed = pd.Series([0.0, 1.0, 5.0, 2.0])
    exact = pd.Series([0.0, 1.0, 5.0, 2.0])
    result = base_metrics(observed, exact)
    assert result["n"] == 4
    assert result["mae"] == 0.0
    assert result["rmse"] == 0.0
    assert result["pearson_r"] == 1.0
    event = event_metrics(observed, exact)
    assert event["hits"] == 3
    assert event["false_alarms"] == 0
    assert event["misses"] == 0
    assert abs(haversine_km(6.95, 80.75, 6.95, 80.75)) < 1e-12
    print("SELF_TEST_PASS")
    print("No project, model, training, or RL action executed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        run_self_test()
        return

    prectot_root = Path(os.environ["TTE_PRECTOT_ROOT"])
    imerg_root = Path(os.environ["TTE_IMERG_ROOT"])
    weather_base = Path(os.environ["TTE_WEATHER_BASE"])
    out_root = Path(os.environ["TTE_BENCH_OUT"])
    out_root.mkdir(parents=True, exist_ok=False)

    prectot_path = one_daily_csv(
        prectot_root, "nasa_power_37_estate_daily_*.csv"
    )
    imerg_path = one_daily_csv(
        imerg_root, "nasa_power_imerg_37_estate_daily_*.csv"
    )
    prectot_xwalk_path = (
        prectot_root / "03_audit" / "estate_parameter_grid_crosswalk.csv"
    )
    imerg_xwalk_path = (
        imerg_root / "03_audit" / "estate_parameter_grid_crosswalk.csv"
    )
    station_master_path = weather_base / "config" / "weather_station_master.csv"
    cache_root = weather_base / "outputs" / "gpu_candidates_20260722_181042"
    cache_paths = {code: cache_root / name for code, name in STATIONS.items()}
    required = [
        prectot_path,
        imerg_path,
        prectot_xwalk_path,
        imerg_xwalk_path,
        station_master_path,
        *cache_paths.values(),
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing required files:\n" + "\n".join(missing))

    prectot = pd.read_csv(prectot_path)
    imerg = pd.read_csv(imerg_path)
    prectot["observation_date"] = pd.to_datetime(
        prectot["observation_date"], errors="raise"
    )
    imerg["observation_date"] = pd.to_datetime(
        imerg["observation_date"], errors="raise"
    )
    prectot_xwalk = pd.read_csv(prectot_xwalk_path)
    imerg_xwalk = pd.read_csv(imerg_xwalk_path)
    station_master = pd.read_csv(station_master_path)

    metric_rows: list[dict] = []
    grid_rows: list[dict] = []
    exclusion_rows: list[dict] = []
    detail_frames: list[pd.DataFrame] = []

    for station_code, cache_path in cache_paths.items():
        meta = station_master.loc[station_master["station_code"].eq(station_code)]
        if len(meta) != 1:
            raise RuntimeError(
                f"Expected one station-master row for {station_code}; found {len(meta)}"
            )
        meta = meta.iloc[0]
        latitude, longitude = float(meta["latitude"]), float(meta["longitude"])
        prectot_grid = nearest_grid(
            prectot_xwalk, "PRECTOTCORR", latitude, longitude
        )
        imerg_grid = nearest_grid(
            imerg_xwalk, "IMERG_PRECTOT", latitude, longitude
        )
        grid_rows.extend(
            [
                {
                    "station_code": station_code,
                    "source": "PRECTOTCORR_LST",
                    **prectot_grid,
                },
                {
                    "station_code": station_code,
                    "source": "IMERG_UTC",
                    **imerg_grid,
                },
            ]
        )

        station = pd.read_csv(cache_path)
        station["date"] = pd.to_datetime(station["date"], errors="coerce")
        if station["date"].isna().any() or station["date"].duplicated().any():
            raise RuntimeError(f"Invalid or duplicate station dates: {cache_path}")
        station[["weather_records", "rainfall_mm"]] = station[
            ["weather_records", "rainfall_mm"]
        ].apply(pd.to_numeric, errors="coerce")
        median_records = float(
            station.loc[station["weather_records"] > 0, "weather_records"].median()
        )
        station["records_quality_pass"] = (
            station["weather_records"] >= median_records * 0.50
        )
        station["station_rain_quality_pass"] = (
            station["records_quality_pass"]
            & station["rainfall_mm"].between(0, 500, inclusive="both")
        )

        prectot_series = prectot.loc[
            prectot["estate_key"].eq(prectot_grid["representative_estate_key"]),
            ["observation_date", "rain_power_corrected_mm_day"],
        ].rename(
            columns={
                "observation_date": "date",
                "rain_power_corrected_mm_day": "prectotcorr_mm_day",
            }
        )
        imerg_columns = ["observation_date", "rain_imerg_mm_day"]
        if "imerg_training_eligible_by_age" in imerg.columns:
            imerg_columns.append("imerg_training_eligible_by_age")
        imerg_series = imerg.loc[
            imerg["estate_key"].eq(imerg_grid["representative_estate_key"]),
            imerg_columns,
        ].copy()
        if "imerg_training_eligible_by_age" not in imerg_series.columns:
            imerg_series["imerg_training_eligible_by_age"] = "UNKNOWN"

        for shift_days in (-1, 0, 1):
            shifted_imerg = imerg_series.copy()
            shifted_imerg["date"] = shifted_imerg["observation_date"] + pd.Timedelta(
                days=shift_days
            )
            shifted_imerg = shifted_imerg.drop(columns=["observation_date"])
            joined = station.merge(
                prectot_series, on="date", how="inner", validate="one_to_one"
            ).merge(shifted_imerg, on="date", how="inner", validate="one_to_one")
            source_valid = (
                joined["prectotcorr_mm_day"].between(0, 500, inclusive="both")
                & joined["rain_imerg_mm_day"].between(0, 500, inclusive="both")
            )
            maturity = joined["imerg_training_eligible_by_age"].eq("YES")
            if not maturity.any():
                maturity = pd.Series(True, index=joined.index)
            valid_mask = (
                joined["station_rain_quality_pass"] & source_valid & maturity
            )
            valid = joined.loc[valid_mask].copy()
            valid.insert(0, "station_code", station_code)
            valid.insert(1, "imerg_station_date_shift_days", shift_days)
            detail_frames.append(valid)

            add_metric_rows(
                metric_rows,
                station_code,
                shift_days,
                "PRECTOTCORR_LST",
                valid,
                "prectotcorr_mm_day",
            )
            add_metric_rows(
                metric_rows,
                station_code,
                shift_days,
                "IMERG_UTC",
                valid,
                "rain_imerg_mm_day",
            )
            exclusion_rows.append(
                {
                    "station_code": station_code,
                    "imerg_station_date_shift_days": shift_days,
                    "station_cache_rows": int(len(station)),
                    "three_way_overlap_rows": int(len(joined)),
                    "matched_valid_rows": int(len(valid)),
                    "excluded_rows": int(len(joined) - len(valid)),
                    "median_station_records_per_day": median_records,
                    "raw_workbook_provenance_found": False,
                }
            )

    metrics = pd.DataFrame(metric_rows)
    grids = pd.DataFrame(grid_rows)
    exclusions = pd.DataFrame(exclusion_rows)
    details = pd.concat(detail_frames, ignore_index=True)
    metrics.to_csv(out_root / "matched_source_metrics.csv", index=False)
    grids.to_csv(out_root / "station_source_grid_assignment.csv", index=False)
    exclusions.to_csv(out_root / "matched_sample_exclusions.csv", index=False)
    details.to_csv(out_root / "matched_daily_detail.csv", index=False)

    decision = {
        "status": "MATCHED_RAINFALL_SOURCE_BENCHMARK_COMPLETED_DIAGNOSTIC_ONLY",
        "version": VERSION,
        "training_authorized": False,
        "project_a_accessed": False,
        "project_b_retrained": False,
        "rl_allocation_executed": False,
        "automatic_source_winner_declared": False,
        "limitations": [
            "Only two highland station caches are available.",
            "Raw station-workbook provenance is unresolved.",
            "IMERG is UTC daily while PRECTOTCORR is LST daily.",
            "A whole-day shift is a sensitivity test, not an exact UTC-to-local reconstruction.",
            "Estate coordinates remain provisional and low-country estates are not validated.",
        ],
    }
    (out_root / "decision.json").write_text(
        json.dumps(decision, indent=2), encoding="utf-8"
    )

    print("========== MATCHED RAINFALL SOURCE BENCHMARK ==========")
    print("\nGRID ASSIGNMENT")
    print(grids.to_string(index=False))
    print("\nMATCHED SAMPLE")
    print(exclusions.to_string(index=False))
    print("\nDAILY METRICS")
    daily = metrics.loc[metrics["metric"].eq("daily_rainfall")]
    with pd.option_context("display.max_columns", None, "display.width", 260):
        print(daily.to_string(index=False))
    print("\nMONTHLY METRICS")
    monthly = metrics.loc[metrics["metric"].eq("monthly_matched_day_rainfall")]
    with pd.option_context("display.max_columns", None, "display.width", 260):
        print(monthly.to_string(index=False))
    print("\nEVENT METRICS")
    events = metrics.loc[metrics["metric"].eq("rain_event_1mm")]
    with pd.option_context("display.max_columns", None, "display.width", 260):
        print(events.to_string(index=False))
    print(f"\nOUTPUT: {out_root}")
    print("DECISION: MATCHED_RAINFALL_SOURCE_BENCHMARK_COMPLETED_DIAGNOSTIC_ONLY")
    print("No source files or models were modified.")
    print("Project B retraining remains unauthorized.")
    print("RL allocation remains disabled.")


if __name__ == "__main__":
    main()
