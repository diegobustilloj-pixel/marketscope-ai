from __future__ import annotations

import asyncio
import json
import shutil
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.config import Settings
from polymarket_bot.discovery import DiscoveryError
from polymarket_bot.phase41 import (
    SHADOW_RTDS_WATCHDOG_SECONDS,
    _fetch_market_sync,
    _silver_market,
    _wait_or_stop,
    _websocket_feed,
)
from polymarket_bot.resolution_contract import TWAP_TOPIC_BY_WINDOW, resolution_twap_contract
from polymarket_bot.runtime_policy import enforce_forward_duration
from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v022_runner import V022ProcessLock
from polymarket_bot.v029_forward import is_fresh_twap_message
from polymarket_bot.v031_capture import V031CaptureState, database_footprint, open_read_only, parse_utc, utc_now
from polymarket_bot.v033_capture import technical_snapshot
from polymarket_bot.v034_capture import (
    V034_CAPTURE_HOURS,
    V034_EXPECTED_MARKETS,
    V034_HEALTH_INTERVAL_SECONDS,
    V034_MARKET_SECONDS,
    V034_SUPPORTED_TWAP_WINDOWS,
    V034Store,
)
from polymarket_bot.v034_contract import (
    VARIANT,
    aggregate_probe_results,
    evaluate_market_probes,
    load_and_verify_prereg,
)


IMPLEMENTATION_SCHEMA = "implementation_v034_selected_bid_guard_4h_1"
LAUNCH_SCHEMA = "launch_approval_v034_selected_bid_guard_4h_1"
IMPLEMENTATION_FILES = {
    "guard_design": "src/polymarket_bot/v034_guard_design.py",
    "selected_design": "src/polymarket_bot/v034_selected_bid_design.py",
    "contract": "src/polymarket_bot/v034_contract.py",
    "capture": "src/polymarket_bot/v034_capture.py",
    "runner": "src/polymarket_bot/v034_runner.py",
    "auditor": "src/polymarket_bot/v034_audit.py",
    "entrypoint": "v034_monitor.py",
    "tests": "tests/test_v034_selected_bid_guard.py",
}


class V034RunnerError(RuntimeError):
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
    *, prereg_path: str | Path, output_path: str | Path, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    output = Path(output_path).resolve()
    prereg = load_and_verify_prereg(prereg_file, project_root=root)
    hashes = {}
    for key, relative in IMPLEMENTATION_FILES.items():
        source = root / relative
        if not source.is_file():
            raise V034RunnerError(f"Implementacion V0.34 incompleta: {relative}")
        hashes[key] = sha256_file(source)
    payload = {
        "schema": IMPLEMENTATION_SCHEMA,
        "status": "BUILT_TESTED_AWAITING_ONE_FRESH_TECHNICAL_LAUNCH_APPROVAL",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "preregistration": str(prereg_file),
        "preregistration_sha256": sha256_file(prereg_file),
        "code_hashes": hashes,
        "collector_built": True,
        "technical_auditor_built": True,
        "entrypoint_built": True,
        "economic_strategy_built": False,
        "automatic_final_audit_built": True,
        "scheduled_supervision_built": False,
        "launch_approved": False,
        "launch_status": "NOT_LAUNCHED",
        "capture_contract": prereg["capture_contract"],
        "probe_contract": prereg["probe_contract"],
        "technical_gates": prereg["technical_gates"],
        "stopping": prereg["stopping"],
        "data_policy": prereg["data_policy"],
        "safety": prereg["safety"],
    }
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if existing.get("schema") == IMPLEMENTATION_SCHEMA and existing.get("code_hashes") == hashes:
            return existing
        raise V034RunnerError("Existe otro manifiesto V0.34")
    _write_atomic(output, payload)
    return payload


