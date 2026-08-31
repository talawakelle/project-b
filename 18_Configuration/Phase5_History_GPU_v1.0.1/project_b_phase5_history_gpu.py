#!/usr/bin/env python3
"""
Project B Phase 5: leakage-safe, history-only GPU quantile baseline.

This runner is intentionally limited to the validated all-record estate-day
actual-green-leaf dataset. It does not read or modify Project A, and it does
not authorize operational use. The output is a challenger baseline for the
later weather, spatiotemporal, ensemble, reconciliation, and allocation gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


RUNNER_VERSION = "1.0.1"
EXPECTED_SOURCE_PROFILE = {
    "rows": 40_034,
    "estates": 33,
    "minimum_date": "2021-04-01",
    "maximum_date": "2026-06-19",
    "total_kg": 136_498_964.0,
}
REQUIRED_COLUMNS = {
    "observation_date",
    "estate",
    "actual_green_leaf_kg",
    "weighment_rows",
    "employees",
}
LAGS = (1, 2, 3, 4, 5, 6, 7, 14, 21, 28, 56, 364, 365)
ROLLING_WINDOWS = (7, 14, 28, 56)


@dataclass
class DataProfile:
    source_rows: int
    source_estates: int
    source_unique_dates: int
    source_min_date: str
    source_max_date: str
    source_total_kg: float
    duplicate_estate_date_keys: int
    dense_rows: int
    observed_rows: int
    short_gap_zero_rows: int
    unresolved_long_gap_rows: int
    supervised_rows: int
    train_rows_before_sampling: int
    validation_rows_before_sampling: int
    test_rows_before_sampling: int
    validation_target_start: str
    test_target_start: str
    test_target_end: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, default=json_default),
        encoding="utf-8",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def normalize_input(frame: pd.DataFrame) -> pd.DataFrame:
    missing = REQUIRED_COLUMNS.difference(frame.columns)
    if missing:
        raise ValueError(f"Input is missing required columns: {sorted(missing)}")

    data = frame[list(REQUIRED_COLUMNS)].copy()
    data["observation_date"] = pd.to_datetime(
        data["observation_date"], errors="coerce"
    )
    data["estate"] = data["estate"].astype("string").str.strip()
    for column in ("actual_green_leaf_kg", "weighment_rows", "employees"):
        data[column] = pd.to_numeric(data[column], errors="coerce")

    invalid = (
        data["observation_date"].isna()
        | data["estate"].isna()
        | data["estate"].eq("")
        | data["actual_green_leaf_kg"].isna()
        | (data["actual_green_leaf_kg"] < 0)
    )
    if invalid.any():
        examples = data.loc[invalid].head(10).to_dict(orient="records")
        raise ValueError(
            f"Input contains {int(invalid.sum())} invalid rows. Examples: {examples}"
        )

    data["weighment_rows"] = data["weighment_rows"].fillna(0)
    data["employees"] = data["employees"].fillna(0)
    data = data.sort_values(["estate", "observation_date"]).reset_index(drop=True)
    return data


def validate_expected_source_profile(source: pd.DataFrame) -> dict[str, Any]:
    profile = {
        "rows": int(len(source)),
        "estates": int(source["estate"].nunique()),
        "minimum_date": source["observation_date"].min().date().isoformat(),
        "maximum_date": source["observation_date"].max().date().isoformat(),
        "total_kg": float(source["actual_green_leaf_kg"].sum()),
    }
    mismatches = {
        key: {"expected": expected, "actual": profile[key]}
        for key, expected in EXPECTED_SOURCE_PROFILE.items()
        if not (
            math.isclose(profile[key], expected, rel_tol=0.0, abs_tol=0.01)
            if key == "total_kg"
            else profile[key] == expected
        )
    }
    if mismatches:
        raise ValueError(
            "Input does not match the approved Project B all-record estate-day "
            f"profile: {mismatches}"
        )
    return profile


def missing_run_lengths(missing: pd.Series) -> pd.Series:
    run_id = missing.ne(missing.shift(fill_value=False)).cumsum()
    lengths = missing.groupby(run_id).transform("sum")
    return lengths.where(missing, 0).astype("int32")


def build_dense_history(
    source: pd.DataFrame,
    zero_fill_max_gap: int,
    min_history_days: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    pieces: list[pd.DataFrame] = []

    for estate, estate_rows in source.groupby("estate", sort=True):
        estate_rows = estate_rows.sort_values("observation_date")
        index = pd.date_range(
            estate_rows["observation_date"].min(),
            estate_rows["observation_date"].max(),
            freq="D",
        )
        indexed = estate_rows.set_index("observation_date").reindex(index)
        indexed.index.name = "observation_date"
        indexed["estate"] = estate
        indexed["observed_flag"] = indexed["actual_green_leaf_kg"].notna()

        run_lengths = missing_run_lengths(~indexed["observed_flag"])
        short_gap = (
            ~indexed["observed_flag"]
        ) & run_lengths.le(zero_fill_max_gap)
        indexed["short_gap_zero_flag"] = short_gap
        indexed["model_actual_kg"] = indexed["actual_green_leaf_kg"]
        indexed.loc[short_gap, "model_actual_kg"] = 0.0

        indexed.loc[short_gap, "weighment_rows"] = 0.0
        indexed.loc[short_gap, "employees"] = 0.0
        indexed["history_days"] = np.arange(len(indexed), dtype=np.int32)
        pieces.append(indexed.reset_index())

    dense = pd.concat(pieces, ignore_index=True)
    dense = dense.sort_values(["estate", "observation_date"]).reset_index(drop=True)
    dense["model_actual_kg"] = dense["model_actual_kg"].astype("float32")
    dense["weighment_rows"] = dense["weighment_rows"].astype("float32")
    dense["employees"] = dense["employees"].astype("float32")
    dense["observed_flag"] = dense["observed_flag"].astype("int8")
    dense["short_gap_zero_flag"] = dense["short_gap_zero_flag"].astype("int8")

    grouped_actual = dense.groupby("estate", sort=False)["model_actual_kg"]
    grouped_observed = dense.groupby("estate", sort=False)["observed_flag"]

    dense["latest_actual_kg"] = dense["model_actual_kg"]
    for lag in LAGS:
        dense[f"lag_{lag}"] = grouped_actual.shift(lag).astype("float32")

    for window in ROLLING_WINDOWS:
        minimum = max(3, window // 3)
        dense[f"rolling_mean_{window}"] = (
            grouped_actual.transform(
                lambda values, w=window, m=minimum: values.rolling(
                    w, min_periods=m
                ).mean()
            ).astype("float32")
        )
        dense[f"rolling_std_{window}"] = (
            grouped_actual.transform(
                lambda values, w=window, m=minimum: values.rolling(
                    w, min_periods=m
                ).std()
            ).astype("float32")
        )
        dense[f"observed_rate_{window}"] = (
            grouped_observed.transform(
                lambda values, w=window, m=minimum: values.rolling(
                    w, min_periods=m
                ).mean()
            ).astype("float32")
        )

    dates = dense["observation_date"]
    day_of_week = dates.dt.dayofweek.astype("float32")
    day_of_year = dates.dt.dayofyear.astype("float32")
    month = dates.dt.month.astype("float32")
    dense["origin_dow_sin"] = np.sin(2 * np.pi * day_of_week / 7).astype("float32")
    dense["origin_dow_cos"] = np.cos(2 * np.pi * day_of_week / 7).astype("float32")
    dense["origin_doy_sin"] = np.sin(2 * np.pi * day_of_year / 365.25).astype(
        "float32"
    )
    dense["origin_doy_cos"] = np.cos(2 * np.pi * day_of_year / 365.25).astype(
        "float32"
    )
    dense["origin_month_sin"] = np.sin(2 * np.pi * month / 12).astype("float32")
    dense["origin_month_cos"] = np.cos(2 * np.pi * month / 12).astype("float32")
    global_minimum = dates.min()
    dense["trend_days"] = (dates - global_minimum).dt.days.astype("float32")
    dense["origin_eligible"] = (
        dense["model_actual_kg"].notna()
        & dense["history_days"].ge(min_history_days)
    )

    gap_profile = {
        "dense_rows": int(len(dense)),
        "observed_rows": int(dense["observed_flag"].sum()),
        "short_gap_zero_rows": int(dense["short_gap_zero_flag"].sum()),
        "unresolved_long_gap_rows": int(dense["model_actual_kg"].isna().sum()),
        "zero_fill_max_gap_days": int(zero_fill_max_gap),
        "minimum_history_days": int(min_history_days),
        "missing_day_rule": (
            f"Missing runs of 1-{zero_fill_max_gap} calendar days are treated as "
            "zero-crop non-plucking days; longer gaps remain missing and are excluded "
            "as targets."
        ),
    }
    return dense, gap_profile


def base_feature_columns() -> list[str]:
    columns = [
        "estate_feature",
        "horizon",
        "latest_actual_kg",
        "weighment_rows",
        "employees",
        "observed_flag",
        "short_gap_zero_flag",
        "history_days",
        "origin_dow_sin",
        "origin_dow_cos",
        "origin_doy_sin",
        "origin_doy_cos",
        "origin_month_sin",
        "origin_month_cos",
        "trend_days",
        "target_dow_sin",
        "target_dow_cos",
        "target_doy_sin",
        "target_doy_cos",
        "target_month_sin",
        "target_month_cos",
    ]
    columns.extend(f"lag_{lag}" for lag in LAGS)
    for window in ROLLING_WINDOWS:
        columns.extend(
            (
                f"rolling_mean_{window}",
                f"rolling_std_{window}",
                f"observed_rate_{window}",
            )
        )
    return columns


def build_supervised(
    dense: pd.DataFrame,
    max_horizon: int,
    estate_categories: list[str],
) -> pd.DataFrame:
    grouped = dense.groupby("estate", sort=False)["model_actual_kg"]
    feature_source = [
        "estate",
        "observation_date",
        "latest_actual_kg",
        "weighment_rows",
        "employees",
        "observed_flag",
        "short_gap_zero_flag",
        "history_days",
        "origin_dow_sin",
        "origin_dow_cos",
        "origin_doy_sin",
        "origin_doy_cos",
        "origin_month_sin",
        "origin_month_cos",
        "trend_days",
    ]
    feature_source.extend(f"lag_{lag}" for lag in LAGS)
    for window in ROLLING_WINDOWS:
        feature_source.extend(
            (
                f"rolling_mean_{window}",
                f"rolling_std_{window}",
                f"observed_rate_{window}",
            )
        )

    pieces: list[pd.DataFrame] = []
    eligible = dense["origin_eligible"]

    for horizon in range(1, max_horizon + 1):
        future_target = grouped.shift(-horizon)
        valid = eligible & future_target.notna()
        if not valid.any():
            continue

        piece = dense.loc[valid, feature_source].copy()
        piece = piece.rename(columns={"observation_date": "origin_date"})
        piece["target_date"] = piece["origin_date"] + pd.to_timedelta(
            horizon, unit="D"
        )
        piece["horizon"] = np.float32(horizon)
        piece["target_kg"] = future_target.loc[valid].astype("float32").to_numpy()

        target_dates = piece["target_date"]
        target_dow = target_dates.dt.dayofweek.astype("float32")
        target_doy = target_dates.dt.dayofyear.astype("float32")
        target_month = target_dates.dt.month.astype("float32")
        piece["target_dow_sin"] = np.sin(2 * np.pi * target_dow / 7).astype(
            "float32"
        )
        piece["target_dow_cos"] = np.cos(2 * np.pi * target_dow / 7).astype(
            "float32"
        )
        piece["target_doy_sin"] = np.sin(
            2 * np.pi * target_doy / 365.25
        ).astype("float32")
        piece["target_doy_cos"] = np.cos(
            2 * np.pi * target_doy / 365.25
        ).astype("float32")
        piece["target_month_sin"] = np.sin(
            2 * np.pi * target_month / 12
        ).astype("float32")
        piece["target_month_cos"] = np.cos(
            2 * np.pi * target_month / 12
        ).astype("float32")
        pieces.append(piece)

    if not pieces:
        raise ValueError("No supervised rows could be created.")

    supervised = pd.concat(pieces, ignore_index=True)
    supervised["estate_feature"] = pd.Categorical(
        supervised["estate"],
        categories=estate_categories,
    )
    return supervised


def deterministic_sample(
    frame: pd.DataFrame,
    maximum_rows: int,
    seed: int,
) -> pd.DataFrame:
    if maximum_rows <= 0 or len(frame) <= maximum_rows:
        return frame
    return frame.sample(n=maximum_rows, random_state=seed).sort_index()


def split_supervised(
    supervised: pd.DataFrame,
    maximum_target_date: pd.Timestamp,
    window_days: int,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Timestamp, pd.Timestamp]:
    test_start = maximum_target_date - pd.Timedelta(days=window_days - 1)
    validation_start = test_start - pd.Timedelta(days=window_days)
    train = supervised.loc[supervised["target_date"] < validation_start]
    validation = supervised.loc[
        (supervised["target_date"] >= validation_start)
        & (supervised["target_date"] < test_start)
    ]
    test = supervised.loc[supervised["target_date"] >= test_start]

    if min(len(train), len(validation), len(test)) == 0:
        raise ValueError(
            "Time split produced an empty partition. "
            f"train={len(train)}, validation={len(validation)}, test={len(test)}"
        )
    return train, validation, test, validation_start, test_start


def make_features(frame: pd.DataFrame, feature_columns: list[str]) -> pd.DataFrame:
    features = frame[feature_columns].copy()
    for column in feature_columns:
        if column == "estate_feature":
            continue
        features[column] = pd.to_numeric(features[column], errors="coerce").astype(
            "float32"
        )
    return features


def seasonal_naive(frame: pd.DataFrame) -> np.ndarray:
    horizons = frame["horizon"].astype("int32").to_numpy()
    offsets = (7 - (horizons % 7)) % 7
    prediction = np.full(len(frame), np.nan, dtype=np.float32)
    latest = frame["latest_actual_kg"].to_numpy(dtype=np.float32)
    prediction[offsets == 0] = latest[offsets == 0]
    for offset in range(1, 7):
        values = frame[f"lag_{offset}"].to_numpy(dtype=np.float32)
        prediction[offsets == offset] = values[offsets == offset]
    fallback = frame["rolling_mean_7"].to_numpy(dtype=np.float32)
    prediction = np.where(np.isfinite(prediction), prediction, fallback)
    prediction = np.where(np.isfinite(prediction), prediction, 0.0)
    return np.clip(prediction, 0.0, None)


def pinball_loss(actual: np.ndarray, prediction: np.ndarray, alpha: float) -> float:
    error = actual - prediction
    return float(np.mean(np.maximum(alpha * error, (alpha - 1.0) * error)))


def metrics_for(
    actual: np.ndarray,
    p50: np.ndarray,
    p90: np.ndarray,
    naive: np.ndarray,
) -> dict[str, float]:
    actual = np.asarray(actual, dtype=np.float64)
    p50 = np.clip(np.asarray(p50, dtype=np.float64), 0.0, None)
    p90 = np.maximum(np.asarray(p90, dtype=np.float64), p50)
    naive = np.clip(np.asarray(naive, dtype=np.float64), 0.0, None)
    denominator = max(float(np.abs(actual).sum()), 1e-9)
    return {
        "rows": int(len(actual)),
        "actual_total_kg": float(actual.sum()),
        "p50_mae_kg": float(np.mean(np.abs(actual - p50))),
        "p50_wape_pct": float(100.0 * np.abs(actual - p50).sum() / denominator),
        "p50_bias_pct": float(100.0 * (p50 - actual).sum() / denominator),
        "p50_pinball_loss": pinball_loss(actual, p50, 0.5),
        "p90_pinball_loss": pinball_loss(actual, p90, 0.9),
        "p90_empirical_coverage_pct": float(100.0 * np.mean(actual <= p90)),
        "p90_above_p50_pct": float(100.0 * np.mean(p90 >= p50)),
        "naive_mae_kg": float(np.mean(np.abs(actual - naive))),
        "naive_wape_pct": float(
            100.0 * np.abs(actual - naive).sum() / denominator
        ),
    }


def evaluate_predictions(
    test: pd.DataFrame,
    p50: np.ndarray,
    p90: np.ndarray,
    naive: np.ndarray,
) -> dict[str, Any]:
    actual = test["target_kg"].to_numpy(dtype=np.float64)
    payload: dict[str, Any] = {
        "overall": metrics_for(actual, p50, p90, naive),
        "horizon_bands": {},
    }
    bands = {
        "days_01_07": (1, 7),
        "days_08_30": (8, 30),
        "days_31_60": (31, 60),
        "days_61_90": (61, 90),
    }
    horizons = test["horizon"].to_numpy(dtype=np.int32)
    for label, (minimum, maximum) in bands.items():
        mask = (horizons >= minimum) & (horizons <= maximum)
        if mask.any():
            payload["horizon_bands"][label] = metrics_for(
                actual[mask], p50[mask], p90[mask], naive[mask]
            )
    return payload


def train_quantile_model(
    x_train: pd.DataFrame,
    y_train: pd.Series,
    x_validation: pd.DataFrame | None,
    y_validation: pd.Series | None,
    alpha: float,
    device: str,
    estimators: int,
    seed: int,
    early_stopping_rounds: int | None,
    verbose: bool,
):
    try:
        import xgboost as xgb
    except ImportError as exc:
        raise RuntimeError(
            "xgboost is not installed in the selected Python environment."
        ) from exc

    parameters: dict[str, Any] = {
        "objective": "reg:quantileerror",
        "quantile_alpha": alpha,
        "tree_method": "hist",
        "device": device,
        "n_estimators": estimators,
        "learning_rate": 0.05,
        "max_depth": 8,
        "min_child_weight": 8,
        "subsample": 0.85,
        "colsample_bytree": 0.85,
        "reg_alpha": 0.05,
        "reg_lambda": 1.0,
        "max_bin": 256,
        "random_state": seed,
        "n_jobs": max(1, min(8, (getattr(__import__("os"), "cpu_count")() or 4))),
        "enable_categorical": True,
        "eval_metric": "quantile",
    }
    if early_stopping_rounds:
        parameters["early_stopping_rounds"] = early_stopping_rounds

    model = xgb.XGBRegressor(**parameters)
    fit_parameters: dict[str, Any] = {"verbose": verbose}
    if x_validation is not None and y_validation is not None:
        fit_parameters["eval_set"] = [(x_validation, y_validation)]
    model.fit(x_train, y_train, **fit_parameters)
    if device == "cuda":
        config = json.loads(model.get_booster().save_config())

        def configured_devices(value: Any) -> list[str]:
            if isinstance(value, dict):
                devices = []
                for key, child in value.items():
                    if key == "device":
                        devices.append(str(child))
                    devices.extend(configured_devices(child))
                return devices
            if isinstance(value, list):
                devices = []
                for child in value:
                    devices.extend(configured_devices(child))
                return devices
            return []

        devices = configured_devices(config)
        if not any(value.startswith("cuda") for value in devices):
            raise RuntimeError(
                "CUDA was requested but the trained XGBoost model does not "
                f"report a CUDA device. Reported devices: {devices}"
            )
    return model


def predict_with_explicit_dmatrix(model: Any, features: pd.DataFrame) -> np.ndarray:
    """Predict without XGBoost's CPU-data/GPU-model inplace-predict warning."""
    import xgboost as xgb

    matrix = xgb.DMatrix(features, enable_categorical=True)
    best_iteration = model.get_booster().attr("best_iteration")
    iteration_range = (
        (0, int(best_iteration) + 1)
        if best_iteration is not None
        else (0, 0)
    )
    return model.get_booster().predict(
        matrix,
        iteration_range=iteration_range,
    )


