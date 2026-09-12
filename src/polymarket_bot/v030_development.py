from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from polymarket_bot.phase4 import _taker_cost_per_share
from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v029_audit import holdout_metrics
from polymarket_bot.v029_forward import open_read_only, parse_utc
from polymarket_bot.v030_strategy import (
    CANDIDATE_ID,
    PAIRED_CONTROL_ID,
    frozen_model_spec,
    frozen_selection_spec,
    mechanical_probability_up,
    select_side,
)


DEVELOPMENT_SCHEMA = "development_v030_resolution_mechanics_1"
SOURCE_RESULT_SCHEMA = "result_v029_high_frequency_temporal_holdout_1"
GATES: dict[str, Any] = {
    "minimum_usable_markets": 100,
    "minimum_candidate_trades": 20,
    "positive_net_pnl": True,
    "profit_factor_above_one": True,
    "positive_one_sided_95_lcb": True,
    "positive_without_best_trade": True,
    "positive_first_12h": True,
    "positive_second_12h": True,
    "minimum_represented_four_hour_blocks": 5,
    "minimum_positive_four_hour_block_fraction": 0.60,
    "candidate_mean_above_paired_control": True,
    "brier_not_more_than_0_01_worse_than_market": True,
}


class V030DevelopmentError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _probability_metrics(
    rows: Sequence[Mapping[str, Any]], probability_key: str
) -> dict[str, Any]:
    if not rows:
        return {
            "rows": 0,
            "brier_score": None,
            "log_loss": None,
            "mean_probability_up": None,
            "realized_up_rate": None,
        }
    probabilities = [float(row[probability_key]) for row in rows]
    targets = [int(row["target_up"]) for row in rows]
    epsilon = 1e-12
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
    return {
        "rows": len(rows),
        "brier_score": round(brier, 8),
        "log_loss": round(log_loss, 8),
        "mean_probability_up": round(statistics.fmean(probabilities), 8),
        "realized_up_rate": round(statistics.fmean(targets), 8),
    }


