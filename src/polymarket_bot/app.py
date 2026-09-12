from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.collectors.binance import build_binance_collector
from polymarket_bot.collectors.clob import build_clob_collector
from polymarket_bot.collectors.rtds import build_rtds_collector
from polymarket_bot.config import Settings
from polymarket_bot.discovery import DiscoveryError, GammaDiscovery
from polymarket_bot.domain import MarketDefinition, RawEvent, Sequence
from polymarket_bot.storage import AsyncEventWriter, SQLiteStore


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def settings_with_db(settings: Settings, db_path: str | None) -> Settings:
    return (
        replace(settings, db_path=Path(db_path))
        if db_path is not None
        else settings
    )


def _parse_end_seconds(end_at: str | None) -> float:
    if not end_at:
        return 310.0
    try:
        parsed = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
    except ValueError:
        return 310.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    remaining = (parsed - datetime.now(timezone.utc)).total_seconds() + 8.0
    return max(2.0, min(remaining, 330.0))


def _market_summary(market: MarketDefinition) -> dict[str, Any]:
    return {
        "slug": market.slug,
        "question": market.question,
        "condition_id": market.condition_id,
        "start_at": market.start_at,
        "end_at": market.end_at,
        "resolution_source": market.resolution_source,
        "active": market.active,
        "closed": market.closed,
        "accepting_orders": market.accepting_orders,
        "tokens": market.token_by_outcome,
    }


async def run_demo(settings: Settings) -> dict[str, Any]:
    store = SQLiteStore(settings.db_path)
    store.open()
    sequence = Sequence()
    writer = AsyncEventWriter(store)
    await writer.start()
    try:
        payloads = [
            {
                "topic": "crypto_prices_chainlink",
                "type": "update",
                "timestamp": 1782753357257,
                "payload": {
                    "symbol": "btc/usd",
                    "timestamp": 1782753357213,
                    "value": "67234.50",
                },
            },
            {
                "topic": "crypto_prices",
                "type": "update",
                "timestamp": 1782753357258,
                "payload": {
                    "symbol": "btcusdt",
                    "timestamp": 1782753357214,
                    "value": "67235.10",
                },
            },
            {
                "event_type": "book",
                "market": "0xsynthetic-condition",
                "asset_id": "synthetic-up-token",
                "timestamp": "1782753357259",
                "bids": [{"price": "0.54", "size": "100"}],
                "asks": [{"price": "0.55", "size": "80"}],
            },
            {
                "event_type": "best_bid_ask",
                "market": "0xsynthetic-condition",
                "asset_id": "synthetic-down-token",
                "best_bid": "0.44",
                "best_ask": "0.45",
                "spread": "0.01",
                "timestamp": "1782753357260",
            },
            {
                "event_type": "last_trade_price",
                "market": "0xsynthetic-condition",
                "asset_id": "synthetic-up-token",
                "price": "0.55",
                "size": "12.5",
                "side": "BUY",
                "timestamp": "1782753357261",
            },
        ]
        for payload in payloads:
            raw = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            )
            await writer.submit(
                RawEvent.create(
                    source="synthetic",
                    default_stream="demo",
                    payload_raw=raw,
                    sequence=sequence.next(),
                )
            )
        market_payload = {
            "id": "synthetic-event",
            "slug": "btc-updown-5m-synthetic",
            "title": "BTC Up or Down - synthetic demo",
            "markets": [
                {
                    "slug": "btc-updown-5m-synthetic",
                    "conditionId": "0xsynthetic-condition",
                    "question": "BTC Up or Down - synthetic demo",
                    "startDate": "2026-07-23T20:00:00Z",
                    "endDate": "2026-07-23T20:05:00Z",
                    "resolutionSource": "Chainlink BTC/USD (synthetic)",
                    "outcomes": '["Up","Down"]',
                    "clobTokenIds": (
                        '["synthetic-up-token","synthetic-down-token"]'
                    ),
                    "active": False,
                    "closed": True,
                    "acceptingOrders": False,
                }
            ],
        }
        market_raw = json.dumps(
            market_payload, ensure_ascii=False, separators=(",", ":")
        )
        from polymarket_bot.discovery import parse_market_payload

        market = parse_market_payload(market_payload, market_raw)
        await asyncio.to_thread(store.save_market, market)
    finally:
        await writer.close()
        result = store.stats()
        integrity = store.verify()
        store.close()
    result["integrity"] = integrity
    result["mode"] = "DEMO_SIN_INTERNET_SIN_DINERO"
    return result


