from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import _parse_utc, sha256_file
from polymarket_bot.v025_runner import (
    MAXIMUM_HOURS,
    TARGET_PRIMARY_TRADES,
    VARIANT,
    load_and_verify_prereg,
    v025_arm_records,
)
from polymarket_bot.v025_strategy import PAPER_SHARES, PRIMARY_ARM_ID


RESULT_SCHEMA = "result_v025_down_asymmetry_1"
CONFIDENCE_Z = 1.6448536269514715


def sequence_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda item: int(item["market_start_ms"]))
    if not ordered:
        return {
            "trades": 0,
            "wins": 0,
            "win_rate": None,
            "net_pnl_per_share_sequence": 0.0,
            "net_pnl_at_5_shares": 0.0,
            "roi_on_cost": None,
            "profit_factor": None,
            "mean_pnl_per_share": None,
            "pnl_standard_deviation": None,
            "one_sided_95_lcb": None,
            "maximum_drawdown_per_share": 0.0,
            "first_five": {"trades": 0, "net_pnl_per_share_sequence": 0.0},
            "last_five": {"trades": 0, "net_pnl_per_share_sequence": 0.0},
        }
    pnls: list[float] = []
    total_cost = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    cumulative = 0.0
    peak = 0.0
    maximum_drawdown = 0.0
    wins = 0
    for row in ordered:
        cost = float(row["entry_cost"])
        won = str(row["favorite_side"]) == str(row["label"])
        pnl = 1.0 - cost if won else -cost
        pnls.append(pnl)
        total_cost += cost
        wins += int(won)
        gross_profit += max(0.0, pnl)
        gross_loss += max(0.0, -pnl)
        cumulative += pnl
        peak = max(peak, cumulative)
        maximum_drawdown = max(maximum_drawdown, peak - cumulative)
    mean = statistics.fmean(pnls)
    deviation = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    standard_error = deviation / math.sqrt(len(pnls))
    net = sum(pnls)
    first = pnls[:5]
    last = pnls[-5:]
    return {
        "trades": len(pnls),
        "wins": wins,
        "win_rate": round(wins / len(pnls), 8),
        "net_pnl_per_share_sequence": round(net, 8),
        "net_pnl_at_5_shares": round(net * PAPER_SHARES, 8),
        "roi_on_cost": round(net / total_cost, 8) if total_cost else None,
        "profit_factor": (
            round(gross_profit / gross_loss, 8) if gross_loss else None
        ),
        "mean_pnl_per_share": round(mean, 8),
        "pnl_standard_deviation": round(deviation, 8),
        "one_sided_95_lcb": round(mean - CONFIDENCE_Z * standard_error, 8),
        "maximum_drawdown_per_share": round(maximum_drawdown, 8),
        "first_five": {
            "trades": len(first),
            "net_pnl_per_share_sequence": round(sum(first), 8),
        },
        "last_five": {
            "trades": len(last),
            "net_pnl_per_share_sequence": round(sum(last), 8),
        },
    }


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


