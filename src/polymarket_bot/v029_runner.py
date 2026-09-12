from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.config import Settings
from polymarket_bot.runtime_policy import enforce_forward_duration
from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v022_runner import V022ProcessLock
from polymarket_bot.v029_forward import (
    V029_MAXIMUM_HOURS,
    V029_REQUIRED_GLOBAL_FEEDS,
    V029_REQUIRED_TWAP_WINDOW_SECONDS,
    V029Store,
    open_read_only,
    parse_utc,
    read_v029_meta,
    run_v029_forward,
)
from polymarket_bot.v029_prereg import load_and_verify_frozen_prereg


VARIANT = "V0.29_HIGH_FREQUENCY_TEMPORAL_HOLDOUT_24H"
CHECKPOINT_HOURS = (4.0, 8.0, 12.0, 16.0, 20.0)
TECHNICAL_RETRY_MINUTES = 10.0
MINIMUM_MARKET_COVERAGE = 0.90
MINIMUM_FEATURE_COVERAGE = 0.90
MINIMUM_CONTRACT_COVERAGE = 0.90
MINIMUM_MATURE_RESOLUTION_COVERAGE = 0.90
MAXIMUM_HEALTH_AGE_SECONDS = 180.0
IMPLEMENTATION_SCHEMA = "implementation_v029_high_frequency_temporal_holdout_1"
LAUNCH_SCHEMA = "launch_approval_v029_high_frequency_temporal_holdout_1"
IMPLEMENTATION_FILES = {
    "forward": "src/polymarket_bot/v029_forward.py",
    "runner": "src/polymarket_bot/v029_runner.py",
    "auditor": "src/polymarket_bot/v029_audit.py",
    "entrypoint": "v029_monitor.py",
    "strategy": "src/polymarket_bot/v029_strategy.py",
    "resolution_contract": "src/polymarket_bot/resolution_contract.py",
}