async def discover_once(
    settings: Settings, preferred_slug: str | None
) -> dict[str, Any]:
    store = SQLiteStore(settings.db_path)
    store.open()
    writer = AsyncEventWriter(store)
    await writer.start()
    try:
        discovery = GammaDiscovery(settings, store, writer, Sequence())
        market = await discovery.discover(preferred_slug)
        return _market_summary(market)
    finally:
        await writer.close()
        store.close()


async def _run_clob_rotation(
    *,
    settings: Settings,
    store: SQLiteStore,
    writer: AsyncEventWriter,
    sequence: Sequence,
    stop_event: asyncio.Event,
    preferred_slug: str | None,
) -> None:
    logger = logging.getLogger("market-rotation")
    discovery = GammaDiscovery(settings, store, writer, sequence)
    while not stop_event.is_set():
        try:
            market = await discovery.discover(preferred_slug)
        except DiscoveryError as exc:
            logger.warning("Discovery no disponible: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            except TimeoutError:
                continue
            break

        logger.info(
            "Mercado %s | active=%s | acceptingOrders=%s",
            market.slug,
            market.active,
            market.accepting_orders,
        )
        local_stop = asyncio.Event()
        collector = build_clob_collector(
            settings, store, writer, sequence, market
        )
        collector_task = asyncio.create_task(
            collector.run(local_stop), name=f"collector-{market.slug}"
        )
        global_wait = asyncio.create_task(
            stop_event.wait(), name="global-stop-wait"
        )
        timeout = _parse_end_seconds(market.end_at)
        done, _ = await asyncio.wait(
            {collector_task, global_wait},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        local_stop.set()
        global_wait.cancel()
        if collector_task in done:
            await collector_task
        else:
            collector_task.cancel()
            await asyncio.gather(collector_task, return_exceptions=True)
        await asyncio.gather(global_wait, return_exceptions=True)
        if preferred_slug is not None and not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=2.0)
            except TimeoutError:
                pass


def _database_footprint(path: Path) -> int:
    return sum(
        candidate.stat().st_size
        for candidate in (
            path,
            Path(f"{path}-wal"),
            Path(f"{path}-shm"),
        )
        if candidate.exists()
    )


async def _safety_monitor(
    *,
    db_path: Path,
    stop_event: asyncio.Event,
    max_db_gb: float,
    min_free_gb: float,
    state: dict[str, Any],
) -> None:
    logger = logging.getLogger("storage-guard")
    max_db_bytes = int(max_db_gb * 1_000_000_000)
    min_free_bytes = int(min_free_gb * 1_000_000_000)
    loop = asyncio.get_running_loop()
    last_progress = 0.0
    while not stop_event.is_set():
        database_bytes = _database_footprint(db_path)
        free_bytes = shutil.disk_usage(db_path.parent).free
        state["database_bytes"] = database_bytes
        state["free_bytes"] = free_bytes
        if max_db_bytes and database_bytes >= max_db_bytes:
            state["stop_reason"] = (
                f"BASE_ALCANZO_LIMITE_{max_db_gb:g}_GB"
            )
            logger.error(
                "Parada segura: la base alcanzó %.2f GB.",
                database_bytes / 1_000_000_000,
            )
            stop_event.set()
            break
        if min_free_bytes and free_bytes <= min_free_bytes:
            state["stop_reason"] = (
                f"ESPACIO_LIBRE_BAJO_{min_free_gb:g}_GB"
            )
            logger.error(
                "Parada segura: quedan %.2f GB libres.",
                free_bytes / 1_000_000_000,
            )
            stop_event.set()
            break
        now = loop.time()
        if now - last_progress >= 900:
            logger.info(
                "Control de disco: base %.2f GB | libres %.2f GB",
                database_bytes / 1_000_000_000,
                free_bytes / 1_000_000_000,
            )
            last_progress = now
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=15.0)
        except TimeoutError:
            pass


