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
from polymarket_bot.v022_synced_pair import (
    SyncedPairOpportunityEngine,
    V022Error,
    default_strategy_config,
    validate_strategy_config,
)


PREREG_SCHEMA = "prereg_v022_synced_persistent_observer_1"
DATABASE_SCHEMA = "paper_v022_synced_persistent_observer_1"
TARGET_HOURS = 12.0
MAXIMUM_HOURS = 12.0


DDL = """
CREATE TABLE IF NOT EXISTS v022_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v022_runs(
 run_id INTEGER PRIMARY KEY AUTOINCREMENT,started_at TEXT NOT NULL,finished_at TEXT,
 status TEXT NOT NULL,error TEXT
);
CREATE TABLE IF NOT EXISTS v022_markets(
 condition_id TEXT PRIMARY KEY,slug TEXT NOT NULL UNIQUE,market_start_ms INTEGER NOT NULL,
 market_end_ms INTEGER NOT NULL,discovered_at TEXT NOT NULL,completed_at TEXT,status TEXT NOT NULL,
 message_count INTEGER NOT NULL DEFAULT 0,valid_observations INTEGER NOT NULL DEFAULT 0,
 synchronized_observations INTEGER NOT NULL DEFAULT 0,unsynchronized_observations INTEGER NOT NULL DEFAULT 0,
 raw_eligible_observations INTEGER NOT NULL DEFAULT 0,raw_opportunity_episodes INTEGER NOT NULL DEFAULT 0,
 confirmed_observations INTEGER NOT NULL DEFAULT 0,confirmed_opportunity_episodes INTEGER NOT NULL DEFAULT 0,
 minimum_complete_set_cost REAL,minimum_synchronized_complete_set_cost REAL,
 minimum_raw_eligible_cost REAL,minimum_confirmed_cost REAL,
 maximum_observed_depth_shares REAL NOT NULL DEFAULT 0,
 maximum_candidate_persistence_ms INTEGER NOT NULL DEFAULT 0,
 stale_ask_updates_rejected INTEGER NOT NULL DEFAULT 0,
 nonmonotonic_receive_timestamps INTEGER NOT NULL DEFAULT 0,
 unilateral_positions INTEGER NOT NULL DEFAULT 0,paper_orders INTEGER NOT NULL DEFAULT 0,
 outcomes_read INTEGER NOT NULL DEFAULT 0,orders_sent INTEGER NOT NULL DEFAULT 0,
 real_money INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS v022_samples(
 condition_id TEXT NOT NULL,bucket_ms INTEGER NOT NULL,
 valid_observations INTEGER NOT NULL,synchronized_observations INTEGER NOT NULL,
 raw_eligible_observations INTEGER NOT NULL,confirmed_observations INTEGER NOT NULL,
 minimum_complete_set_cost REAL NOT NULL,minimum_synchronized_cost REAL,
 minimum_raw_eligible_cost REAL,minimum_confirmed_cost REAL,
 minimum_source_skew_ms INTEGER,minimum_receive_skew_ms INTEGER,
 maximum_candidate_age_ms INTEGER NOT NULL,PRIMARY KEY(condition_id,bucket_ms)
);
CREATE TABLE IF NOT EXISTS v022_signals(
 signal_id INTEGER PRIMARY KEY AUTOINCREMENT,condition_id TEXT NOT NULL,event_type TEXT NOT NULL,
 received_timestamp_ms INTEGER NOT NULL,complete_set_cost REAL NOT NULL,
 candidate_age_ms INTEGER NOT NULL,up_source_timestamp_ms INTEGER NOT NULL,
 down_source_timestamp_ms INTEGER NOT NULL,up_received_timestamp_ms INTEGER NOT NULL,
 down_received_timestamp_ms INTEGER NOT NULL,source_skew_ms INTEGER NOT NULL,
 receive_skew_ms INTEGER NOT NULL,maximum_book_age_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS v022_health(
 recorded_at TEXT PRIMARY KEY,process_id INTEGER NOT NULL,connections_json TEXT NOT NULL,
 counters_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v022_markets_start ON v022_markets(market_start_ms);
CREATE INDEX IF NOT EXISTS idx_v022_samples_raw ON v022_samples(raw_eligible_observations);
CREATE INDEX IF NOT EXISTS idx_v022_samples_confirmed ON v022_samples(confirmed_observations);
CREATE INDEX IF NOT EXISTS idx_v022_signals_type ON v022_signals(event_type);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_and_verify_prereg(path: str | Path) -> dict[str, Any]:
    prereg_path = Path(path).resolve()
    payload = json.loads(prereg_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != PREREG_SCHEMA:
        raise V022Error("Prerregistro V0.22 incompatible")
    if payload.get("status") != "FROZEN_SYNCHRONIZED_OBSERVER_ONLY":
        raise V022Error("V0.22 no esta congelado como observador sincronizado")
    hours = float(payload.get("target_hours", 0))
    if hours != TARGET_HOURS or hours > MAXIMUM_HOURS:
        raise V022Error("V0.22 debe durar exactamente 12 horas")
    expected_safety = {
        "wallet_required": False,
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "real_money": "BLOQUEADO",
        "outcomes_before_completion": False,
        "outcomes_after_completion": False,
        "active_forward_read": False,
        "active_forward_modified": False,
        "maximum_hours": 12,
    }
    safety = payload.get("safety")
    if not isinstance(safety, dict):
        raise V022Error("Safety V0.22 ausente")
    for key, value in expected_safety.items():
        if safety.get(key) != value:
            raise V022Error(f"Safety V0.22 incumplido: {key}")
    validate_strategy_config(payload.get("strategy", {}))
    code = payload.get("code_hashes")
    if not isinstance(code, dict):
        raise V022Error("Hashes V0.22 ausentes")
    expected_hashes = {
        "engine": sha256_file(ROOT / "src/polymarket_bot/v022_synced_pair.py"),
        "runner": sha256_file(Path(__file__)),
        "entrypoint": sha256_file(ROOT / "v022_monitor.py"),
        "auditor": sha256_file(ROOT / "src/polymarket_bot/v022_audit.py"),
        "collector": sha256_file(ROOT / "src/polymarket_bot/phase41.py"),
        "cost_model": sha256_file(ROOT / "src/polymarket_bot/v021_safe_pair.py"),
    }
    for key, actual in expected_hashes.items():
        if code.get(key) != actual:
            raise V022Error(f"Hash V0.22 no coincide: {key}")
    design = payload.get("design_evidence")
    if not isinstance(design, dict):
        raise V022Error("Evidencia de diseno V0.22 ausente")
    evidence = (ROOT / str(design.get("relative_path"))).resolve()
    if not evidence.is_file() or sha256_file(evidence) != design.get("sha256"):
        raise V022Error("Evidencia de diseno V0.22 no coincide")
    return payload


class V022Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.connection: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("Base V0.22 cerrada")
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
                raise V022Error("Base V0.22 incompatible")
            if meta.get("prereg_sha256") != prereg_hash:
                raise V022Error("Base V0.22 pertenece a otro prerregistro")
            if float(meta.get("target_hours", 0)) != TARGET_HOURS:
                raise V022Error("Duracion V0.22 incompatible")
            if (
                meta.get("orders_enabled") is not False
                or meta.get("paper_orders_enabled") is not False
                or meta.get("real_money") != "BLOQUEADO"
            ):
                raise V022Error("Base V0.22 insegura")

    def close(self) -> None:
        if self.connection is not None:
            self.db.commit()
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.connection.close()
            self.connection = None

    def set_meta(self, key: str, value: Any, *, commit: bool = True) -> None:
        self.db.execute("INSERT OR REPLACE INTO v022_meta VALUES(?,?)", (str(key), _json(value)))
        if commit:
            self.db.commit()

    def meta(self) -> dict[str, Any]:
        return {
            str(key): json.loads(str(value))
            for key, value in self.db.execute("SELECT key,value FROM v022_meta")
        }

    def start_run(self) -> int:
        cursor = self.db.execute(
            "INSERT INTO v022_runs(started_at,status) VALUES(?,?)", (utc_now(), "RUNNING")
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, error: str | None) -> None:
        self.db.execute(
            "UPDATE v022_runs SET finished_at=?,status=?,error=? WHERE run_id=?",
            (utc_now(), status, error, int(run_id)),
        )
        self.db.commit()

    def mark_interrupted_markets(self) -> None:
        self.db.execute(
            "UPDATE v022_markets SET status='INTERRUPTED_EXCLUDED' WHERE status='COLLECTING'"
        )
        self.db.commit()

    def market_seen(self, slug: str) -> bool:
        return self.db.execute("SELECT 1 FROM v022_markets WHERE slug=?", (slug,)).fetchone() is not None

    def start_market(self, market: Any) -> None:
        self.db.execute(
            "INSERT INTO v022_markets(condition_id,slug,market_start_ms,market_end_ms,discovered_at,status) VALUES(?,?,?,?,?,'COLLECTING')",
            (market.condition_id, market.slug, int(market.start_ms), int(market.end_ms), utc_now()),
        )
        self.db.commit()

    def save_samples(self, samples: list[Mapping[str, Any]]) -> None:
        if not samples:
            return
        self.db.executemany(
            "INSERT OR REPLACE INTO v022_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    item["condition_id"], item["bucket_ms"], item["valid_observations"],
                    item["synchronized_observations"], item["raw_eligible_observations"],
                    item["confirmed_observations"], item["minimum_complete_set_cost"],
                    item["minimum_synchronized_cost"], item["minimum_raw_eligible_cost"],
                    item["minimum_confirmed_cost"], item["minimum_source_skew_ms"],
                    item["minimum_receive_skew_ms"], item["maximum_candidate_age_ms"],
                )
                for item in samples
            ],
        )
        self.db.commit()

    def save_signals(self, signals: list[Mapping[str, Any]]) -> None:
        if not signals:
            return
        self.db.executemany(
            """
            INSERT INTO v022_signals(
             condition_id,event_type,received_timestamp_ms,complete_set_cost,candidate_age_ms,
             up_source_timestamp_ms,down_source_timestamp_ms,up_received_timestamp_ms,
             down_received_timestamp_ms,source_skew_ms,receive_skew_ms,maximum_book_age_ms
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    item["condition_id"], item["event_type"], item["received_timestamp_ms"],
                    item["complete_set_cost"], item["candidate_age_ms"],
                    item["up_source_timestamp_ms"], item["down_source_timestamp_ms"],
                    item["up_received_timestamp_ms"], item["down_received_timestamp_ms"],
                    item["source_skew_ms"], item["receive_skew_ms"],
                    item["maximum_book_age_ms"],
                )
                for item in signals
            ],
        )
        self.db.commit()

    def finalize_market(self, summary: Mapping[str, Any], status: str) -> None:
        if status not in {"COMPLETE", "INTERRUPTED_EXCLUDED"}:
            raise ValueError("Estado V0.22 invalido")
        self.db.execute(
            """
            UPDATE v022_markets SET completed_at=?,status=?,message_count=?,valid_observations=?,
             synchronized_observations=?,unsynchronized_observations=?,raw_eligible_observations=?,
             raw_opportunity_episodes=?,confirmed_observations=?,confirmed_opportunity_episodes=?,
             minimum_complete_set_cost=?,minimum_synchronized_complete_set_cost=?,
             minimum_raw_eligible_cost=?,minimum_confirmed_cost=?,maximum_observed_depth_shares=?,
             maximum_candidate_persistence_ms=?,stale_ask_updates_rejected=?,
             nonmonotonic_receive_timestamps=?,unilateral_positions=0,paper_orders=0,
             outcomes_read=0,orders_sent=0,real_money=0 WHERE condition_id=?
            """,
            (
                utc_now(), status, summary["message_count"], summary["valid_observations"],
                summary["synchronized_observations"], summary["unsynchronized_observations"],
                summary["raw_eligible_observations"], summary["raw_opportunity_episodes"],
                summary["confirmed_observations"], summary["confirmed_opportunity_episodes"],
                summary["minimum_complete_set_cost"],
                summary["minimum_synchronized_complete_set_cost"],
                summary["minimum_raw_eligible_cost"], summary["minimum_confirmed_cost"],
                summary["maximum_observed_depth_shares"],
                summary["maximum_candidate_persistence_ms"],
                summary["stale_ask_updates_rejected"],
                summary["nonmonotonic_receive_timestamps"], summary["condition_id"],
            ),
        )
        self.db.commit()

    def save_health(self, state: LiveShadowState) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO v022_health VALUES(?,?,?,?)",
            (utc_now(), os.getpid(), _json(state.connections), _json(dict(state.counters))),
        )
        self.db.commit()

    def quick_check(self) -> str:
        return str(self.db.execute("PRAGMA quick_check").fetchone()[0])


