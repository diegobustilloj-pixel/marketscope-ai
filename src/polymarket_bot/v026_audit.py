from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import _parse_utc, sha256_file
from polymarket_bot.v025_audit import sequence_metrics
from polymarket_bot.v026_diagnostic import (
    cost_matched_direction_contrast,
    direction_contrast,
)
from polymarket_bot.v026_runner import (
    MAXIMUM_HOURS,
    VARIANT,
    load_and_verify_prereg,
    v026_arm_records,
)
from polymarket_bot.v026_strategy import (
    ALL_CONTROL_ID,
    PRIMARY_ARM_ID,
    UP_CONTROL_ID,
)


RESULT_SCHEMA = "result_v026_down_cost_adjusted_replication_1"
MINIMUM_DOWN_TRADES = 15
MINIMUM_DOWN_TRADES_PER_HALF = 5


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def evaluate_v026(
    arm_records: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    midpoint_ms: int,
) -> dict[str, Any]:
    down = sorted(
        arm_records[PRIMARY_ARM_ID], key=lambda row: int(row["market_start_ms"])
    )
    up = sorted(
        arm_records[UP_CONTROL_ID], key=lambda row: int(row["market_start_ms"])
    )
    all_rows = sorted(
        arm_records[ALL_CONTROL_ID], key=lambda row: int(row["market_start_ms"])
    )
    first_half = [row for row in down if int(row["market_start_ms"]) < midpoint_ms]
    second_half = [row for row in down if int(row["market_start_ms"]) >= midpoint_ms]
    primary_metrics = sequence_metrics(down)
    first_metrics = sequence_metrics(first_half)
    second_metrics = sequence_metrics(second_half)
    raw_contrast = direction_contrast(down, up)
    matched_contrast = cost_matched_direction_contrast(all_rows)
    factor = primary_metrics["profit_factor"]
    matched_value = matched_contrast["matched_down_minus_up_mean_pnl"]
    frequency_gates = {
        "minimum_down_trades_passed": len(down) >= MINIMUM_DOWN_TRADES,
        "minimum_first_half_down_trades_passed": (
            len(first_half) >= MINIMUM_DOWN_TRADES_PER_HALF
        ),
        "minimum_second_half_down_trades_passed": (
            len(second_half) >= MINIMUM_DOWN_TRADES_PER_HALF
        ),
    }
    economic_gates = {
        "positive_down_net_pnl_passed": (
            float(primary_metrics["net_pnl_per_share_sequence"]) > 0
        ),
        "down_profit_factor_above_one_passed": factor is None or float(factor) > 1.0,
        "positive_first_half_pnl_passed": (
            float(first_metrics["net_pnl_per_share_sequence"]) > 0
        ),
        "positive_second_half_pnl_passed": (
            float(second_metrics["net_pnl_per_share_sequence"]) > 0
        ),
        "positive_raw_direction_contrast_passed": (
            raw_contrast.get("available") is True
            and float(raw_contrast["down_minus_up_mean_pnl"]) > 0
        ),
        "positive_cost_matched_contrast_passed": (
            matched_value is not None and float(matched_value) > 0
        ),
    }
    statistical_gates = {
        "positive_down_one_sided_95_lcb_passed": (
            primary_metrics["one_sided_95_lcb"] is not None
            and float(primary_metrics["one_sided_95_lcb"]) > 0
        ),
        "positive_raw_direction_one_sided_95_lcb_passed": (
            raw_contrast.get("available") is True
            and float(raw_contrast["one_sided_95_lcb_for_difference"]) > 0
        ),
    }
    frequency_passed = all(frequency_gates.values())
    economic_replication_passed = frequency_passed and all(economic_gates.values())
    full_statistical_passed = economic_replication_passed and all(
        statistical_gates.values()
    )
    return {
        "primary": {
            "strategy_id": PRIMARY_ARM_ID,
            "metrics": primary_metrics,
            "first_half_metrics": first_metrics,
            "second_half_metrics": second_metrics,
        },
        "controls": {
            UP_CONTROL_ID: {
                "metrics": sequence_metrics(up),
                "selectable": False,
            },
            ALL_CONTROL_ID: {
                "metrics": sequence_metrics(all_rows),
                "selectable": False,
            },
        },
        "direction_contrast": raw_contrast,
        "cost_matched_direction_contrast": matched_contrast,
        "gates": {
            "frequency": frequency_gates,
            "economic_replication": economic_gates,
            "statistical": statistical_gates,
        },
        "frequency_passed": frequency_passed,
        "economic_replication_passed": economic_replication_passed,
        "full_statistical_passed": full_statistical_passed,
    }


