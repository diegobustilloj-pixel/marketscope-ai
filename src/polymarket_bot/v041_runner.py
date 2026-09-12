from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import random
import shutil
import socket
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

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
    database_footprint,
    open_read_only,
    parse_utc,
    utc_now,
)
from polymarket_bot.v033_capture import technical_snapshot
from polymarket_bot.v038_runner import (
    clob_book_ages_ms,
    clob_generation_is_fresh,
    reconnect_backoff_ms,
)
from polymarket_bot.v041_chainlink_watchdog_design import (
    CHAINLINK_WATCHDOG_SECONDS,
    is_fresh_chainlink_message,
)
from polymarket_bot.v041_capture import (
    V041_CAPTURE_HOURS,
    V041_EXPECTED_MARKETS,
    V041_HEALTH_INTERVAL_SECONDS,
    V041_MARKET_SECONDS,
    V041_SUPPORTED_TWAP_WINDOWS,
    V041Store,
)
from polymarket_bot.v041_contract import (
    VARIANT,
    evaluate_market_probes,
    load_and_verify_prereg,
    summarize_transport_guard,
)


IMPLEMENTATION_SCHEMA = "implementation_v041_chainlink_silence_watchdog_4h_1"
LAUNCH_SCHEMA = "launch_approval_v041_chainlink_silence_watchdog_4h_1"
IMPLEMENTATION_FILES = {
    "shared_websocket_feed": "src/polymarket_bot/phase41.py",
    "transport_guard_design": "src/polymarket_bot/v040_transport_guard_design.py",
    "chainlink_watchdog_design": "src/polymarket_bot/v041_chainlink_watchdog_design.py",
    "contract": "src/polymarket_bot/v041_contract.py",
    "capture": "src/polymarket_bot/v041_capture.py",
    "runner": "src/polymarket_bot/v041_runner.py",
    "auditor": "src/polymarket_bot/v041_audit.py",
    "entrypoint": "v041_monitor.py",
    "tests": "tests/test_v041_chainlink_silence_watchdog.py",
}


class V041RunnerError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