class V029RunnerError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_implementation_manifest(
    *,
    prereg_path: str | Path,
    output_path: str | Path,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    output = Path(output_path).resolve()
    prereg = load_and_verify_frozen_prereg(prereg_file, project_root=root)
    code_hashes: dict[str, str] = {}
    for key, relative in IMPLEMENTATION_FILES.items():
        path = root / relative
        if not path.is_file():
            raise V029RunnerError(f"Implementacion V0.29 incompleta: {relative}")
        code_hashes[key] = sha256_file(path)
    payload = {
        "schema": IMPLEMENTATION_SCHEMA,
        "status": "BUILT_TESTED_AWAITING_LAUNCH_APPROVAL",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "preregistration": str(prereg_file),
        "preregistration_sha256": sha256_file(prereg_file),
        "frozen_prereg_implementation_state": prereg["implementation"],
        "code_hashes": code_hashes,
        "runner_built": True,
        "auditor_built": True,
        "entrypoint_built": True,
        "launch_approved": False,
        "launch_status": "NOT_LAUNCHED",
        "maximum_hours": V029_MAXIMUM_HOURS,
        "new_backtest_hours": 0,
        "temporal_holdout": prereg["temporal_holdout"],
        "model": prereg["candidate"]["model"],
        "twap_contract": {
            "required_window": 60,
            "selection": "resolution_source_fail_closed_exact_60s",
            "fallback_allowed": False,
            "open_and_decision_max_age_ms": 5000,
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
    }
    if output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == IMPLEMENTATION_SCHEMA
            and existing.get("preregistration_sha256")
            == payload["preregistration_sha256"]
            and existing.get("code_hashes") == code_hashes
        ):
            return existing
        raise V029RunnerError("Existe otro manifiesto de implementacion V0.29")
    _write_atomic(output, payload)
    return payload


def load_and_verify_implementation(
    path: str | Path,
    *,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    implementation_file = Path(path).resolve()
    if not implementation_file.is_file():
        raise V029RunnerError("La implementacion V0.29 todavia no fue congelada")
    payload = json.loads(implementation_file.read_text(encoding="utf-8"))
    if payload.get("schema") != IMPLEMENTATION_SCHEMA:
        raise V029RunnerError("Manifiesto de implementacion V0.29 incompatible")
    preregistration_value = payload.get("preregistration")
    preregistration_hash = payload.get("preregistration_sha256")
    if not isinstance(preregistration_value, str) or not isinstance(
        preregistration_hash, str
    ):
        raise V029RunnerError("Manifiesto V0.29 sin preinscripcion sellada")
    preregistration_file = Path(preregistration_value).resolve()
    if (
        not preregistration_file.is_file()
        or sha256_file(preregistration_file) != preregistration_hash
    ):
        raise V029RunnerError("Preinscripcion del manifiesto V0.29 no coincide")
    prereg = load_and_verify_frozen_prereg(
        preregistration_file,
        project_root=root,
    )
    expected = {
        "status": "BUILT_TESTED_AWAITING_LAUNCH_APPROVAL",
        "variant": VARIANT,
        "runner_built": True,
        "auditor_built": True,
        "entrypoint_built": True,
        "launch_approved": False,
        "launch_status": "NOT_LAUNCHED",
        "maximum_hours": V029_MAXIMUM_HOURS,
        "new_backtest_hours": 0,
        "temporal_holdout": prereg["temporal_holdout"],
        "model": prereg["candidate"]["model"],
        "twap_contract": {
            "required_window": 60,
            "selection": "resolution_source_fail_closed_exact_60s",
            "fallback_allowed": False,
            "open_and_decision_max_age_ms": 5000,
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V029RunnerError(f"Manifiesto V0.29 invalido: {key}")
    hashes = payload.get("code_hashes")
    if not isinstance(hashes, Mapping) or set(hashes) != set(IMPLEMENTATION_FILES):
        raise V029RunnerError("Inventario de implementacion V0.29 incompatible")
    for key, relative in IMPLEMENTATION_FILES.items():
        if sha256_file(root / relative) != hashes.get(key):
            raise V029RunnerError(f"Hash de implementacion V0.29 no coincide: {key}")
    return dict(payload)


def load_and_verify_launch_approval(
    path: str | Path,
    *,
    prereg_path: str | Path,
    implementation_path: str | Path,
) -> dict[str, Any]:
    launch_file = Path(path).resolve()
    prereg_file = Path(prereg_path).resolve()
    implementation_file = Path(implementation_path).resolve()
    if not launch_file.is_file():
        raise V029RunnerError(
            "V0.29 no tiene aprobacion explicita; permanece NOT_LAUNCHED"
        )
    payload = json.loads(launch_file.read_text(encoding="utf-8"))
    if payload.get("schema") != LAUNCH_SCHEMA:
        raise V029RunnerError("Aprobacion de lanzamiento V0.29 incompatible")
    expected = {
        "status": "APPROVED_FOR_ONE_PAPER_FORWARD",
        "variant": VARIANT,
        "maximum_hours": V029_MAXIMUM_HOURS,
        "preregistration_sha256": sha256_file(prereg_file),
        "implementation_sha256": sha256_file(implementation_file),
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V029RunnerError(f"Aprobacion de lanzamiento invalida: {key}")
    return dict(payload)


def bind_v029_database(
    *,
    database: str | Path,
    prereg_path: str | Path,
    launch_path: str | Path,
) -> None:
    store = V029Store(database)
    store.open(
        preregistration_sha256=sha256_file(Path(prereg_path).resolve()),
        launch_manifest_sha256=sha256_file(Path(launch_path).resolve()),
    )
    store.close()


def technical_snapshot(
    *,
    database: str | Path,
    checkpoint_at: datetime,
) -> dict[str, Any]:
    database_path = Path(database).resolve()
    connection = open_read_only(database_path)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        counts = connection.execute(
            """
            SELECT COUNT(*) AS markets,
             SUM(CASE WHEN feature_status='SAVED' THEN 1 ELSE 0 END) AS features,
             SUM(CASE WHEN resolution_contract_status='VERIFIED' THEN 1 ELSE 0 END)
              AS verified_contracts,
             MIN(market_start_ms) AS first_start
            FROM v029_markets
            """
        ).fetchone()
        mature = connection.execute(
            """
            SELECT COUNT(*) AS markets,
             SUM(CASE WHEN label_verified=1 THEN 1 ELSE 0 END) AS resolved
            FROM v029_markets WHERE market_end_ms<=?
            """,
            (int(checkpoint_at.timestamp() * 1000) - 600_000,),
        ).fetchone()
        health = connection.execute(
            """
            SELECT recorded_at,connections_json FROM v029_health
            ORDER BY recorded_at DESC LIMIT 1
            """
        ).fetchone()
        meta = {
            str(row[0]): json.loads(str(row[1]))
            for row in connection.execute("SELECT key,value FROM v029_meta")
        }
        selected_windows = {
            int(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT resolution_twap_window_s FROM v029_markets
                WHERE feature_status='SAVED' AND resolution_twap_window_s IS NOT NULL
                """
            )
        }
        alignment_rows = connection.execute(
            """
            SELECT m.resolution_twap_window_s,f.feature_json
            FROM v029_markets AS m JOIN v029_features AS f USING(condition_id)
            """
        ).fetchall()
        tick_windows = {
            int(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT window_s,COUNT(*) FROM v029_twap_ticks GROUP BY window_s"
            )
        }
    finally:
        connection.close()
    markets = int(counts["markets"] or 0)
    features = int(counts["features"] or 0)
    contracts = int(counts["verified_contracts"] or 0)
    first_start = counts["first_start"]
    checkpoint_ms = int(checkpoint_at.timestamp() * 1000)
    expected = (
        int((checkpoint_ms - int(first_start)) // 300_000) + 1
        if first_start is not None
        else 0
    )
    market_coverage = min(1.0, markets / expected) if expected else 0.0
    feature_coverage = features / markets if markets else 0.0
    contract_coverage = contracts / markets if markets else 0.0
    mature_markets = int(mature["markets"] or 0)
    mature_resolved = int(mature["resolved"] or 0)
    resolution_coverage = mature_resolved / mature_markets if mature_markets else 1.0
    connections = json.loads(str(health["connections_json"])) if health else {}
    health_age = (
        max(
            0.0,
            (checkpoint_at - parse_utc(str(health["recorded_at"]))).total_seconds(),
        )
        if health
        else math.inf
    )
    alignment_passed = all(
        int(json.loads(str(row["feature_json"]))["resolution_twap_window_s"])
        == int(row["resolution_twap_window_s"])
        for row in alignment_rows
    )
    feed_gates = {
        f"{feed}_connected_passed": connections.get(feed) == "CONNECTED"
        for feed in V029_REQUIRED_GLOBAL_FEEDS
    }
    for window in selected_windows:
        feed_gates[f"twap_{window}s_connected_passed"] = (
            connections.get(f"v029-rtds-twap-{window}s") == "CONNECTED"
        )
    gates = {
        "sqlite_quick_check_passed": quick_check == "ok",
        "market_coverage_passed": market_coverage >= MINIMUM_MARKET_COVERAGE,
        "feature_coverage_passed": feature_coverage >= MINIMUM_FEATURE_COVERAGE,
        "resolution_contract_coverage_passed": (
            contract_coverage >= MINIMUM_CONTRACT_COVERAGE
        ),
        "feature_contract_alignment_passed": alignment_passed,
        "selected_twap_ticks_passed": all(
            tick_windows.get(window, 0) > 0 for window in selected_windows
        ),
        "required_twap_window_passed": selected_windows.issubset(
            {V029_REQUIRED_TWAP_WINDOW_SECONDS}
        ),
        "mature_resolution_coverage_passed": (
            resolution_coverage >= MINIMUM_MATURE_RESOLUTION_COVERAGE
        ),
        "health_fresh_passed": health_age <= MAXIMUM_HEALTH_AGE_SECONDS,
        **feed_gates,
    }
    safety_gates = {
        "orders_disabled_passed": meta.get("orders_enabled") is False,
        "paper_orders_disabled_passed": meta.get("paper_orders_enabled") is False,
        "wallet_not_required_passed": meta.get("wallet_required") is False,
        "money_real_disabled_passed": meta.get("money_real_enabled") is False,
        "v029_real_money_blocked_passed": meta.get("v029_real_money") == "BLOQUEADO",
    }
    return {
        "passed": all(gates.values()),
        "gates": gates,
        "sqlite_quick_check": quick_check,
        "expected_markets": expected,
        "markets": markets,
        "market_coverage": round(market_coverage, 8),
        "features": features,
        "feature_coverage": round(feature_coverage, 8),
        "verified_resolution_contracts": contracts,
        "resolution_contract_coverage": round(contract_coverage, 8),
        "selected_twap_windows": sorted(selected_windows),
        "twap_updates_by_window": {
            str(key): value for key, value in tick_windows.items()
        },
        "mature_markets": mature_markets,
        "mature_resolved": mature_resolved,
        "mature_resolution_coverage": round(resolution_coverage, 8),
        "latest_connections": connections,
        "health_age_seconds": round(health_age, 3) if math.isfinite(health_age) else None,
        "safety_passed": all(safety_gates.values()),
        "safety_gates": safety_gates,
        "outcome_values_read": 0,
    }


def checkpoint_snapshot(
    *,
    database: str | Path,
    checkpoint_hour: float,
) -> dict[str, Any]:
    database_path = Path(database).resolve()
    meta = read_v029_meta(database_path)
    start = parse_utc(str(meta["experiment_started_at"]))
    evaluated_at = datetime.now(timezone.utc)
    technical = technical_snapshot(
        database=database_path,
        checkpoint_at=evaluated_at,
    )
    if technical["safety_passed"] is not True:
        decision = "FREEZE_SAFETY"
    elif technical["passed"] is not True:
        decision = "RETRY_TECHNICAL"
    else:
        decision = "CONTINUE"
    return {
        "schema": "checkpoint_v029_1",
        "variant": VARIANT,
        "checkpoint_hour": checkpoint_hour,
        "checkpoint_at": (start + timedelta(hours=checkpoint_hour)).isoformat(
            timespec="seconds"
        ),
        "evaluated_at": evaluated_at.isoformat(timespec="seconds"),
        "decision": decision,
        "technical": technical,
        "model_fit": False,
        "labels_or_outcomes_exposed": 0,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


def _save_checkpoint(database: Path, snapshot: Mapping[str, Any]) -> None:
    connection = sqlite3.connect(database, timeout=60)
    try:
        row = connection.execute(
            "SELECT value FROM v029_meta WHERE key='v029_checkpoints'"
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
        checkpoints.sort(
            key=lambda item: (
                float(item["checkpoint_hour"]),
                int(item.get("attempt", 1)),
            )
        )
        connection.execute(
            "INSERT OR REPLACE INTO v029_meta(key,value) VALUES(?,?)",
            (
                "v029_checkpoints",
                json.dumps(
                    checkpoints,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _mark_completion(database: Path, reason: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection = sqlite3.connect(database, timeout=60)
    try:
        connection.executemany(
            "INSERT OR REPLACE INTO v029_meta(key,value) VALUES(?,?)",
            (
                ("v029_completion_reason", json.dumps(reason)),
                ("v029_observation_ended_at", json.dumps(now)),
            ),
        )
        connection.commit()
    finally:
        connection.close()


async def _wait_for_due(
    collector: asyncio.Task[dict[str, Any]],
    due_timestamp: float,
) -> bool:
    try:
        await asyncio.wait_for(
            asyncio.shield(collector),
            timeout=max(0.0, due_timestamp - time.time()),
        )
        return False
    except TimeoutError:
        return True


async def run_v029(
    *,
    settings: Settings,
    prereg_path: str | Path,
    implementation_path: str | Path,
    launch_path: str | Path,
    output_db: str | Path,
) -> dict[str, Any]:
    prereg_file = Path(prereg_path).resolve()
    implementation_file = Path(implementation_path).resolve()
    launch_file = Path(launch_path).resolve()
    load_and_verify_frozen_prereg(prereg_file, project_root=ROOT)
    implementation = load_and_verify_implementation(implementation_file)
    if implementation.get("preregistration_sha256") != sha256_file(prereg_file):
        raise V029RunnerError("Implementacion y preinscripcion V0.29 no coinciden")
    load_and_verify_launch_approval(
        launch_file,
        prereg_path=prereg_file,
        implementation_path=implementation_file,
    )
    database = Path(output_db).resolve()
    enforce_forward_duration(V029_MAXIMUM_HOURS, database)
    lock = V022ProcessLock(f"{database}.lock")
    lock.acquire()
    try:
        bind_v029_database(
            database=database,
            prereg_path=prereg_file,
            launch_path=launch_file,
        )
        meta = read_v029_meta(database)
        if meta.get("v029_completion_reason") is not None:
            return v029_status(database)
        start = parse_utc(str(meta["experiment_started_at"]))
        stop_event = asyncio.Event()
        collector = asyncio.create_task(
            run_v029_forward(
                settings=settings,
                output_db=database,
                preregistration_sha256=sha256_file(prereg_file),
                launch_manifest_sha256=sha256_file(launch_file),
                stop_event=stop_event,
            )
        )
        try:
            completed = list(meta.get("v029_checkpoints", []))
            completed_hours = {
                float(item["checkpoint_hour"])
                for item in completed
                if int(item.get("attempt", 1)) >= 1
            }
            for checkpoint_hour in CHECKPOINT_HOURS:
                if checkpoint_hour in completed_hours:
                    continue
                due = (start + timedelta(hours=checkpoint_hour)).timestamp()
                if not await _wait_for_due(collector, due):
                    break
                snapshot = checkpoint_snapshot(
                    database=database,
                    checkpoint_hour=checkpoint_hour,
                )
                snapshot["attempt"] = 1
                _save_checkpoint(database, snapshot)
                if snapshot["decision"] == "FREEZE_SAFETY":
                    _mark_completion(database, "FREEZE_SAFETY")
                    stop_event.set()
                    break
                if snapshot["decision"] == "RETRY_TECHNICAL":
                    retry_due = time.time() + TECHNICAL_RETRY_MINUTES * 60
                    if not await _wait_for_due(collector, retry_due):
                        break
                    retry = checkpoint_snapshot(
                        database=database,
                        checkpoint_hour=checkpoint_hour,
                    )
                    retry["attempt"] = 2
                    _save_checkpoint(database, retry)
                    if retry["decision"] != "CONTINUE":
                        reason = (
                            "FREEZE_SAFETY"
                            if retry["decision"] == "FREEZE_SAFETY"
                            else "FREEZE_TECHNICAL_FAILURE"
                        )
                        _mark_completion(database, reason)
                        stop_event.set()
                        break
            return await collector
        except asyncio.CancelledError:
            stop_event.set()
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
            raise
    finally:
        lock.release()


def _public_checkpoint(item: Mapping[str, Any]) -> dict[str, Any]:
    technical = dict(item.get("technical", {}))
    return {
        "checkpoint_hour": item.get("checkpoint_hour"),
        "attempt": item.get("attempt", 1),
        "decision": item.get("decision"),
        "technical_passed": technical.get("passed"),
        "safety_passed": technical.get("safety_passed"),
        "markets": technical.get("markets"),
        "features": technical.get("features"),
        "model_fit": False,
        "outcomes_exposed": 0,
    }


def v029_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).resolve()
    if not database.is_file():
        return {
            "status": "NOT_STARTED",
            "variant": VARIANT,
            "database": str(database),
            "maximum_hours": V029_MAXIMUM_HOURS,
            "checkpoint_hours": list(CHECKPOINT_HOURS),
            "model_fit": False,
            "orders_created": 0,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
    meta = read_v029_meta(database)
    start = parse_utc(str(meta["experiment_started_at"]))
    midpoint_ms = int((start + timedelta(hours=12)).timestamp() * 1000)
    checkpoints = list(meta.get("v029_checkpoints", []))
    connection = open_read_only(database)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        counts = connection.execute(
            """
            SELECT COUNT(*) AS markets,
             SUM(CASE WHEN feature_status='SAVED' THEN 1 ELSE 0 END) AS features,
             SUM(CASE WHEN feature_status='SAVED' AND market_start_ms<? THEN 1 ELSE 0 END)
              AS training_features,
             SUM(CASE WHEN feature_status='SAVED' AND market_start_ms>=? THEN 1 ELSE 0 END)
              AS validation_features
            FROM v029_markets
            """,
            (midpoint_ms, midpoint_ms),
        ).fetchone()
    finally:
        connection.close()
    completion = meta.get("v029_completion_reason")
    target_end = parse_utc(str(meta["target_end_at"])).timestamp()
    return {
        "status": "COMPLETED_OR_FROZEN" if completion else "RUNNING_OR_RESUMABLE",
        "variant": VARIANT,
        "database": str(database),
        "sqlite_quick_check": quick_check,
        "maximum_hours": V029_MAXIMUM_HOURS,
        "checkpoint_hours": list(CHECKPOINT_HOURS),
        "completion_reason": completion,
        "remaining_hours": max(0.0, target_end - time.time()) / 3600,
        "markets": int(counts["markets"] or 0),
        "features": int(counts["features"] or 0),
        "training_features": int(counts["training_features"] or 0),
        "validation_features": int(counts["validation_features"] or 0),
        "completed_checkpoints": len(checkpoints),
        "latest_checkpoint": _public_checkpoint(checkpoints[-1]) if checkpoints else None,
        "model_fit": False,
        "intermediate_outcome_metrics_exposed": False,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


__all__ = [
    "CHECKPOINT_HOURS",
    "IMPLEMENTATION_FILES",
    "IMPLEMENTATION_SCHEMA",
    "LAUNCH_SCHEMA",
    "VARIANT",
    "V029RunnerError",
    "bind_v029_database",
    "build_implementation_manifest",
    "checkpoint_snapshot",
    "load_and_verify_implementation",
    "load_and_verify_launch_approval",
    "run_v029",
    "technical_snapshot",
    "v029_status",
]
