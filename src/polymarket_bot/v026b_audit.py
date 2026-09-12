from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import _parse_utc, sha256_file
from polymarket_bot.v026_audit import evaluate_v026
from polymarket_bot.v026_runner import v026_arm_records
from polymarket_bot.v026_strategy import PRIMARY_ARM_ID
from polymarket_bot.v026b_runner import (
    MAXIMUM_HOURS,
    VARIANT,
    _read_meta,
    _technical_snapshot,
    load_and_verify_prereg,
)


RESULT_SCHEMA = "result_v026b_adaptive_checkpoints_1"
FINAL_REASONS = {
    "FULL_24H_REACHED",
    "FREEZE_SAFETY",
    "FREEZE_TECHNICAL_FAILURE",
    "FREEZE_FIRST_HALF_FREQUENCY",
    "FREEZE_FREQUENCY_FUTILITY",
    "FREEZE_NEGATIVE_FUTILITY",
    "FREEZE_COLLECTOR_SAFETY_STOP",
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


def audit_v026b(
    *,
    database: str | Path,
    prereg_path: str | Path,
    result_path: str | Path | None = None,
) -> dict[str, Any]:
    database_path = Path(database).resolve()
    prereg_file = Path(prereg_path).resolve()
    output = Path(result_path).resolve() if result_path is not None else None
    prereg = load_and_verify_prereg(prereg_file)
    if not database_path.is_file():
        raise RuntimeError("Base V0.26b no encontrada")
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
        raise RuntimeError("Existe un resultado V0.26b para otra evidencia")

    meta = _read_meta(database_path)
    completion_reason = meta.get("v026b_completion_reason")
    if completion_reason not in FINAL_REASONS:
        raise RuntimeError("V0.26b todavia no completo ni fue congelado")
    start = _parse_utc(str(meta["experiment_started_at"]))
    target_end = _parse_utc(str(meta["target_end_at"]))
    observation_end = _parse_utc(str(meta["v026b_observation_ended_at"]))
    cutoff = target_end if completion_reason == "FULL_24H_REACHED" else observation_end
    cutoff_ms = int(cutoff.timestamp() * 1000)
    midpoint_ms = int((start.timestamp() + MAXIMUM_HOURS * 1800) * 1000)
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
    technical = _technical_snapshot(
        database=database_path, prereg=prereg, checkpoint_at=observation_end
    )

    connection = _open_read_only(database_path)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        counts = connection.execute(
            """
            SELECT COUNT(*) AS markets,
             SUM(CASE WHEN feature_status='SAVED' THEN 1 ELSE 0 END) AS features,
             SUM(CASE WHEN label_verified=1 THEN 1 ELSE 0 END) AS resolved,
             MIN(market_start_ms) AS first_start
            FROM shadow_markets WHERE market_start_ms<=?
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
    resolved = int(counts["resolved"] or 0)
    first_start = counts["first_start"]
    expected_markets = (
        int((cutoff_ms - int(first_start)) // 300_000) + 1
        if first_start is not None
        else 0
    )
    market_coverage = min(1.0, markets / expected_markets) if expected_markets else 0.0
    feature_coverage = features / markets if markets else 0.0
    resolution_coverage = resolved / markets if markets else 0.0
    safety_passed = bool(technical["safety_passed"])
    full_technical_passed = (
        completion_reason == "FULL_24H_REACHED"
        and bool(technical["passed"])
        and query_only == 1
        and quick_check == "ok"
        and market_coverage >= 0.90
        and feature_coverage >= 0.90
        and resolution_coverage == 1.0
        and bool(run_rows)
        and run_rows[-1]["status"] == "COMPLETED"
    )
    if completion_reason == "FREEZE_SAFETY" or not safety_passed:
        verdict = "FAIL_SAFETY"
    elif completion_reason == "FREEZE_COLLECTOR_SAFETY_STOP":
        verdict = "FROZEN_COLLECTOR_SAFETY_STOP"
    elif completion_reason == "FREEZE_TECHNICAL_FAILURE":
        verdict = "FROZEN_TECHNICAL_FAILURE"
    elif completion_reason in {
        "FREEZE_FIRST_HALF_FREQUENCY",
        "FREEZE_FREQUENCY_FUTILITY",
    }:
        verdict = "FROZEN_INSUFFICIENT_FREQUENCY"
    elif completion_reason == "FREEZE_NEGATIVE_FUTILITY":
        verdict = "FROZEN_NEGATIVE_FUTILITY"
    elif not full_technical_passed:
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
        "FAIL_SAFETY": "Se detecto un incumplimiento de seguridad.",
        "FROZEN_COLLECTOR_SAFETY_STOP": "El colector se detuvo por su limite de seguridad.",
        "FROZEN_TECHNICAL_FAILURE": "Dos revisiones tecnicas separadas por diez minutos fallaron; queda congelado para modificar.",
        "FROZEN_INSUFFICIENT_FREQUENCY": "El limite optimista al 95% ya no proyecta las 15 operaciones Down o la primera mitad no alcanzo cinco.",
        "FROZEN_NEGATIVE_FUTILITY": "Con al menos ocho operaciones resueltas, incluso el limite optimista al 95% del PnL medio fue no positivo.",
        "FAIL_TECHNICAL_QUALITY": "La ventana completa no tiene integridad o cobertura suficiente.",
        "FAIL_INSUFFICIENT_FREQUENCY": "La ventana completa no alcanzo 15 Down y cinco por mitad.",
        "FAIL_REPLICATION": "La ventaja Down no se repitio en todos los gates economicos.",
        "CONTINUE_PAPER_ACCUMULATION": "La economia se repitio, pero la confianza aun no permite promocion.",
        "PASS_STATISTICAL_PAPER_CANDIDATE": "La replica economica y ambos limites de confianza pasaron; dinero real sigue bloqueado.",
    }
    selected = (
        PRIMARY_ARM_ID
        if verdict == "PASS_STATISTICAL_PAPER_CANDIDATE"
        else None
    )
    checkpoints = meta.get("v026b_checkpoints", [])
    result = {
        "schema": RESULT_SCHEMA,
        "variant": VARIANT,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": verdict,
        "meaning": meanings[verdict],
        "selected_strategy": selected,
        "replicated_strategy": (
            PRIMARY_ARM_ID if evaluation["economic_replication_passed"] else None
        ),
        "paper_accumulation_candidate": evaluation["economic_replication_passed"],
        "paper_forward_candidate": selected is not None,
        "money_real_candidate": False,
        **evaluation,
        "window": {
            "maximum_hours": MAXIMUM_HOURS,
            "adaptive_futility_checkpoints": True,
            "checkpoint_hours": prereg["stopping"]["checkpoint_hours"],
            "completion_reason": completion_reason,
            "experiment_started_at": start.isoformat(timespec="seconds"),
            "target_end_at": target_end.isoformat(timespec="seconds"),
            "observation_ended_at": observation_end.isoformat(timespec="seconds"),
            "elapsed_wall_hours": round(
                (observation_end - start).total_seconds() / 3600, 8
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
        },
        "technical_passed": full_technical_passed,
        "technical_snapshot": technical,
        "safety_passed": safety_passed,
        "technical_audit": {
            "database_opened_query_only": query_only == 1,
            "sqlite_quick_check": quick_check,
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


__all__ = ["RESULT_SCHEMA", "audit_v026b"]