class V022ProcessLock:
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
            raise V022Error("Ya existe otro monitor V0.22 activo") from exc
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


def _engine(market: Any, strategy: Mapping[str, Any]) -> SyncedPairOpportunityEngine:
    return SyncedPairOpportunityEngine(
        condition_id=market.condition_id,
        market_start_ms=market.start_ms,
        token_sides={market.up_token_id: "UP", market.down_token_id: "DOWN"},
        config=strategy,
    )


async def run_smoke(settings: Settings, seconds: float) -> dict[str, Any]:
    if seconds <= 0 or seconds > 180:
        raise ValueError("Smoke V0.22 debe durar entre 0 y 180 segundos")
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
            name="v022-smoke-clob", endpoint=settings.clob_ws_url,
            subscription={"assets_ids": [market.up_token_id, market.down_token_id], "type": "market", "custom_feature_enabled": True},
            heartbeat_text="PING", heartbeat_seconds=10.0, use_proxy=settings.ws_use_proxy,
            reconnect_max_seconds=settings.reconnect_max_seconds, stop_event=stop, state=state,
            handler=handler,
        )
    )
    deadline = min(time.time() + seconds, market.end_ms / 1000 - 0.1)
    try:
        while time.time() < deadline:
            engine.tick(received_timestamp_ms=int(time.time() * 1000))
            await asyncio.sleep(min(0.1, max(0.01, deadline - time.time())))
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    summary = engine.finish(received_timestamp_ms=int(time.time() * 1000))
    samples = engine.drain_samples()
    signals = engine.drain_signals()
    return {
        "status": "SMOKE_OK" if summary["valid_observations"] > 0 else "SMOKE_NO_ASK_BOOK",
        "slug": slug,
        "connections": state.connections,
        "counters": dict(state.counters),
        "sample_buckets": len(samples),
        "signals": len(signals),
        **summary,
        "orders_sent": 0,
        "outcomes_read": 0,
        "real_money": "BLOQUEADO",
    }