async def run_collect(
    settings: Settings,
    *,
    seconds: float,
    preferred_slug: str | None,
    max_db_gb: float,
    min_free_gb: float,
) -> dict[str, Any]:
    if seconds < 0:
        raise ValueError("--seconds no puede ser negativo")
    if max_db_gb < 0 or min_free_gb < 0:
        raise ValueError("Los límites de almacenamiento no pueden ser negativos")
    store = SQLiteStore(settings.db_path)
    store.open()
    writer = AsyncEventWriter(store)
    await writer.start()
    sequence = Sequence()
    stop_event = asyncio.Event()
    tasks = [
        asyncio.create_task(
            build_binance_collector(
                settings, store, writer, sequence
            ).run(stop_event),
            name="collector-binance",
        ),
        asyncio.create_task(
            build_rtds_collector(
                settings, store, writer, sequence
            ).run(stop_event),
            name="collector-rtds",
        ),
        asyncio.create_task(
            _run_clob_rotation(
                settings=settings,
                store=store,
                writer=writer,
                sequence=sequence,
                stop_event=stop_event,
                preferred_slug=preferred_slug,
            ),
            name="collector-clob-rotation",
        ),
    ]
    safety_state: dict[str, Any] = {
        "stop_reason": None,
        "database_bytes": _database_footprint(settings.db_path),
        "free_bytes": shutil.disk_usage(settings.db_path.parent).free,
    }
    safety_task = asyncio.create_task(
        _safety_monitor(
            db_path=settings.db_path,
            stop_event=stop_event,
            max_db_gb=max_db_gb,
            min_free_gb=min_free_gb,
            state=safety_state,
        ),
        name="storage-safety-monitor",
    )
    try:
        if seconds > 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=seconds)
            except TimeoutError:
                stop_event.set()
            done, pending = await asyncio.wait(tasks, timeout=5.0)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        else:
            await asyncio.gather(*tasks)
    finally:
        stop_event.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not safety_task.done():
            safety_task.cancel()
        await asyncio.gather(safety_task, return_exceptions=True)
        await writer.close()
        result = store.stats()
        logging.getLogger("integrity").info(
            "Verificando bloques comprimidos; espere a que termine."
        )
        result["integrity"] = store.verify_fast()
        logging.getLogger("integrity").info(
            "Verificación rápida completada: ok=%s",
            result["integrity"]["ok"],
        )
        safety_state["database_bytes"] = _database_footprint(settings.db_path)
        safety_state["free_bytes"] = shutil.disk_usage(
            settings.db_path.parent
        ).free
        result["safety"] = {
            "stop_reason": safety_state["stop_reason"],
            "max_database_gb": max_db_gb,
            "minimum_free_gb": min_free_gb,
            "database_gb": round(
                safety_state["database_bytes"] / 1_000_000_000, 3
            ),
            "free_gb": round(
                safety_state["free_bytes"] / 1_000_000_000, 3
            ),
        }
        store.close()
    return result


def database_status(settings: Settings) -> dict[str, Any]:
    store = SQLiteStore(settings.db_path)
    store.open()
    try:
        return store.stats()
    finally:
        store.close()


def database_diagnostics(settings: Settings) -> dict[str, Any]:
    store = SQLiteStore(settings.db_path)
    store.open()
    try:
        return store.diagnostics()
    finally:
        store.close()


def verify_database(settings: Settings) -> dict[str, Any]:
    store = SQLiteStore(settings.db_path)
    store.open()
    try:
        result: dict[str, Any] = store.verify()
        result["database"] = str(settings.db_path)
        result["ok"] = (
            result["corrupt"] == 0
            and result["corrupt_chunks"] == 0
        )
        return result
    finally:
        store.close()


def fast_verify_database(settings: Settings) -> dict[str, Any]:
    store = SQLiteStore(settings.db_path)
    store.open()
    try:
        result = store.verify_fast()
        result["database"] = str(settings.db_path)
        return result
    finally:
        store.close()


def audit_database(
    settings: Settings, *, min_hours: float, min_coverage: float
) -> dict[str, Any]:
    store = SQLiteStore(settings.db_path)
    store.open()
    try:
        result = store.audit(
            min_hours=min_hours,
            min_coverage=min_coverage,
        )
        result["database"] = str(settings.db_path)
        return result
    finally:
        store.close()
