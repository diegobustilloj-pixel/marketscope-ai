from __future__ import annotations

import json
import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from polymarket_bot.phase4 import _taker_cost_per_share
from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v027_final_postmortem import (
    contribution_diagnostics,
    descriptive_distribution,
)
from polymarket_bot.v029_audit import (
    RESULT_SCHEMA,
    _build_model,
    holdout_metrics,
    load_holdout_rows,
)
from polymarket_bot.v029_forward import open_read_only, parse_utc
from polymarket_bot.v029_prereg import load_and_verify_frozen_prereg
from polymarket_bot.v029_strategy import MODEL_FEATURES, select_side


POSTMORTEM_SCHEMA = "postmortem_v029_validation_economics_final_1"
REQUIRED_VERDICT = "FAIL_VALIDATION_ECONOMICS"
VALIDATION_BLOCKS = (
    "h12_to_h16",
    "h16_to_h20",
    "h20_to_h24",
)
SIDES = ("Up", "Down")
FAVORITE_ALIGNMENTS = ("selected_favorite", "selected_underdog")
ENTRY_COST_BANDS = (
    "cost_lt_025",
    "cost_ge_025_lt_050",
    "cost_ge_050_lt_075",
    "cost_ge_075",
)
SELECTED_PROBABILITY_BANDS = (
    "p_selected_lt_050",
    "p_selected_ge_050_lt_065",
    "p_selected_ge_065_lt_080",
    "p_selected_ge_080",
)
EXPECTED_EDGE_BANDS = (
    "edge_gt_000_le_001",
    "edge_gt_001_le_003",
    "edge_gt_003_le_005",
    "edge_gt_005",
)
SIGNED_TWAP_BANDS = (
    "twap_lt_neg10",
    "twap_ge_neg10_lt_neg5",
    "twap_ge_neg5_lt_0",
    "twap_ge_0_lt_5",
    "twap_ge_5_lt_10",
    "twap_ge_10",
    "unavailable",
)
DIRECTION_ALIGNMENTS = (
    "direction_aligned",
    "neutral_abs_le_1bps",
    "direction_opposed",
    "unavailable",
)
VOLATILITY_REGIMES = (
    "vol_ratio_low_lt_075",
    "vol_ratio_normal_ge_075_lt_125",
    "vol_ratio_high_ge_125",
    "unavailable",
)
PROBABILITY_UP_BANDS = (
    "p_up_ge_000_lt_020",
    "p_up_ge_020_lt_040",
    "p_up_ge_040_lt_060",
    "p_up_ge_060_lt_080",
    "p_up_ge_080_le_100",
)


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def entry_cost_band(value: float) -> str:
    cost = float(value)
    if cost < 0.25:
        return "cost_lt_025"
    if cost < 0.50:
        return "cost_ge_025_lt_050"
    if cost < 0.75:
        return "cost_ge_050_lt_075"
    return "cost_ge_075"


def selected_probability_band(value: float) -> str:
    probability = float(value)
    if probability < 0.50:
        return "p_selected_lt_050"
    if probability < 0.65:
        return "p_selected_ge_050_lt_065"
    if probability < 0.80:
        return "p_selected_ge_065_lt_080"
    return "p_selected_ge_080"


def expected_edge_band(value: float) -> str:
    edge = float(value)
    if not edge > 0.0:
        raise ValueError("Una señal V0.29 no puede tener EV no positivo")
    if edge <= 0.01:
        return "edge_gt_000_le_001"
    if edge <= 0.03:
        return "edge_gt_001_le_003"
    if edge <= 0.05:
        return "edge_gt_003_le_005"
    return "edge_gt_005"


def signed_twap_band(value: float | None) -> str:
    if value is None:
        return "unavailable"
    distance = float(value)
    if distance < -10.0:
        return "twap_lt_neg10"
    if distance < -5.0:
        return "twap_ge_neg10_lt_neg5"
    if distance < 0.0:
        return "twap_ge_neg5_lt_0"
    if distance < 5.0:
        return "twap_ge_0_lt_5"
    if distance < 10.0:
        return "twap_ge_5_lt_10"
    return "twap_ge_10"


