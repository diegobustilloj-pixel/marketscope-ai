from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from polymarket_bot.phase4 import _taker_cost_per_share
from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v025_audit import sequence_metrics
from polymarket_bot.v029_forward import (
    V029_MAXIMUM_HOURS,
    V029_REQUIRED_GLOBAL_FEEDS,
    open_read_only,
    parse_utc,
    read_v029_meta,
)
from polymarket_bot.v029_prereg import load_and_verify_frozen_prereg
from polymarket_bot.v029_runner import (
    CHECKPOINT_HOURS,
    VARIANT,
    load_and_verify_implementation,
    technical_snapshot,
)
from polymarket_bot.v029_strategy import (
    CANDIDATE_ID,
    MODEL_FEATURES,
    PAIRED_CONTROL_ID,
    feature_vector,
    select_side,
)


RESULT_SCHEMA = "result_v029_high_frequency_temporal_holdout_1"
ONE_SIDED_95_Z = 1.6448536269514722
FINAL_REASONS = {
    "FULL_24H_REACHED",
    "FREEZE_SAFETY",
    "FREEZE_TECHNICAL_FAILURE",
    "FREEZE_COLLECTOR_SAFETY",
}


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def holdout_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    normalized = [
        {
            **dict(row),
            "favorite_side": str(row["selected_side"]),
        }
        for row in rows
    ]
    metrics = sequence_metrics(normalized)
    trades = int(metrics["trades"])
    mean = metrics["mean_pnl_per_share"]
    deviation = metrics["pnl_standard_deviation"]
    lcb = (
        float(mean) - ONE_SIDED_95_Z * float(deviation) / math.sqrt(trades)
        if trades and mean is not None and deviation is not None
        else None
    )
    ordered = sorted(normalized, key=lambda item: int(item["market_start_ms"]))
    pnl = [
        (
            1.0 - float(row["entry_cost"])
            if str(row["selected_side"]) == str(row["label"])
            else -float(row["entry_cost"])
        )
        for row in ordered
    ]
    best = max(pnl) if pnl else None
    without_best = sum(pnl) - (best if best is not None else 0.0)
    output = dict(metrics)
    output.update(
        {
            "one_sided_95_lcb": round(lcb, 8) if lcb is not None else None,
            "one_sided_95_z": ONE_SIDED_95_Z,
            "best_trade_pnl_per_share": round(best, 8) if best is not None else None,
            "net_without_best_trade_per_share": round(without_best, 8),
            "net_without_best_trade_at_5_shares": round(without_best * 5.0, 8),
        }
    )
    return output


def _build_model(model_spec: Mapping[str, Any]) -> Pipeline:
    imputer = model_spec["imputer"]
    estimator = model_spec["estimator"]
    return Pipeline(
        [
            (
                "imputer",
                SimpleImputer(
                    strategy=str(imputer["strategy"]),
                    add_indicator=bool(imputer["add_indicator"]),
                    keep_empty_features=bool(imputer["keep_empty_features"]),
                ),
            ),
            ("scaler", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    C=float(estimator["C"]),
                    max_iter=int(estimator["max_iter"]),
                    random_state=int(estimator["random_state"]),
                    solver=str(estimator["solver"]),
                    class_weight=estimator["class_weight"],
                ),
            ),
        ]
    )