async def _run_v022_locked(
    *, settings: Settings, prereg_path: str | Path, output_db: str | Path
) -> dict[str, Any]:
    prereg = load_and_verify_prereg(prereg_path)
    store = V022Store(output_db)
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
                logging.getLogger("v022").exception("Discovery V0.22 fallo")
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
                    name=f"v022-clob-{slug}", endpoint=settings.clob_ws_url,
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
                state.connections.pop(f"v022-clob-{slug}", None)
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
        logging.getLogger("v022").exception("V0.22 fallo")
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


async def run_v022(
    *, settings: Settings, prereg_path: str | Path, output_db: str | Path
) -> dict[str, Any]:
    lock = V022ProcessLock(f"{Path(output_db).resolve()}.lock")
    lock.acquire()
    try:
        return await _run_v022_locked(
            settings=settings, prereg_path=prereg_path, output_db=output_db
        )
    finally:
        lock.release()


def v022_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).resolve()
    if not database.is_file():
        return {
            "status": "NOT_STARTED", "database": str(database), "outcomes_read": 0,
            "orders_sent": 0, "paper_orders": 0, "real_money": "BLOQUEADO",
        }
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        meta = {
            str(key): json.loads(str(value))
            for key, value in connection.execute("SELECT key,value FROM v022_meta")
        }
        market = connection.execute(
            """
            SELECT COUNT(*) AS markets_seen,
             COALESCE(SUM(status='COMPLETE'),0) AS markets_complete,
             COALESCE(SUM(status='INTERRUPTED_EXCLUDED'),0) AS markets_interrupted,
             COALESCE(SUM(message_count),0) AS clob_messages,
             COALESCE(SUM(valid_observations),0) AS valid_observations,
             COALESCE(SUM(synchronized_observations),0) AS synchronized_observations,
             COALESCE(SUM(unsynchronized_observations),0) AS unsynchronized_observations,
             COALESCE(SUM(raw_eligible_observations),0) AS raw_eligible_observations,
             COALESCE(SUM(raw_opportunity_episodes),0) AS raw_opportunity_episodes,
             COALESCE(SUM(confirmed_observations),0) AS confirmed_observations,
             COALESCE(SUM(confirmed_opportunity_episodes),0) AS confirmed_opportunity_episodes,
             MIN(minimum_complete_set_cost) AS minimum_complete_set_cost,
             MIN(minimum_synchronized_complete_set_cost) AS minimum_synchronized_complete_set_cost,
             MIN(minimum_raw_eligible_cost) AS minimum_raw_eligible_cost,
             MIN(minimum_confirmed_cost) AS minimum_confirmed_cost,
             MAX(maximum_observed_depth_shares) AS maximum_observed_depth_shares,
             MAX(maximum_candidate_persistence_ms) AS maximum_candidate_persistence_ms,
             COALESCE(SUM(stale_ask_updates_rejected),0) AS stale_ask_updates_rejected,
             COALESCE(SUM(nonmonotonic_receive_timestamps),0) AS nonmonotonic_receive_timestamps,
             COALESCE(SUM(unilateral_positions),0) AS unilateral_positions,
             COALESCE(SUM(paper_orders),0) AS paper_orders,
             COALESCE(SUM(outcomes_read),0) AS outcomes_read,
             COALESCE(SUM(orders_sent),0) AS orders_sent,
             COALESCE(SUM(real_money),0) AS real_money_rows
            FROM v022_markets
            """
        ).fetchone()
        samples = connection.execute(
            """
            SELECT COUNT(*) AS buckets,
             COALESCE(SUM(raw_eligible_observations>0),0) AS raw_buckets,
             COALESCE(SUM(confirmed_observations>0),0) AS confirmed_buckets
            FROM v022_samples
            """
        ).fetchone()
        run = connection.execute(
            "SELECT run_id,started_at,finished_at,status,error FROM v022_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        health = connection.execute(
            "SELECT recorded_at,process_id,connections_json FROM v022_health ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        connection.close()
    assert market is not None and samples is not None
    target_end = _parse_utc(str(meta["target_end_at"])).timestamp()
    expected = int(round(float(meta["target_hours"]) * 12))
    complete = int(market["markets_complete"] or 0)
    return {
        "status": "COMPLETED" if meta.get("experiment_completed_at") else "RUNNING_OR_RESUMABLE",
        "database": str(database),
        "experiment_started_at": meta.get("experiment_started_at"),
        "target_end_at": meta.get("target_end_at"),
        "remaining_hours": max(0.0, target_end - time.time()) / 3600.0,
        "expected_markets": expected,
        "markets_seen": int(market["markets_seen"] or 0),
        "markets_complete": complete,
        "complete_market_fraction": complete / expected if expected else None,
        "markets_interrupted_excluded": int(market["markets_interrupted"] or 0),
        "clob_messages": int(market["clob_messages"] or 0),
        "valid_observations": int(market["valid_observations"] or 0),
        "synchronized_observations": int(market["synchronized_observations"] or 0),
        "unsynchronized_observations": int(market["unsynchronized_observations"] or 0),
        "raw_eligible_observations": int(market["raw_eligible_observations"] or 0),
        "raw_opportunity_episodes": int(market["raw_opportunity_episodes"] or 0),
        "confirmed_observations": int(market["confirmed_observations"] or 0),
        "confirmed_opportunity_episodes": int(market["confirmed_opportunity_episodes"] or 0),
        "minimum_complete_set_cost": market["minimum_complete_set_cost"],
        "minimum_synchronized_complete_set_cost": market["minimum_synchronized_complete_set_cost"],
        "minimum_raw_eligible_cost": market["minimum_raw_eligible_cost"],
        "minimum_confirmed_cost": market["minimum_confirmed_cost"],
        "maximum_observed_depth_shares": float(market["maximum_observed_depth_shares"] or 0),
        "maximum_candidate_persistence_ms": int(market["maximum_candidate_persistence_ms"] or 0),
        "stale_ask_updates_rejected": int(market["stale_ask_updates_rejected"] or 0),
        "nonmonotonic_receive_timestamps": int(market["nonmonotonic_receive_timestamps"] or 0),
        "unilateral_positions": int(market["unilateral_positions"] or 0),
        "paper_orders": int(market["paper_orders"] or 0),
        "outcomes_read": int(meta.get("outcomes_read", 0)) + int(market["outcomes_read"] or 0),
        "orders_sent": int(market["orders_sent"] or 0),
        "real_money_rows": int(market["real_money_rows"] or 0),
        "sample_buckets_250ms": int(samples["buckets"] or 0),
        "raw_eligible_sample_buckets": int(samples["raw_buckets"] or 0),
        "confirmed_sample_buckets": int(samples["confirmed_buckets"] or 0),
        "latest_run": dict(run) if run else None,
        "latest_health": {
            "recorded_at": health["recorded_at"],
            "process_id": health["process_id"],
            "connections": json.loads(health["connections_json"]),
        } if health else None,
        "sqlite_quick_check": quick,
        "real_money": "BLOQUEADO",
        "active_forward_read": False,
        "active_forward_modified": False,
    }


__all__ = [
    "DATABASE_SCHEMA", "MAXIMUM_HOURS", "PREREG_SCHEMA", "TARGET_HOURS",
    "V022ProcessLock", "V022Store", "load_and_verify_prereg", "run_smoke",
    "run_v022", "v022_status",
]