def direction_alignment(value: float | None, selected_side: str) -> str:
    if value is None:
        return "unavailable"
    signed = float(value) * (1.0 if selected_side == "Up" else -1.0)
    if abs(float(value)) <= 1.0:
        return "neutral_abs_le_1bps"
    return "direction_aligned" if signed > 0.0 else "direction_opposed"


def volatility_regime(value: float | None) -> str:
    if value is None:
        return "unavailable"
    ratio = float(value)
    if ratio < 0.75:
        return "vol_ratio_low_lt_075"
    if ratio < 1.25:
        return "vol_ratio_normal_ge_075_lt_125"
    return "vol_ratio_high_ge_125"


def probability_up_band(value: float) -> str:
    probability = float(value)
    if probability < 0.20:
        return "p_up_ge_000_lt_020"
    if probability < 0.40:
        return "p_up_ge_020_lt_040"
    if probability < 0.60:
        return "p_up_ge_040_lt_060"
    if probability < 0.80:
        return "p_up_ge_060_lt_080"
    return "p_up_ge_080_le_100"


def validation_block(elapsed_hours: float) -> str | None:
    elapsed = float(elapsed_hours)
    if 12.0 <= elapsed < 16.0:
        return "h12_to_h16"
    if 16.0 <= elapsed < 20.0:
        return "h16_to_h20"
    if 20.0 <= elapsed <= 24.1:
        return "h20_to_h24"
    return None


def _pnl(row: Mapping[str, Any], *, side_key: str = "selected_side", cost_key: str = "entry_cost") -> float:
    payout = 1.0 if str(row[side_key]) == str(row["label"]) else 0.0
    return payout - float(row[cost_key])


def _mean(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 8) if values else None


def _segment_profile(
    rows: Sequence[Mapping[str, Any]],
    *,
    all_signal_count: int,
) -> dict[str, Any]:
    metrics = holdout_metrics(rows)
    total_cost = sum(float(row["entry_cost"]) for row in rows)
    raw_ask_cost = sum(float(row["selected_raw_ask"]) for row in rows)
    fill_cost = sum(float(row["selected_fill_price"]) for row in rows)
    fees = sum(float(row["selected_modeled_fee"]) for row in rows)
    payouts = sum(
        1.0 if str(row["selected_side"]) == str(row["label"]) else 0.0
        for row in rows
    )
    net_before_execution = payouts - raw_ask_cost
    execution_drag = total_cost - raw_ask_cost
    candidate_minus_control = sum(
        float(row["candidate_minus_control_pnl_per_share"]) for row in rows
    )
    selected_probabilities = [float(row["selected_probability"]) for row in rows]
    expected_edges = [float(row["expected_pnl_per_share"]) for row in rows]
    realized_win_rate = metrics["win_rate"]
    average_selected_probability = _mean(selected_probabilities)
    return {
        "metrics": metrics,
        "share_of_candidate_signals": (
            round(len(rows) / all_signal_count, 8) if all_signal_count else 0.0
        ),
        "average_entry_cost": _mean([float(row["entry_cost"]) for row in rows]),
        "average_selected_probability": average_selected_probability,
        "average_expected_pnl_per_share": _mean(expected_edges),
        "realized_minus_predicted_win_rate": (
            round(float(realized_win_rate) - float(average_selected_probability), 8)
            if realized_win_rate is not None and average_selected_probability is not None
            else None
        ),
        "cost_decomposition": {
            "raw_ask_cost_per_share_sequence": round(raw_ask_cost, 8),
            "modeled_fill_cost_per_share_sequence": round(fill_cost, 8),
            "modeled_fee_per_share_sequence": round(fees, 8),
            "all_in_entry_cost_per_share_sequence": round(total_cost, 8),
            "settlement_payout_per_share_sequence": round(payouts, 8),
            "net_before_modeled_execution_cost_per_share": round(
                net_before_execution, 8
            ),
            "net_before_modeled_execution_cost_at_5_shares": round(
                net_before_execution * 5.0, 8
            ),
            "modeled_execution_drag_per_share": round(execution_drag, 8),
            "modeled_execution_drag_at_5_shares": round(execution_drag * 5.0, 8),
            "loss_present_before_modeled_execution_cost": net_before_execution < 0.0,
        },
        "candidate_minus_paired_control": {
            "net_pnl_per_share": round(candidate_minus_control, 8),
            "net_pnl_at_5_shares": round(candidate_minus_control * 5.0, 8),
        },
    }