def load_and_verify_implementation(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V034RunnerError("Implementacion V0.34 no sellada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema") != IMPLEMENTATION_SCHEMA:
        raise V034RunnerError("Manifiesto V0.34 incompatible")
    prereg_file = Path(str(payload.get("preregistration") or "")).resolve()
    prereg = load_and_verify_prereg(prereg_file, project_root=root)
    expected = {
        "status": "BUILT_TESTED_AWAITING_ONE_FRESH_TECHNICAL_LAUNCH_APPROVAL",
        "variant": VARIANT,
        "preregistration_sha256": sha256_file(prereg_file),
        "collector_built": True,
        "technical_auditor_built": True,
        "entrypoint_built": True,
        "economic_strategy_built": False,
        "automatic_final_audit_built": True,
        "scheduled_supervision_built": False,
        "launch_approved": False,
        "launch_status": "NOT_LAUNCHED",
        "capture_contract": prereg["capture_contract"],
        "probe_contract": prereg["probe_contract"],
        "technical_gates": prereg["technical_gates"],
        "stopping": prereg["stopping"],
        "data_policy": prereg["data_policy"],
        "safety": prereg["safety"],
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V034RunnerError(f"Manifiesto V0.34 invalido: {key}")
    hashes = payload.get("code_hashes")
    if not isinstance(hashes, Mapping) or set(hashes) != set(IMPLEMENTATION_FILES):
        raise V034RunnerError("Inventario V0.34 incompatible")
    for key, relative in IMPLEMENTATION_FILES.items():
        if hashes.get(key) != sha256_file(root / relative):
            raise V034RunnerError(f"Hash V0.34 no coincide: {key}")
    return dict(payload)


def load_and_verify_launch_approval(
    path: str | Path, *, prereg_path: str | Path, implementation_path: str | Path
) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.is_file():
        raise V034RunnerError("V0.34 permanece NOT_LAUNCHED sin aprobacion explicita")
    payload = json.loads(source.read_text(encoding="utf-8"))
    expected = {
        "schema": LAUNCH_SCHEMA,
        "status": "APPROVED_FOR_ONE_FRESH_SELECTED_BID_REPLICATION",
        "variant": VARIANT,
        "technical_capture_hours": V034_CAPTURE_HOURS,
        "preregistration_sha256": sha256_file(Path(prereg_path).resolve()),
        "implementation_sha256": sha256_file(Path(implementation_path).resolve()),
        "automatic_final_audit": True,
        "scheduled_supervision": False,
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
        "outcomes_read": 0,
        "pnl_calculated": False,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V034RunnerError(f"Aprobacion V0.34 invalida: {key}")
    return dict(payload)


async def run_v034_capture(
    *,
    settings: Settings,
    output_db: str | Path,
    preregistration_sha256: str,
    implementation_sha256: str,
    launch_manifest_sha256: str,
    probe_contract: Mapping[str, Any],
    stop_event: asyncio.Event,
    maximum_database_gb: float,
    minimum_free_gb: float,
) -> dict[str, Any]:
    database = Path(output_db).resolve()
    store = V034Store(database)
    store.open(
        preregistration_sha256=preregistration_sha256,
        implementation_sha256=implementation_sha256,
        launch_manifest_sha256=launch_manifest_sha256,
    )
    meta = store.meta()
    capture_start = parse_utc(str(meta["capture_start_at"])).timestamp()
    target_end = parse_utc(str(meta["target_end_at"])).timestamp()
    run_id = store.start_run()
    state = V031CaptureState()
    status = "COMPLETED"
    error: str | None = None
    safety_stop_reason: str | None = None

    async def ingest_rtds(raw: str) -> None:
        state.ingest_rtds(raw, received_timestamp_ms=int(time.time() * 1000))

    global_tasks = [
        asyncio.create_task(
            _websocket_feed(
                name="v034-rtds-chainlink", endpoint=settings.rtds_ws_url,
                subscription={"action": "subscribe", "subscriptions": [{
                    "topic": "crypto_prices_chainlink", "type": "*", "filters": '{"symbol":"btc/usd"}'
                }]},
                heartbeat_text="PING", heartbeat_seconds=5.0,
                use_proxy=settings.ws_use_proxy, reconnect_max_seconds=settings.reconnect_max_seconds,
                stop_event=stop_event, state=state, handler=ingest_rtds,  # type: ignore[arg-type]
            )
        )
    ]
    for window_s in V034_SUPPORTED_TWAP_WINDOWS:
        global_tasks.append(
            asyncio.create_task(
                _websocket_feed(
                    name=f"v034-rtds-twap-{window_s}s", endpoint=settings.rtds_ws_url,
                    subscription={"action": "subscribe", "subscriptions": [{
                        "topic": TWAP_TOPIC_BY_WINDOW[window_s], "type": "update",
                        "filters": '{"symbol":"btc/usd"}'
                    }]},
                    heartbeat_text="PING", heartbeat_seconds=5.0,
                    use_proxy=settings.ws_use_proxy, reconnect_max_seconds=settings.reconnect_max_seconds,
                    stop_event=stop_event, state=state, handler=ingest_rtds,  # type: ignore[arg-type]
                    freshness_timeout_seconds=SHADOW_RTDS_WATCHDOG_SECONDS,
                    freshness_predicate=lambda raw, expected=window_s: is_fresh_twap_message(raw, expected),
                )
            )
        )

    async def health_monitor() -> None:
        nonlocal safety_stop_reason
        while not stop_event.is_set():
            store.save_health(counters=state.counters, connections=state.connections)
            if database_footprint(database) >= maximum_database_gb * 1_000_000_000:
                safety_stop_reason = "DATABASE_LIMIT_REACHED"
            elif shutil.disk_usage(database.parent).free <= minimum_free_gb * 1_000_000_000:
                safety_stop_reason = "INSUFFICIENT_FREE_SPACE"
            if safety_stop_reason:
                store.set_meta("completion_reason", "FREEZE_COLLECTOR_SAFETY")
                store.set_meta("observation_ended_at", utc_now())
                stop_event.set()
                return
            await _wait_or_stop(stop_event, V034_HEALTH_INTERVAL_SECONDS)

    health_task = asyncio.create_task(health_monitor())
    try:
        await _wait_or_stop(stop_event, max(0.0, capture_start - time.time()))
        market_start = int(capture_start)
        while market_start < int(target_end) and not stop_event.is_set():
            slug = f"btc-updown-5m-{market_start}"
            if store.market_exists(slug):
                await _wait_or_stop(stop_event, max(0.0, market_start + V034_MARKET_SECONDS - time.time()))
                market_start += V034_MARKET_SECONDS
                continue
            await _wait_or_stop(stop_event, max(0.0, market_start + 1 - time.time()))
            discovered = None
            deadline = market_start + 30
            while time.time() < deadline and not stop_event.is_set():
                try:
                    discovered = await asyncio.to_thread(_fetch_market_sync, settings, slug)
                    break
                except DiscoveryError:
                    await _wait_or_stop(stop_event, 2.0)
            if discovered is None:
                state.counters["market_discovery_missed"] += 1
                market_start += V034_MARKET_SECONDS
                continue
            definition, initial_resolution, _ = discovered
            market = _silver_market(definition, initial_resolution)
            resolution_contract = resolution_twap_contract(market.resolution_source)
            store.save_market(market, contract=resolution_contract)
            if not resolution_contract.verified or resolution_contract.window_seconds not in V034_SUPPORTED_TWAP_WINDOWS:
                state.counters["market_contract_rejected"] += 1
                market_start += V034_MARKET_SECONDS
                continue
            state.activate_market(market)

            async def ingest_clob(raw: str) -> None:
                state.ingest_clob(raw, received_timestamp_ms=int(time.time() * 1000))

            clob_stop = asyncio.Event()
            clob_name = f"v034-clob-{market.slug}"
            clob_task = asyncio.create_task(
                _websocket_feed(
                    name=clob_name, endpoint=settings.clob_ws_url,
                    subscription={"assets_ids": [market.up_token_id, market.down_token_id],
                                  "type": "market", "custom_feature_enabled": True},
                    heartbeat_text="PING", heartbeat_seconds=10.0,
                    use_proxy=settings.ws_use_proxy, reconnect_max_seconds=settings.reconnect_max_seconds,
                    stop_event=clob_stop, state=state, handler=ingest_clob,  # type: ignore[arg-type]
                )
            )
            captured = 0
            for offset in range(V034_MARKET_SECONDS):
                if stop_event.is_set():
                    break
                scheduled = market_start + offset + 0.95
                if time.time() > scheduled + 1.0:
                    state.counters["snapshot_seconds_missed"] += 1
                    continue
                await _wait_or_stop(stop_event, max(0.0, scheduled - time.time()))
                if stop_event.is_set():
                    break
                store.save_snapshot(
                    technical_snapshot(
                        state, condition_id=market.condition_id, second_offset=offset,
                        snapshot_timestamp_ms=market.start_ms + offset * 1000,
                        official_twap_window_s=int(resolution_contract.window_seconds),
                    )
                )
                captured += 1
            await _wait_or_stop(stop_event, max(0.0, market.end_ms / 1000 + 2 - time.time()))
            clob_stop.set()
            clob_task.cancel()
            await asyncio.gather(clob_task, return_exceptions=True)
            state.connections.pop(clob_name, None)
            probes = evaluate_market_probes(
                store.load_market_snapshots(market.condition_id), condition_id=market.condition_id,
                slug=market.slug, contract=probe_contract,
            )
            summary = aggregate_probe_results(probes)
            capture_error = None if captured >= 285 else f"INSUFFICIENT_SNAPSHOTS:{captured}"
            store.finish_market(market.condition_id, error=capture_error, probe_summary=summary)
            state.clear_market()
            if capture_error is None and int(summary["trapped_positions"]) > 0:
                store.set_meta("completion_reason", "FREEZE_FIRST_TRAPPED_POSITION")
                store.set_meta("observation_ended_at", utc_now())
                stop_event.set()
            market_start += V034_MARKET_SECONDS

        current = store.meta()
        if safety_stop_reason or current.get("completion_reason") is not None:
            status = "COMPLETED"
        elif time.time() >= target_end:
            store.set_meta("completion_reason", "FULL_4H_REACHED")
            store.set_meta("observation_ended_at", utc_now())
        else:
            status = "INTERRUPTED"
    except asyncio.CancelledError:
        status = "INTERRUPTED"
        raise
    except Exception as exc:
        status = "FAILED"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        stop_event.set()
        for task in global_tasks:
            task.cancel()
        health_task.cancel()
        await asyncio.gather(*global_tasks, health_task, return_exceptions=True)
        store.save_health(counters=state.counters, connections=state.connections)
        store.finish_run(run_id, status=status, error=error)
        quick_check = store.quick_check()
        result_meta = store.meta()
        store.close()
    return {
        "status": status, "error": error, "safety_stop_reason": safety_stop_reason,
        "database": str(database), "database_bytes": database_footprint(database),
        "quick_check": quick_check, "capture_start_at": result_meta["capture_start_at"],
        "target_end_at": result_meta["target_end_at"],
        "completion_reason": result_meta.get("completion_reason"),
        "outcomes_read": 0, "prices_stored": False, "pnl_calculated": False,
        "signals_generated": False, "trades_generated": False, "orders_created": 0,
        "paper_orders": 0, "wallet_required": False, "real_money": "BLOQUEADO",
    }


async def run_v034(
    *, settings: Settings, prereg_path: str | Path, implementation_path: str | Path,
    launch_path: str | Path, output_db: str | Path
) -> dict[str, Any]:
    prereg_file = Path(prereg_path).resolve()
    implementation_file = Path(implementation_path).resolve()
    launch_file = Path(launch_path).resolve()
    prereg = load_and_verify_prereg(prereg_file)
    load_and_verify_implementation(implementation_file)
    load_and_verify_launch_approval(
        launch_file, prereg_path=prereg_file, implementation_path=implementation_file
    )
    database = Path(output_db).resolve()
    enforce_forward_duration(V034_CAPTURE_HOURS, database)
    lock = V022ProcessLock(Path(f"{database}.lock"))
    lock.acquire()
    try:
        binding = V034Store(database)
        binding.open(
            preregistration_sha256=sha256_file(prereg_file),
            implementation_sha256=sha256_file(implementation_file),
            launch_manifest_sha256=sha256_file(launch_file),
        )
        meta = binding.meta()
        binding.close()
        if meta.get("completion_reason") is not None:
            return v034_status(database)
        return await run_v034_capture(
            settings=settings, output_db=database,
            preregistration_sha256=sha256_file(prereg_file),
            implementation_sha256=sha256_file(implementation_file),
            launch_manifest_sha256=sha256_file(launch_file),
            probe_contract=prereg["probe_contract"], stop_event=asyncio.Event(),
            maximum_database_gb=float(prereg["safety"]["maximum_database_gb"]),
            minimum_free_gb=float(prereg["safety"]["minimum_free_disk_gb"]),
        )
    finally:
        lock.release()


def v034_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).resolve()
    safe = {
        "outcomes_read": 0, "prices_stored": False, "pnl_calculated": False,
        "signals_generated": False, "trades_generated": False, "orders_created": 0,
        "paper_orders": 0, "wallet_required": False, "real_money": "BLOQUEADO",
    }
    if not database.exists():
        return {"status": "NOT_STARTED", "database": str(database),
                "expected_markets": V034_EXPECTED_MARKETS, **safe}
    connection = open_read_only(database)
    try:
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        meta = {str(row[0]): json.loads(str(row[1])) for row in connection.execute("SELECT key,value FROM v034_meta")}
        markets = connection.execute(
            """SELECT COUNT(*) markets,SUM(capture_status='COMPLETED') completed,
            SUM(COALESCE(probe_total,0)) probes,SUM(COALESCE(probe_entered,0)) entered,
            SUM(COALESCE(probe_relative_guard_exits,0)) relative_guards,
            SUM(COALESCE(probe_exit_successes,0)) exits,SUM(COALESCE(probe_trapped,0)) trapped
            FROM v034_markets"""
        ).fetchone()
        snapshots = connection.execute("SELECT COUNT(*) rows,SUM(complete_v2) complete FROM v034_snapshots").fetchone()
    finally:
        connection.close()
    completion = meta.get("completion_reason")
    return {
        "status": "COMPLETED" if completion else "RUNNING_OR_INTERRUPTED",
        "database": str(database), "sqlite_quick_check": quick,
        "capture_start_at": meta.get("capture_start_at"), "target_end_at": meta.get("target_end_at"),
        "estimated_remaining_seconds": 0 if completion else max(0, int(parse_utc(str(meta["target_end_at"])).timestamp() - time.time())),
        "completion_reason": completion, "expected_markets": V034_EXPECTED_MARKETS,
        "markets": int(markets["markets"] or 0), "completed_markets": int(markets["completed"] or 0),
        "snapshots": int(snapshots["rows"] or 0), "complete_v2_snapshots": int(snapshots["complete"] or 0),
        "capacity_probes": int(markets["probes"] or 0), "entry_eligible_probes": int(markets["entered"] or 0),
        "relative_guard_exits": int(markets["relative_guards"] or 0), "exit_successes": int(markets["exits"] or 0),
        "trapped_positions": int(markets["trapped"] or 0), **safe,
    }


__all__ = [
    "IMPLEMENTATION_FILES", "IMPLEMENTATION_SCHEMA", "LAUNCH_SCHEMA", "V034RunnerError",
    "build_implementation_manifest", "load_and_verify_implementation",
    "load_and_verify_launch_approval", "run_v034", "v034_status",
]