def audit_v025(
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
        raise RuntimeError("Base V0.25 no encontrada")
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
        raise RuntimeError("Existe un resultado V0.25 para otra evidencia")

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
        completion_reason = meta.get("v025_completion_reason")
        completed_at = meta.get("experiment_completed_at")
        cutoff_meta = meta.get("v025_effective_cutoff_market_start_ms")
    finally:
        connection.close()
    if completion_reason not in {
        "TARGET_PRIMARY_TRADES_REACHED",
        "MAXIMUM_HOURS_REACHED",
    } or not completed_at:
        raise RuntimeError("V0.25 todavia no completo su contrato de parada")

    arm_records = v025_arm_records(database_path)
    primary_resolved = [
        row
        for row in arm_records[PRIMARY_ARM_ID]
        if int(row["label_verified"]) == 1
    ]
    primary_resolved.sort(key=lambda row: int(row["market_start_ms"]))
    if len(primary_resolved) >= TARGET_PRIMARY_TRADES:
        cutoff = int(primary_resolved[TARGET_PRIMARY_TRADES - 1]["market_start_ms"])
    else:
        cutoff = int(cutoff_meta)
    if int(cutoff_meta) != cutoff:
        raise RuntimeError("Cutoff V0.25 no coincide con la muestra congelada")

    evaluated: dict[str, list[dict[str, Any]]] = {}
    for arm_id, rows in arm_records.items():
        resolved = [
            row
            for row in rows
            if int(row["label_verified"]) == 1
            and int(row["market_start_ms"]) <= cutoff
        ]
        resolved.sort(key=lambda row: int(row["market_start_ms"]))
        evaluated[arm_id] = (
            resolved[:TARGET_PRIMARY_TRADES]
            if arm_id == PRIMARY_ARM_ID and len(resolved) >= TARGET_PRIMARY_TRADES
            else resolved
        )

    connection = _open_read_only(database_path)
    try:
        first_start = connection.execute(
            "SELECT MIN(market_start_ms) FROM shadow_markets WHERE market_start_ms<=?",
            (cutoff,),
        ).fetchone()[0]
        if first_start is None:
            raise RuntimeError("V0.25 no contiene mercados antes del cutoff")
        counts = connection.execute(
            """
            SELECT COUNT(*) AS markets,
             SUM(CASE WHEN feature_status='SAVED' THEN 1 ELSE 0 END) AS features,
             SUM(CASE WHEN label_verified=1 THEN 1 ELSE 0 END) AS resolved
            FROM shadow_markets
            WHERE market_start_ms>=? AND market_start_ms<=?
            """,
            (int(first_start), cutoff),
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
                FROM shadow_runs ORDER BY run_id
                """
            )
        ]
    finally:
        connection.close()

    markets = int(counts["markets"] or 0)
    features = int(counts["features"] or 0)
    resolved_markets = int(counts["resolved"] or 0)
    expected_markets = int((cutoff - int(first_start)) // 300_000) + 1
    market_coverage = markets / expected_markets if expected_markets else 0.0
    feature_coverage = features / markets if markets else 0.0
    resolution_coverage = resolved_markets / markets if markets else 0.0
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
        and meta.get("v025_real_money") == "BLOQUEADO"
        and meta.get("wallet_required") is False
    )

    primary_metrics = sequence_metrics(evaluated[PRIMARY_ARM_ID])
    factor = primary_metrics["profit_factor"]
    gates = {
        "minimum_trades_passed": primary_metrics["trades"] >= 10,
        "positive_net_pnl_passed": (
            primary_metrics["net_pnl_per_share_sequence"] > 0
        ),
        "positive_one_sided_95_lcb_passed": (
            primary_metrics["one_sided_95_lcb"] is not None
            and primary_metrics["one_sided_95_lcb"] > 0
        ),
        "profit_factor_above_one_passed": factor is None or factor > 1.0,
        "positive_first_five_passed": (
            primary_metrics["first_five"]["trades"] == 5
            and primary_metrics["first_five"]["net_pnl_per_share_sequence"] > 0
        ),
        "positive_last_five_passed": (
            primary_metrics["last_five"]["trades"] == 5
            and primary_metrics["last_five"]["net_pnl_per_share_sequence"] > 0
        ),
    }
    primary_passed = safety_passed and technical_passed and all(gates.values())
    if not safety_passed:
        verdict = "FAIL_SAFETY"
    elif not technical_passed:
        verdict = "FAIL_TECHNICAL_QUALITY"
    elif primary_metrics["trades"] < TARGET_PRIMARY_TRADES:
        verdict = "FAIL_INSUFFICIENT_FREQUENCY"
    elif not primary_passed:
        verdict = "FAIL_DOWN_ASYMMETRY"
    else:
        verdict = "PASS_DOWN_ASYMMETRY_PAPER_CANDIDATE"
    meanings = {
        "FAIL_SAFETY": "La prueba incumplio el bloqueo operativo.",
        "FAIL_TECHNICAL_QUALITY": "La integridad o cobertura no permite evaluar.",
        "FAIL_INSUFFICIENT_FREQUENCY": "No llegaron diez operaciones Down resueltas antes de doce horas.",
        "FAIL_DOWN_ASYMMETRY": "La muestra Down llego, pero no paso PnL, confianza y ambas mitades.",
        "PASS_DOWN_ASYMMETRY_PAPER_CANDIDATE": "La hipotesis Down pasa para mas validacion paper; dinero real sigue bloqueado.",
    }
    start = _parse_utc(str(meta["experiment_started_at"]))
    end = _parse_utc(str(completed_at))
    result = {
        "schema": RESULT_SCHEMA,
        "variant": VARIANT,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": verdict,
        "meaning": meanings[verdict],
        "selected_strategy": PRIMARY_ARM_ID if primary_passed else None,
        "paper_forward_candidate": primary_passed,
        "money_real_candidate": False,
        "primary": {
            "strategy_id": PRIMARY_ARM_ID,
            "metrics": primary_metrics,
            "gates": gates,
            "passed": primary_passed,
        },
        "controls": {
            arm_id: {
                "metrics": sequence_metrics(rows),
                "selectable": False,
            }
            for arm_id, rows in evaluated.items()
            if arm_id != PRIMARY_ARM_ID
        },
        "stopping": {
            "completion_reason": completion_reason,
            "target_resolved_primary_trades": TARGET_PRIMARY_TRADES,
            "maximum_hours": MAXIMUM_HOURS,
            "captured_primary_trades": len(arm_records[PRIMARY_ARM_ID]),
            "resolved_primary_trades": len(primary_resolved),
            "evaluated_primary_trades": len(evaluated[PRIMARY_ARM_ID]),
            "effective_cutoff_market_start_ms": cutoff,
            "elapsed_wall_hours": round((end - start).total_seconds() / 3600, 8),
        },
        "coverage": {
            "expected_markets_between_first_and_cutoff": expected_markets,
            "markets": markets,
            "market_coverage": round(market_coverage, 8),
            "features": features,
            "feature_coverage": round(feature_coverage, 8),
            "resolved": resolved_markets,
            "resolution_coverage": round(resolution_coverage, 8),
            "later_arm_memberships_excluded_by_cutoff": sum(
                int(row["market_start_ms"]) > cutoff
                for rows in arm_records.values()
                for row in rows
            ),
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
    "CONFIDENCE_Z",
    "RESULT_SCHEMA",
    "audit_v025",
    "sequence_metrics",
]
