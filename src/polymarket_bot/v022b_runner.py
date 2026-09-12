from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.config import Settings
from polymarket_bot.phase41 import LiveShadowState, _websocket_feed
from polymarket_bot.v018_runner import ROOT, _discover, _parse_utc, sha256_file, utc_now
from polymarket_bot.v022_runner import DDL, V022ProcessLock, V022Store, run_smoke, v022_status
from polymarket_bot.v022_synced_pair import (
    SyncedPairOpportunityEngine,
    V022Error,
    validate_strategy_config,
)


PREREG_SCHEMA = "prereg_v022b_synced_persistent_observer_1"
DATABASE_SCHEMA = "paper_v022b_synced_persistent_observer_1"
TARGET_HOURS = 8.0
MAXIMUM_HOURS = 8.0


def load_and_verify_prereg(path: str | Path) -> dict[str, Any]:
    prereg_path = Path(path).resolve()
    import json

    payload = json.loads(prereg_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != PREREG_SCHEMA:
        raise V022Error("Prerregistro V0.22b incompatible")
    if payload.get("status") != "FROZEN_SYNCHRONIZED_OBSERVER_ONLY":
        raise V022Error("V0.22b no esta congelado como observador sincronizado")
    hours = float(payload.get("target_hours", 0))
    if hours != TARGET_HOURS or hours > MAXIMUM_HOURS:
        raise V022Error("V0.22b debe durar exactamente 8 horas")
    expected_safety = {
        "wallet_required": False,
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "real_money": "BLOQUEADO",
        "outcomes_before_completion": False,
        "outcomes_after_completion": False,
        "active_forward_read": False,
        "active_forward_modified": False,
        "maximum_hours": 8,
    }
    safety = payload.get("safety")
    if not isinstance(safety, dict):
        raise V022Error("Safety V0.22b ausente")
    for key, value in expected_safety.items():
        if safety.get(key) != value:
            raise V022Error(f"Safety V0.22b incumplido: {key}")
    validate_strategy_config(payload.get("strategy", {}))
    code = payload.get("code_hashes")
    if not isinstance(code, dict):
        raise V022Error("Hashes V0.22b ausentes")
    expected_hashes = {
        "engine": sha256_file(ROOT / "src/polymarket_bot/v022_synced_pair.py"),
        "runner": sha256_file(Path(__file__)),
        "entrypoint": sha256_file(ROOT / "v022b_monitor.py"),
        "auditor": sha256_file(ROOT / "src/polymarket_bot/v022b_audit.py"),
        "base_runner": sha256_file(ROOT / "src/polymarket_bot/v022_runner.py"),
        "base_auditor": sha256_file(ROOT / "src/polymarket_bot/v022_audit.py"),
        "collector": sha256_file(ROOT / "src/polymarket_bot/phase41.py"),
        "cost_model": sha256_file(ROOT / "src/polymarket_bot/v021_safe_pair.py"),
    }
    for key, actual in expected_hashes.items():
        if code.get(key) != actual:
            raise V022Error(f"Hash V0.22b no coincide: {key}")
    design = payload.get("design_evidence")
    if not isinstance(design, dict):
        raise V022Error("Evidencia de diseno V0.22b ausente")
    evidence = (ROOT / str(design.get("relative_path"))).resolve()
    if not evidence.is_file() or sha256_file(evidence) != design.get("sha256"):
        raise V022Error("Evidencia de diseno V0.22b no coincide")
    return payload


class V022BStore(V022Store):
    def open(self, prereg: Mapping[str, Any], prereg_path: str | Path) -> None:
        import sqlite3

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(DDL)
        meta = self.meta()
        prereg_hash = sha256_file(prereg_path)
        if not meta:
            started = time.time()
            target_hours = float(prereg["target_hours"])
            values = {
                "schema": DATABASE_SCHEMA,
                "created_at": utc_now(),
                "experiment_started_at": datetime.fromtimestamp(
                    started, timezone.utc
                ).isoformat(timespec="seconds"),
                "target_end_at": datetime.fromtimestamp(
                    started + target_hours * 3600, timezone.utc
                ).isoformat(timespec="seconds"),
                "target_hours": target_hours,
                "prereg_sha256": prereg_hash,
                "strategy": prereg["strategy"],
                "orders_enabled": False,
                "paper_orders_enabled": False,
                "wallet_required": False,
                "real_money": "BLOQUEADO",
                "outcomes_read": 0,
            }
            for key, value in values.items():
                self.set_meta(key, value, commit=False)
            self.db.commit()
            return
        if meta.get("schema") != DATABASE_SCHEMA:
            raise V022Error("Base V0.22b incompatible")
        if meta.get("prereg_sha256") != prereg_hash:
            raise V022Error("Base V0.22b pertenece a otro prerregistro")
        if float(meta.get("target_hours", 0)) != TARGET_HOURS:
            raise V022Error("Duracion V0.22b incompatible")
        if (
            meta.get("orders_enabled") is not False
            or meta.get("paper_orders_enabled") is not False
            or meta.get("real_money") != "BLOQUEADO"
        ):
            raise V022Error("Base V0.22b insegura")


def _engine(market: Any, strategy: Mapping[str, Any]) -> SyncedPairOpportunityEngine:
    return SyncedPairOpportunityEngine(
        condition_id=market.condition_id,
        market_start_ms=market.start_ms,
        token_sides={market.up_token_id: "UP", market.down_token_id: "DOWN"},
        config=strategy,
    )


async def _run_v022b_locked(
    *, settings: Settings, prereg_path: str | Path, output_db: str | Path
) -> dict[str, Any]:
    prereg = load_and_verify_prereg(prereg_path)
    store = V022BStore(output_db)
    store.open(prereg, prereg_path)
    store.mark_interrupted_markets()
    meta = store.meta()
    target_end = _parse_utc(str(meta["target_end_at"])).timestamp()
    run_id = store.start_run()
    state = LiveShadowState()
    status = "RUNNING"
    error: str | None = None
    try:
        while time.time() < target_end:
            now = time.time()
            current = int(now) - int(now) % 300
            target_start = current if now <= current + 15 else current + 300
            if target_start + 300 > target_end:
                while time.time() < target_end:
                    await asyncio.sleep(min(0.5, max(0.01, target_end - time.time())))
                break
            await asyncio.sleep(max(0.0, target_start + 1 - time.time()))
            slug = f"btc-updown-5m-{target_start}"
            if store.market_seen(slug):
                while time.time() < min(target_end, target_start + 300):
                    await asyncio.sleep(
                        min(0.5, max(0.01, min(target_end, target_start + 300) - time.time()))
                    )
                continue
            try:
                market = await _discover(settings, slug, target_start + 30)
            except Exception:
                logging.getLogger("v022b").exception("Discovery V0.22b fallo")
                continue
            store.start_market(market)
            state.set_market(market)
            engine = _engine(market, prereg["strategy"])
            clob_stop = asyncio.Event()

            async def handler(raw: str) -> None:
                state.ingest(source="clob", default_stream="market", raw=raw)
                engine.ingest(raw, received_timestamp_ms=int(time.time() * 1000))
                store.save_samples(engine.drain_samples())
                store.save_signals(engine.drain_signals())

            clob_task = asyncio.create_task(
                _websocket_feed(
                    name=f"v022b-clob-{slug}", endpoint=settings.clob_ws_url,
                    subscription={"assets_ids": [market.up_token_id, market.down_token_id], "type": "market", "custom_feature_enabled": True},
                    heartbeat_text="PING", heartbeat_seconds=10.0, use_proxy=settings.ws_use_proxy,
                    reconnect_max_seconds=settings.reconnect_max_seconds, stop_event=clob_stop,
                    state=state, handler=handler,
                )
            )
            end = min(target_end, target_start + 300)
            market_completed = False
            next_health = time.time()
            try:
                while time.time() < end:
                    engine.tick(received_timestamp_ms=int(time.time() * 1000))
                    await asyncio.sleep(min(0.1, max(0.01, end - time.time())))
                    if time.time() >= next_health:
                        store.save_health(state)
                        next_health = time.time() + 10.0
                market_completed = target_start + 300 <= target_end and time.time() >= target_start + 300
            finally:
                clob_stop.set()
                clob_task.cancel()
                await asyncio.gather(clob_task, return_exceptions=True)
                summary = engine.finish(received_timestamp_ms=int(min(time.time(), end) * 1000))
                store.save_samples(engine.drain_samples())
                store.save_signals(engine.drain_signals())
                store.finalize_market(
                    summary, "COMPLETE" if market_completed else "INTERRUPTED_EXCLUDED"
                )
                state.connections.pop(f"v022b-clob-{slug}", None)
                state.clear_market()
        store.set_meta("experiment_completed_at", utc_now())
        status = "COMPLETED"
    except asyncio.CancelledError:
        status = "INTERRUPTED"
        raise
    except KeyboardInterrupt:
        status = "INTERRUPTED"
    except Exception as exc:
        status = "FAILED"
        error = f"{type(exc).__name__}: {exc}"
        logging.getLogger("v022b").exception("V0.22b fallo")
    finally:
        quick = store.quick_check()
        store.finish_run(run_id, status, error)
        final_meta = store.meta()
        store.close()
    return {
        "status": status,
        "error": error,
        "database": str(Path(output_db).resolve()),
        "quick_check": quick,
        "experiment_started_at": final_meta["experiment_started_at"],
        "target_end_at": final_meta["target_end_at"],
        "target_hours": final_meta["target_hours"],
        "orders_sent": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "outcomes_read": 0,
        "real_money": "BLOQUEADO",
    }


async def run_v022b(
    *, settings: Settings, prereg_path: str | Path, output_db: str | Path
) -> dict[str, Any]:
    lock = V022ProcessLock(f"{Path(output_db).resolve()}.lock")
    lock.acquire()
    try:
        return await _run_v022b_locked(
            settings=settings, prereg_path=prereg_path, output_db=output_db
        )
    finally:
        lock.release()


def v022b_status(path: str | Path) -> dict[str, Any]:
    payload = v022_status(path)
    payload["variant"] = "V0.22b_8h"
    return payload


__all__ = [
    "DATABASE_SCHEMA", "MAXIMUM_HOURS", "PREREG_SCHEMA", "TARGET_HOURS",
    "V022BStore", "load_and_verify_prereg", "run_smoke", "run_v022b", "v022b_status",
]
