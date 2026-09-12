from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.config import Settings
from polymarket_bot.phase41 import LiveShadowState, _websocket_feed
from polymarket_bot.v018_runner import ROOT, _discover, _parse_utc, sha256_file, utc_now
from polymarket_bot.v021_safe_pair import (
    SafePairOpportunityEngine,
    V021Error,
    default_strategy_config,
    validate_strategy_config,
)


PREREG_SCHEMA = "prereg_v021_safe_pair_observer_1"
DATABASE_SCHEMA = "paper_v021_safe_pair_observer_1"
MAXIMUM_HOURS = 24.0


DDL = """
CREATE TABLE IF NOT EXISTS v021_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v021_runs(
 run_id INTEGER PRIMARY KEY AUTOINCREMENT,started_at TEXT NOT NULL,finished_at TEXT,
 status TEXT NOT NULL,error TEXT
);
CREATE TABLE IF NOT EXISTS v021_markets(
 condition_id TEXT PRIMARY KEY,slug TEXT NOT NULL UNIQUE,market_start_ms INTEGER NOT NULL,
 market_end_ms INTEGER NOT NULL,discovered_at TEXT NOT NULL,completed_at TEXT,status TEXT NOT NULL,
 message_count INTEGER NOT NULL DEFAULT 0,valid_observations INTEGER NOT NULL DEFAULT 0,
 eligible_observations INTEGER NOT NULL DEFAULT 0,opportunity_episodes INTEGER NOT NULL DEFAULT 0,
 minimum_complete_set_cost REAL,maximum_observed_depth_shares REAL NOT NULL DEFAULT 0,
 maximum_opportunity_persistence_ms INTEGER NOT NULL DEFAULT 0,
 unilateral_positions INTEGER NOT NULL DEFAULT 0,paper_orders INTEGER NOT NULL DEFAULT 0,
 outcomes_read INTEGER NOT NULL DEFAULT 0,orders_sent INTEGER NOT NULL DEFAULT 0,
 real_money INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS v021_samples(
 condition_id TEXT NOT NULL,bucket_ms INTEGER NOT NULL,source_timestamp_ms INTEGER NOT NULL,
 received_timestamp_ms INTEGER NOT NULL,up_best_ask REAL NOT NULL,down_best_ask REAL NOT NULL,
 up_ask_depth REAL NOT NULL,down_ask_depth REAL NOT NULL,up_total_cost REAL NOT NULL,
 down_total_cost REAL NOT NULL,complete_set_cost REAL NOT NULL,eligible INTEGER NOT NULL,
 message_count INTEGER NOT NULL,PRIMARY KEY(condition_id,bucket_ms)
);
CREATE TABLE IF NOT EXISTS v021_health(
 recorded_at TEXT PRIMARY KEY,process_id INTEGER NOT NULL,connections_json TEXT NOT NULL,
 counters_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v021_markets_start ON v021_markets(market_start_ms);
CREATE INDEX IF NOT EXISTS idx_v021_samples_cost ON v021_samples(complete_set_cost);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_and_verify_prereg(path: str | Path) -> dict[str, Any]:
    prereg_path = Path(path).resolve()
    payload = json.loads(prereg_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != PREREG_SCHEMA:
        raise V021Error("Prerregistro V0.21 incompatible")
    if payload.get("status") != "FROZEN_OBSERVER_ONLY":
        raise V021Error("V0.21 no esta congelado como observador")
    hours = float(payload.get("target_hours", 0))
    if hours <= 0 or hours > MAXIMUM_HOURS:
        raise V021Error("V0.21 debe durar como maximo 24 horas")
    expected_safety = {
        "wallet_required": False,
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "real_money": "BLOQUEADO",
        "outcomes_before_completion": False,
        "outcomes_after_completion": False,
        "active_forward_read": False,
        "active_forward_modified": False,
        "maximum_hours": 24,
    }
    safety = payload.get("safety")
    if not isinstance(safety, dict):
        raise V021Error("Safety V0.21 ausente")
    for key, value in expected_safety.items():
        if safety.get(key) != value:
            raise V021Error(f"Safety V0.21 incumplido: {key}")
    validate_strategy_config(payload.get("strategy", {}))
    code = payload.get("code_hashes")
    if not isinstance(code, dict):
        raise V021Error("Hashes V0.21 ausentes")
    expected_hashes = {
        "engine": sha256_file(ROOT / "src" / "polymarket_bot" / "v021_safe_pair.py"),
        "runner": sha256_file(Path(__file__)),
        "entrypoint": sha256_file(ROOT / "v021_monitor.py"),
        "auditor": sha256_file(ROOT / "src" / "polymarket_bot" / "v021_audit.py"),
        "collector": sha256_file(ROOT / "src" / "polymarket_bot" / "phase41.py"),
    }
    for key, actual in expected_hashes.items():
        if code.get(key) != actual:
            raise V021Error(f"Hash V0.21 no coincide: {key}")
    design = payload.get("design_evidence")
    if not isinstance(design, dict):
        raise V021Error("Evidencia de diseno V0.21 ausente")
    evidence = (ROOT / str(design.get("relative_path"))).resolve()
    if not evidence.is_file() or sha256_file(evidence) != design.get("sha256"):
        raise V021Error("Evidencia de diseno V0.21 no coincide")
    return payload


class V021Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.connection: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("Base V0.21 cerrada")
        return self.connection

    def open(self, prereg: Mapping[str, Any], prereg_path: str | Path) -> None:
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
        else:
            if meta.get("schema") != DATABASE_SCHEMA:
                raise V021Error("Base V0.21 incompatible")
            if meta.get("prereg_sha256") != prereg_hash:
                raise V021Error("Base V0.21 pertenece a otro prerregistro")
            if float(meta.get("target_hours", 0)) != float(prereg["target_hours"]):
                raise V021Error("Duracion V0.21 incompatible")
            if (
                meta.get("orders_enabled") is not False
                or meta.get("paper_orders_enabled") is not False
                or meta.get("real_money") != "BLOQUEADO"
            ):
                raise V021Error("Base V0.21 insegura")

    def close(self) -> None:
        if self.connection is not None:
            self.db.commit()
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.connection.close()
            self.connection = None

    def set_meta(self, key: str, value: Any, *, commit: bool = True) -> None:
        self.db.execute("INSERT OR REPLACE INTO v021_meta VALUES(?,?)", (str(key), _json(value)))
        if commit:
            self.db.commit()

    def meta(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in self.db.execute("SELECT key,value FROM v021_meta"):
            result[str(key)] = json.loads(str(value))
        return result

    def start_run(self) -> int:
        cursor = self.db.execute(
            "INSERT INTO v021_runs(started_at,status) VALUES(?,?)", (utc_now(), "RUNNING")
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, error: str | None) -> None:
        self.db.execute(
            "UPDATE v021_runs SET finished_at=?,status=?,error=? WHERE run_id=?",
            (utc_now(), status, error, int(run_id)),
        )
        self.db.commit()

    def mark_interrupted_markets(self) -> None:
        self.db.execute(
            "UPDATE v021_markets SET status='INTERRUPTED_EXCLUDED' WHERE status='COLLECTING'"
        )
        self.db.commit()

    def market_seen(self, slug: str) -> bool:
        return self.db.execute("SELECT 1 FROM v021_markets WHERE slug=?", (slug,)).fetchone() is not None

    def start_market(self, market: Any) -> None:
        self.db.execute(
            "INSERT INTO v021_markets(condition_id,slug,market_start_ms,market_end_ms,discovered_at,status) VALUES(?,?,?,?,?,'COLLECTING')",
            (market.condition_id, market.slug, int(market.start_ms), int(market.end_ms), utc_now()),
        )
        self.db.commit()

    def save_samples(self, samples: list[Mapping[str, Any]]) -> None:
        if not samples:
            return
        self.db.executemany(
            "INSERT OR REPLACE INTO v021_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    item["condition_id"], item["bucket_ms"], item["source_timestamp_ms"],
                    item["received_timestamp_ms"], item["up_best_ask"], item["down_best_ask"],
                    item["up_ask_depth"], item["down_ask_depth"], item["up_total_cost"],
                    item["down_total_cost"], item["complete_set_cost"], item["eligible"],
                    item["message_count"],
                )
                for item in samples
            ],
        )
        self.db.commit()

    def finalize_market(self, summary: Mapping[str, Any], status: str) -> None:
        if status not in {"COMPLETE", "INTERRUPTED_EXCLUDED"}:
            raise ValueError("Estado V0.21 invalido")
        self.db.execute(
            """
            UPDATE v021_markets SET completed_at=?,status=?,message_count=?,valid_observations=?,
             eligible_observations=?,opportunity_episodes=?,minimum_complete_set_cost=?,
             maximum_observed_depth_shares=?,maximum_opportunity_persistence_ms=?,
             unilateral_positions=0,paper_orders=0,outcomes_read=0,orders_sent=0,real_money=0
            WHERE condition_id=?
            """,
            (
                utc_now(), status, summary["message_count"], summary["valid_observations"],
                summary["eligible_observations"], summary["opportunity_episodes"],
                summary["minimum_complete_set_cost"], summary["maximum_observed_depth_shares"],
                summary["maximum_opportunity_persistence_ms"], summary["condition_id"],
            ),
        )
        self.db.commit()

    def save_health(self, state: LiveShadowState) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO v021_health VALUES(?,?,?,?)",
            (utc_now(), os.getpid(), _json(state.connections), _json(dict(state.counters))),
        )
        self.db.commit()

    def quick_check(self) -> str:
        return str(self.db.execute("PRAGMA quick_check").fetchone()[0])


class V021ProcessLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.handle: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
            self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise V021Error("Ya existe otro monitor V0.21 activo") from exc
        self.handle.seek(0)
        self.handle.write(f"{os.getpid():032d}".encode("ascii"))
        self.handle.flush()

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _engine(market: Any, strategy: Mapping[str, Any]) -> SafePairOpportunityEngine:
    return SafePairOpportunityEngine(
        condition_id=market.condition_id,
        market_start_ms=market.start_ms,
        token_sides={market.up_token_id: "UP", market.down_token_id: "DOWN"},
        config=strategy,
    )


async def run_smoke(settings: Settings, seconds: float) -> dict[str, Any]:
    if seconds <= 0 or seconds > 180:
        raise ValueError("Smoke V0.21 debe durar entre 0 y 180 segundos")
    now = int(time.time())
    start = now - now % 300
    slug = f"btc-updown-5m-{start}"
    market = await _discover(settings, slug, time.time() + 30)
    state = LiveShadowState()
    state.set_market(market)
    engine = _engine(market, default_strategy_config())
    stop = asyncio.Event()

    async def handler(raw: str) -> None:
        state.ingest(source="clob", default_stream="market", raw=raw)
        engine.ingest(raw, received_timestamp_ms=int(time.time() * 1000))

    task = asyncio.create_task(
        _websocket_feed(
            name="v021-smoke-clob", endpoint=settings.clob_ws_url,
            subscription={"assets_ids": [market.up_token_id, market.down_token_id], "type": "market", "custom_feature_enabled": True},
            heartbeat_text="PING", heartbeat_seconds=10.0, use_proxy=settings.ws_use_proxy,
            reconnect_max_seconds=settings.reconnect_max_seconds, stop_event=stop, state=state, handler=handler,
        )
    )
    deadline = min(time.time() + seconds, market.end_ms / 1000 - 0.1)
    try:
        while time.time() < deadline:
            await asyncio.sleep(min(1.0, max(0.01, deadline - time.time())))
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    summary = engine.finish(timestamp_ms=int(time.time() * 1000))
    samples = engine.drain_samples()
    return {
        "status": "SMOKE_OK" if summary["valid_observations"] > 0 else "SMOKE_NO_BOOK",
        "slug": slug,
        "connections": state.connections,
        "counters": dict(state.counters),
        "samples": len(samples),
        **summary,
        "orders_sent": 0,
        "outcomes_read": 0,
        "real_money": "BLOQUEADO",
    }


async def _run_v021_locked(
    *, settings: Settings, prereg_path: str | Path, output_db: str | Path
) -> dict[str, Any]:
    prereg = load_and_verify_prereg(prereg_path)
    store = V021Store(output_db)
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
                    await asyncio.sleep(min(1.0, target_end - time.time()))
                break
            await asyncio.sleep(max(0.0, target_start + 1 - time.time()))
            slug = f"btc-updown-5m-{target_start}"
            if store.market_seen(slug):
                while time.time() < min(target_end, target_start + 300):
                    await asyncio.sleep(min(1.0, min(target_end, target_start + 300) - time.time()))
                continue
            try:
                market = await _discover(settings, slug, target_start + 30)
            except Exception:
                logging.getLogger("v021").exception("Discovery V0.21 fallo")
                continue
            store.start_market(market)
            state.set_market(market)
            engine = _engine(market, prereg["strategy"])
            clob_stop = asyncio.Event()

            async def handler(raw: str) -> None:
                state.ingest(source="clob", default_stream="market", raw=raw)
                engine.ingest(raw, received_timestamp_ms=int(time.time() * 1000))
                store.save_samples(engine.drain_samples())

            clob_task = asyncio.create_task(
                _websocket_feed(
                    name=f"v021-clob-{slug}", endpoint=settings.clob_ws_url,
                    subscription={"assets_ids": [market.up_token_id, market.down_token_id], "type": "market", "custom_feature_enabled": True},
                    heartbeat_text="PING", heartbeat_seconds=10.0, use_proxy=settings.ws_use_proxy,
                    reconnect_max_seconds=settings.reconnect_max_seconds, stop_event=clob_stop, state=state, handler=handler,
                )
            )
            end = min(target_end, target_start + 300)
            market_completed = False
            next_health = time.time()
            try:
                while time.time() < end:
                    await asyncio.sleep(min(1.0, end - time.time()))
                    if time.time() >= next_health:
                        store.save_health(state)
                        next_health = time.time() + 10.0
                market_completed = target_start + 300 <= target_end and time.time() >= target_start + 300
            finally:
                clob_stop.set()
                clob_task.cancel()
                await asyncio.gather(clob_task, return_exceptions=True)
                summary = engine.finish(timestamp_ms=int(min(time.time(), end) * 1000))
                store.save_samples(engine.drain_samples())
                store.finalize_market(
                    summary, "COMPLETE" if market_completed else "INTERRUPTED_EXCLUDED"
                )
                state.connections.pop(f"v021-clob-{slug}", None)
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
        logging.getLogger("v021").exception("V0.21 fallo")
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


async def run_v021(
    *, settings: Settings, prereg_path: str | Path, output_db: str | Path
) -> dict[str, Any]:
    lock = V021ProcessLock(f"{Path(output_db).resolve()}.lock")
    lock.acquire()
    try:
        return await _run_v021_locked(
            settings=settings, prereg_path=prereg_path, output_db=output_db
        )
    finally:
        lock.release()


def v021_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).resolve()
    if not database.is_file():
        return {
            "status": "NOT_STARTED", "database": str(database), "outcomes_read": 0,
            "orders_sent": 0, "paper_orders": 0, "real_money": "BLOQUEADO",
        }
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        meta = {str(k): json.loads(str(v)) for k, v in connection.execute("SELECT key,value FROM v021_meta")}
        market = connection.execute(
            """
            SELECT COUNT(*),SUM(status='COMPLETE'),SUM(status='INTERRUPTED_EXCLUDED'),
             COALESCE(SUM(message_count),0),COALESCE(SUM(valid_observations),0),
             COALESCE(SUM(eligible_observations),0),COALESCE(SUM(opportunity_episodes),0),
             MIN(minimum_complete_set_cost),MAX(maximum_observed_depth_shares),
             MAX(maximum_opportunity_persistence_ms),COALESCE(SUM(unilateral_positions),0),
             COALESCE(SUM(paper_orders),0),COALESCE(SUM(outcomes_read),0),
             COALESCE(SUM(orders_sent),0),COALESCE(SUM(real_money),0)
            FROM v021_markets
            """
        ).fetchone()
        samples = connection.execute("SELECT COUNT(*),COALESCE(SUM(eligible),0) FROM v021_samples").fetchone()
        run = connection.execute("SELECT run_id,started_at,finished_at,status,error FROM v021_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        health = connection.execute("SELECT recorded_at,process_id,connections_json FROM v021_health ORDER BY recorded_at DESC LIMIT 1").fetchone()
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        connection.close()
    target_end = _parse_utc(str(meta["target_end_at"])).timestamp()
    expected = int(round(float(meta["target_hours"]) * 12))
    complete = int(market[1] or 0)
    return {
        "status": "COMPLETED" if meta.get("experiment_completed_at") else "RUNNING_OR_RESUMABLE",
        "database": str(database),
        "experiment_started_at": meta.get("experiment_started_at"),
        "target_end_at": meta.get("target_end_at"),
        "remaining_hours": max(0.0, target_end - time.time()) / 3600.0,
        "expected_markets": expected,
        "markets_seen": int(market[0] or 0),
        "markets_complete": complete,
        "complete_market_fraction": complete / expected if expected else None,
        "markets_interrupted_excluded": int(market[2] or 0),
        "clob_messages": int(market[3] or 0),
        "valid_observations": int(market[4] or 0),
        "eligible_observations": int(market[5] or 0),
        "opportunity_episodes": int(market[6] or 0),
        "minimum_complete_set_cost": float(market[7]) if market[7] is not None else None,
        "maximum_observed_depth_shares": float(market[8] or 0),
        "maximum_opportunity_persistence_ms": int(market[9] or 0),
        "unilateral_positions": int(market[10] or 0),
        "paper_orders": int(market[11] or 0),
        "outcomes_read": int(meta.get("outcomes_read", 0)) + int(market[12] or 0),
        "orders_sent": int(market[13] or 0),
        "real_money_rows": int(market[14] or 0),
        "sample_buckets_250ms": int(samples[0] or 0),
        "eligible_sample_buckets": int(samples[1] or 0),
        "latest_run": {"run_id": run[0], "started_at": run[1], "finished_at": run[2], "status": run[3], "error": run[4]} if run else None,
        "latest_health": {"recorded_at": health[0], "process_id": health[1], "connections": json.loads(health[2])} if health else None,
        "sqlite_quick_check": quick,
        "real_money": "BLOQUEADO",
        "active_forward_read": False,
        "active_forward_modified": False,
    }


__all__ = [
    "DATABASE_SCHEMA", "MAXIMUM_HOURS", "PREREG_SCHEMA", "V021ProcessLock", "V021Store",
    "load_and_verify_prereg", "run_smoke", "run_v021", "v021_status",
]