def make_future_frame(
    dense: pd.DataFrame,
    estate_categories: list[str],
    max_horizon: int,
) -> pd.DataFrame:
    latest_rows = (
        dense.loc[dense["origin_eligible"]]
        .sort_values(["estate", "observation_date"])
        .groupby("estate", as_index=False)
        .tail(1)
    )
    pieces: list[pd.DataFrame] = []
    feature_source = [
        column
        for column in base_feature_columns()
        if column
        not in {
            "estate_feature",
            "horizon",
            "target_dow_sin",
            "target_dow_cos",
            "target_doy_sin",
            "target_doy_cos",
            "target_month_sin",
            "target_month_cos",
        }
    ]

    for horizon in range(1, max_horizon + 1):
        piece = latest_rows[
            ["estate", "observation_date", *feature_source]
        ].copy()
        piece = piece.rename(columns={"observation_date": "forecast_origin"})
        piece["forecast_date"] = piece["forecast_origin"] + pd.to_timedelta(
            horizon, unit="D"
        )
        piece["horizon"] = np.float32(horizon)
        dates = piece["forecast_date"]
        dow = dates.dt.dayofweek.astype("float32")
        doy = dates.dt.dayofyear.astype("float32")
        month = dates.dt.month.astype("float32")
        piece["target_dow_sin"] = np.sin(2 * np.pi * dow / 7).astype("float32")
        piece["target_dow_cos"] = np.cos(2 * np.pi * dow / 7).astype("float32")
        piece["target_doy_sin"] = np.sin(2 * np.pi * doy / 365.25).astype(
            "float32"
        )
        piece["target_doy_cos"] = np.cos(2 * np.pi * doy / 365.25).astype(
            "float32"
        )
        piece["target_month_sin"] = np.sin(2 * np.pi * month / 12).astype(
            "float32"
        )
        piece["target_month_cos"] = np.cos(2 * np.pi * month / 12).astype(
            "float32"
        )
        pieces.append(piece)

    future = pd.concat(pieces, ignore_index=True)
    future["estate_feature"] = pd.Categorical(
        future["estate"], categories=estate_categories
    )
    return future


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Project B Phase 5 history-only GPU quantile baseline."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--project-b-root", type=Path, required=True)
    parser.add_argument("--device", default="cuda", choices=("cuda", "cpu"))
    parser.add_argument("--max-horizon", type=int, default=90)
    parser.add_argument("--zero-fill-max-gap", type=int, default=3)
    parser.add_argument("--min-history-days", type=int, default=56)
    parser.add_argument("--split-window-days", type=int, default=90)
    parser.add_argument("--max-train-rows", type=int, default=2_000_000)
    parser.add_argument("--max-eval-rows", type=int, default=500_000)
    parser.add_argument("--max-final-rows", type=int, default=2_500_000)
    parser.add_argument("--estimators", type=int, default=1200)
    parser.add_argument("--early-stopping-rounds", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20260731)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--quick-smoke", action="store_true")
    parser.add_argument("--no-final-refit", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.quick_smoke:
        args.max_horizon = min(args.max_horizon, 14)
        args.split_window_days = min(args.split_window_days, 28)
        args.max_train_rows = min(args.max_train_rows, 100_000)
        args.max_eval_rows = min(args.max_eval_rows, 50_000)
        args.max_final_rows = min(args.max_final_rows, 120_000)
        args.estimators = min(args.estimators, 80)
        args.early_stopping_rounds = min(args.early_stopping_rounds, 10)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = (
        args.project_b_root
        / "04_Gradient_Boosting"
        / f"Phase5_History_Only_GPU_{timestamp}"
    )
    output_root.mkdir(parents=True, exist_ok=False)
    manifest_path = output_root / "run_manifest.json"

    manifest: dict[str, Any] = {
        "status": "STARTED",
        "runner_version": RUNNER_VERSION,
        "started_at_utc": utc_now(),
        "input": str(args.input),
        "output_folder": str(output_root),
        "device_requested": args.device,
        "model_scope": "HISTORY_ONLY_QUANTILE_GRADIENT_BOOSTING_BASELINE",
        "target_rule": (
            "Use the validated all-record daily estate aggregates. "
            "Do not filter historical crop by active_value."
        ),
        "project_a_changed": False,
        "project_b_source_data_changed": False,
        "operational_use_allowed": False,
        "live_assignment_authorized": False,
        "arguments": vars(args),
    }
    write_json(manifest_path, manifest)

    try:
        log(f"Phase 5 runner v{RUNNER_VERSION}")
        log(f"Reading {args.input}")
        if "active_only" in str(args.input).lower():
            raise ValueError(
                "Refusing active-only input. Use the validated all-record "
                "daily_estate_actual_green_leaf.csv file."
            )
        source = normalize_input(pd.read_csv(args.input))
        approved_profile = validate_expected_source_profile(source)
        duplicate_keys = int(
            source.duplicated(["estate", "observation_date"], keep=False).sum()
        )
        if duplicate_keys:
            raise ValueError(
                f"Input has {duplicate_keys} rows with duplicate estate-date keys."
            )

        log("Building dense calendar and leakage-safe history features")
        dense, gap_profile = build_dense_history(
            source,
            zero_fill_max_gap=args.zero_fill_max_gap,
            min_history_days=args.min_history_days,
        )
        estate_categories = sorted(source["estate"].unique().tolist())
        log(
            f"Dense rows={len(dense):,}; short-gap zeros="
            f"{gap_profile['short_gap_zero_rows']:,}; unresolved long gaps="
            f"{gap_profile['unresolved_long_gap_rows']:,}"
        )

        log(f"Building direct horizons 1-{args.max_horizon}")
        supervised = build_supervised(
            dense,
            max_horizon=args.max_horizon,
            estate_categories=estate_categories,
        )
        maximum_target_date = supervised["target_date"].max()
        train, validation, test, validation_start, test_start = split_supervised(
            supervised,
            maximum_target_date=maximum_target_date,
            window_days=args.split_window_days,
        )
        profile = DataProfile(
            source_rows=int(len(source)),
            source_estates=int(source["estate"].nunique()),
            source_unique_dates=int(source["observation_date"].nunique()),
            source_min_date=source["observation_date"].min().date().isoformat(),
            source_max_date=source["observation_date"].max().date().isoformat(),
            source_total_kg=float(source["actual_green_leaf_kg"].sum()),
            duplicate_estate_date_keys=duplicate_keys,
            dense_rows=int(len(dense)),
            observed_rows=int(gap_profile["observed_rows"]),
            short_gap_zero_rows=int(gap_profile["short_gap_zero_rows"]),
            unresolved_long_gap_rows=int(
                gap_profile["unresolved_long_gap_rows"]
            ),
            supervised_rows=int(len(supervised)),
            train_rows_before_sampling=int(len(train)),
            validation_rows_before_sampling=int(len(validation)),
            test_rows_before_sampling=int(len(test)),
            validation_target_start=validation_start.date().isoformat(),
            test_target_start=test_start.date().isoformat(),
            test_target_end=maximum_target_date.date().isoformat(),
        )
        write_json(
            output_root / "data_profile.json",
            {
                **asdict(profile),
                **gap_profile,
                "approved_source_profile": approved_profile,
                "source_sha256": sha256_file(args.input),
                "estate_categories": estate_categories,
            },
        )
        log(
            f"Supervised rows={len(supervised):,}; train={len(train):,}; "
            f"validation={len(validation):,}; test={len(test):,}"
        )

        if args.validate_only:
            manifest.update(
                {
                    "status": "VALIDATION_COMPLETED",
                    "completed_at_utc": utc_now(),
                    "advanced_model_training_started": False,
                    "next_gate": "RUN_PHASE5_GPU_BASELINE",
                }
            )
            write_json(manifest_path, manifest)
            log(f"Validation completed: {output_root}")
            return 0

        feature_columns = base_feature_columns()
        train_sample = deterministic_sample(
            train, args.max_train_rows, args.seed
        )
        validation_sample = deterministic_sample(
            validation, args.max_eval_rows, args.seed + 1
        )
        test_sample = deterministic_sample(test, args.max_eval_rows, args.seed + 2)
        log(
            f"Training samples: train={len(train_sample):,}; "
            f"validation={len(validation_sample):,}; test={len(test_sample):,}"
        )

        x_train = make_features(train_sample, feature_columns)
        y_train = train_sample["target_kg"].astype("float32")
        x_validation = make_features(validation_sample, feature_columns)
        y_validation = validation_sample["target_kg"].astype("float32")

        manifest["advanced_model_training_started"] = True
        manifest["training_started_at_utc"] = utc_now()
        write_json(manifest_path, manifest)

        log("Training evaluation P50 model")
        p50_evaluation = train_quantile_model(
            x_train,
            y_train,
            x_validation,
            y_validation,
            alpha=0.5,
            device=args.device,
            estimators=args.estimators,
            seed=args.seed,
            early_stopping_rounds=args.early_stopping_rounds,
            verbose=True,
        )
        log("Training evaluation P90 model")
        p90_evaluation = train_quantile_model(
            x_train,
            y_train,
            x_validation,
            y_validation,
            alpha=0.9,
            device=args.device,
            estimators=args.estimators,
            seed=args.seed + 10,
            early_stopping_rounds=args.early_stopping_rounds,
            verbose=True,
        )
        model_folder = output_root / "models"
        model_folder.mkdir(parents=True, exist_ok=True)
        p50_evaluation.save_model(model_folder / "evaluation_p50.json")
        p90_evaluation.save_model(model_folder / "evaluation_p90.json")

        x_test = make_features(test_sample, feature_columns)
        p50_prediction = np.clip(
            predict_with_explicit_dmatrix(p50_evaluation, x_test),
            0.0,
            None,
        )
        p90_prediction = np.maximum(
            np.clip(
                predict_with_explicit_dmatrix(p90_evaluation, x_test),
                0.0,
                None,
            ),
            p50_prediction,
        )
        naive_prediction = seasonal_naive(test_sample)
        metrics = evaluate_predictions(
            test_sample,
            p50=p50_prediction,
            p90=p90_prediction,
            naive=naive_prediction,
        )
        metrics["model_scope"] = manifest["model_scope"]
        metrics["test_target_start"] = test_start.date().isoformat()
        metrics["test_target_end"] = maximum_target_date.date().isoformat()
        metrics["best_iteration_p50"] = int(
            getattr(p50_evaluation, "best_iteration", args.estimators - 1)
        )
        metrics["best_iteration_p90"] = int(
            getattr(p90_evaluation, "best_iteration", args.estimators - 1)
        )
        write_json(output_root / "backtest_metrics.json", metrics)

        predictions = test_sample[
            ["estate", "origin_date", "target_date", "horizon", "target_kg"]
        ].copy()
        predictions["p50_kg"] = p50_prediction
        predictions["p90_kg"] = p90_prediction
        predictions["seasonal_naive_kg"] = naive_prediction
        predictions.to_csv(
            output_root / "test_predictions_sample.csv", index=False
        )
        log(
            "Backtest P50 WAPE="
            f"{metrics['overall']['p50_wape_pct']:.2f}%; "
            "naive WAPE="
            f"{metrics['overall']['naive_wape_pct']:.2f}%; "
            "P90 coverage="
            f"{metrics['overall']['p90_empirical_coverage_pct']:.2f}%"
        )

        final_p50 = p50_evaluation
        final_p90 = p90_evaluation
        final_refit = not args.no_final_refit
        if final_refit:
            full_sample = deterministic_sample(
                supervised, args.max_final_rows, args.seed + 3
            )
            x_full = make_features(full_sample, feature_columns)
            y_full = full_sample["target_kg"].astype("float32")
            p50_estimators = max(
                50,
                int(getattr(p50_evaluation, "best_iteration", 299)) + 1,
            )
            p90_estimators = max(
                50,
                int(getattr(p90_evaluation, "best_iteration", 299)) + 1,
            )
            log(
                f"Refitting final models on {len(full_sample):,} rows "
                f"(P50 trees={p50_estimators}, P90 trees={p90_estimators})"
            )
            final_p50 = train_quantile_model(
                x_full,
                y_full,
                None,
                None,
                alpha=0.5,
                device=args.device,
                estimators=p50_estimators,
                seed=args.seed + 20,
                early_stopping_rounds=None,
                verbose=False,
            )
            final_p90 = train_quantile_model(
                x_full,
                y_full,
                None,
                None,
                alpha=0.9,
                device=args.device,
                estimators=p90_estimators,
                seed=args.seed + 30,
                early_stopping_rounds=None,
                verbose=False,
            )
            final_p50.save_model(model_folder / "final_p50.json")
            final_p90.save_model(model_folder / "final_p90.json")

        log("Generating 90-day estate forecasts")
        future = make_future_frame(
            dense,
            estate_categories=estate_categories,
            max_horizon=args.max_horizon,
        )
        x_future = make_features(future, feature_columns)
        future_p50 = np.clip(
            predict_with_explicit_dmatrix(final_p50, x_future),
            0.0,
            None,
        )
        future_p90 = np.maximum(
            np.clip(
                predict_with_explicit_dmatrix(final_p90, x_future),
                0.0,
                None,
            ),
            future_p50,
        )
        forecast = future[
            ["estate", "forecast_origin", "forecast_date", "horizon"]
        ].copy()
        forecast["p50_expected_green_leaf_kg"] = future_p50
        forecast["p90_conservative_upper_green_leaf_kg"] = future_p90
        forecast["model_scope"] = "HISTORY_ONLY_BASELINE"
        forecast["operational_use_allowed"] = False
        forecast.to_csv(
            output_root / "estate_90_day_p50_p90_forecast.csv", index=False
        )

        manifest.update(
            {
                "status": "PHASE5_HISTORY_ONLY_GPU_BASELINE_COMPLETED",
                "completed_at_utc": utc_now(),
                "device_used": args.device,
                "source_rows": int(len(source)),
                "source_estates": int(source["estate"].nunique()),
                "supervised_rows": int(len(supervised)),
                "forecast_rows": int(len(forecast)),
                "final_refit_completed": final_refit,
                "backtest_metrics_file": str(
                    output_root / "backtest_metrics.json"
                ),
                "forecast_file": str(
                    output_root / "estate_90_day_p50_p90_forecast.csv"
                ),
                "next_gate": (
                    "REVIEW_BACKTEST_AND_COMPARE_WITH_PROJECT_A_BEFORE_WEATHER_"
                    "AND_SPATIOTEMPORAL_UPGRADES"
                ),
                "operational_use_allowed": False,
                "live_assignment_authorized": False,
            }
        )
        write_json(manifest_path, manifest)
        log(f"Phase 5 completed: {output_root}")
        return 0
    except Exception as exc:
        manifest.update(
            {
                "status": "FAILED",
                "failed_at_utc": utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "operational_use_allowed": False,
                "live_assignment_authorized": False,
            }
        )
        write_json(manifest_path, manifest)
        log(f"FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