def audit_v026(
    *,
    database: str | Path,
    prereg_path: str | Path,
    phase4_db: str | Path,
    result_path: str | Path | None = None,
) -> dict[str, Any]:
    database_path = Path(database).resolve()
    prereg_file = Path(prereg_path).resolve()
    phase4_path = Path(phase4_db).resolve()
    output = Path(result_path).resolve() if result_path is not None else None
    prereg = load_and_verify_prereg(prereg_file)
    if not database_path.is_file():
        raise RuntimeError("Base V0.26 no encontrada")
    database_hash = sha256_file(database_path)
    prereg_hash = sha256_file(prereg_file)
    if output is not None and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == RESULT_SCHEMA
            and existing.get("database_sha256") == database_hash
            and existing.get("preregistration_sha256") == prereg_hash
        ):
            return existing
        raise RuntimeError("Existe un resultado V0.26 para otra evidencia")

    phase4_hash_before = sha256_file(phase4_path)
    expected_phase4_hash = str(prereg["artifacts"]["phase4"]["sha256"])
    connection = _open_read_only(database_path)
    try:
        meta = {
            str(key): json.loads(str(value))
            for key, value in connection.execute("SELECT key,value FROM shadow_meta")
        }
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        completion_reason = meta.get("v026_completion_reason")
        completed_at = meta.get("experiment_completed_at")
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
                FROM shadow_runs ORDER BY run_id
                """
            )
        ]
    finally:
        connection.close()
    if completion_reason != "FIXED_24H_REACHED" or not completed_at:
        raise RuntimeError("V0.26 todavia no completo su ventana fija")

    start = _parse_utc(str(meta["experiment_started_at"]))
    target_end = _parse_utc(str(meta["target_end_at"]))
    completed = _parse_utc(str(completed_at))
    start_ms = int(start.timestamp() * 1000)
    cutoff_ms = int(target_end.timestamp() * 1000)
    midpoint_ms = start_ms + int(MAXIMUM_HOURS * 3600 * 1000 / 2)
    records = v026_arm_records(database_path)
    evaluated = {
        arm_id: [
            row
            for row in rows
            if int(row["label_verified"]) == 1
            and int(row["market_start_ms"]) <= cutoff_ms
        ]
        for arm_id, rows in records.items()
    }
    evaluation = evaluate_v026(evaluated, midpoint_ms=midpoint_ms)

    connection = _open_read_only(database_path)
    try:
        first_start = connection.execute(
            "SELECT MIN(market_start_ms) FROM shadow_markets WHERE market_start_ms<=?",
            (cutoff_ms,),
        ).fetchone()[0]
        if first_start is None:
            raise RuntimeError("V0.26 no contiene mercados evaluables")
        counts = connection.execute(
            """
            SELECT COUNT(*) AS markets,
             SUM(CASE WHEN feature_status='SAVED' THEN 1 ELSE 0 END) AS features,
             SUM(CASE WHEN label_verified=1 THEN 1 ELSE 0 END) AS resolved
            FROM shadow_markets
            WHERE market_start_ms>=? AND market_start_ms<=?
            """,
            (int(first_start), cutoff_ms),
        ).fetchone()
    finally:
        connection.close()
    markets = int(counts["markets"] or 0)
    features = int(counts["features"] or 0)
    resolved = int(counts["resolved"] or 0)
    expected_markets = int((cutoff_ms - int(first_start)) // 300_000) + 1
    market_coverage = markets / expected_markets if expected_markets else 0.0
    feature_coverage = features / markets if markets else 0.0
    resolution_coverage = resolved / markets if markets else 0.0
    phase4_hash_after = sha256_file(phase4_path)
    technical_passed = (
        query_only == 1
        and quick_check == "ok"
        and phase4_hash_before == expected_phase4_hash == phase4_hash_after
        and market_coverage >= 0.95
        and feature_coverage >= 0.95
        and resolution_coverage == 1.0
        and bool(run_rows)
        and run_rows[-1]["status"] == "COMPLETED"
    )
    safety_passed = (
        meta.get("orders_enabled") is False
        and meta.get("money_real_enabled") is False
        and meta.get("v026_real_money") == "BLOQUEADO"
        and meta.get("wallet_required") is False
    )
    if not safety_passed:
        verdict = "FAIL_SAFETY"
    elif not technical_passed:
        verdict = "FAIL_TECHNICAL_QUALITY"
    elif not evaluation["frequency_passed"]:
        verdict = "FAIL_INSUFFICIENT_FREQUENCY"
    elif not evaluation["economic_replication_passed"]:
        verdict = "FAIL_REPLICATION"
    elif evaluation["full_statistical_passed"]:
        verdict = "PASS_STATISTICAL_PAPER_CANDIDATE"
    else:
        verdict = "CONTINUE_PAPER_ACCUMULATION"
    meanings = {
        "FAIL_SAFETY": "La prueba incumplio el bloqueo operativo.",
        "FAIL_TECHNICAL_QUALITY": "La integridad o cobertura no permite evaluar.",
        "FAIL_INSUFFICIENT_FREQUENCY": "No hubo al menos 15 Down y 5 en cada mitad.",
        "FAIL_REPLICATION": "La ventaja Down no se repitio en todos los gates economicos.",
        "CONTINUE_PAPER_ACCUMULATION": "La economia se repitio, pero la confianza aun no permite promover la estrategia.",
        "PASS_STATISTICAL_PAPER_CANDIDATE": "La replica economica y ambos limites de confianza pasaron; dinero real sigue bloqueado.",
    }
    result = {
        "schema": RESULT_SCHEMA,
        "variant": VARIANT,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": verdict,
        "meaning": meanings[verdict],
        "selected_strategy": (
            PRIMARY_ARM_ID if evaluation["full_statistical_passed"] else None
        ),
        "replicated_strategy": (
            PRIMARY_ARM_ID if evaluation["economic_replication_passed"] else None
        ),
        "paper_accumulation_candidate": evaluation[
            "economic_replication_passed"
        ],
        "paper_forward_candidate": evaluation["full_statistical_passed"],
        "money_real_candidate": False,
        **evaluation,
        "window": {
            "maximum_hours": MAXIMUM_HOURS,
            "fixed_full_window": True,
            "completion_reason": completion_reason,
            "experiment_started_at": start.isoformat(timespec="seconds"),
            "target_end_at": target_end.isoformat(timespec="seconds"),
            "experiment_completed_at": completed.isoformat(timespec="seconds"),
            "elapsed_wall_hours": round(
                (completed - start).total_seconds() / 3600, 8
            ),
            "midpoint_ms": midpoint_ms,
        },
        "coverage": {
            "expected_markets": expected_markets,
            "markets": markets,
            "market_coverage": round(market_coverage, 8),
            "features": features,
            "feature_coverage": round(feature_coverage, 8),
            "resolved": resolved,
            "resolution_coverage": round(resolution_coverage, 8),
        },
        "technical_passed": technical_passed,
        "safety_passed": safety_passed,
        "technical_audit": {
            "database_opened_query_only": query_only == 1,
            "sqlite_quick_check": quick_check,
            "phase4_unchanged": phase4_hash_before == phase4_hash_after,
            "phase4_matches_prereg": phase4_hash_after == expected_phase4_hash,
            "runs": run_rows,
        },
        "database": str(database_path),
        "database_sha256": database_hash,
        "database_read_only_verified": database_hash == sha256_file(database_path),
        "preregistration": str(prereg_file),
        "preregistration_sha256": prereg_hash,
        "real_money": "BLOQUEADO",
        "wallet_required": False,
        "orders_created": False,
        "paper_orders": 0,
    }
    if output is not None:
        _write_atomic(output, result)
    return result


__all__ = [
    "MINIMUM_DOWN_TRADES",
    "MINIMUM_DOWN_TRADES_PER_HALF",
    "RESULT_SCHEMA",
    "audit_v026",
    "evaluate_v026",
]