def _grouped_profiles(
    rows: Sequence[Mapping[str, Any]],
    *,
    labels: Sequence[str],
    key: Callable[[Mapping[str, Any]], str | None],
) -> dict[str, Any]:
    return {
        label: _segment_profile(
            [row for row in rows if key(row) == label],
            all_signal_count=len(rows),
        )
        for label in labels
    }


def _assert_metrics(calculated: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    keys = (
        "trades",
        "wins",
        "win_rate",
        "net_pnl_per_share_sequence",
        "net_pnl_at_5_shares",
        "mean_pnl_per_share",
        "profit_factor",
        "roi_on_cost",
        "maximum_drawdown_per_share",
        "one_sided_95_lcb",
        "net_without_best_trade_per_share",
        "net_without_best_trade_at_5_shares",
    )
    for key in keys:
        if calculated.get(key) != expected.get(key):
            raise RuntimeError(f"Postmortem V0.29 no reproduce la métrica: {key}")


def _assert_partition(
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    expected_count: int,
    expected_net: float,
) -> None:
    count = sum(int(profile["metrics"]["trades"]) for profile in profiles.values())
    net = sum(
        float(profile["metrics"]["net_pnl_per_share_sequence"])
        for profile in profiles.values()
    )
    if count != expected_count or not math.isclose(net, expected_net, abs_tol=1e-7):
        raise RuntimeError("Una partición fija V0.29 no conserva conteo y PnL")


def _load_feature_map(database: Path) -> dict[str, dict[str, Any]]:
    connection = open_read_only(database)
    try:
        return {
            str(row["condition_id"]): json.loads(str(row["feature_json"]))
            for row in connection.execute(
                "SELECT condition_id,feature_json FROM v029_features"
            )
        }
    finally:
        connection.close()


def reconstruct_validation(
    *,
    database: str | Path,
    result: Mapping[str, Any],
    prereg: Mapping[str, Any],
) -> dict[str, Any]:
    database_path = Path(database).resolve()
    start = parse_utc(str(result["window"]["experiment_started_at"]))
    observation_end = parse_utc(str(result["window"]["observation_ended_at"]))
    training, validation = load_holdout_rows(
        database=database_path,
        start=start,
        observation_end=observation_end,
    )
    model = _build_model(prereg["candidate"]["model"])
    training_matrix = np.asarray(
        [
            [np.nan if value is None else float(value) for value in row["features"]]
            for row in training
        ],
        dtype=float,
    )
    targets = np.asarray([int(row["target_up"]) for row in training])
    model.fit(training_matrix, targets)
    validation_matrix = np.asarray(
        [
            [np.nan if value is None else float(value) for value in row["features"]]
            for row in validation
        ],
        dtype=float,
    )
    probabilities = model.predict_proba(validation_matrix)[:, 1]
    features_by_condition = _load_feature_map(database_path)
    start_ms = int(start.timestamp() * 1000)
    candidate_rows: list[dict[str, Any]] = []
    control_rows: list[dict[str, Any]] = []
    prediction_rows: list[dict[str, Any]] = []
    abstentions: list[dict[str, Any]] = []
    for row, probability_up in zip(validation, probabilities, strict=True):
        condition_id = str(row["condition_id"])
        feature = features_by_condition[condition_id]
        probability = float(probability_up)
        implied = float(row["implied_up_mid_probability"])
        up_ev = probability - float(row["up_entry_cost"])
        down_ev = 1.0 - probability - float(row["down_entry_cost"])
        prediction_rows.append(
            {
                "condition_id": condition_id,
                "market_start_ms": int(row["market_start_ms"]),
                "probability_up": probability,
                "implied_up_mid_probability": implied,
                "target_up": int(row["target_up"]),
            }
        )
        selected = select_side(
            probability_up=probability,
            up_entry_cost=float(row["up_entry_cost"]),
            down_entry_cost=float(row["down_entry_cost"]),
        )
        if selected is None:
            abstentions.append(
                {
                    "condition_id": condition_id,
                    "reason": (
                        "both_expected_values_nonpositive"
                        if up_ev <= 0.0 and down_ev <= 0.0
                        else "equal_expected_value_tie"
                    ),
                    "up_expected_pnl_per_share": round(up_ev, 12),
                    "down_expected_pnl_per_share": round(down_ev, 12),
                }
            )
            continue
        selected_side = str(selected["side"])
        selected_ask = float(
            feature["up_best_ask"]
            if selected_side == "Up"
            else feature["down_best_ask"]
        )
        all_in_cost, fill_price, fee = _taker_cost_per_share(
            selected_ask,
            fee_rate=0.07,
            slippage_per_share=0.005,
        )
        if not math.isclose(
            all_in_cost,
            float(selected["entry_cost"]),
            abs_tol=1e-12,
        ):
            raise RuntimeError("Coste reconstruido V0.29 no coincide")
        favorite = "Up" if implied > 0.5 else "Down"
        control_cost = float(
            row["up_entry_cost"] if favorite == "Up" else row["down_entry_cost"]
        )
        control_row = {
            "condition_id": condition_id,
            "market_start_ms": int(row["market_start_ms"]),
            "label": str(row["label"]),
            "selected_side": favorite,
            "entry_cost": control_cost,
        }
        control_rows.append(control_row)
        candidate_pnl = (
            (1.0 if selected_side == str(row["label"]) else 0.0) - all_in_cost
        )
        control_pnl = _pnl(control_row)
        elapsed_hours = (int(row["market_start_ms"]) - start_ms) / 3_600_000
        selected_probability = (
            probability if selected_side == "Up" else 1.0 - probability
        )
        twap_distance = feature.get("twap_distance_to_open_bps")
        binance_return = feature.get("binance_return_60s_bps")
        volatility = feature.get("volatility_regime_ratio")
        candidate_rows.append(
            {
                "condition_id": condition_id,
                "market_start_ms": int(row["market_start_ms"]),
                "label": str(row["label"]),
                "selected_side": selected_side,
                "favorite_side": selected_side,
                "market_favorite_side": favorite,
                "favorite_alignment": (
                    "selected_favorite"
                    if selected_side == favorite
                    else "selected_underdog"
                ),
                "entry_cost": all_in_cost,
                "selected_raw_ask": selected_ask,
                "selected_fill_price": fill_price,
                "selected_modeled_fee": fee,
                "selected_modeled_slippage": fill_price - selected_ask,
                "expected_pnl_per_share": float(
                    selected["expected_pnl_per_share"]
                ),
                "probability_up": probability,
                "selected_probability": selected_probability,
                "implied_up_mid_probability": implied,
                "twap_distance_to_open_bps": (
                    float(twap_distance) if twap_distance is not None else None
                ),
                "binance_return_60s_bps": (
                    float(binance_return) if binance_return is not None else None
                ),
                "volatility_regime_ratio": (
                    float(volatility) if volatility is not None else None
                ),
                "elapsed_hours": elapsed_hours,
                "validation_block": validation_block(elapsed_hours),
                "candidate_pnl_per_share": candidate_pnl,
                "paired_control_side": favorite,
                "paired_control_entry_cost": control_cost,
                "paired_control_pnl_per_share": control_pnl,
                "candidate_minus_control_pnl_per_share": (
                    candidate_pnl - control_pnl
                ),
            }
        )
    estimator = model.named_steps["model"]
    model_artifact = {
        "raw_pipeline_feature_names": list(MODEL_FEATURES),
        "transformed_coefficient_count": int(estimator.coef_.shape[1]),
        "coefficients": [round(float(value), 12) for value in estimator.coef_[0]],
        "intercept": round(float(estimator.intercept_[0]), 12),
    }
    if model_artifact != result["holdout"]["model_artifact"]:
        raise RuntimeError("El modelo reconstruido V0.29 no coincide con el auditor")
    return {
        "training": training,
        "validation": validation,
        "candidate_rows": candidate_rows,
        "control_rows": control_rows,
        "prediction_rows": prediction_rows,
        "abstentions": abstentions,
        "model_artifact": model_artifact,
    }


def _probability_metrics(
    predictions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    epsilon = 1e-12

    def metrics(key: str) -> dict[str, Any]:
        probabilities = [float(row[key]) for row in predictions]
        targets = [int(row["target_up"]) for row in predictions]
        brier = statistics.fmean(
            (probability - target) ** 2
            for probability, target in zip(probabilities, targets, strict=True)
        )
        log_loss = -statistics.fmean(
            target * math.log(min(1.0 - epsilon, max(epsilon, probability)))
            + (1 - target)
            * math.log(min(1.0 - epsilon, max(epsilon, 1.0 - probability)))
            for probability, target in zip(probabilities, targets, strict=True)
        )
        accuracy = statistics.fmean(
            int((probability > 0.5) == bool(target))
            for probability, target in zip(probabilities, targets, strict=True)
        )
        return {
            "brier_score": round(brier, 8),
            "log_loss": round(log_loss, 8),
            "directional_accuracy": round(accuracy, 8),
        }

    model = metrics("probability_up")
    market = metrics("implied_up_mid_probability")
    calibration: dict[str, Any] = {}
    for label in PROBABILITY_UP_BANDS:
        rows = [
            row
            for row in predictions
            if probability_up_band(float(row["probability_up"])) == label
        ]
        probabilities = [float(row["probability_up"]) for row in rows]
        targets = [int(row["target_up"]) for row in rows]
        mean_probability = _mean(probabilities)
        observed = _mean([float(value) for value in targets])
        calibration[label] = {
            "rows": len(rows),
            "mean_probability_up": mean_probability,
            "observed_up_rate": observed,
            "observed_minus_predicted": (
                round(float(observed) - float(mean_probability), 8)
                if observed is not None and mean_probability is not None
                else None
            ),
        }
    return {
        "rows": len(predictions),
        "model": model,
        "market_implied_probability": market,
        "model_minus_market": {
            "brier_score": round(
                float(model["brier_score"]) - float(market["brier_score"]), 8
            ),
            "log_loss": round(
                float(model["log_loss"]) - float(market["log_loss"]), 8
            ),
            "directional_accuracy": round(
                float(model["directional_accuracy"])
                - float(market["directional_accuracy"]),
                8,
            ),
            "negative_score_difference_is_better": True,
        },
        "fixed_probability_calibration_bands": calibration,
    }


def _feature_shift(
    training: Sequence[Mapping[str, Any]],
    validation: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for index, name in enumerate(MODEL_FEATURES):
        training_values = [
            float(row["features"][index])
            for row in training
            if row["features"][index] is not None
        ]
        validation_values = [
            float(row["features"][index])
            for row in validation
            if row["features"][index] is not None
        ]
        train_mean = statistics.fmean(training_values) if training_values else None
        validation_mean = (
            statistics.fmean(validation_values) if validation_values else None
        )
        pooled_variance = (
            (
                statistics.pvariance(training_values)
                + statistics.pvariance(validation_values)
            )
            / 2.0
            if training_values and validation_values
            else None
        )
        standardized_shift = (
            (validation_mean - train_mean) / math.sqrt(pooled_variance)
            if train_mean is not None
            and validation_mean is not None
            and pooled_variance is not None
            and pooled_variance > 0.0
            else None
        )
        output[name] = {
            "training": descriptive_distribution(training_values),
            "validation": descriptive_distribution(validation_values),
            "training_missing": len(training) - len(training_values),
            "validation_missing": len(validation) - len(validation_values),
            "validation_minus_training_mean": (
                round(validation_mean - train_mean, 8)
                if train_mean is not None and validation_mean is not None
                else None
            ),
            "standardized_mean_shift": (
                round(standardized_shift, 8)
                if standardized_shift is not None
                else None
            ),
        }
    return output


def build_v029_final_postmortem(
    *,
    result_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    result_file = Path(result_path).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    result = json.loads(result_file.read_text(encoding="utf-8"))
    if result.get("schema") != RESULT_SCHEMA:
        raise RuntimeError("Resultado final V0.29 incompatible")
    if result.get("verdict") != REQUIRED_VERDICT:
        raise RuntimeError("El postmortem requiere el fallo económico V0.29")
    if result.get("selected_strategy") is not None:
        raise RuntimeError("V0.29 no puede cerrar con estrategia seleccionada")
    database = Path(str(result["database"])).resolve()
    preregistration = Path(str(result["preregistration"])).resolve()
    implementation = Path(str(result["implementation"])).resolve()
    generator = Path(__file__).resolve()
    source_hashes = {
        "result": sha256_file(result_file),
        "database": sha256_file(database),
        "preregistration": sha256_file(preregistration),
        "implementation": sha256_file(implementation),
    }
    expected_hashes = {
        "database": result.get("database_sha256"),
        "preregistration": result.get("preregistration_sha256"),
        "implementation": result.get("implementation_sha256"),
    }
    for key, expected in expected_hashes.items():
        if source_hashes[key] != expected:
            raise RuntimeError(f"Evidencia sellada V0.29 no coincide: {key}")
    generator_hash = sha256_file(generator)
    if output is not None and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == POSTMORTEM_SCHEMA
            and existing.get("source_hashes") == source_hashes
            and existing.get("generator_sha256") == generator_hash
        ):
            return existing
        raise RuntimeError("Existe otro postmortem final V0.29")

    prereg = load_and_verify_frozen_prereg(preregistration)
    reconstructed = reconstruct_validation(
        database=database,
        result=result,
        prereg=prereg,
    )
    training = reconstructed["training"]
    validation = reconstructed["validation"]
    candidate_rows = reconstructed["candidate_rows"]
    control_rows = reconstructed["control_rows"]
    candidate_metrics = holdout_metrics(candidate_rows)
    control_metrics = holdout_metrics(control_rows)
    audited = result["holdout"]
    _assert_metrics(candidate_metrics, audited["candidate_metrics"])
    _assert_metrics(control_metrics, audited["paired_control_metrics"])

    fixed_segments = {
        "selected_side": _grouped_profiles(
            candidate_rows,
            labels=SIDES,
            key=lambda row: str(row["selected_side"]),
        ),
        "favorite_alignment": _grouped_profiles(
            candidate_rows,
            labels=FAVORITE_ALIGNMENTS,
            key=lambda row: str(row["favorite_alignment"]),
        ),
        "entry_cost_bands": _grouped_profiles(
            candidate_rows,
            labels=ENTRY_COST_BANDS,
            key=lambda row: entry_cost_band(float(row["entry_cost"])),
        ),
        "selected_probability_bands": _grouped_profiles(
            candidate_rows,
            labels=SELECTED_PROBABILITY_BANDS,
            key=lambda row: selected_probability_band(
                float(row["selected_probability"])
            ),
        ),
        "expected_edge_bands": _grouped_profiles(
            candidate_rows,
            labels=EXPECTED_EDGE_BANDS,
            key=lambda row: expected_edge_band(
                float(row["expected_pnl_per_share"])
            ),
        ),
        "signed_twap_distance_bands": _grouped_profiles(
            candidate_rows,
            labels=SIGNED_TWAP_BANDS,
            key=lambda row: signed_twap_band(row.get("twap_distance_to_open_bps")),
        ),
        "twap_selected_direction_alignment": _grouped_profiles(
            candidate_rows,
            labels=DIRECTION_ALIGNMENTS,
            key=lambda row: direction_alignment(
                row.get("twap_distance_to_open_bps"),
                str(row["selected_side"]),
            ),
        ),
        "binance_selected_direction_alignment": _grouped_profiles(
            candidate_rows,
            labels=DIRECTION_ALIGNMENTS,
            key=lambda row: direction_alignment(
                row.get("binance_return_60s_bps"),
                str(row["selected_side"]),
            ),
        ),
        "volatility_regimes": _grouped_profiles(
            candidate_rows,
            labels=VOLATILITY_REGIMES,
            key=lambda row: volatility_regime(row.get("volatility_regime_ratio")),
        ),
        "validation_four_hour_blocks": _grouped_profiles(
            candidate_rows,
            labels=VALIDATION_BLOCKS,
            key=lambda row: (
                str(row["validation_block"])
                if row.get("validation_block") is not None
                else None
            ),
        ),
    }
    expected_net = float(candidate_metrics["net_pnl_per_share_sequence"])
    for profiles in fixed_segments.values():
        _assert_partition(
            profiles,
            expected_count=len(candidate_rows),
            expected_net=expected_net,
        )
    worst_segments = {
        family: min(
            (
                {
                    "segment": segment,
                    "trades": int(profile["metrics"]["trades"]),
                    "net_pnl_at_5_shares": profile["metrics"][
                        "net_pnl_at_5_shares"
                    ],
                }
                for segment, profile in profiles.items()
                if int(profile["metrics"]["trades"]) > 0
            ),
            key=lambda item: float(item["net_pnl_at_5_shares"]),
        )
        for family, profiles in fixed_segments.items()
    }
    overall_profile = _segment_profile(
        candidate_rows,
        all_signal_count=len(candidate_rows),
    )
    probability_diagnostics = _probability_metrics(
        reconstructed["prediction_rows"]
    )
    abstention_reasons: dict[str, int] = {}
    for row in reconstructed["abstentions"]:
        reason = str(row["reason"])
        abstention_reasons[reason] = abstention_reasons.get(reason, 0) + 1
    contribution = contribution_diagnostics(candidate_rows)
    paired_difference_at_5 = round(
        (
            float(candidate_metrics["net_pnl_per_share_sequence"])
            - float(control_metrics["net_pnl_per_share_sequence"])
        )
        * 5.0,
        8,
    )
    payload = {
        "schema": POSTMORTEM_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "closed_read_only_exploratory_no_new_backtest_no_promotion",
        "source_files": {
            "result": str(result_file),
            "database": str(database),
            "preregistration": str(preregistration),
            "implementation": str(implementation),
            "generator": str(generator),
        },
        "source_hashes": source_hashes,
        "generator_sha256": generator_hash,
        "source_verdict": result["verdict"],
        "source_completion_reason": result["window"]["completion_reason"],
        "reproduction": {
            "training_rows": len(training),
            "validation_rows": len(validation),
            "candidate_signals": len(candidate_rows),
            "abstentions": len(reconstructed["abstentions"]),
            "abstention_reasons": abstention_reasons,
            "candidate_metrics": candidate_metrics,
            "paired_control_metrics": control_metrics,
            "model_artifact": reconstructed["model_artifact"],
            "auditor_reproduced_exactly": True,
        },
        "candidate_overall": overall_profile,
        "paired_control": {
            "metrics": control_metrics,
            "candidate_minus_control_mean_pnl": audited[
                "candidate_minus_control_mean_pnl"
            ],
            "candidate_minus_control_net_pnl_at_5_shares": (
                paired_difference_at_5
            ),
        },
        "candidate_contribution_diagnostics": contribution,
        "validation_probability_diagnostics": probability_diagnostics,
        "training_to_validation_feature_shift": _feature_shift(
            training,
            validation,
        ),
        "fixed_segment_definitions": {
            "selected_side": list(SIDES),
            "favorite_alignment": list(FAVORITE_ALIGNMENTS),
            "entry_cost_bands": list(ENTRY_COST_BANDS),
            "selected_probability_bands": list(SELECTED_PROBABILITY_BANDS),
            "expected_edge_bands": list(EXPECTED_EDGE_BANDS),
            "signed_twap_distance_bands": list(SIGNED_TWAP_BANDS),
            "direction_alignments_deadband_abs_bps": 1.0,
            "volatility_regimes": list(VOLATILITY_REGIMES),
            "validation_four_hour_blocks": list(VALIDATION_BLOCKS),
            "definitions_fixed_before_segment_outcomes_were_computed": True,
        },
        "fixed_segment_diagnostics": fixed_segments,
        "worst_net_segment_by_family": worst_segments,
        "decisive_findings": {
            "frequency_problem_solved": (
                bool(audited["validation_frequency_passed"])
                and len(candidate_rows) >= 20
            ),
            "candidate_net_pnl_at_5_shares": candidate_metrics[
                "net_pnl_at_5_shares"
            ],
            "paired_control_net_pnl_at_5_shares": control_metrics[
                "net_pnl_at_5_shares"
            ],
            "candidate_worse_than_control": (
                float(audited["candidate_minus_control_mean_pnl"]) < 0.0
            ),
            "loss_present_before_modeled_execution_cost": overall_profile[
                "cost_decomposition"
            ]["loss_present_before_modeled_execution_cost"],
            "model_brier_worse_than_market": (
                float(probability_diagnostics["model_minus_market"]["brier_score"])
                > 0.0
            ),
            "model_log_loss_worse_than_market": (
                float(probability_diagnostics["model_minus_market"]["log_loss"])
                > 0.0
            ),
            "net_remains_negative_without_best_trade": (
                float(contribution["net_without_best_trade_per_share"]) < 0.0
            ),
            "all_audited_economic_gates_failed": not any(
                bool(value) for value in audited["economic_gates"].values()
            ),
            "worst_segments_are_descriptive_not_selection_rules": True,
        },
        "posthoc_controls": {
            "outcomes_were_available_for_this_postmortem": True,
            "multiple_fixed_segments_examined": sum(
                len(profiles) for profiles in fixed_segments.values()
            ),
            "segment_selection_allowed": False,
            "threshold_retuning_allowed": False,
            "validation_evidence_reusable_as_fresh_holdout": False,
            "generated_hypothesis": None,
        },
        "branch_decision": {
            "v029": "CLOSED_REJECTED_BY_VALIDATION_ECONOMICS",
            "cost_aware_logistic_temporal_holdout": "DISCARD",
            "direct_segment_or_threshold_retune": "PROHIBITED_OVERFIT_RISK",
            "selected_strategy": None,
            "paper_forward_candidate": False,
            "money_real_candidate": False,
            "recommended_next_work": (
                "INDEPENDENT_HYPOTHESIS_DESIGN_OR_CLOSE_CURRENT_MODEL_FAMILY"
            ),
            "reason": (
                "V0.29 resolvió la frecuencia pero perdió antes y después de los "
                "costes modelados, quedó por debajo del control y falló todas las "
                "puertas económicas. Los segmentos del holdout ya expuesto sólo "
                "sirven para atribución, no para rescatar ni ajustar la estrategia."
            ),
        },
        "safety": {
            "database_query_only": True,
            "source_evidence_modified": False,
            "new_backtest_hours": 0,
            "orders_created": 0,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
    }
    if (
        sha256_file(result_file) != source_hashes["result"]
        or sha256_file(database) != source_hashes["database"]
        or sha256_file(preregistration) != source_hashes["preregistration"]
        or sha256_file(implementation) != source_hashes["implementation"]
        or sha256_file(generator) != generator_hash
    ):
        raise RuntimeError("La evidencia o el generador V0.29 cambió durante el postmortem")
    if output is not None:
        _write_atomic(output, payload)
    return payload


__all__ = [
    "POSTMORTEM_SCHEMA",
    "REQUIRED_VERDICT",
    "build_v029_final_postmortem",
    "direction_alignment",
    "entry_cost_band",
    "expected_edge_band",
    "probability_up_band",
    "reconstruct_validation",
    "selected_probability_band",
    "signed_twap_band",
    "validation_block",
    "volatility_regime",
]
