from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import statistics
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import joblib

from polymarket_bot.config import Settings
from polymarket_bot.phase41 import ShadowStore, run_shadow_forward, shadow_status
from polymarket_bot.runtime_policy import enforce_forward_duration
from polymarket_bot.v018_runner import ROOT, _parse_utc, sha256_file
from polymarket_bot.v022_runner import V022ProcessLock
from polymarket_bot.v025_audit import CONFIDENCE_Z, sequence_metrics
from polymarket_bot.v026_audit import evaluate_v026
from polymarket_bot.v026_runner import v026_arm_records
from polymarket_bot.v026_strategy import PRIMARY_ARM_ID, frozen_arm_config, validate_arm_config


PREREG_SCHEMA = "prereg_v026b_adaptive_checkpoints_1"
MAXIMUM_HOURS = 24.0
VARIANT = "V0.26B_DOWN_REPLICATION_ADAPTIVE_4H_OR_24H"
CHECKPOINT_HOURS = (4.0, 8.0, 12.0, 16.0, 20.0)
TECHNICAL_RETRY_MINUTES = 10.0
MINIMUM_FINAL_DOWN_TRADES = 15
MINIMUM_FIRST_HALF_DOWN_TRADES = 5
MINIMUM_PROFITABILITY_TRADES = 8
MINIMUM_MARKET_COVERAGE = 0.90
MINIMUM_FEATURE_COVERAGE = 0.90
MINIMUM_MATURE_RESOLUTION_COVERAGE = 0.90
MAXIMUM_HEALTH_AGE_SECONDS = 180.0
FUTILITY_ALPHA = 0.05


class V026BError(RuntimeError):
    pass


