from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v025_audit import sequence_metrics
from polymarket_bot.v028_forward import (
    V028_MAXIMUM_HOURS,
    V028_REQUIRED_GLOBAL_FEEDS,
    open_read_only,
    parse_utc,
    read_v028_meta,
)
from polymarket_bot.v027_postmortem import FOUR_HOUR_BLOCKS, elapsed_four_hour_block
from polymarket_bot.v028_prereg import load_and_verify_frozen_prereg
from polymarket_bot.v028_runner import (
    CANDIDATE_IDS,
    VARIANT,
    _technical_snapshot,
    load_and_verify_implementation,
    v028_arm_records,
)
from polymarket_bot.v028_strategy import (
    PARENT_CONTROL_ID,
    PRIMARY_ID,
)


RESULT_SCHEMA = "result_v028_twap_lt5_replication_1"
ONE_SIDED_95_Z = 1.6448536269514722
FINAL_REASONS = {
    "FULL_24H_REACHED",
    "FREEZE_PRIMARY_FUTILITY",
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


def single_hypothesis_metrics(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    metrics = sequence_metrics(rows)
    trades = int(metrics["trades"])
    mean = metrics["mean_pnl_per_share"]
    deviation = metrics["pnl_standard_deviation"]
    if trades == 0 or mean is None or deviation is None:
        lcb = None
    else:
        lcb = float(mean) - ONE_SIDED_95_Z * float(deviation) / math.sqrt(trades)
    ordered = sorted(rows, key=lambda item: int(item["market_start_ms"]))
    per_share = [
        (
            1.0 - float(row["entry_cost"])
            if str(row["favorite_side"]) == str(row["label"])
            else -float(row["entry_cost"])
        )
        for row in ordered
    ]
    best = max(per_share) if per_share else None
    net_without_best = sum(per_share) - (best if best is not None else 0.0)
    result = dict(metrics)
    result["one_sided_95_lcb"] = (
        round(lcb, 8) if lcb is not None else None
    )
    result["one_sided_95_z"] = ONE_SIDED_95_Z
    result["best_trade_pnl_per_share"] = (
        round(best, 8) if best is not None else None
    )
    result["net_without_best_trade_per_share"] = round(net_without_best, 8)
    result["net_without_best_trade_at_5_shares"] = round(
        net_without_best * 5.0,
        8,
    )
    return result


def evaluate_primary(
    *,
    candidate_id: str,
    candidate_rows: Sequence[Mapping[str, Any]],
    parent_rows: Sequence[Mapping[str, Any]],
    start_ms: int,
    cutoff_ms: int,
    frequency_contract: Mapping[str, Any],
    economic_contract: Mapping[str, Any],
    candidate_state: Mapping[str, Any],
) -> dict[str, Any]:
    candidate = sorted(
        (
            row
            for row in candidate_rows
            if int(row["label_verified"]) == 1
            and int(row["market_start_ms"]) <= cutoff_ms
        ),
        key=lambda row: int(row["market_start_ms"]),
    )
    parent = sorted(
        (
            row
            for row in parent_rows
            if int(row["label_verified"]) == 1
            and int(row["market_start_ms"]) <= cutoff_ms
        ),
        key=lambda row: int(row["market_start_ms"]),
    )
    midpoint_ms = start_ms + 12 * 3_600_000
    first_half = [row for row in candidate if int(row["market_start_ms"]) < midpoint_ms]
    second_half = [row for row in candidate if int(row["market_start_ms"]) >= midpoint_ms]
    blocks: dict[str, dict[str, Any]] = {}
    for block in FOUR_HOUR_BLOCKS:
        block_rows = [
            row
            for row in candidate
            if elapsed_four_hour_block(
                (int(row["market_start_ms"]) - start_ms) / 3_600_000
            )
            == block
        ]
        blocks[block] = single_hypothesis_metrics(block_rows)
    minimum_block_trades = int(
        economic_contract["minimum_evaluable_block_trades"]
    )
    evaluable = [
        metrics
        for metrics in blocks.values()
        if int(metrics["trades"]) >= minimum_block_trades
    ]
    positive_blocks = sum(
        float(metrics["net_pnl_per_share_sequence"]) > 0 for metrics in evaluable
    )
    positive_fraction = (
        positive_blocks / len(evaluable) if evaluable else None
    )
    metrics = single_hypothesis_metrics(candidate)
    parent_metrics = single_hypothesis_metrics(parent)
    candidate_mean = metrics["mean_pnl_per_share"]
    parent_mean = parent_metrics["mean_pnl_per_share"]
    difference = (
        float(candidate_mean) - float(parent_mean)
        if candidate_mean is not None and parent_mean is not None
        else None
    )
    factor = metrics["profit_factor"]
    frequency_gates = {
        "minimum_final_trades_passed": (
            int(metrics["trades"]) >= int(frequency_contract["minimum_final_trades"])
        ),
        "minimum_first_half_trades_passed": (
            len(first_half) >= int(frequency_contract["minimum_first_half_trades"])
        ),
        "minimum_second_half_trades_passed": (
            len(second_half) >= int(frequency_contract["minimum_second_half_trades"])
        ),
    }
    economic_gates = {
        "positive_total_net_pnl_passed": (
            float(metrics["net_pnl_per_share_sequence"]) > 0
        ),
        "profit_factor_above_one_passed": factor is None or float(factor) > 1.0,
        "positive_first_half_pnl_passed": (
            float(single_hypothesis_metrics(first_half)["net_pnl_per_share_sequence"]) > 0
        ),
        "positive_second_half_pnl_passed": (
            float(single_hypothesis_metrics(second_half)["net_pnl_per_share_sequence"]) > 0
        ),
        "positive_net_pnl_without_best_trade_passed": (
            float(metrics["net_without_best_trade_per_share"]) > 0
        ),
        "minimum_evaluable_blocks_passed": (
            len(evaluable) >= int(economic_contract["minimum_evaluable_blocks"])
        ),
        "positive_evaluable_block_fraction_passed": (
            positive_fraction is not None
            and positive_fraction
            >= float(economic_contract["minimum_positive_evaluable_block_fraction"])
        ),
        "candidate_mean_above_parent_control_passed": (
            difference is not None and difference > 0
        ),
    }
    statistical_gates = {
        "positive_one_sided_95_lcb_passed": (
            metrics["one_sided_95_lcb"] is not None
            and float(metrics["one_sided_95_lcb"]) > 0
        )
    }
    frequency_passed = all(frequency_gates.values())
    economic_passed = frequency_passed and all(economic_gates.values())
    full_statistical_passed = economic_passed and all(statistical_gates.values())
    return {
        "candidate_id": candidate_id,
        "selectable": True,
        "candidate_state": dict(candidate_state),
        "evaluation_cutoff_market_start_ms": cutoff_ms,
        "metrics": metrics,
        "first_half_metrics": single_hypothesis_metrics(first_half),
        "second_half_metrics": single_hypothesis_metrics(second_half),
        "four_hour_blocks": blocks,
        "evaluable_blocks": len(evaluable),
        "positive_evaluable_blocks": positive_blocks,
        "positive_evaluable_block_fraction": (
            round(positive_fraction, 8) if positive_fraction is not None else None
        ),
        "parent_control_id": PARENT_CONTROL_ID,
        "parent_metrics": parent_metrics,
        "candidate_minus_parent_mean_pnl": (
            round(difference, 8) if difference is not None else None
        ),
        "gates": {
            "frequency": frequency_gates,
            "economic_replication": economic_gates,
            "statistical": statistical_gates,
        },
        "frequency_passed": frequency_passed,
        "economic_replication_passed": economic_passed,
        "full_statistical_passed": full_statistical_passed,
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
    if completion_reason not in {
        "FULL_24H_REACHED",
        "FREEZE_PRIMARY_FUTILITY",
    }:
        return False
    if not run_rows or str(run_rows[-1].get("status")) != "COMPLETED":
        return False
    if not all(bool(value) for value in final_non_feed_gates.values()):
        return False
    required_feeds = [
        *V028_REQUIRED_GLOBAL_FEEDS,
        *(f"v028-rtds-twap-{window_s}s" for window_s in selected_windows),
    ]
    if not all(final_connections.get(feed) == "CANCELLED" for feed in required_feeds):
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
    checkpoint_connections = technical.get("latest_connections")
    if not isinstance(checkpoint_connections, Mapping):
        return False
    return all(checkpoint_connections.get(feed) == "CONNECTED" for feed in required_feeds)


def _select_candidate(candidates: Mapping[str, Mapping[str, Any]]) -> str | None:
    primary = candidates.get(PRIMARY_ID)
    if primary is None or primary.get("full_statistical_passed") is not True:
        return None
    return PRIMARY_ID


def audit_v028(
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
        raise RuntimeError("Base V0.28 no encontrada")
    database_hash = sha256_file(database_path)
    prereg_hash = sha256_file(prereg_file)
    implementation_hash = sha256_file(implementation_file)
    if implementation.get("preregistration_sha256") != prereg_hash:
        raise RuntimeError("Implementación y preinscripción V0.28 no coinciden")
    if output is not None and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == RESULT_SCHEMA
            and existing.get("database_sha256") == database_hash
            and existing.get("preregistration_sha256") == prereg_hash
            and existing.get("implementation_sha256") == implementation_hash
        ):
            return existing
        raise RuntimeError("Existe un resultado V0.28 para otra evidencia")
    meta = read_v028_meta(database_path)
    completion_reason = meta.get("v028_completion_reason")
    if completion_reason not in FINAL_REASONS:
        raise RuntimeError("V0.28 todavía no completó ni fue congelado")
    if meta.get("preregistration_sha256") != prereg_hash:
        raise RuntimeError("La base y la preinscripción V0.28 no coinciden")
    start = parse_utc(str(meta["experiment_started_at"]))
    target_end = parse_utc(str(meta["target_end_at"]))
    observation_end = parse_utc(str(meta["v028_observation_ended_at"]))
    observation_cutoff_ms = int(observation_end.timestamp() * 1000)
    start_ms = int(start.timestamp() * 1000)
    states = dict(meta.get("v028_candidate_states") or {})
    records = v028_arm_records(database_path)
    candidates: dict[str, dict[str, Any]] = {}
    for candidate_id in CANDIDATE_IDS:
        state = dict(states.get(candidate_id) or {})
        frozen_cutoff = state.get("evaluation_cutoff_market_start_ms")
        cutoff_ms = (
            min(observation_cutoff_ms, int(frozen_cutoff))
            if frozen_cutoff is not None
            else observation_cutoff_ms
        )
        candidates[candidate_id] = evaluate_primary(
            candidate_id=candidate_id,
            candidate_rows=records[candidate_id],
            parent_rows=records[PARENT_CONTROL_ID],
            start_ms=start_ms,
            cutoff_ms=cutoff_ms,
            frequency_contract=prereg["candidate_frequency"],
            economic_contract=prereg["economic_replication_gates"],
            candidate_state=state,
        )
    technical = _technical_snapshot(
        database=database_path,
        checkpoint_at=observation_end,
    )
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
              AS verified_contracts,
             MIN(market_start_ms) AS first_start
            FROM v028_markets WHERE market_start_ms<=?
            """,
            (observation_cutoff_ms,),
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
                """
                SELECT run_id,started_at,finished_at,status,error
                FROM v028_runs ORDER BY run_id
                """
            )
        ]
    finally:
        connection.close()
    markets = int(counts["markets"] or 0)
    features = int(counts["features"] or 0)
    resolved = int(counts["resolved"] or 0)
    contracts = int(counts["verified_contracts"] or 0)
    first_start = counts["first_start"]
    expected_markets = (
        int((observation_cutoff_ms - int(first_start)) // 300_000) + 1
        if first_start is not None
        else 0
    )
    market_coverage = min(1.0, markets / expected_markets) if expected_markets else 0.0
    feature_coverage = features / markets if markets else 0.0
    resolution_coverage = resolved / markets if markets else 0.0
    contract_coverage = contracts / markets if markets else 0.0
    gates = dict(technical["gates"])
    feed_gate_names = {
        key for key in gates if key.endswith("_connected_passed")
    }
    non_feed_gates = {
        key: value for key, value in gates.items() if key not in feed_gate_names
    }
    checkpoints = list(meta.get("v028_checkpoints", []))
    expected_shutdown = _terminal_feed_shutdown_expected(
        completion_reason=str(completion_reason),
        run_rows=run_rows,
        final_connections=technical["latest_connections"],
        final_non_feed_gates=non_feed_gates,
        checkpoints=checkpoints,
        selected_windows=technical["selected_twap_windows"],
    )
    corrected_technical_passed = bool(
        expected_shutdown
        and query_only == 1
        and quick_check == "ok"
        and market_coverage >= 0.90
        and feature_coverage >= 0.90
        and resolution_coverage >= 0.90
        and contract_coverage >= 0.90
        and (
            completion_reason != "FULL_24H_REACHED"
            or observation_end >= target_end
        )
    )
    safety_passed = bool(technical["safety_passed"])
    primary = candidates[PRIMARY_ID]
    economic_candidates = [
        candidate_id
        for candidate_id, candidate in candidates.items()
        if candidate["economic_replication_passed"] is True
    ]
    selected = _select_candidate(candidates)
    if not safety_passed or completion_reason in {
        "FREEZE_SAFETY",
        "FREEZE_COLLECTOR_SAFETY",
    }:
        verdict = "FAIL_SAFETY"
    elif completion_reason == "FREEZE_TECHNICAL_FAILURE":
        verdict = "FAIL_TECHNICAL_QUALITY"
    elif not corrected_technical_passed:
        verdict = "FAIL_TECHNICAL_QUALITY"
    elif primary["frequency_passed"] is False:
        verdict = "FAIL_INSUFFICIENT_FREQUENCY"
    elif not economic_candidates:
        verdict = "FAIL_ECONOMIC_REPLICATION"
    elif selected is None:
        verdict = "CONTINUE_PAPER_ACCUMULATION"
    else:
        verdict = "PASS_SINGLE_HYPOTHESIS_PAPER_CANDIDATE"
    meanings = {
        "FAIL_SAFETY": "La prueba incumplió una puerta de seguridad.",
        "FAIL_TECHNICAL_QUALITY": "La integridad, cobertura o cierre terminal no permiten evaluar.",
        "FAIL_INSUFFICIENT_FREQUENCY": "La hipótesis primaria no alcanzó la frecuencia congelada.",
        "FAIL_ECONOMIC_REPLICATION": "La hipótesis primaria no repitió todas las puertas económicas.",
        "CONTINUE_PAPER_ACCUMULATION": "Existe réplica económica, pero el LCB unilateral 95% no es positivo.",
        "PASS_SINGLE_HYPOTHESIS_PAPER_CANDIDATE": "La única hipótesis pasó frecuencia, economía y LCB unilateral 95%; dinero real sigue bloqueado.",
    }
    controls = {
        PARENT_CONTROL_ID: {
            "selectable": False,
            "metrics": primary["parent_metrics"],
        },
    }
    result = {
        "schema": RESULT_SCHEMA,
        "variant": VARIANT,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": verdict,
        "meaning": meanings[verdict],
        "selected_strategy": selected,
        "replicated_strategies": economic_candidates,
        "paper_forward_candidate": selected is not None,
        "money_real_candidate": False,
        "candidates": candidates,
        "controls": controls,
        "selection": {
            "selected_at_most_one": True,
            "rule": "only_frozen_primary_can_be_selected",
            "controls_selectable": False,
            "selected": selected,
        },
        "statistical_contract": prereg["statistical_gate"],
        "window": {
            "maximum_hours": V028_MAXIMUM_HOURS,
            "checkpoint_hours": prereg["stopping"]["checkpoint_hours"],
            "completion_reason": completion_reason,
            "experiment_started_at": start.isoformat(timespec="seconds"),
            "target_end_at": target_end.isoformat(timespec="seconds"),
            "observation_ended_at": observation_end.isoformat(timespec="seconds"),
            "elapsed_wall_hours": round(
                (observation_end - start).total_seconds() / 3600,
                8,
            ),
        },
        "checkpoints": checkpoints,
        "coverage": {
            "expected_markets": expected_markets,
            "markets": markets,
            "market_coverage": round(market_coverage, 8),
            "features": features,
            "feature_coverage": round(feature_coverage, 8),
            "resolved": resolved,
            "resolution_coverage": round(resolution_coverage, 8),
            "verified_resolution_contracts": contracts,
            "resolution_contract_coverage": round(contract_coverage, 8),
        },
        "technical_passed": corrected_technical_passed,
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
        raise RuntimeError("La base V0.28 cambió durante la auditoría")
    if output is not None:
        _write_atomic(output, result)
    return result


__all__ = [
    "ONE_SIDED_95_Z",
    "RESULT_SCHEMA",
    "audit_v028",
    "evaluate_primary",
    "single_hypothesis_metrics",
]