def load_holdout_rows(
    *,
    database: str | Path,
    start: datetime,
    observation_end: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    midpoint_ms = int((start + timedelta(hours=12)).timestamp() * 1000)
    cutoff_ms = int(observation_end.timestamp() * 1000)
    training: list[dict[str, Any]] = []
    validation: list[dict[str, Any]] = []
    connection = open_read_only(database)
    try:
        for raw in connection.execute(
            """
            SELECT m.condition_id,m.market_start_ms,m.label,m.label_verified,
             f.feature_json
            FROM v029_markets AS m JOIN v029_features AS f USING(condition_id)
            WHERE m.market_start_ms<? ORDER BY m.market_start_ms
            """,
            (cutoff_ms,),
        ):
            if int(raw["label_verified"]) != 1 or raw["label"] not in {"Up", "Down"}:
                continue
            feature = json.loads(str(raw["feature_json"]))
            if int(feature.get("resolution_twap_window_s") or 0) != 60:
                continue
            implied = feature.get("implied_up_mid_probability")
            up_ask = feature.get("up_best_ask")
            down_ask = feature.get("down_best_ask")
            if implied is None or up_ask is None or down_ask is None:
                continue
            implied_value = float(implied)
            if implied_value == 0.5:
                continue
            up_cost, _, _ = _taker_cost_per_share(
                float(up_ask), fee_rate=0.07, slippage_per_share=0.005
            )
            down_cost, _, _ = _taker_cost_per_share(
                float(down_ask), fee_rate=0.07, slippage_per_share=0.005
            )
            item = {
                "condition_id": str(raw["condition_id"]),
                "market_start_ms": int(raw["market_start_ms"]),
                "label": str(raw["label"]),
                "target_up": int(str(raw["label"]) == "Up"),
                "features": feature_vector(feature),
                "implied_up_mid_probability": implied_value,
                "up_entry_cost": up_cost,
                "down_entry_cost": down_cost,
            }
            (training if item["market_start_ms"] < midpoint_ms else validation).append(
                item
            )
    finally:
        connection.close()
    return training, validation


def evaluate_holdout(
    *,
    training: Sequence[Mapping[str, Any]],
    validation: Sequence[Mapping[str, Any]],
    prereg: Mapping[str, Any],
) -> dict[str, Any]:
    gates = prereg["validation_gates"]
    minimum_training = int(gates["minimum_training_rows"])
    minimum_validation = int(gates["minimum_validation_rows"])
    training_gate = len(training) >= minimum_training
    validation_gate = len(validation) >= minimum_validation
    target_counts = {
        "Up": sum(int(row["target_up"]) == 1 for row in training),
        "Down": sum(int(row["target_up"]) == 0 for row in training),
    }
    model_fit = False
    model_error: str | None = None
    model: Pipeline | None = None
    candidate_rows: list[dict[str, Any]] = []
    control_rows: list[dict[str, Any]] = []
    if training_gate and validation_gate and all(target_counts.values()):
        try:
            model = _build_model(prereg["candidate"]["model"])
            matrix = np.asarray(
                [
                    [np.nan if value is None else float(value) for value in row["features"]]
                    for row in training
                ],
                dtype=float,
            )
            targets = np.asarray([int(row["target_up"]) for row in training])
            model.fit(matrix, targets)
            validation_matrix = np.asarray(
                [
                    [np.nan if value is None else float(value) for value in row["features"]]
                    for row in validation
                ],
                dtype=float,
            )
            probabilities = model.predict_proba(validation_matrix)[:, 1]
            model_fit = True
            for row, probability_up in zip(validation, probabilities, strict=True):
                selected = select_side(
                    probability_up=float(probability_up),
                    up_entry_cost=float(row["up_entry_cost"]),
                    down_entry_cost=float(row["down_entry_cost"]),
                )
                if selected is None:
                    continue
                candidate_rows.append(
                    {
                        "condition_id": row["condition_id"],
                        "market_start_ms": row["market_start_ms"],
                        "label": row["label"],
                        "selected_side": selected["side"],
                        "entry_cost": selected["entry_cost"],
                        "expected_pnl_per_share": selected[
                            "expected_pnl_per_share"
                        ],
                        "probability_up": float(probability_up),
                    }
                )
                favorite = (
                    "Up"
                    if float(row["implied_up_mid_probability"]) > 0.5
                    else "Down"
                )
                control_rows.append(
                    {
                        "condition_id": row["condition_id"],
                        "market_start_ms": row["market_start_ms"],
                        "label": row["label"],
                        "selected_side": favorite,
                        "entry_cost": (
                            row["up_entry_cost"]
                            if favorite == "Up"
                            else row["down_entry_cost"]
                        ),
                    }
                )
        except Exception as exc:
            model_error = f"{type(exc).__name__}: {exc}"
            model_fit = False
            model = None
            candidate_rows = []
            control_rows = []
    elif not all(target_counts.values()):
        model_error = "TRAINING_TARGET_HAS_SINGLE_CLASS"

    candidate_metrics = holdout_metrics(candidate_rows)
    control_metrics = holdout_metrics(control_rows)
    candidate_mean = candidate_metrics["mean_pnl_per_share"]
    control_mean = control_metrics["mean_pnl_per_share"]
    difference = (
        float(candidate_mean) - float(control_mean)
        if candidate_mean is not None and control_mean is not None
        else None
    )
    factor = candidate_metrics["profit_factor"]
    frequency_gate = int(candidate_metrics["trades"]) >= int(
        gates["minimum_validation_candidate_trades"]
    )
    economic_gates = {
        "positive_validation_net_pnl_passed": (
            float(candidate_metrics["net_pnl_per_share_sequence"]) > 0
        ),
        "validation_profit_factor_above_one_passed": (
            factor is None or float(factor) > 1.0
        ),
        "positive_validation_one_sided_95_lcb_passed": (
            candidate_metrics["one_sided_95_lcb"] is not None
            and float(candidate_metrics["one_sided_95_lcb"]) > 0
        ),
        "positive_validation_without_best_trade_passed": (
            float(candidate_metrics["net_without_best_trade_per_share"]) > 0
        ),
        "candidate_mean_above_paired_control_passed": (
            difference is not None and difference > 0
        ),
    }
    coefficients: dict[str, Any] | None = None
    if model_fit and model is not None:
        estimator = model.named_steps["model"]
        coefficients = {
            "raw_pipeline_feature_names": list(MODEL_FEATURES),
            "transformed_coefficient_count": int(estimator.coef_.shape[1]),
            "coefficients": [round(float(value), 12) for value in estimator.coef_[0]],
            "intercept": round(float(estimator.intercept_[0]), 12),
        }
    return {
        "training_rows": len(training),
        "validation_rows": len(validation),
        "training_target_counts": target_counts,
        "training_rows_passed": training_gate,
        "validation_rows_passed": validation_gate,
        "model_fit": model_fit,
        "model_error": model_error,
        "model_artifact": coefficients,
        "candidate_id": CANDIDATE_ID,
        "candidate_metrics": candidate_metrics,
        "candidate_signal_count": len(candidate_rows),
        "paired_control_id": PAIRED_CONTROL_ID,
        "paired_control_metrics": control_metrics,
        "candidate_minus_control_mean_pnl": (
            round(difference, 8) if difference is not None else None
        ),
        "validation_frequency_passed": frequency_gate,
        "economic_gates": economic_gates,
        "validation_economics_passed": frequency_gate
        and all(economic_gates.values()),
        "training_pnl_computed": False,
    }


def _terminal_feed_shutdown_expected(
    *,
    completion_reason: str,
    run_rows: Sequence[Mapping[str, Any]],
    final_connections: Mapping[str, Any],
    final_non_feed_gates: Mapping[str, Any],
    checkpoints: Sequence[Mapping[str, Any]],
    selected_windows: Sequence[int],
) -> bool:
    if completion_reason != "FULL_24H_REACHED":
        return False
    if not run_rows or str(run_rows[-1].get("status")) != "COMPLETED":
        return False
    if not all(bool(value) for value in final_non_feed_gates.values()):
        return False
    required = [
        *V029_REQUIRED_GLOBAL_FEEDS,
        *(f"v029-rtds-twap-{window}s" for window in selected_windows),
    ]
    if not all(final_connections.get(feed) == "CANCELLED" for feed in required):
        return False
    if not checkpoints:
        return False
    latest = max(
        checkpoints,
        key=lambda item: (
            float(item["checkpoint_hour"]),
            int(item.get("attempt", 1)),
        ),
    )
    technical = latest.get("technical")
    if not isinstance(technical, Mapping) or technical.get("passed") is not True:
        return False
    connections = technical.get("latest_connections")
    return isinstance(connections, Mapping) and all(
        connections.get(feed) == "CONNECTED" for feed in required
    )


def audit_v029(
    *,
    database: str | Path,
    prereg_path: str | Path,
    implementation_path: str | Path,
    result_path: str | Path | None = None,
) -> dict[str, Any]:
    database_path = Path(database).resolve()
    prereg_file = Path(prereg_path).resolve()
    implementation_file = Path(implementation_path).resolve()
    output = Path(result_path).resolve() if result_path is not None else None
    prereg = load_and_verify_frozen_prereg(prereg_file)
    implementation = load_and_verify_implementation(implementation_file)
    if not database_path.is_file():
        raise RuntimeError("Base V0.29 no encontrada")
    database_hash = sha256_file(database_path)
    prereg_hash = sha256_file(prereg_file)
    implementation_hash = sha256_file(implementation_file)
    if implementation.get("preregistration_sha256") != prereg_hash:
        raise RuntimeError("Implementacion y preinscripcion V0.29 no coinciden")
    if output is not None and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == RESULT_SCHEMA
            and existing.get("database_sha256") == database_hash
            and existing.get("preregistration_sha256") == prereg_hash
            and existing.get("implementation_sha256") == implementation_hash
        ):
            return existing
        raise RuntimeError("Existe un resultado V0.29 para otra evidencia")
    meta = read_v029_meta(database_path)
    completion_reason = meta.get("v029_completion_reason")
    if completion_reason not in FINAL_REASONS:
        raise RuntimeError("V0.29 todavia no completo")
    if meta.get("preregistration_sha256") != prereg_hash:
        raise RuntimeError("Base y preinscripcion V0.29 no coinciden")
    start = parse_utc(str(meta["experiment_started_at"]))
    target_end = parse_utc(str(meta["target_end_at"]))
    observation_end = parse_utc(str(meta["v029_observation_ended_at"]))
    cutoff_ms = int(observation_end.timestamp() * 1000)
    technical = technical_snapshot(database=database_path, checkpoint_at=observation_end)
    connection = open_read_only(database_path)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        counts = connection.execute(
            """
            SELECT COUNT(*) AS markets,
             SUM(CASE WHEN feature_status='SAVED' THEN 1 ELSE 0 END) AS features,
             SUM(CASE WHEN label_verified=1 THEN 1 ELSE 0 END) AS resolved,
             SUM(CASE WHEN resolution_contract_status='VERIFIED' THEN 1 ELSE 0 END)
              AS contracts,MIN(market_start_ms) AS first_start
            FROM v029_markets WHERE market_start_ms<?
            """,
            (cutoff_ms,),
        ).fetchone()
        run_rows = [
            {
                "run_id": int(row[0]),
                "started_at": str(row[1]),
                "finished_at": str(row[2]) if row[2] is not None else None,
                "status": str(row[3]),
                "error": str(row[4]) if row[4] is not None else None,
            }
            for row in connection.execute(
                "SELECT run_id,started_at,finished_at,status,error FROM v029_runs ORDER BY run_id"
            )
        ]
    finally:
        connection.close()
    markets = int(counts["markets"] or 0)
    features = int(counts["features"] or 0)
    resolved = int(counts["resolved"] or 0)
    contracts = int(counts["contracts"] or 0)
    first_start = counts["first_start"]
    expected = (
        int((cutoff_ms - int(first_start)) // 300_000) + 1
        if first_start is not None
        else 0
    )
    market_coverage = min(1.0, markets / expected) if expected else 0.0
    feature_coverage = features / markets if markets else 0.0
    resolution_coverage = resolved / markets if markets else 0.0
    contract_coverage = contracts / markets if markets else 0.0
    feed_gates = {
        key for key in technical["gates"] if key.endswith("_connected_passed")
    }
    non_feed_gates = {
        key: value
        for key, value in technical["gates"].items()
        if key not in feed_gates
    }
    checkpoints = list(meta.get("v029_checkpoints", []))
    expected_shutdown = _terminal_feed_shutdown_expected(
        completion_reason=str(completion_reason),
        run_rows=run_rows,
        final_connections=technical["latest_connections"],
        final_non_feed_gates=non_feed_gates,
        checkpoints=checkpoints,
        selected_windows=technical["selected_twap_windows"],
    )
    corrected_technical = bool(
        expected_shutdown
        and query_only == 1
        and quick_check == "ok"
        and market_coverage >= 0.90
        and feature_coverage >= 0.90
        and resolution_coverage >= 0.90
        and contract_coverage >= 0.90
        and observation_end >= target_end
    )
    safety_passed = bool(technical["safety_passed"])
    holdout: dict[str, Any] | None = None
    if corrected_technical and safety_passed:
        training, validation = load_holdout_rows(
            database=database_path,
            start=start,
            observation_end=observation_end,
        )
        holdout = evaluate_holdout(
            training=training,
            validation=validation,
            prereg=prereg,
        )
    if not safety_passed or completion_reason in {
        "FREEZE_SAFETY",
        "FREEZE_COLLECTOR_SAFETY",
    }:
        verdict = "FAIL_SAFETY"
    elif not corrected_technical:
        verdict = "FAIL_TECHNICAL_QUALITY"
    elif holdout is None or holdout["training_rows_passed"] is not True:
        verdict = "FAIL_INSUFFICIENT_TRAINING_DATA"
    elif holdout["model_fit"] is not True:
        verdict = "FAIL_MODEL_TRAINING"
    elif holdout["validation_rows_passed"] is not True:
        verdict = "FAIL_INSUFFICIENT_VALIDATION_FREQUENCY"
    elif holdout["validation_frequency_passed"] is not True:
        verdict = "FAIL_INSUFFICIENT_VALIDATION_FREQUENCY"
    elif holdout["validation_economics_passed"] is not True:
        verdict = "FAIL_VALIDATION_ECONOMICS"
    else:
        verdict = "HYPOTHESIS_REQUIRES_FRESH_V030_REPLICATION"
    meanings = {
        "FAIL_SAFETY": "V0.29 incumplio una puerta de seguridad.",
        "FAIL_TECHNICAL_QUALITY": "La cobertura o el cierre no permiten evaluar V0.29.",
        "FAIL_INSUFFICIENT_TRAINING_DATA": "La primera mitad no alcanza el minimo congelado.",
        "FAIL_MODEL_TRAINING": "El unico modelo congelado no pudo ajustarse.",
        "FAIL_INSUFFICIENT_VALIDATION_FREQUENCY": "El holdout o las senales no alcanzan frecuencia.",
        "FAIL_VALIDATION_ECONOMICS": "La hipotesis no supera todas las puertas economicas del holdout.",
        "HYPOTHESIS_REQUIRES_FRESH_V030_REPLICATION": "El holdout paso, pero solo genera una hipotesis para replica fresca V0.30.",
    }
    result = {
        "schema": RESULT_SCHEMA,
        "variant": VARIANT,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": verdict,
        "meaning": meanings[verdict],
        "generated_hypothesis": (
            CANDIDATE_ID
            if verdict == "HYPOTHESIS_REQUIRES_FRESH_V030_REPLICATION"
            else None
        ),
        "selected_strategy": None,
        "paper_forward_candidate": False,
        "money_real_candidate": False,
        "holdout": holdout,
        "promotion_cap": prereg["promotion_cap"],
        "window": {
            "maximum_hours": V029_MAXIMUM_HOURS,
            "checkpoint_hours": list(CHECKPOINT_HOURS),
            "completion_reason": completion_reason,
            "experiment_started_at": start.isoformat(timespec="seconds"),
            "training_ended_at": (start + timedelta(hours=12)).isoformat(
                timespec="seconds"
            ),
            "target_end_at": target_end.isoformat(timespec="seconds"),
            "observation_ended_at": observation_end.isoformat(timespec="seconds"),
            "elapsed_wall_hours": round(
                (observation_end - start).total_seconds() / 3600,
                8,
            ),
        },
        "checkpoints": checkpoints,
        "coverage": {
            "expected_markets": expected,
            "markets": markets,
            "market_coverage": round(market_coverage, 8),
            "features": features,
            "feature_coverage": round(feature_coverage, 8),
            "resolved": resolved,
            "resolution_coverage": round(resolution_coverage, 8),
            "verified_resolution_contracts": contracts,
            "resolution_contract_coverage": round(contract_coverage, 8),
        },
        "technical_passed": corrected_technical,
        "terminal_feed_shutdown_expected": expected_shutdown,
        "technical_snapshot": technical,
        "technical_audit": {
            "database_opened_query_only": query_only == 1,
            "sqlite_quick_check": quick_check,
            "runs": run_rows,
        },
        "safety_passed": safety_passed,
        "database": str(database_path),
        "database_sha256": database_hash,
        "database_read_only_verified": database_hash == sha256_file(database_path),
        "preregistration": str(prereg_file),
        "preregistration_sha256": prereg_hash,
        "implementation": str(implementation_file),
        "implementation_sha256": implementation_hash,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }
    if sha256_file(database_path) != database_hash:
        raise RuntimeError("La base V0.29 cambio durante la auditoria")
    if output is not None:
        _write_atomic(output, result)
    return result


__all__ = [
    "ONE_SIDED_95_Z",
    "RESULT_SCHEMA",
    "audit_v029",
    "evaluate_holdout",
    "holdout_metrics",
    "load_holdout_rows",
]