def _resolve_project_file(relative_path: Any, *, label: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise V026BError(f"Ruta V0.26b ausente: {label}")
    path = (ROOT / relative_path).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise V026BError(f"Ruta V0.26b fuera del proyecto: {label}") from exc
    if not path.is_file():
        raise V026BError(f"Archivo V0.26b ausente: {label}")
    return path


def load_and_verify_prereg(path: str | Path) -> dict[str, Any]:
    prereg_path = Path(path).resolve()
    payload = json.loads(prereg_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != PREREG_SCHEMA:
        raise V026BError("Prerregistro V0.26b incompatible")
    if payload.get("status") != "FROZEN_ADAPTIVE_FUTILITY_CHECKPOINTS":
        raise V026BError("V0.26b no esta congelado")
    if float(payload.get("maximum_hours", 0)) != MAXIMUM_HOURS:
        raise V026BError("V0.26b no puede exceder 24 horas")
    validate_arm_config(payload.get("arms"))

    stopping = payload.get("stopping")
    expected_stopping = {
        "maximum_hours": MAXIMUM_HOURS,
        "checkpoint_hours": list(CHECKPOINT_HOURS),
        "technical_retry_minutes": TECHNICAL_RETRY_MINUTES,
        "early_success_allowed": False,
        "early_stop_for_futility_only": True,
        "final_result_only": True,
        "intermediate_scheduled_reports": False,
    }
    if not isinstance(stopping, dict):
        raise V026BError("Contrato de parada V0.26b ausente")
    for key, expected in expected_stopping.items():
        if stopping.get(key) != expected:
            raise V026BError(f"Parada V0.26b incompatible: {key}")

    futility = payload.get("futility_contract")
    expected_futility = {
        "minimum_final_down_trades": MINIMUM_FINAL_DOWN_TRADES,
        "minimum_first_half_down_trades": MINIMUM_FIRST_HALF_DOWN_TRADES,
        "frequency_method": "one_sided_95_poisson_upper_projection_to_24h",
        "frequency_alpha": FUTILITY_ALPHA,
        "minimum_profitability_trades": MINIMUM_PROFITABILITY_TRADES,
        "profitability_method": "one_sided_95_mean_pnl_upper_bound_nonpositive",
        "technical_failure_requires_retry": True,
    }
    if not isinstance(futility, dict):
        raise V026BError("Contrato de futilidad V0.26b ausente")
    for key, expected in expected_futility.items():
        if futility.get(key) != expected:
            raise V026BError(f"Futilidad V0.26b incompatible: {key}")

    technical = payload.get("technical")
    expected_technical = {
        "minimum_market_coverage": MINIMUM_MARKET_COVERAGE,
        "minimum_feature_coverage": MINIMUM_FEATURE_COVERAGE,
        "minimum_mature_resolution_coverage": MINIMUM_MATURE_RESOLUTION_COVERAGE,
        "maximum_health_age_seconds": MAXIMUM_HEALTH_AGE_SECONDS,
        "sqlite_quick_check": "ok",
    }
    if not isinstance(technical, dict):
        raise V026BError("Contrato tecnico V0.26b ausente")
    for key, expected in expected_technical.items():
        if technical.get(key) != expected:
            raise V026BError(f"Tecnica V0.26b incompatible: {key}")

    safety = payload.get("safety")
    expected_safety = {
        "wallet_required": False,
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "real_money": "BLOQUEADO",
        "maximum_hours": 24,
    }
    if not isinstance(safety, dict):
        raise V026BError("Safety V0.26b ausente")
    for key, expected in expected_safety.items():
        if safety.get(key) != expected:
            raise V026BError(f"Safety V0.26b incumplido: {key}")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise V026BError("Artefactos V0.26b ausentes")
    for key in ("model", "phase4"):
        item = artifacts.get(key)
        if not isinstance(item, dict):
            raise V026BError(f"Artefacto V0.26b ausente: {key}")
        artifact_path = _resolve_project_file(item.get("relative_path"), label=key)
        if sha256_file(artifact_path) != item.get("sha256"):
            raise V026BError(f"Hash V0.26b no coincide: {key}")

    hashes = payload.get("code_hashes")
    if not isinstance(hashes, dict):
        raise V026BError("Hashes de codigo V0.26b ausentes")
    expected_hashes = {
        "strategy": sha256_file(ROOT / "src/polymarket_bot/v026_strategy.py"),
        "runner": sha256_file(Path(__file__)),
        "entrypoint": sha256_file(ROOT / "v026b_monitor.py"),
        "auditor": sha256_file(ROOT / "src/polymarket_bot/v026b_audit.py"),
        "collector_and_model_runtime": sha256_file(ROOT / "src/polymarket_bot/phase41.py"),
        "cost_runtime": sha256_file(ROOT / "src/polymarket_bot/phase4.py"),
        "duration_policy_runtime": sha256_file(ROOT / "src/polymarket_bot/runtime_policy.py"),
    }
    for key, actual in expected_hashes.items():
        if hashes.get(key) != actual:
            raise V026BError(f"Hash V0.26b no coincide: {key}")
    return payload


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _read_meta(path: Path) -> dict[str, Any]:
    connection = _open_read_only(path)
    try:
        return {
            str(key): json.loads(str(value))
            for key, value in connection.execute("SELECT key,value FROM shadow_meta")
        }
    finally:
        connection.close()


def _bind_database(*, database: Path, prereg: dict[str, Any], prereg_path: Path) -> None:
    existed = database.is_file()
    model_path = _resolve_project_file(prereg["artifacts"]["model"]["relative_path"], label="model")
    artifact = joblib.load(model_path)
    if (
        not isinstance(artifact, dict)
        or artifact.get("artifact_type") != "polymarket_shadow_forward_models"
        or artifact.get("orders_enabled") is not False
    ):
        raise V026BError("Modelo V0.26b incompatible o inseguro")
    store = ShadowStore(database)
    store.open(target_hours=MAXIMUM_HOURS, artifact=artifact)
    try:
        meta = store.meta()
        prereg_hash = sha256_file(prereg_path)
        if existed and meta.get("v026b_prereg_sha256") != prereg_hash:
            raise V026BError("Base V0.26b pertenece a otro prerregistro")
        if not existed:
            store.set_meta("v026b_prereg_sha256", prereg_hash)
            store.set_meta("v026b_variant", VARIANT)
            store.set_meta("v026b_arms", prereg["arms"])
            # Reutiliza el lector estable de brazos V0.26 sin cambiar su regla.
            store.set_meta("v026_arms", prereg["arms"])
            store.set_meta("v026b_completion_reason", None)
            store.set_meta("v026b_checkpoints", [])
            store.set_meta("v026b_real_money", "BLOQUEADO")
    finally:
        store.close()


def poisson_mean_upper_bound(count: int, *, alpha: float = FUTILITY_ALPHA) -> float:
    if count < 0 or not 0 < alpha < 1:
        raise ValueError("Parametros Poisson invalidos")

    def cdf(mean: float) -> float:
        term = math.exp(-mean)
        total = term
        for index in range(1, count + 1):
            term *= mean / index
            total += term
        return total

    low = 0.0
    high = max(1.0, float(count + 1))
    while cdf(high) > alpha:
        high *= 2.0
    for _ in range(100):
        midpoint = (low + high) / 2.0
        if cdf(midpoint) > alpha:
            low = midpoint
        else:
            high = midpoint
    return high


def mean_pnl_upper_bound(rows: Sequence[Mapping[str, Any]]) -> float | None:
    if not rows:
        return None
    pnls = [
        (1.0 - float(row["entry_cost"]))
        if str(row["favorite_side"]) == str(row["label"])
        else -float(row["entry_cost"])
        for row in rows
    ]
    mean = statistics.fmean(pnls)
    deviation = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    return mean + CONFIDENCE_Z * deviation / math.sqrt(len(pnls))


def checkpoint_decision(
    *,
    checkpoint_hour: float,
    captured_down: int,
    captured_first_half_down: int,
    resolved_down_rows: Sequence[Mapping[str, Any]],
    technical_passed: bool,
    safety_passed: bool,
) -> dict[str, Any]:
    upper_count = poisson_mean_upper_bound(captured_down)
    projected_upper_total = upper_count * MAXIMUM_HOURS / checkpoint_hour
    frequency_futile = projected_upper_total < MINIMUM_FINAL_DOWN_TRADES
    first_half_closed_futile = (
        checkpoint_hour >= MAXIMUM_HOURS / 2
        and captured_first_half_down < MINIMUM_FIRST_HALF_DOWN_TRADES
    )
    pnl_ucb = mean_pnl_upper_bound(resolved_down_rows)
    profitability_futile = (
        len(resolved_down_rows) >= MINIMUM_PROFITABILITY_TRADES
        and pnl_ucb is not None
        and pnl_ucb <= 0
    )
    if not safety_passed:
        decision = "FREEZE_SAFETY"
    elif not technical_passed:
        decision = "RETRY_TECHNICAL"
    elif first_half_closed_futile:
        decision = "FREEZE_FIRST_HALF_FREQUENCY"
    elif frequency_futile:
        decision = "FREEZE_FREQUENCY_FUTILITY"
    elif profitability_futile:
        decision = "FREEZE_NEGATIVE_FUTILITY"
    else:
        decision = "CONTINUE"
    return {
        "decision": decision,
        "frequency": {
            "captured_down": captured_down,
            "captured_first_half_down": captured_first_half_down,
            "poisson_one_sided_95_upper_count": round(upper_count, 8),
            "projected_24h_upper_down_trades": round(projected_upper_total, 8),
            "minimum_final_down_trades": MINIMUM_FINAL_DOWN_TRADES,
            "frequency_futile": frequency_futile,
            "first_half_closed_futile": first_half_closed_futile,
        },
        "profitability": {
            "resolved_down_trades": len(resolved_down_rows),
            "minimum_trades_before_futility": MINIMUM_PROFITABILITY_TRADES,
            "one_sided_95_mean_pnl_ucb": round(pnl_ucb, 8) if pnl_ucb is not None else None,
            "profitability_futile": profitability_futile,
        },
    }


def _technical_snapshot(
    *, database: Path, prereg: Mapping[str, Any], checkpoint_at: datetime
) -> dict[str, Any]:
    connection = _open_read_only(database)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        counts = connection.execute(
            """
            SELECT COUNT(*) AS markets,
             SUM(CASE WHEN feature_status='SAVED' THEN 1 ELSE 0 END) AS features,
             MIN(market_start_ms) AS first_start
            FROM shadow_markets
            """
        ).fetchone()
        mature = connection.execute(
            """
            SELECT COUNT(*) AS markets,
             SUM(CASE WHEN label_verified=1 THEN 1 ELSE 0 END) AS resolved
            FROM shadow_markets WHERE market_end_ms<=?
            """,
            (int(checkpoint_at.timestamp() * 1000) - 600_000,),
        ).fetchone()
        health = connection.execute(
            """
            SELECT recorded_at,connections_json FROM shadow_health
            ORDER BY recorded_at DESC LIMIT 1
            """
        ).fetchone()
        meta = {
            str(key): json.loads(str(value))
            for key, value in connection.execute("SELECT key,value FROM shadow_meta")
        }
    finally:
        connection.close()
    markets = int(counts["markets"] or 0)
    features = int(counts["features"] or 0)
    first_start = counts["first_start"]
    checkpoint_ms = int(checkpoint_at.timestamp() * 1000)
    expected_markets = (
        int((checkpoint_ms - int(first_start)) // 300_000) + 1
        if first_start is not None
        else 0
    )
    market_coverage = min(1.0, markets / expected_markets) if expected_markets else 0.0
    feature_coverage = features / markets if markets else 0.0
    mature_markets = int(mature["markets"] or 0)
    mature_resolved = int(mature["resolved"] or 0)
    mature_resolution_coverage = (
        mature_resolved / mature_markets if mature_markets else 1.0
    )
    connections = json.loads(str(health["connections_json"])) if health else {}
    health_age = (
        max(0.0, (checkpoint_at - _parse_utc(str(health["recorded_at"]))).total_seconds())
        if health
        else math.inf
    )
    phase4_path = _resolve_project_file(
        prereg["artifacts"]["phase4"]["relative_path"], label="phase4"
    )
    phase4_matches = sha256_file(phase4_path) == prereg["artifacts"]["phase4"]["sha256"]
    gates = {
        "sqlite_quick_check_passed": quick_check == "ok",
        "market_coverage_passed": market_coverage >= MINIMUM_MARKET_COVERAGE,
        "feature_coverage_passed": feature_coverage >= MINIMUM_FEATURE_COVERAGE,
        "mature_resolution_coverage_passed": (
            mature_resolution_coverage >= MINIMUM_MATURE_RESOLUTION_COVERAGE
        ),
        "health_fresh_passed": health_age <= MAXIMUM_HEALTH_AGE_SECONDS,
        "rtds_connected_passed": connections.get("shadow-rtds") == "CONNECTED",
        "binance_connected_passed": connections.get("shadow-binance") == "CONNECTED",
        "phase4_unchanged_passed": phase4_matches,
    }
    safety_gates = {
        "orders_disabled_passed": meta.get("orders_enabled") is False,
        "money_real_disabled_passed": meta.get("money_real_enabled") is False,
        "wallet_not_required_passed": meta.get("wallet_required") is False,
        "v026b_real_money_blocked_passed": meta.get("v026b_real_money") == "BLOQUEADO",
    }
    return {
        "passed": all(gates.values()),
        "gates": gates,
        "sqlite_quick_check": quick_check,
        "expected_markets": expected_markets,
        "markets": markets,
        "market_coverage": round(market_coverage, 8),
        "features": features,
        "feature_coverage": round(feature_coverage, 8),
        "mature_markets": mature_markets,
        "mature_resolved": mature_resolved,
        "mature_resolution_coverage": round(mature_resolution_coverage, 8),
        "latest_connections": connections,
        "health_age_seconds": round(health_age, 3) if math.isfinite(health_age) else None,
        "safety_passed": all(safety_gates.values()),
        "safety_gates": safety_gates,
    }


def checkpoint_snapshot(
    *, database: str | Path, prereg: Mapping[str, Any], checkpoint_hour: float
) -> dict[str, Any]:
    database_path = Path(database).resolve()
    meta = _read_meta(database_path)
    start = _parse_utc(str(meta["experiment_started_at"]))
    checkpoint_at = start + timedelta(hours=checkpoint_hour)
    evaluated_at = datetime.now(timezone.utc)
    midpoint_ms = int((start.timestamp() + MAXIMUM_HOURS * 1800) * 1000)
    records = v026_arm_records(database_path)
    resolved = {
        arm_id: [row for row in rows if int(row["label_verified"]) == 1]
        for arm_id, rows in records.items()
    }
    down_rows = records[PRIMARY_ARM_ID]
    resolved_down = resolved[PRIMARY_ARM_ID]
    captured_first_half = sum(
        int(row["market_start_ms"]) < midpoint_ms for row in down_rows
    )
    technical = _technical_snapshot(
        database=database_path, prereg=prereg, checkpoint_at=evaluated_at
    )
    decision = checkpoint_decision(
        checkpoint_hour=checkpoint_hour,
        captured_down=len(down_rows),
        captured_first_half_down=captured_first_half,
        resolved_down_rows=resolved_down,
        technical_passed=bool(technical["passed"]),
        safety_passed=bool(technical["safety_passed"]),
    )
    return {
        "schema": "checkpoint_v026b_1",
        "variant": VARIANT,
        "checkpoint_hour": checkpoint_hour,
        "checkpoint_at": checkpoint_at.isoformat(timespec="seconds"),
        "evaluated_at": evaluated_at.isoformat(timespec="seconds"),
        "captured_arm_counts": {key: len(value) for key, value in records.items()},
        "resolved_arm_counts": {key: len(value) for key, value in resolved.items()},
        "primary_metrics": sequence_metrics(resolved_down),
        "full_descriptive_evaluation": evaluate_v026(resolved, midpoint_ms=midpoint_ms),
        "technical": technical,
        **decision,
        "orders_created": False,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


def _save_checkpoint(database: Path, snapshot: Mapping[str, Any]) -> None:
    connection = sqlite3.connect(database, timeout=60)
    try:
        row = connection.execute(
            "SELECT value FROM shadow_meta WHERE key='v026b_checkpoints'"
        ).fetchone()
        checkpoints = json.loads(str(row[0])) if row else []
        attempt = int(snapshot.get("attempt", 1))
        checkpoints = [
            item
            for item in checkpoints
            if not (
                float(item.get("checkpoint_hour", -1))
                == float(snapshot["checkpoint_hour"])
                and int(item.get("attempt", 1)) == attempt
            )
        ]
        checkpoints.append(dict(snapshot))
        checkpoints.sort(key=lambda item: float(item["checkpoint_hour"]))
        connection.execute(
            "INSERT OR REPLACE INTO shadow_meta(key,value) VALUES(?,?)",
            ("v026b_checkpoints", json.dumps(checkpoints, ensure_ascii=False, separators=(",", ":"))),
        )
        connection.commit()
    finally:
        connection.close()


def _mark_completion(database: Path, *, reason: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection = sqlite3.connect(database, timeout=60)
    try:
        values = {
            "v026b_observation_ended_at": now,
            "v026b_completion_reason": reason,
        }
        connection.executemany(
            "INSERT OR REPLACE INTO shadow_meta(key,value) VALUES(?,?)",
            [
                (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))
                for key, value in values.items()
            ],
        )
        connection.commit()
    finally:
        connection.close()


async def _wait_until_checkpoint(
    collector: asyncio.Task[dict[str, Any]], due_timestamp: float
) -> bool:
    delay = max(0.0, due_timestamp - time.time())
    try:
        await asyncio.wait_for(asyncio.shield(collector), timeout=delay)
        return False
    except TimeoutError:
        return True


async def run_v026b(
    *, settings: Settings, prereg_path: str | Path, output_db: str | Path
) -> dict[str, Any]:
    prereg_file = Path(prereg_path).resolve()
    prereg = load_and_verify_prereg(prereg_file)
    database = Path(output_db).resolve()
    enforce_forward_duration(MAXIMUM_HOURS, database)
    lock = V022ProcessLock(f"{database}.lock")
    lock.acquire()
    try:
        _bind_database(database=database, prereg=prereg, prereg_path=prereg_file)
        meta = _read_meta(database)
        start_timestamp = _parse_utc(str(meta["experiment_started_at"])).timestamp()
        completed_hours = {
            float(item["checkpoint_hour"])
            for item in meta.get("v026b_checkpoints", [])
            if item.get("decision") != "RETRY_TECHNICAL"
        }
        model_path = _resolve_project_file(
            prereg["artifacts"]["model"]["relative_path"], label="model"
        )
        collector = asyncio.create_task(
            run_shadow_forward(
                settings=settings,
                model_file=model_path,
                output_db=database,
                target_hours=MAXIMUM_HOURS,
            )
        )
        freeze_reason: str | None = None
        try:
            for checkpoint_hour in CHECKPOINT_HOURS:
                if checkpoint_hour in completed_hours:
                    continue
                due = start_timestamp + checkpoint_hour * 3600
                if not await _wait_until_checkpoint(collector, due):
                    break
                snapshot = checkpoint_snapshot(
                    database=database, prereg=prereg, checkpoint_hour=checkpoint_hour
                )
                snapshot["attempt"] = 1
                _save_checkpoint(database, snapshot)
                decision = str(snapshot["decision"])
                if decision == "RETRY_TECHNICAL":
                    retry_due = time.time() + TECHNICAL_RETRY_MINUTES * 60
                    if not await _wait_until_checkpoint(collector, retry_due):
                        break
                    retry = checkpoint_snapshot(
                        database=database,
                        prereg=prereg,
                        checkpoint_hour=checkpoint_hour,
                    )
                    retry["attempt"] = 2
                    retry["technical_retry"] = True
                    if retry["decision"] == "RETRY_TECHNICAL":
                        retry["decision"] = "FREEZE_TECHNICAL_FAILURE"
                    _save_checkpoint(database, retry)
                    snapshot = retry
                    decision = str(snapshot["decision"])
                if decision != "CONTINUE":
                    freeze_reason = decision
                    collector.cancel()
                    break
            try:
                collector_result = await collector
            except asyncio.CancelledError:
                if freeze_reason is None:
                    raise
                stopped = shadow_status(database)
                collector_result = {
                    "status": "INTERRUPTED",
                    "error": None,
                    "safety_stop_reason": None,
                    "database": str(database),
                    "database_bytes": stopped.get("database_bytes"),
                    "quick_check": stopped.get("sqlite_quick_check"),
                    "experiment_started_at": stopped.get("experiment_started_at"),
                    "target_end_at": stopped.get("target_end_at"),
                    "target_hours": MAXIMUM_HOURS,
                    "reanudable": False,
                }
        except BaseException:
            if not collector.done():
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)
            raise
        if freeze_reason is not None:
            _mark_completion(database, reason=freeze_reason)
        elif collector_result.get("status") == "COMPLETED":
            _mark_completion(database, reason="FULL_24H_REACHED")
        elif collector_result.get("status") == "SAFETY_STOP":
            _mark_completion(database, reason="FREEZE_COLLECTOR_SAFETY_STOP")
        completion_reason = freeze_reason
        if collector_result.get("status") == "COMPLETED":
            completion_reason = "FULL_24H_REACHED"
        elif collector_result.get("status") == "SAFETY_STOP":
            completion_reason = "FREEZE_COLLECTOR_SAFETY_STOP"
        result = dict(collector_result)
        result.update(
            {
                "variant": VARIANT,
                "maximum_hours": MAXIMUM_HOURS,
                "completion_reason": completion_reason,
                "orders_created": False,
                "paper_orders": 0,
                "wallet_required": False,
                "real_money": "BLOQUEADO",
            }
        )
        return result
    finally:
        lock.release()


def v026b_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).resolve()
    empty_counts = {str(item["id"]): 0 for item in frozen_arm_config()}
    if not database.is_file():
        return {
            "status": "NOT_STARTED",
            "variant": VARIANT,
            "database": str(database),
            "maximum_hours": MAXIMUM_HOURS,
            "checkpoint_hours": list(CHECKPOINT_HOURS),
            "captured_arm_counts": empty_counts,
            "resolved_arm_counts": empty_counts,
            "orders_created": False,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
    base = shadow_status(database)
    meta = _read_meta(database)
    records = v026_arm_records(database)
    captured = {key: len(value) for key, value in records.items()}
    resolved = {
        key: sum(int(row["label_verified"]) == 1 for row in value)
        for key, value in records.items()
    }
    target_end = _parse_utc(str(meta["target_end_at"])).timestamp()
    checkpoints = meta.get("v026b_checkpoints", [])
    completion_reason = meta.get("v026b_completion_reason")
    base.update(
        {
            "status": "COMPLETED_OR_FROZEN" if completion_reason else "RUNNING_OR_RESUMABLE",
            "variant": VARIANT,
            "maximum_hours": MAXIMUM_HOURS,
            "checkpoint_hours": list(CHECKPOINT_HOURS),
            "completion_reason": completion_reason,
            "remaining_hours": max(0.0, target_end - time.time()) / 3600,
            "captured_arm_counts": captured,
            "resolved_arm_counts": resolved,
            "completed_checkpoints": len(checkpoints),
            "latest_checkpoint": checkpoints[-1] if checkpoints else None,
            "orders_created": False,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
    )
    return base


__all__ = [
    "CHECKPOINT_HOURS",
    "MAXIMUM_HOURS",
    "PREREG_SCHEMA",
    "VARIANT",
    "V026BError",
    "checkpoint_decision",
    "checkpoint_snapshot",
    "load_and_verify_prereg",
    "mean_pnl_upper_bound",
    "poisson_mean_upper_bound",
    "run_v026b",
    "v026b_status",
]