async def supervise_chainlink_rtds(
    *,
    name: str,
    endpoint: str,
    subscription: dict[str, Any],
    heartbeat_text: str,
    heartbeat_seconds: float,
    use_proxy: bool,
    reconnect_max_seconds: float,
    stop_event: asyncio.Event,
    state: V031CaptureState,
    handler: Any,
    freshness_timeout_seconds: float = CHAINLINK_WATCHDOG_SECONDS,
) -> None:
    """Own Chainlink receive, silence detection, close and resubscription."""

    logger = logging.getLogger(name)
    attempt = 0
    while not stop_event.is_set():
        heartbeat_task: asyncio.Task[Any] | None = None
        watchdog_task: asyncio.Task[Any] | None = None
        stale_triggered = asyncio.Event()
        try:
            async with connect(
                endpoint,
                ping_interval=None,
                open_timeout=12,
                close_timeout=5,
                max_size=8 * 1024 * 1024,
                max_queue=4096,
                user_agent_header="polymarket-quant-bot-v041/0.41",
                proxy=True if use_proxy else None,
            ) as websocket:
                await websocket.send(
                    json.dumps(
                        subscription,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                state.connections[name] = "CONNECTED"
                attempt = 0
                freshness_clock = {"last": time.monotonic()}

                async def heartbeat() -> None:
                    while True:
                        await asyncio.sleep(heartbeat_seconds)
                        await websocket.send(heartbeat_text)

                async def freshness_watchdog() -> None:
                    poll_seconds = max(
                        0.01, min(2.0, freshness_timeout_seconds / 4.0)
                    )
                    while True:
                        await asyncio.sleep(poll_seconds)
                        age = time.monotonic() - freshness_clock["last"]
                        if age <= freshness_timeout_seconds:
                            continue
                        stale_triggered.set()
                        state.connections[name] = f"STALE:CHAINLINK:{age:.1f}s"
                        state.counters["rtds:chainlink:stale_watchdog"] += 1
                        logger.warning(
                            "%s sin Chainlink valido durante %.1fs; "
                            "forzando reconexion",
                            name,
                            age,
                        )
                        await websocket.close()
                        return

                heartbeat_task = asyncio.create_task(heartbeat())
                watchdog_task = asyncio.create_task(freshness_watchdog())
                async for message in websocket:
                    if stop_event.is_set():
                        break
                    raw = (
                        message.decode("utf-8", errors="replace")
                        if isinstance(message, bytes)
                        else str(message)
                    )
                    if is_fresh_chainlink_message(raw):
                        freshness_clock["last"] = time.monotonic()
                        state.counters["rtds:chainlink:watchdog_fresh"] += 1
                    await handler(raw)

                if stale_triggered.is_set() and not stop_event.is_set():
                    state.connections[name] = "RECONNECTING:STALE_CHAINLINK"
                    state.counters["rtds:chainlink:stale_reconnects"] += 1
                    await _wait_or_stop(stop_event, 1.0)
        except asyncio.CancelledError:
            state.connections[name] = "CANCELLED"
            raise
        except (ConnectionClosed, OSError, TimeoutError) as exc:
            state.connections[name] = f"RECONNECTING:{type(exc).__name__}"
            attempt += 1
            delay = min(reconnect_max_seconds, 2 ** min(attempt, 6))
            delay *= random.uniform(0.8, 1.2)
            logger.warning(
                "%s desconectado; reconexion en %.1fs", name, delay
            )
            await _wait_or_stop(stop_event, delay)
        except Exception as exc:
            state.connections[name] = f"FAILED:{type(exc).__name__}"
            logger.exception("%s fallo: %s", name, exc)
            await _wait_or_stop(stop_event, 5.0)
        finally:
            for task in (heartbeat_task, watchdog_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(
                    task
                    for task in (heartbeat_task, watchdog_task)
                    if task is not None
                ),
                return_exceptions=True,
            )


async def run_v041_chainlink_preflight(
    *, settings: Settings, seconds: float = 45.0
) -> dict[str, Any]:
    if seconds <= 0.0 or seconds > 120.0:
        raise V041RunnerError("Preflight V0.41 debe durar entre 0 y 120 segundos")
    state = V031CaptureState()
    stop_event = asyncio.Event()

    async def ingest(raw: str) -> None:
        state.ingest_rtds(raw, received_timestamp_ms=int(time.time() * 1000))

    started = time.monotonic()
    task = asyncio.create_task(
        supervise_chainlink_rtds(
            name="v041-preflight-rtds-chainlink",
            endpoint=settings.rtds_ws_url,
            subscription={"action": "subscribe", "subscriptions": [{
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": "{\"symbol\":\"btc/usd\"}",
            }]},
            heartbeat_text="PING",
            heartbeat_seconds=5.0,
            use_proxy=settings.ws_use_proxy,
            reconnect_max_seconds=settings.reconnect_max_seconds,
            stop_event=stop_event,
            state=state,
            handler=ingest,
        )
    )
    try:
        await asyncio.sleep(seconds)
    finally:
        stop_event.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    elapsed = round(time.monotonic() - started, 3)
    valid = int(state.counters.get("rtds:chainlink:watchdog_fresh", 0))
    updates = int(state.counters.get("rtds:chainlink_updates", 0))
    return {
        "schema": "evidence_v041_chainlink_network_preflight_1",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_seconds": elapsed,
        "strict_valid_chainlink_messages": valid,
        "ingested_chainlink_updates": updates,
        "watchdog_stale_events": int(
            state.counters.get("rtds:chainlink:stale_watchdog", 0)
        ),
        "watchdog_reconnects": int(
            state.counters.get("rtds:chainlink:stale_reconnects", 0)
        ),
        "passed": valid > 0 and updates > 0,
        "prices_stored": False,
        "outcomes_read": 0,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


@dataclass(slots=True)
class ValidatedPeerCache:
    """Process-only cache populated from a successful direct TLS connection."""

    peer_ip: str | None = None
    validated_at_ms: int | None = None

    @property
    def available(self) -> bool:
        return self.peer_ip is not None and self.validated_at_ms is not None

    def age_ms(self, now_ms: int) -> int | None:
        if self.validated_at_ms is None:
            return None
        return max(0, int(now_ms) - self.validated_at_ms)

    def remember(self, websocket: Any, *, expected_hostname: str, proxy_enabled: bool) -> bool:
        if proxy_enabled:
            return False
        transport = getattr(websocket, "transport", None)
        if transport is None:
            return False
        ssl_object = transport.get_extra_info("ssl_object")
        peer = transport.get_extra_info("peername")
        if ssl_object is None or not isinstance(peer, tuple) or not peer:
            return False
        if str(getattr(ssl_object, "server_hostname", "")) != expected_hostname:
            return False
        try:
            parsed = ipaddress.ip_address(str(peer[0]))
        except ValueError:
            return False
        if not parsed.is_global:
            return False
        self.peer_ip = str(parsed)
        self.validated_at_ms = int(time.time() * 1000)
        return True

    def connect_override(
        self, *, expected_hostname: str, port: int, proxy_enabled: bool
    ) -> dict[str, Any] | None:
        if not self.available or proxy_enabled:
            return None
        return {
            "host": self.peer_ip,
            "port": int(port),
            "server_hostname": expected_hostname,
        }


async def supervise_validated_peer_clob(
    *,
    settings: Settings,
    state: V031CaptureState,
    store: V041Store,
    peer_cache: ValidatedPeerCache,
    condition_id: str,
    slug: str,
    market_start_ms: int,
    up_token_id: str,
    down_token_id: str,
    stop_event: asyncio.Event,
    transport_contract: Mapping[str, Any],
) -> None:
    parsed_uri = urllib.parse.urlparse(settings.clob_ws_url)
    expected_hostname = str(parsed_uri.hostname or "")
    expected_port = int(parsed_uri.port or (443 if parsed_uri.scheme == "wss" else 80))
    if parsed_uri.scheme != "wss" or not expected_hostname:
        raise V041RunnerError("V0.41 exige URI CLOB wss valida")
    stale_timeout_ms = int(transport_contract["stale_timeout_ms"])
    poll_seconds = int(transport_contract["poll_ms"]) / 1000.0
    heartbeat_seconds = int(transport_contract["heartbeat_ms"]) / 1000.0
    backoff_schedule = [
        int(value) for value in transport_contract["reconnect_backoff_schedule_ms"]
    ]
    connection_name = f"v041-clob-{slug}"
    generation = 0
    incident_sequence = 0
    pending_incident: int | None = None
    pending_started_ms: int | None = None
    consecutive_reconnects = 0
    prefer_cached_peer = False

    while not stop_event.is_set():
        generation += 1
        generation_started_ms = int(time.time() * 1000)
        override = (
            peer_cache.connect_override(
                expected_hostname=expected_hostname,
                port=expected_port,
                proxy_enabled=settings.ws_use_proxy,
            )
            if prefer_cached_peer
            else None
        )
        dial_mode = "VALIDATED_PEER_IP" if override is not None else "DNS_HOSTNAME"
        generation_fresh_logged = False
        trigger_reason: str | None = None
        transport_error: str | None = None
        trigger_recorded_ms: int | None = None
        trigger_up_age_ms: int | None = None
        trigger_down_age_ms: int | None = None
        dns_failure = False
        try:
            connection_options: dict[str, Any] = {
                "ping_interval": None,
                "open_timeout": int(transport_contract["open_timeout_ms"]) / 1000.0,
                "close_timeout": int(transport_contract["close_timeout_ms"]) / 1000.0,
                "max_size": 8 * 1024 * 1024,
                "max_queue": 4096,
                "user_agent_header": "polymarket-quant-bot-v041/0.9.5a1",
                "proxy": True if settings.ws_use_proxy else None,
            }
            if override is not None:
                connection_options.update(override)
            async with connect(settings.clob_ws_url, **connection_options) as websocket:
                peer_cache.remember(
                    websocket,
                    expected_hostname=expected_hostname,
                    proxy_enabled=settings.ws_use_proxy,
                )
                await websocket.send(
                    json.dumps(
                        {
                            "assets_ids": [up_token_id, down_token_id],
                            "type": "market",
                            "custom_feature_enabled": True,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                state.connections[connection_name] = f"CONNECTED:{dial_mode}:AWAITING_BOTH_BOOKS"
                last_heartbeat = time.monotonic()
                while not stop_event.is_set():
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=poll_seconds)
                    except TimeoutError:
                        message = None
                    if message is not None:
                        raw = (
                            message.decode("utf-8", errors="replace")
                            if isinstance(message, bytes)
                            else str(message)
                        )
                        state.ingest_clob(raw, received_timestamp_ms=int(time.time() * 1000))
                    if time.monotonic() - last_heartbeat >= heartbeat_seconds:
                        await websocket.send("PING")
                        last_heartbeat = time.monotonic()
                    now_ms = int(time.time() * 1000)
                    up_age, down_age = clob_book_ages_ms(state, now_timestamp_ms=now_ms)
                    fresh = clob_generation_is_fresh(
                        state,
                        generation_started_ms=generation_started_ms,
                        now_timestamp_ms=now_ms,
                        stale_timeout_ms=stale_timeout_ms,
                    )
                    second_offset = max(0, (now_ms - market_start_ms) // 1000)
                    if fresh and not generation_fresh_logged:
                        cache_age = peer_cache.age_ms(now_ms)
                        store.save_transport_event(
                            condition_id=condition_id, slug=slug,
                            incident_id=pending_incident, generation=generation,
                            event_type="GENERATION_FRESH", trigger_reason=None,
                            recorded_timestamp_ms=now_ms, second_offset=second_offset,
                            up_book_age_ms=up_age, down_book_age_ms=down_age,
                            recovery_ms=None, transport_error=None, dial_mode=dial_mode,
                            validated_peer_cache_available=peer_cache.available,
                            validated_peer_cache_age_ms=cache_age,
                        )
                        state.counters["clob:validated_peer_fresh_generations"] += 1
                        if dial_mode == "VALIDATED_PEER_IP":
                            state.counters["clob:validated_peer_fresh_dials"] += 1
                        state.connections[connection_name] = f"CONNECTED:{dial_mode}:FRESH"
                        generation_fresh_logged = True
                        consecutive_reconnects = 0
                        if pending_incident is not None and pending_started_ms is not None:
                            recovery_ms = max(0, now_ms - pending_started_ms)
                            store.save_transport_event(
                                condition_id=condition_id, slug=slug,
                                incident_id=pending_incident, generation=generation,
                                event_type="RECOVERED", trigger_reason=None,
                                recorded_timestamp_ms=now_ms, second_offset=second_offset,
                                up_book_age_ms=up_age, down_book_age_ms=down_age,
                                recovery_ms=recovery_ms, transport_error=None,
                                dial_mode=dial_mode,
                                validated_peer_cache_available=peer_cache.available,
                                validated_peer_cache_age_ms=cache_age,
                            )
                            state.counters["clob:validated_peer_recoveries"] += 1
                            if dial_mode == "VALIDATED_PEER_IP":
                                state.counters["clob:validated_peer_fallback_recoveries"] += 1
                            if recovery_ms > int(transport_contract["recovery_service_level_ms"]):
                                state.counters["clob:validated_peer_slow_recoveries"] += 1
                            pending_incident = None
                            pending_started_ms = None
                    generation_age_ms = max(0, now_ms - generation_started_ms)
                    if generation_age_ms > stale_timeout_ms and (
                        up_age is None
                        or down_age is None
                        or up_age > stale_timeout_ms
                        or down_age > stale_timeout_ms
                    ):
                        trigger_reason = "STALE_CLOB"
                        trigger_recorded_ms = now_ms
                        trigger_up_age_ms = up_age
                        trigger_down_age_ms = down_age
                        break
        except asyncio.CancelledError:
            state.connections[connection_name] = "CANCELLED"
            raise
        except socket.gaierror as exc:
            dns_failure = True
            trigger_reason = "TRANSPORT_gaierror"
            transport_error = str(exc)[:500]
        except (ConnectionClosed, OSError, TimeoutError, WebSocketException) as exc:
            trigger_reason = f"TRANSPORT_{type(exc).__name__}"
            transport_error = str(exc)[:500]

        if stop_event.is_set():
            break
        if trigger_reason is None:
            trigger_reason = "TRANSPORT_SESSION_ENDED"
        if trigger_recorded_ms is None:
            trigger_recorded_ms = int(time.time() * 1000)
            trigger_up_age_ms, trigger_down_age_ms = clob_book_ages_ms(
                state, now_timestamp_ms=trigger_recorded_ms
            )
        second_offset = max(0, (trigger_recorded_ms - market_start_ms) // 1000)
        if pending_incident is None:
            incident_sequence += 1
            pending_incident = incident_sequence
            pending_started_ms = trigger_recorded_ms
            state.counters["clob:validated_peer_incidents"] += 1
        store.save_transport_event(
            condition_id=condition_id, slug=slug, incident_id=pending_incident,
            generation=generation, event_type="RECONNECT_TRIGGER",
            trigger_reason=trigger_reason, recorded_timestamp_ms=trigger_recorded_ms,
            second_offset=second_offset, up_book_age_ms=trigger_up_age_ms,
            down_book_age_ms=trigger_down_age_ms, recovery_ms=None,
            transport_error=transport_error, dial_mode=dial_mode,
            validated_peer_cache_available=peer_cache.available,
            validated_peer_cache_age_ms=peer_cache.age_ms(trigger_recorded_ms),
        )
        state.counters["clob:validated_peer_reconnect_triggers"] += 1
        state.connections[connection_name] = f"RECONNECTING:{dial_mode}:{trigger_reason}"
        if dial_mode == "VALIDATED_PEER_IP":
            prefer_cached_peer = False
        else:
            prefer_cached_peer = peer_cache.available and (
                generation_fresh_logged or dns_failure
            )
        consecutive_reconnects += 1
        delay_ms = reconnect_backoff_ms(consecutive_reconnects, backoff_schedule)
        await _wait_or_stop(stop_event, delay_ms / 1000.0)


def build_implementation_manifest(
    *, prereg_path: str | Path, output_path: str | Path, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    output = Path(output_path).resolve()
    prereg = load_and_verify_prereg(prereg_file, project_root=root)
    hashes: dict[str, str] = {}
    for key, relative in IMPLEMENTATION_FILES.items():
        source = root / relative
        if not source.is_file():
            raise V041RunnerError(f"Implementacion V0.41 incompleta: {relative}")
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
        "single_layer_clob_transport_built": True,
        "validated_peer_memory_cache_built": True,
        "tls_identity_preservation_built": True,
        "pre_stale_transport_guard_built": True,
        "chainlink_silence_watchdog_built": True,
        "transport_event_journal_built": True,
        "full_reserved_retry_budget_built": True,
        "technical_auditor_built": True,
        "entrypoint_built": True,
        "economic_strategy_built": False,
        "automatic_final_audit_built": True,
        "scheduled_supervision_built": False,
        "launch_approved": False,
        "launch_status": "NOT_LAUNCHED",
        "capture_contract": prereg["capture_contract"],
        "transport_contract": prereg["transport_contract"],
        "probe_contract": prereg["probe_contract"],
        "chainlink_feed_contract": prereg["chainlink_feed_contract"],
        "technical_gates": prereg["technical_gates"],
        "stopping": prereg["stopping"],
        "data_policy": prereg["data_policy"],
        "safety": prereg["safety"],
    }
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if existing.get("schema") == IMPLEMENTATION_SCHEMA and existing.get("code_hashes") == hashes:
            return existing
        raise V041RunnerError("Existe otro manifiesto V0.41")
    _write_atomic(output, payload)
    return payload


def load_and_verify_implementation(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V041RunnerError("Implementacion V0.41 no sellada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema") != IMPLEMENTATION_SCHEMA:
        raise V041RunnerError("Manifiesto V0.41 incompatible")
    prereg_file = Path(str(payload.get("preregistration") or "")).resolve()
    prereg = load_and_verify_prereg(prereg_file, project_root=root)
    expected = {
        "status": "BUILT_TESTED_AWAITING_ONE_FRESH_TECHNICAL_LAUNCH_APPROVAL",
        "variant": VARIANT,
        "preregistration_sha256": sha256_file(prereg_file),
        "collector_built": True,
        "single_layer_clob_transport_built": True,
        "validated_peer_memory_cache_built": True,
        "tls_identity_preservation_built": True,
        "pre_stale_transport_guard_built": True,
        "chainlink_silence_watchdog_built": True,
        "transport_event_journal_built": True,
        "full_reserved_retry_budget_built": True,
        "technical_auditor_built": True,
        "entrypoint_built": True,
        "economic_strategy_built": False,
        "automatic_final_audit_built": True,
        "scheduled_supervision_built": False,
        "launch_approved": False,
        "launch_status": "NOT_LAUNCHED",
        "capture_contract": prereg["capture_contract"],
        "transport_contract": prereg["transport_contract"],
        "probe_contract": prereg["probe_contract"],
        "chainlink_feed_contract": prereg["chainlink_feed_contract"],
        "technical_gates": prereg["technical_gates"],
        "stopping": prereg["stopping"],
        "data_policy": prereg["data_policy"],
        "safety": prereg["safety"],
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V041RunnerError(f"Manifiesto V0.41 invalido: {key}")
    hashes = payload.get("code_hashes")
    if not isinstance(hashes, Mapping) or set(hashes) != set(IMPLEMENTATION_FILES):
        raise V041RunnerError("Inventario V0.41 incompatible")
    for key, relative in IMPLEMENTATION_FILES.items():
        if hashes.get(key) != sha256_file(root / relative):
            raise V041RunnerError(f"Hash V0.41 no coincide: {key}")
    return dict(payload)


def load_and_verify_launch_approval(
    path: str | Path, *, prereg_path: str | Path, implementation_path: str | Path
) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.is_file():
        raise V041RunnerError("V0.41 permanece NOT_LAUNCHED sin aprobacion explicita")
    payload = json.loads(source.read_text(encoding="utf-8"))
    expected = {
        "schema": LAUNCH_SCHEMA,
        "status": "APPROVED_FOR_ONE_FRESH_CHAINLINK_WATCHDOG_REPLICATION",
        "variant": VARIANT,
        "technical_capture_hours": V041_CAPTURE_HOURS,
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
            raise V041RunnerError(f"Aprobacion V0.41 invalida: {key}")
    return dict(payload)


async def run_v041_capture(
    *,
    settings: Settings,
    output_db: str | Path,
    preregistration_sha256: str,
    implementation_sha256: str,
    launch_manifest_sha256: str,
    transport_contract: Mapping[str, Any],
    probe_contract: Mapping[str, Any],
    stop_event: asyncio.Event,
    maximum_database_gb: float,
    minimum_free_gb: float,
) -> dict[str, Any]:
    database = Path(output_db).resolve()
    store = V041Store(database)
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
    peer_cache = ValidatedPeerCache()
    status = "COMPLETED"
    error: str | None = None
    safety_stop_reason: str | None = None

    async def ingest_rtds(raw: str) -> None:
        state.ingest_rtds(raw, received_timestamp_ms=int(time.time() * 1000))

    global_tasks = [
        asyncio.create_task(
            supervise_chainlink_rtds(
                name="v041-rtds-chainlink", endpoint=settings.rtds_ws_url,
                subscription={"action": "subscribe", "subscriptions": [{
                    "topic": "crypto_prices_chainlink", "type": "*", "filters": "{\"symbol\":\"btc/usd\"}"
                }]},
                heartbeat_text="PING", heartbeat_seconds=5.0,
                use_proxy=settings.ws_use_proxy,
                reconnect_max_seconds=settings.reconnect_max_seconds,
                stop_event=stop_event, state=state, handler=ingest_rtds,
                freshness_timeout_seconds=CHAINLINK_WATCHDOG_SECONDS,
            )
        )
    ]
    for window_s in V041_SUPPORTED_TWAP_WINDOWS:
        global_tasks.append(
            asyncio.create_task(
                _websocket_feed(
                    name=f"v041-rtds-twap-{window_s}s", endpoint=settings.rtds_ws_url,
                    subscription={"action": "subscribe", "subscriptions": [{
                        "topic": TWAP_TOPIC_BY_WINDOW[window_s], "type": "update", "filters": "{\"symbol\":\"btc/usd\"}"
                    }]},
                    heartbeat_text="PING", heartbeat_seconds=5.0,
                    use_proxy=settings.ws_use_proxy,
                    reconnect_max_seconds=settings.reconnect_max_seconds,
                    stop_event=stop_event, state=state, handler=ingest_rtds,
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
            await _wait_or_stop(stop_event, V041_HEALTH_INTERVAL_SECONDS)

    health_task = asyncio.create_task(health_monitor())
    try:
        await _wait_or_stop(stop_event, max(0.0, capture_start - time.time()))
        market_start = int(capture_start)
        while market_start < int(target_end) and not stop_event.is_set():
            slug = f"btc-updown-5m-{market_start}"
            if store.market_exists(slug):
                await _wait_or_stop(stop_event, max(0.0, market_start + V041_MARKET_SECONDS - time.time()))
                market_start += V041_MARKET_SECONDS
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
                market_start += V041_MARKET_SECONDS
                continue
            definition, initial_resolution, _ = discovered
            market = _silver_market(definition, initial_resolution)
            resolution_contract = resolution_twap_contract(market.resolution_source)
            store.save_market(market, contract=resolution_contract)
            if not resolution_contract.verified or resolution_contract.window_seconds not in V041_SUPPORTED_TWAP_WINDOWS:
                state.counters["market_contract_rejected"] += 1
                market_start += V041_MARKET_SECONDS
                continue
            state.activate_market(market)
            clob_stop = asyncio.Event()
            clob_task = asyncio.create_task(
                supervise_validated_peer_clob(
                    settings=settings, state=state, store=store, peer_cache=peer_cache,
                    condition_id=market.condition_id, slug=market.slug,
                    market_start_ms=market.start_ms, up_token_id=market.up_token_id,
                    down_token_id=market.down_token_id, stop_event=clob_stop,
                    transport_contract=transport_contract,
                )
            )
            captured = 0
            for offset in range(V041_MARKET_SECONDS):
                if stop_event.is_set():
                    break
                if clob_task.done():
                    failure = clob_task.exception()
                    if failure is not None:
                        raise V041RunnerError(f"Supervisor CLOB termino: {failure}")
                    raise V041RunnerError("Supervisor CLOB termino inesperadamente")
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
            state.connections.pop(f"v041-clob-{market.slug}", None)
            probes = evaluate_market_probes(
                store.load_market_snapshots(market.condition_id),
                condition_id=market.condition_id, slug=market.slug,
                contract=probe_contract,
            )
            summary = summarize_transport_guard(probes)
            capture_error = None if captured >= 285 else f"INSUFFICIENT_SNAPSHOTS:{captured}"
            store.finish_market(market.condition_id, error=capture_error, probe_summary=summary)
            state.clear_market()
            if capture_error is None and int(summary["trapped_positions"]) > 0:
                store.set_meta("completion_reason", "FREEZE_FIRST_TRAPPED_POSITION")
                store.set_meta("observation_ended_at", utc_now())
                stop_event.set()
            market_start += V041_MARKET_SECONDS
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
        "signals_generated": False, "trades_generated": False,
        "orders_created": 0, "paper_orders": 0, "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


async def run_v041(
    *, settings: Settings, prereg_path: str | Path, implementation_path: str | Path,
    launch_path: str | Path, output_db: str | Path,
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
    enforce_forward_duration(V041_CAPTURE_HOURS, database)
    lock = V022ProcessLock(Path(f"{database}.lock"))
    lock.acquire()
    try:
        binding = V041Store(database)
        binding.open(
            preregistration_sha256=sha256_file(prereg_file),
            implementation_sha256=sha256_file(implementation_file),
            launch_manifest_sha256=sha256_file(launch_file),
        )
        meta = binding.meta()
        binding.close()
        if meta.get("completion_reason") is not None:
            return v041_status(database)
        return await run_v041_capture(
            settings=settings, output_db=database,
            preregistration_sha256=sha256_file(prereg_file),
            implementation_sha256=sha256_file(implementation_file),
            launch_manifest_sha256=sha256_file(launch_file),
            transport_contract=prereg["transport_contract"],
            probe_contract=prereg["probe_contract"], stop_event=asyncio.Event(),
            maximum_database_gb=float(prereg["safety"]["maximum_database_gb"]),
            minimum_free_gb=float(prereg["safety"]["minimum_free_disk_gb"]),
        )
    finally:
        lock.release()


def v041_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).resolve()
    safe = {
        "outcomes_read": 0, "prices_stored": False, "pnl_calculated": False,
        "signals_generated": False, "trades_generated": False,
        "orders_created": 0, "paper_orders": 0, "wallet_required": False,
        "real_money": "BLOQUEADO",
    }
    if not database.exists():
        return {"status": "NOT_STARTED", "database": str(database),
                "expected_markets": V041_EXPECTED_MARKETS, **safe}
    connection = open_read_only(database)
    try:
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        meta = {str(row[0]): json.loads(str(row[1]))
                for row in connection.execute("SELECT key,value FROM v041_meta")}
        markets = connection.execute(
            """SELECT COUNT(*) markets,SUM(capture_status='COMPLETED') completed,
            SUM(COALESCE(probe_total,0)) probes,SUM(COALESCE(probe_entered,0)) entered,
            SUM(COALESCE(probe_relative_guard_exits,0)) relative_guards,
            SUM(COALESCE(probe_transport_guard_exits,0)) transport_guards,
            SUM(COALESCE(probe_exit_successes,0)) exits,SUM(COALESCE(probe_trapped,0)) trapped
            FROM v041_markets"""
        ).fetchone()
        snapshots = connection.execute(
            """SELECT COUNT(*) rows,SUM(complete_v2) complete,
            SUM(chainlink_fresh) chainlink_fresh FROM v041_snapshots"""
        ).fetchone()
        health_row = connection.execute(
            "SELECT counters_json FROM v041_health ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        health_counters = (
            json.loads(str(health_row["counters_json"])) if health_row else {}
        )
        transport = connection.execute(
            """SELECT SUM(event_type='GENERATION_FRESH') fresh_generations,
            SUM(event_type='RECONNECT_TRIGGER') reconnect_triggers,
            SUM(event_type='RECOVERED') recoveries,
            SUM(event_type='GENERATION_FRESH' AND dial_mode='VALIDATED_PEER_IP') cached_fresh,
            SUM(event_type='RECOVERED' AND dial_mode='VALIDATED_PEER_IP') cached_recoveries,
            MAX(CASE WHEN event_type='RECOVERED' THEN recovery_ms END) max_recovery_ms
            FROM v041_clob_transport_events"""
        ).fetchone()
    finally:
        connection.close()
    completion = meta.get("completion_reason")
    return {
        "status": "COMPLETED" if completion else "RUNNING_OR_INTERRUPTED",
        "database": str(database), "sqlite_quick_check": quick,
        "capture_start_at": meta.get("capture_start_at"),
        "target_end_at": meta.get("target_end_at"),
        "estimated_remaining_seconds": 0 if completion else max(
            0, int(parse_utc(str(meta["target_end_at"])).timestamp() - time.time())
        ),
        "completion_reason": completion, "expected_markets": V041_EXPECTED_MARKETS,
        "markets": int(markets["markets"] or 0),
        "completed_markets": int(markets["completed"] or 0),
        "snapshots": int(snapshots["rows"] or 0),
        "complete_v2_snapshots": int(snapshots["complete"] or 0),
        "chainlink_fresh_snapshots": int(snapshots["chainlink_fresh"] or 0),
        "chainlink_watchdog_fresh_messages": int(
            health_counters.get("rtds:chainlink:watchdog_fresh", 0)
        ),
        "chainlink_watchdog_stale_events": int(
            health_counters.get("rtds:chainlink:stale_watchdog", 0)
        ),
        "chainlink_watchdog_reconnects": int(
            health_counters.get("rtds:chainlink:stale_reconnects", 0)
        ),
        "capacity_probes": int(markets["probes"] or 0),
        "entry_eligible_probes": int(markets["entered"] or 0),
        "relative_guard_exits": int(markets["relative_guards"] or 0),
        "transport_pre_stale_guard_exits": int(markets["transport_guards"] or 0),
        "exit_successes": int(markets["exits"] or 0),
        "trapped_positions": int(markets["trapped"] or 0),
        "transport_fresh_generations": int(transport["fresh_generations"] or 0),
        "transport_reconnect_triggers": int(transport["reconnect_triggers"] or 0),
        "transport_recoveries": int(transport["recoveries"] or 0),
        "validated_peer_fresh_dials": int(transport["cached_fresh"] or 0),
        "validated_peer_fallback_recoveries": int(transport["cached_recoveries"] or 0),
        "transport_max_recovery_ms": (
            int(transport["max_recovery_ms"])
            if transport["max_recovery_ms"] is not None else None
        ),
        **safe,
    }


__all__ = [
    "IMPLEMENTATION_FILES", "IMPLEMENTATION_SCHEMA", "LAUNCH_SCHEMA",
    "V041RunnerError", "ValidatedPeerCache", "build_implementation_manifest",
    "load_and_verify_implementation", "load_and_verify_launch_approval",
    "run_v041", "run_v041_chainlink_preflight", "supervise_chainlink_rtds",
    "supervise_validated_peer_clob", "v041_status",
]