def _load_source_rows(
    *, database: Path, observation_end_ms: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    connection = open_read_only(database)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        total_markets = int(connection.execute("SELECT COUNT(*) FROM v029_markets").fetchone()[0])
        total_features = int(connection.execute("SELECT COUNT(*) FROM v029_features").fetchone()[0])
        rows: list[dict[str, Any]] = []
        for raw in connection.execute(
            """
            SELECT m.condition_id,m.market_start_ms,m.label,m.label_verified,
             m.resolution_contract_status,m.resolution_twap_window_s,
             f.feature_json
            FROM v029_markets AS m JOIN v029_features AS f USING(condition_id)
            WHERE m.market_start_ms<? ORDER BY m.market_start_ms
            """,
            (observation_end_ms,),
        ):
            feature = json.loads(str(raw["feature_json"]))
            rows.append(
                {
                    "condition_id": str(raw["condition_id"]),
                    "market_start_ms": int(raw["market_start_ms"]),
                    "label": raw["label"],
                    "label_verified": int(raw["label_verified"]),
                    "resolution_contract_status": str(raw["resolution_contract_status"]),
                    "resolution_twap_window_s": raw["resolution_twap_window_s"],
                    "feature": feature,
                }
            )
        return rows, {
            "sqlite_quick_check": quick_check,
            "query_only": bool(query_only),
            "total_markets": total_markets,
            "total_features": total_features,
        }
    finally:
        connection.close()


def evaluate_records(
    *,
    source_rows: Sequence[Mapping[str, Any]],
    experiment_start_ms: int,
) -> dict[str, Any]:
    rejection_reasons: Counter[str] = Counter()
    usable: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    control_rows: list[dict[str, Any]] = []
    midpoint_ms = experiment_start_ms + 12 * 60 * 60 * 1000
    block_ms = 4 * 60 * 60 * 1000
    for raw in source_rows:
        if int(raw.get("label_verified") or 0) != 1 or raw.get("label") not in {
            "Up",
            "Down",
        }:
            rejection_reasons["label_not_verified"] += 1
            continue
        if str(raw.get("resolution_contract_status")) != "VERIFIED":
            rejection_reasons["resolution_contract_not_verified"] += 1
            continue
        if int(raw.get("resolution_twap_window_s") or 0) != 60:
            rejection_reasons["resolution_twap_window_not_60s"] += 1
            continue
        feature = raw["feature"]
        if int(feature.get("twap_open_fresh") or 0) != 1:
            rejection_reasons["opening_twap_not_fresh"] += 1
            continue
        if int(feature.get("resolution_twap_fresh") or 0) != 1:
            rejection_reasons["decision_twap_not_fresh"] += 1
            continue
        probability = mechanical_probability_up(
            chainlink_spot=feature.get("chainlink_price"),
            opening_twap=feature.get("twap_open_price"),
            volatility_60s_bps=feature.get("chainlink_vol_60s_bps"),
            horizon_seconds=int(feature.get("horizon_seconds") or 0),
            resolution_twap_window_seconds=int(
                feature.get("resolution_twap_window_s") or 0
            ),
        )
        if probability is None:
            rejection_reasons["mechanical_probability_unavailable"] += 1
            continue
        up_ask = feature.get("up_best_ask")
        down_ask = feature.get("down_best_ask")
        implied = feature.get("implied_up_mid_probability")
        if up_ask is None or down_ask is None or implied is None:
            rejection_reasons["executable_market_price_unavailable"] += 1
            continue
        up_cost, _, _ = _taker_cost_per_share(
            float(up_ask), fee_rate=0.07, slippage_per_share=0.005
        )
        down_cost, _, _ = _taker_cost_per_share(
            float(down_ask), fee_rate=0.07, slippage_per_share=0.005
        )
        market_probability = float(implied)
        market_start_ms = int(raw["market_start_ms"])
        common = {
            "condition_id": str(raw["condition_id"]),
            "market_start_ms": market_start_ms,
            "label": str(raw["label"]),
            "target_up": int(str(raw["label"]) == "Up"),
            "mechanical_probability_up": probability,
            "market_probability_up": market_probability,
            "clock_half": "first_12h" if market_start_ms < midpoint_ms else "second_12h",
            "four_hour_block": max(
                0, min(5, int((market_start_ms - experiment_start_ms) // block_ms))
            ),
        }
        usable.append(common)
        selected = select_side(
            probability_up=probability,
            up_entry_cost=up_cost,
            down_entry_cost=down_cost,
        )
        if selected is None:
            rejection_reasons["nonpositive_or_tied_expected_value"] += 1
            continue
        candidate_rows.append(
            {
                **common,
                "selected_side": str(selected["side"]),
                "favorite_side": str(selected["side"]),
                "entry_cost": float(selected["entry_cost"]),
                "expected_pnl_per_share": float(selected["expected_pnl_per_share"]),
            }
        )
        favorite = "Up" if market_probability > 0.5 else "Down"
        control_rows.append(
            {
                **common,
                "selected_side": favorite,
                "favorite_side": favorite,
                "entry_cost": up_cost if favorite == "Up" else down_cost,
            }
        )

    candidate_metrics = holdout_metrics(candidate_rows)
    control_metrics = holdout_metrics(control_rows)
    halves = {
        half: holdout_metrics(
            [row for row in candidate_rows if row["clock_half"] == half]
        )
        for half in ("first_12h", "second_12h")
    }
    blocks = {
        str(index + 1): holdout_metrics(
            [row for row in candidate_rows if int(row["four_hour_block"]) == index]
        )
        for index in range(6)
    }
    represented_blocks = sum(int(value["trades"]) > 0 for value in blocks.values())
    positive_blocks = sum(
        int(value["trades"]) > 0
        and float(value["net_pnl_per_share_sequence"]) > 0.0
        for value in blocks.values()
    )
    positive_fraction = (
        positive_blocks / represented_blocks if represented_blocks else 0.0
    )
    candidate_mean = candidate_metrics["mean_pnl_per_share"]
    control_mean = control_metrics["mean_pnl_per_share"]
    mean_difference = (
        float(candidate_mean) - float(control_mean)
        if candidate_mean is not None and control_mean is not None
        else None
    )
    mechanical_probability_metrics = _probability_metrics(
        usable, "mechanical_probability_up"
    )
    market_probability_metrics = _probability_metrics(usable, "market_probability_up")
    brier_difference = (
        float(mechanical_probability_metrics["brier_score"])
        - float(market_probability_metrics["brier_score"])
        if mechanical_probability_metrics["brier_score"] is not None
        and market_probability_metrics["brier_score"] is not None
        else None
    )
    factor = candidate_metrics["profit_factor"]
    gate_results = {
        "minimum_usable_markets_passed": len(usable) >= int(GATES["minimum_usable_markets"]),
        "minimum_candidate_trades_passed": len(candidate_rows) >= int(GATES["minimum_candidate_trades"]),
        "positive_net_pnl_passed": float(candidate_metrics["net_pnl_per_share_sequence"]) > 0.0,
        "profit_factor_above_one_passed": factor is None or float(factor) > 1.0,
        "positive_one_sided_95_lcb_passed": candidate_metrics["one_sided_95_lcb"] is not None and float(candidate_metrics["one_sided_95_lcb"]) > 0.0,
        "positive_without_best_trade_passed": float(candidate_metrics["net_without_best_trade_per_share"]) > 0.0,
        "positive_first_12h_passed": float(halves["first_12h"]["net_pnl_per_share_sequence"]) > 0.0,
        "positive_second_12h_passed": float(halves["second_12h"]["net_pnl_per_share_sequence"]) > 0.0,
        "minimum_represented_four_hour_blocks_passed": represented_blocks >= int(GATES["minimum_represented_four_hour_blocks"]),
        "minimum_positive_four_hour_block_fraction_passed": positive_fraction >= float(GATES["minimum_positive_four_hour_block_fraction"]),
        "candidate_mean_above_paired_control_passed": mean_difference is not None and mean_difference > 0.0,
        "brier_not_more_than_0_01_worse_than_market_passed": brier_difference is not None and brier_difference <= 0.01,
    }
    all_gates_passed = all(gate_results.values())
    return {
        "usable_markets": len(usable),
        "candidate_signal_count": len(candidate_rows),
        "abstentions_after_usable": len(usable) - len(candidate_rows),
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "candidate_id": CANDIDATE_ID,
        "candidate_metrics": candidate_metrics,
        "paired_control_id": PAIRED_CONTROL_ID,
        "paired_control_metrics": control_metrics,
        "candidate_minus_control_mean_pnl_per_share": round(mean_difference, 8) if mean_difference is not None else None,
        "probability_quality": {
            "mechanical": mechanical_probability_metrics,
            "market": market_probability_metrics,
            "mechanical_minus_market_brier": round(brier_difference, 8) if brier_difference is not None else None,
        },
        "clock_halves": halves,
        "four_hour_blocks": blocks,
        "represented_four_hour_blocks": represented_blocks,
        "positive_four_hour_blocks": positive_blocks,
        "positive_four_hour_block_fraction": round(positive_fraction, 8),
        "gate_results": gate_results,
        "all_gates_passed": all_gates_passed,
        "decision": "ADVANCE_TO_FRESH_V030_PREREGISTRATION" if all_gates_passed else "CLOSE_RESOLUTION_MECHANICS_FAMILY",
    }


def run_development_screen(
    *,
    database: str | Path,
    source_result_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    database_path = Path(database).resolve()
    source_result_file = Path(source_result_path).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    if not database_path.is_file() or not source_result_file.is_file():
        raise V030DevelopmentError("Falta evidencia fuente V0.29")
    database_hash_before = sha256_file(database_path)
    source_result_hash = sha256_file(source_result_file)
    source_result = json.loads(source_result_file.read_text(encoding="utf-8"))
    if source_result.get("schema") != SOURCE_RESULT_SCHEMA:
        raise V030DevelopmentError("Resultado fuente V0.29 incompatible")
    if source_result.get("database_sha256") != database_hash_before:
        raise V030DevelopmentError("Hash de base V0.29 no coincide con su auditoria")
    window = source_result.get("window")
    if not isinstance(window, Mapping) or window.get("completion_reason") != "FULL_24H_REACHED":
        raise V030DevelopmentError("La ventana fuente no completo 24 horas")
    started = parse_utc(str(window["experiment_started_at"]))
    observation_end = parse_utc(str(window["observation_ended_at"]))
    if observation_end - started > timedelta(hours=24, minutes=5):
        raise V030DevelopmentError("La evidencia excede la politica de 24 horas")
    source_rows, database_audit = _load_source_rows(
        database=database_path,
        observation_end_ms=int(observation_end.timestamp() * 1000),
    )
    evaluation = evaluate_records(
        source_rows=source_rows,
        experiment_start_ms=int(started.timestamp() * 1000),
    )
    database_hash_after = sha256_file(database_path)
    if database_hash_after != database_hash_before:
        raise V030DevelopmentError("La base V0.29 cambio durante la lectura")
    payload = {
        "schema": DEVELOPMENT_SCHEMA,
        "status": "FINAL_DEVELOPMENT_SCREEN",
        "purpose": "single_formula_rejection_screen_before_any_fresh_collection",
        "source": {
            "database": str(database_path),
            "database_sha256_before": database_hash_before,
            "database_sha256_after": database_hash_after,
            "database_unchanged": True,
            "result": str(source_result_file),
            "result_sha256": source_result_hash,
            "source_verdict": source_result.get("verdict"),
            "source_window_hours_maximum": 24,
            "source_window_started_at": window["experiment_started_at"],
            "source_window_observation_ended_at": window["observation_ended_at"],
            **database_audit,
        },
        "hypothesis": {
            "candidate_id": CANDIDATE_ID,
            "economic_mechanism": "probability_that_the_future_60s_chainlink_average_finishes_at_or_above_the_opening_official_twap",
            "model": frozen_model_spec(),
            "selection": frozen_selection_spec(),
            "formula_variants_tested": 1,
            "threshold_variants_tested": 1,
            "parameters_fitted": False,
            "outcomes_used_by_formula": False,
        },
        "development_gates": dict(GATES),
        "evaluation": evaluation,
        "interpretation": {
            "is_fresh_validation": False,
            "can_approve_paper_or_money": False,
            "pass_meaning": "only_allows_a_fresh_preregistered_forward_replication",
            "fail_meaning": "close_this_mechanical_family_without_threshold_retuning",
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
            "database_read_only": True,
            "automatic_launch": False,
            "new_backtest_hours": 0,
        },
    }
    if output is not None:
        if output.exists():
            existing = json.loads(output.read_text(encoding="utf-8"))
            if existing != payload:
                raise V030DevelopmentError("Ya existe otro resultado V0.30")
        else:
            _write_atomic(output, payload)
    return payload


__all__ = [
    "DEVELOPMENT_SCHEMA",
    "GATES",
    "V030DevelopmentError",
    "evaluate_records",
    "run_development_screen",
]
