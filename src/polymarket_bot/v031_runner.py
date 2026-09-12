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
from polymarket_bot.v031_capture import (
    V031CaptureState,
    V031Store,
    V031_CAPTURE_HOURS,
    V031_EXPECTED_MARKETS,
    V031_HEALTH_INTERVAL_SECONDS,
    V031_MARKET_SECONDS,
    V031_MAX_DATABASE_GB,
    V031_MINIMUM_FREE_GB,
    V031_SUPPORTED_TWAP_WINDOWS,
    database_footprint,
    open_read_only,
    parse_utc,
    read_v031_meta,
    utc_now,
)
from polymarket_bot.v031_prereg import VARIANT, load_and_verify_frozen_prereg


IMPLEMENTATION_SCHEMA = "implementation_v031_path_execution_capture_1"
LAUNCH_SCHEMA = "launch_approval_v031_path_execution_capture_1"
IMPLEMENTATION_FILES = {
    "capture": "src/polymarket_bot/v031_capture.py",
    "runner": "src/polymarket_bot/v031_runner.py",
    "auditor": "src/polymarket_bot/v031_audit.py",
    "entrypoint": "v031_monitor.py",
    "prereg_loader": "src/polymarket_bot/v031_prereg.py",
    "resolution_contract": "src/polymarket_bot/resolution_contract.py",
}


class V031RunnerError(RuntimeError):
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
    hashes: dict[str, str] = {}
    for key, relative in IMPLEMENTATION_FILES.items():
        path = root / relative
        if not path.is_file():
            raise V031RunnerError(f"Implementacion V0.31 incompleta: {relative}")
        hashes[key] = sha256_file(path)
    payload = {
        "schema": IMPLEMENTATION_SCHEMA,
        "status": "BUILT_TESTED_AWAITING_LAUNCH_APPROVAL",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "preregistration": str(prereg_file),
        "preregistration_sha256": sha256_file(prereg_file),
        "code_hashes": hashes,
        "capture_contract": prereg["capture_contract"],
        "technical_gates": prereg["technical_gates"],
        "collector_built": True,
        "technical_auditor_built": True,
        "entrypoint_built": True,
        "economic_strategy_built": False,
        "launch_approved": False,
        "launch_status": "NOT_LAUNCHED",
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
            "outcomes_read": 0,
            "pnl_calculated": False,
        },
    }
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == IMPLEMENTATION_SCHEMA
            and existing.get("preregistration_sha256") == payload["preregistration_sha256"]
            and existing.get("code_hashes") == hashes
        ):
            return existing
        raise V031RunnerError("Existe otro manifiesto de implementacion V0.31")
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
        raise V031RunnerError("La implementacion V0.31 todavia no fue congelada")
    payload = json.loads(implementation_file.read_text(encoding="utf-8"))
    if payload.get("schema") != IMPLEMENTATION_SCHEMA:
        raise V031RunnerError("Manifiesto V0.31 incompatible")
    preregistration_file = Path(str(payload.get("preregistration") or "")).resolve()
    preregistration_hash = str(payload.get("preregistration_sha256") or "")
    if (
        not preregistration_file.is_file()
        or sha256_file(preregistration_file) != preregistration_hash
    ):
        raise V031RunnerError("Preinscripcion del manifiesto V0.31 no coincide")
    prereg = load_and_verify_frozen_prereg(preregistration_file, project_root=root)
    expected = {
        "status": "BUILT_TESTED_AWAITING_LAUNCH_APPROVAL",
        "variant": VARIANT,
        "capture_contract": prereg["capture_contract"],
        "technical_gates": prereg["technical_gates"],
        "collector_built": True,
        "technical_auditor_built": True,
        "entrypoint_built": True,
        "economic_strategy_built": False,
        "launch_approved": False,
        "launch_status": "NOT_LAUNCHED",
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
            "outcomes_read": 0,
            "pnl_calculated": False,
        },
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V031RunnerError(f"Manifiesto V0.31 invalido: {key}")
    hashes = payload.get("code_hashes")
    if not isinstance(hashes, Mapping) or set(hashes) != set(IMPLEMENTATION_FILES):
        raise V031RunnerError("Inventario V0.31 incompatible")
    for key, relative in IMPLEMENTATION_FILES.items():
        if sha256_file(root / relative) != hashes.get(key):
            raise V031RunnerError(f"Hash de implementacion V0.31 no coincide: {key}")
    return dict(payload)


def load_and_verify_launch_approval(
    path: str | Path,
    *,
    prereg_path: str | Path,
    implementation_path: str | Path,
) -> dict[str, Any]:
    launch_file = Path(path).resolve()
    if not launch_file.is_file():
        raise V031RunnerError(
            "V0.31 no tiene aprobacion explicita; permanece NOT_LAUNCHED"
        )
    payload = json.loads(launch_file.read_text(encoding="utf-8"))
    expected = {
        "schema": LAUNCH_SCHEMA,
        "status": "APPROVED_FOR_ONE_TECHNICAL_CAPTURE",
        "variant": VARIANT,
        "technical_pilot_hours": V031_CAPTURE_HOURS,
        "preregistration_sha256": sha256_file(Path(prereg_path).resolve()),
        "implementation_sha256": sha256_file(Path(implementation_path).resolve()),
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
        "outcomes_read": 0,
        "pnl_calculated": False,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V031RunnerError(f"Aprobacion V0.31 invalida: {key}")
    return dict(payload)


async def run_v031_capture(
    *,
    settings: Settings,
    output_db: str | Path,
    preregistration_sha256: str,
    launch_manifest_sha256: str,
    stop_event: asyncio.Event,
    maximum_database_gb: float = V031_MAX_DATABASE_GB,
    minimum_free_gb: float = V031_MINIMUM_FREE_GB,
) -> dict[str, Any]:
    database = Path(output_db).expanduser().resolve()
    store = V031Store(database)
    store.open(
        preregistration_sha256=preregistration_sha256,
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
        received_ms = int(time.time() * 1000)
        twap_message = state.ingest_rtds(raw, received_timestamp_ms=received_ms)
        if twap_message is not None:
            outcome = store.save_twap_message(
                twap_message,
                received_timestamp_ms=received_ms,
            )
            state.counters[f"twap:{outcome.lower()}"] += 1

    global_tasks = [
        asyncio.create_task(
            _websocket_feed(
                name="v031-rtds-chainlink",
                endpoint=settings.rtds_ws_url,
                subscription={
                    "action": "subscribe",
                    "subscriptions": [
                        {
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": '{"symbol":"btc/usd"}',
                        }
                    ],
                },
                heartbeat_text="PING",
                heartbeat_seconds=5.0,
                use_proxy=settings.ws_use_proxy,
                reconnect_max_seconds=settings.reconnect_max_seconds,
                stop_event=stop_event,
                state=state,  # type: ignore[arg-type]
                handler=ingest_rtds,
            )
        )
    ]
    for window_s in V031_SUPPORTED_TWAP_WINDOWS:
        global_tasks.append(
            asyncio.create_task(
                _websocket_feed(
                    name=f"v031-rtds-twap-{window_s}s",
                    endpoint=settings.rtds_ws_url,
                    subscription={
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": TWAP_TOPIC_BY_WINDOW[window_s],
                                "type": "update",
                                "filters": '{"symbol":"btc/usd"}',
                            }
                        ],
                    },
                    heartbeat_text="PING",
                    heartbeat_seconds=5.0,
                    use_proxy=settings.ws_use_proxy,
                    reconnect_max_seconds=settings.reconnect_max_seconds,
                    stop_event=stop_event,
                    state=state,  # type: ignore[arg-type]
                    handler=ingest_rtds,
                    freshness_timeout_seconds=SHADOW_RTDS_WATCHDOG_SECONDS,
                    freshness_predicate=(
                        lambda raw, expected=window_s: is_fresh_twap_message(raw, expected)
                    ),
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
            if safety_stop_reason is not None:
                store.set_meta("completion_reason", "FREEZE_COLLECTOR_SAFETY")
                store.set_meta("observation_ended_at", utc_now())
                stop_event.set()
                return
            await _wait_or_stop(stop_event, V031_HEALTH_INTERVAL_SECONDS)

    health_task = asyncio.create_task(health_monitor())
    try:
        await _wait_or_stop(stop_event, max(0.0, capture_start - time.time()))
        market_start = int(capture_start)
        while market_start < int(target_end) and not stop_event.is_set():
            if store.market_exists(f"btc-updown-5m-{market_start}"):
                await _wait_or_stop(
                    stop_event,
                    max(0.0, market_start + V031_MARKET_SECONDS - time.time()),
                )
                market_start += V031_MARKET_SECONDS
                continue
            await _wait_or_stop(stop_event, max(0.0, market_start + 1 - time.time()))
            slug = f"btc-updown-5m-{market_start}"
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
                market_start += V031_MARKET_SECONDS
                continue
            definition, initial_resolution, _ = discovered
            market = _silver_market(definition, initial_resolution)
            contract = resolution_twap_contract(market.resolution_source)
            store.save_market(market, contract=contract)
            if not contract.verified or contract.window_seconds not in V031_SUPPORTED_TWAP_WINDOWS:
                state.counters["market_contract_rejected"] += 1
                market_start += V031_MARKET_SECONDS
                continue
            state.activate_market(market)

            async def ingest_clob(raw: str) -> None:
                state.ingest_clob(raw, received_timestamp_ms=int(time.time() * 1000))

            clob_stop = asyncio.Event()
            clob_name = f"v031-clob-{market.slug}"
            clob_task = asyncio.create_task(
                _websocket_feed(
                    name=clob_name,
                    endpoint=settings.clob_ws_url,
                    subscription={
                        "assets_ids": [market.up_token_id, market.down_token_id],
                        "type": "market",
                        "custom_feature_enabled": True,
                    },
                    heartbeat_text="PING",
                    heartbeat_seconds=10.0,
                    use_proxy=settings.ws_use_proxy,
                    reconnect_max_seconds=settings.reconnect_max_seconds,
                    stop_event=clob_stop,
                    state=state,  # type: ignore[arg-type]
                    handler=ingest_clob,
                )
            )
            captured = 0
            for second_offset in range(V031_MARKET_SECONDS):
                if stop_event.is_set():
                    break
                scheduled = market_start + second_offset + 0.95
                if time.time() > scheduled + 1.0:
                    state.counters["snapshot_seconds_missed"] += 1
                    continue
                await _wait_or_stop(stop_event, max(0.0, scheduled - time.time()))
                if stop_event.is_set():
                    break
                snapshot = state.snapshot(
                    condition_id=market.condition_id,
                    second_offset=second_offset,
                    snapshot_timestamp_ms=market.start_ms + second_offset * 1000,
                    official_twap_window_s=int(contract.window_seconds),
                )
                store.save_snapshot(snapshot)
                captured += 1
            await _wait_or_stop(
                stop_event,
                max(0.0, market.end_ms / 1000 + 2 - time.time()),
            )
            clob_stop.set()
            clob_task.cancel()
            await asyncio.gather(clob_task, return_exceptions=True)
            state.connections.pop(clob_name, None)
            store.finish_market(
                market.condition_id,
                error=None if captured >= 285 else f"INSUFFICIENT_SNAPSHOTS:{captured}",
            )
            state.clear_market()
            market_start += V031_MARKET_SECONDS

        current_meta = store.meta()
        if safety_stop_reason is not None:
            status = "COMPLETED"
        elif current_meta.get("completion_reason") is not None:
            status = "COMPLETED"
        elif time.time() >= target_end:
            store.set_meta("completion_reason", "FULL_1H_REACHED")
            store.set_meta("observation_ended_at", utc_now())
            status = "COMPLETED"
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
        "status": status,
        "error": error,
        "safety_stop_reason": safety_stop_reason,
        "database": str(database),
        "database_bytes": database_footprint(database),
        "quick_check": quick_check,
        "capture_start_at": result_meta["capture_start_at"],
        "target_end_at": result_meta["target_end_at"],
        "completion_reason": result_meta.get("completion_reason"),
        "outcomes_read": 0,
        "pnl_calculated": False,
        "signals_generated": False,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


async def run_v031(
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
    load_and_verify_frozen_prereg(prereg_file)
    implementation = load_and_verify_implementation(implementation_file)
    if implementation.get("preregistration_sha256") != sha256_file(prereg_file):
        raise V031RunnerError("Implementacion y preinscripcion V0.31 no coinciden")
    load_and_verify_launch_approval(
        launch_file,
        prereg_path=prereg_file,
        implementation_path=implementation_file,
    )
    database = Path(output_db).resolve()
    enforce_forward_duration(V031_CAPTURE_HOURS, database)
    lock = V022ProcessLock(Path(f"{database}.lock"))
    lock.acquire()
    try:
        binding = V031Store(database)
        binding.open(
            preregistration_sha256=sha256_file(prereg_file),
            launch_manifest_sha256=sha256_file(launch_file),
        )
        binding.close()
        meta = read_v031_meta(database)
        if meta.get("completion_reason") is not None:
            return v031_status(database)
        stop_event = asyncio.Event()
        return await run_v031_capture(
            settings=settings,
            output_db=database,
            preregistration_sha256=sha256_file(prereg_file),
            launch_manifest_sha256=sha256_file(launch_file),
            stop_event=stop_event,
        )
    finally:
        lock.release()


def v031_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).resolve()
    if not database.exists():
        return {
            "status": "NOT_STARTED",
            "database": str(database),
            "expected_markets": V031_EXPECTED_MARKETS,
            "outcomes_read": 0,
            "pnl_calculated": False,
            "orders_created": 0,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
    connection = open_read_only(database)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        meta = {
            str(row[0]): json.loads(str(row[1]))
            for row in connection.execute("SELECT key,value FROM v031_meta")
        }
        counts = connection.execute(
            """
            SELECT COUNT(*) AS markets,
             SUM(CASE WHEN capture_status='COMPLETED' THEN 1 ELSE 0 END) AS completed
            FROM v031_markets
            """
        ).fetchone()
        snapshots = connection.execute(
            """
            SELECT COUNT(*) AS rows,SUM(complete) AS complete,
             SUM(chainlink_fresh) AS chainlink_fresh,
             SUM(official_twap_fresh) AS twap_fresh,
             SUM(up_book_fresh AND down_book_fresh) AS books_fresh
            FROM v031_snapshots
            """
        ).fetchone()
    finally:
        connection.close()
    rows = int(snapshots["rows"] or 0)
    return {
        "status": (
            "COMPLETED" if meta.get("completion_reason") is not None else "RUNNING_OR_INTERRUPTED"
        ),
        "database": str(database),
        "sqlite_quick_check": quick_check,
        "capture_start_at": meta.get("capture_start_at"),
        "target_end_at": meta.get("target_end_at"),
        "completion_reason": meta.get("completion_reason"),
        "expected_markets": V031_EXPECTED_MARKETS,
        "markets": int(counts["markets"] or 0),
        "completed_markets": int(counts["completed"] or 0),
        "snapshots": rows,
        "complete_snapshots": int(snapshots["complete"] or 0),
        "complete_snapshot_coverage": round(int(snapshots["complete"] or 0) / rows, 8) if rows else 0.0,
        "chainlink_fresh_coverage": round(int(snapshots["chainlink_fresh"] or 0) / rows, 8) if rows else 0.0,
        "official_twap_fresh_coverage": round(int(snapshots["twap_fresh"] or 0) / rows, 8) if rows else 0.0,
        "both_books_fresh_coverage": round(int(snapshots["books_fresh"] or 0) / rows, 8) if rows else 0.0,
        "outcomes_read": 0,
        "pnl_calculated": False,
        "signals_generated": False,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


__all__ = [
    "IMPLEMENTATION_FILES",
    "IMPLEMENTATION_SCHEMA",
    "LAUNCH_SCHEMA",
    "V031RunnerError",
    "build_implementation_manifest",
    "load_and_verify_implementation",
    "load_and_verify_launch_approval",
    "run_v031",
    "run_v031_capture",
    "v031_status",
]
