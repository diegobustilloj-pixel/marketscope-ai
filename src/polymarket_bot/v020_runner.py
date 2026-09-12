from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.config import Settings
from polymarket_bot.phase41 import LiveShadowState, _materialize_rows, _wait_or_stop, _websocket_feed
from polymarket_bot.v018_runner import (
    ROOT,
    _discover,
    _parse_utc,
    extract_trade_event,
    first_live_second,
    sha256_file,
    utc_now,
)
from polymarket_bot.v020_mandatory_hedge import (
    MandatoryHedgePaperEngine,
    V020Error,
    default_strategy_config,
    validate_strategy_config,
)


PREREG_SCHEMA = "prereg_v020_mandatory_hedge_paper_1"
DATABASE_SCHEMA = "paper_v020_mandatory_hedge_1"
MAXIMUM_HOURS = 24.0


DDL = """
CREATE TABLE IF NOT EXISTS v020_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v020_runs(
 run_id INTEGER PRIMARY KEY AUTOINCREMENT,started_at TEXT NOT NULL,finished_at TEXT,
 status TEXT NOT NULL,error TEXT
);
CREATE TABLE IF NOT EXISTS v020_markets(
 condition_id TEXT PRIMARY KEY,slug TEXT NOT NULL UNIQUE,market_start_ms INTEGER NOT NULL,
 market_end_ms INTEGER NOT NULL,discovered_at TEXT NOT NULL,completed_at TEXT,status TEXT NOT NULL,
 quote_count INTEGER NOT NULL DEFAULT 0,fill_count INTEGER NOT NULL DEFAULT 0,
 pair_match_count INTEGER NOT NULL DEFAULT 0,up_shares REAL NOT NULL DEFAULT 0,
 down_shares REAL NOT NULL DEFAULT 0,up_cost REAL NOT NULL DEFAULT 0,down_cost REAL NOT NULL DEFAULT 0,
 paired_shares REAL NOT NULL DEFAULT 0,paired_cost REAL NOT NULL DEFAULT 0,
 weighted_complete_set_cost REAL,paired_capital_fraction REAL,unmatched_side TEXT,
 unmatched_shares REAL NOT NULL DEFAULT 0,unmatched_cost REAL NOT NULL DEFAULT 0,
 cash_deployed REAL NOT NULL DEFAULT 0,maximum_pair_set_cost REAL,
 emergency_attempts INTEGER NOT NULL DEFAULT 0,emergency_fills INTEGER NOT NULL DEFAULT 0,
 emergency_shares REAL NOT NULL DEFAULT 0,emergency_cash_cost REAL NOT NULL DEFAULT 0,
 emergency_pair_matches INTEGER NOT NULL DEFAULT 0,passive_pair_matches INTEGER NOT NULL DEFAULT 0,
 emergency_weighted_set_cost REAL,passive_weighted_set_cost REAL,
 unmatched_episodes INTEGER NOT NULL DEFAULT 0,maximum_unmatched_age_ms INTEGER NOT NULL DEFAULT 0,
 mandatory_hedge_completed INTEGER NOT NULL DEFAULT 0,
 real_money INTEGER NOT NULL DEFAULT 0,orders_sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS v020_seconds(
 condition_id TEXT NOT NULL,second_offset INTEGER NOT NULL,captured_at TEXT NOT NULL,
 up_best_bid REAL,up_best_ask REAL,up_bid_depth_1c REAL,up_ask_depth_1c REAL,
 down_best_bid REAL,down_best_ask REAL,down_bid_depth_1c REAL,down_ask_depth_1c REAL,
 polymarket_trade_count INTEGER NOT NULL,book_messages INTEGER NOT NULL,
 price_change_messages INTEGER NOT NULL,PRIMARY KEY(condition_id,second_offset)
);
CREATE TABLE IF NOT EXISTS v020_quotes(
 order_id TEXT PRIMARY KEY,condition_id TEXT NOT NULL,side TEXT NOT NULL,purpose TEXT NOT NULL,
 placed_second INTEGER NOT NULL,active_at_ms INTEGER NOT NULL,expires_at_ms INTEGER NOT NULL,
 price REAL NOT NULL,price_ceiling REAL NOT NULL,original_size REAL NOT NULL,
 remaining_size REAL NOT NULL,filled_size REAL NOT NULL,queue_ahead_initial REAL NOT NULL,
 queue_ahead_remaining REAL NOT NULL,status TEXT NOT NULL,cancelled_second INTEGER,
 cancel_reason TEXT,real_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS v020_fills(
 fill_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,condition_id TEXT NOT NULL,side TEXT NOT NULL,
 purpose TEXT NOT NULL,timestamp_ms INTEGER NOT NULL,second_offset REAL NOT NULL,
 fill_price REAL NOT NULL,fill_size REAL NOT NULL,fee REAL NOT NULL,rebate REAL NOT NULL,
 cash_cost REAL NOT NULL,trigger TEXT NOT NULL,trigger_price REAL NOT NULL,
 real_money INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS v020_pairs(
 pair_id TEXT PRIMARY KEY,condition_id TEXT NOT NULL,timestamp_ms INTEGER NOT NULL,size REAL NOT NULL,
 up_fill_id TEXT NOT NULL,down_fill_id TEXT NOT NULL,up_price REAL NOT NULL,down_price REAL NOT NULL,
 set_cost REAL NOT NULL,pairing_mode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v020_health(
 recorded_at TEXT PRIMARY KEY,process_id INTEGER NOT NULL,connections_json TEXT NOT NULL,
 counters_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v020_markets_start ON v020_markets(market_start_ms);
CREATE INDEX IF NOT EXISTS idx_v020_fills_market ON v020_fills(condition_id,timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_v020_pairs_market ON v020_pairs(condition_id,timestamp_ms);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_and_verify_prereg(path: str | Path) -> dict[str, Any]:
    prereg_path = Path(path).resolve()
    payload = json.loads(prereg_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != PREREG_SCHEMA:
        raise V020Error("Prerregistro V0.20 incompatible")
    if payload.get("status") != "FROZEN_PAPER_ONLY":
        raise V020Error("V0.20 no esta congelado para paper")
    hours = float(payload.get("target_hours", 0))
    if hours <= 0 or hours > MAXIMUM_HOURS:
        raise V020Error("V0.20 debe durar como maximo 24 horas")
    expected_safety = {
        "wallet_required": False,
        "orders_enabled": False,
        "real_money": "BLOQUEADO",
        "active_forward_read": False,
        "active_forward_modified": False,
        "outcomes_before_completion": False,
        "maximum_hours": 24,
    }
    safety = payload.get("safety")
    if not isinstance(safety, dict):
        raise V020Error("Safety V0.20 ausente")
    for key, value in expected_safety.items():
        if safety.get(key) != value:
            raise V020Error(f"Safety V0.20 incumplido: {key}")
    validate_strategy_config(payload.get("strategy", {}))
    code = payload.get("code_hashes")
    if not isinstance(code, dict):
        raise V020Error("Hashes V0.20 ausentes")
    expected_hashes = {
        "engine": sha256_file(ROOT / "src" / "polymarket_bot" / "v020_mandatory_hedge.py"),
        "runner": sha256_file(Path(__file__)),
        "entrypoint": sha256_file(ROOT / "v020_paper.py"),
        "collector_runner": sha256_file(ROOT / "src" / "polymarket_bot" / "v018_runner.py"),
        "collector": sha256_file(ROOT / "src" / "polymarket_bot" / "phase41.py"),
    }
    for key, actual in expected_hashes.items():
        if code.get(key) != actual:
            raise V020Error(f"Hash V0.20 no coincide: {key}")
    source = payload.get("design_evidence")
    if not isinstance(source, dict):
        raise V020Error("Evidencia de diseno V0.20 ausente")
    evidence = (ROOT / str(source.get("relative_path"))).resolve()
    if not evidence.is_file() or sha256_file(evidence) != source.get("sha256"):
        raise V020Error("Evidencia de diseno V0.20 no coincide")
    return payload


class V020Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.connection: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("Base V0.20 cerrada")
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
                "experiment_started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds"),
                "target_end_at": datetime.fromtimestamp(started + target_hours * 3600, timezone.utc).isoformat(timespec="seconds"),
                "target_hours": target_hours,
                "prereg_sha256": prereg_hash,
                "strategy": prereg["strategy"],
                "orders_enabled": False,
                "wallet_required": False,
                "real_money": "BLOQUEADO",
                "outcomes_read": 0,
            }
            for key, value in values.items():
                self.set_meta(key, value, commit=False)
            self.db.commit()
        else:
            if meta.get("schema") != DATABASE_SCHEMA:
                raise V020Error("Base V0.20 incompatible")
            if meta.get("prereg_sha256") != prereg_hash:
                raise V020Error("Base V0.20 pertenece a otro prerregistro")
            if float(meta.get("target_hours", 0)) != float(prereg["target_hours"]):
                raise V020Error("Duracion V0.20 incompatible")
            if meta.get("orders_enabled") is not False or meta.get("real_money") != "BLOQUEADO":
                raise V020Error("Base V0.20 insegura")

    def close(self) -> None:
        if self.connection is not None:
            self.db.commit()
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.connection.close()
            self.connection = None

    def set_meta(self, key: str, value: Any, *, commit: bool = True) -> None:
        self.db.execute("INSERT OR REPLACE INTO v020_meta VALUES(?,?)", (str(key), _json(value)))
        if commit:
            self.db.commit()

    def meta(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in self.db.execute("SELECT key,value FROM v020_meta"):
            try:
                result[str(key)] = json.loads(str(value))
            except json.JSONDecodeError:
                result[str(key)] = value
        return result

    def start_run(self) -> int:
        cursor = self.db.execute(
            "INSERT INTO v020_runs(started_at,status) VALUES(?,?)", (utc_now(), "RUNNING")
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, error: str | None) -> None:
        self.db.execute(
            "UPDATE v020_runs SET finished_at=?,status=?,error=? WHERE run_id=?",
            (utc_now(), status, error, int(run_id)),
        )
        self.db.commit()

    def mark_interrupted_markets(self) -> None:
        self.db.execute("UPDATE v020_markets SET status='INTERRUPTED_EXCLUDED' WHERE status='COLLECTING'")
        self.db.commit()

    def market_seen(self, slug: str) -> bool:
        return self.db.execute("SELECT 1 FROM v020_markets WHERE slug=?", (slug,)).fetchone() is not None

    def start_market(self, market: Any) -> None:
        self.db.execute(
            "INSERT INTO v020_markets(condition_id,slug,market_start_ms,market_end_ms,discovered_at,status) VALUES(?,?,?,?,?,'COLLECTING')",
            (market.condition_id, market.slug, int(market.start_ms), int(market.end_ms), utc_now()),
        )
        self.db.commit()

    def save_second(self, condition_id: str, row: Mapping[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO v020_seconds VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                condition_id, int(row["second_offset"]), utc_now(),
                row.get("up_best_bid"), row.get("up_best_ask"), row.get("up_bid_depth_1c"), row.get("up_ask_depth_1c"),
                row.get("down_best_bid"), row.get("down_best_ask"), row.get("down_bid_depth_1c"), row.get("down_ask_depth_1c"),
                int(row.get("polymarket_trade_count") or 0), int(row.get("book_messages") or 0),
                int(row.get("price_change_messages") or 0),
            ),
        )
        self.db.commit()

    def apply_events(self, condition_id: str, events: list[dict[str, Any]]) -> None:
        for event in events:
            kind = event["kind"]
            if kind == "QUOTE_PLACED":
                order = event["order"]
                self.db.execute(
                    "INSERT INTO v020_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        order["order_id"], condition_id, order["side"], order["purpose"], order["placed_second"],
                        order["active_at_ms"], order["expires_at_ms"], order["price"], order["price_ceiling"],
                        order["original_size"], order["remaining_size"], order["filled_size"],
                        order["queue_ahead_initial"], order["queue_ahead_remaining"], order["status"], None, None,
                    ),
                )
            elif kind == "QUOTE_CANCELLED":
                order = event["order"]
                self.db.execute(
                    "UPDATE v020_quotes SET remaining_size=?,filled_size=?,queue_ahead_remaining=?,status=?,cancelled_second=?,cancel_reason=? WHERE order_id=?",
                    (order["remaining_size"], order["filled_size"], order["queue_ahead_remaining"], order["status"], event["cancelled_second"], event["reason"], order["order_id"]),
                )
            elif kind == "FILL":
                fill = event["fill"]
                self.db.execute(
                    "INSERT INTO v020_fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        fill["fill_id"], fill["order_id"], condition_id, fill["side"], fill["purpose"],
                        fill["timestamp_ms"], fill["second_offset"], fill["fill_price"], fill["fill_size"],
                        fill["fee"], fill["rebate"], fill["cash_cost"], fill["trigger"], fill["trigger_price"],
                    ),
                )
                order = event.get("order")
                if isinstance(order, dict):
                    self.db.execute(
                        "UPDATE v020_quotes SET remaining_size=?,filled_size=?,queue_ahead_remaining=?,status=? WHERE order_id=?",
                        (order["remaining_size"], order["filled_size"], order["queue_ahead_remaining"], order["status"], order["order_id"]),
                    )
            elif kind == "PAIR_MATCHED":
                pair = event["pair"]
                self.db.execute(
                    "INSERT INTO v020_pairs VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        pair["pair_id"], condition_id, pair["timestamp_ms"], pair["size"],
                        pair["up_fill_id"], pair["down_fill_id"], pair["up_price"], pair["down_price"],
                        pair["set_cost"], pair["pairing_mode"],
                    ),
                )
        if events:
            self.db.commit()

    def complete_market(self, summary: Mapping[str, Any]) -> None:
        self._finalize_market(summary, "COMPLETE")

    def exclude_interrupted_market(self, summary: Mapping[str, Any]) -> None:
        self._finalize_market(summary, "INTERRUPTED_EXCLUDED")

    def _finalize_market(self, summary: Mapping[str, Any], status: str) -> None:
        if status not in {"COMPLETE", "INTERRUPTED_EXCLUDED"}:
            raise ValueError(f"Estado V0.20 invalido: {status}")
        self.db.execute(
            """
            UPDATE v020_markets SET completed_at=?,status=?,quote_count=?,fill_count=?,pair_match_count=?,
             up_shares=?,down_shares=?,up_cost=?,down_cost=?,paired_shares=?,paired_cost=?,
             weighted_complete_set_cost=?,paired_capital_fraction=?,unmatched_side=?,unmatched_shares=?,
             unmatched_cost=?,cash_deployed=?,maximum_pair_set_cost=?,emergency_attempts=?,emergency_fills=?,
             emergency_shares=?,emergency_cash_cost=?,emergency_pair_matches=?,passive_pair_matches=?,
             emergency_weighted_set_cost=?,passive_weighted_set_cost=?,unmatched_episodes=?,
             maximum_unmatched_age_ms=?,mandatory_hedge_completed=?,real_money=0,orders_sent=0
             WHERE condition_id=?
            """,
            (
                utc_now(), status, summary["quotes"], summary["fills"], summary["pair_matches"],
                summary["up_shares"], summary["down_shares"], summary["up_cost"], summary["down_cost"],
                summary["paired_shares"], summary["paired_cost"], summary["weighted_complete_set_cost"],
                summary["paired_capital_fraction"], summary["unmatched_side"], summary["unmatched_shares"],
                summary["unmatched_cost"], summary["cash_deployed"], summary["maximum_pair_set_cost"],
                summary["emergency_attempts"], summary["emergency_fills"], summary["emergency_shares"],
                summary["emergency_cash_cost"], summary["emergency_pair_matches"], summary["passive_pair_matches"],
                summary["emergency_weighted_set_cost"], summary["passive_weighted_set_cost"],
                summary["unmatched_episodes"], summary["maximum_unmatched_age_ms"],
                int(bool(summary["mandatory_hedge_completed"])), summary["condition_id"],
            ),
        )
        self.db.commit()

    def save_health(self, state: LiveShadowState) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO v020_health VALUES(?,?,?,?)",
            (utc_now(), os.getpid(), _json(state.connections), _json(dict(state.counters))),
        )
        self.db.commit()

    def quick_check(self) -> str:
        return str(self.db.execute("PRAGMA quick_check").fetchone()[0])


class V020ProcessLock:
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
            raise V020Error("Ya existe otro monitor V0.20 activo") from exc
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


async def run_smoke(settings: Settings, seconds: float) -> dict[str, Any]:
    if seconds <= 0 or seconds > 180:
        raise ValueError("Smoke V0.20 debe durar entre 0 y 180 segundos")
    now = int(time.time())
    start = now - now % 300
    slug = f"btc-updown-5m-{start}"
    market = await _discover(settings, slug, time.time() + 30)
    state = LiveShadowState()
    state.set_market(market)
    engine = MandatoryHedgePaperEngine(condition_id=market.condition_id, market_start_ms=market.start_ms, config=default_strategy_config())
    stop = asyncio.Event()

    async def handler(raw: str) -> None:
        state.ingest(source="clob", default_stream="market", raw=raw)
        trade = extract_trade_event(raw, market)
        if trade is not None:
            engine.on_trade(**trade)

    task = asyncio.create_task(
        _websocket_feed(
            name="v020-smoke-clob", endpoint=settings.clob_ws_url,
            subscription={"assets_ids": [market.up_token_id, market.down_token_id], "type": "market", "custom_feature_enabled": True},
            heartbeat_text="PING", heartbeat_seconds=10.0, use_proxy=settings.ws_use_proxy,
            reconnect_max_seconds=settings.reconnect_max_seconds, stop_event=stop, state=state, handler=handler,
        )
    )
    deadline = min(time.time() + seconds, market.end_ms / 1000 - 1)
    sampled = 0
    try:
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            offset = max(0, min(299, int(time.time()) - market.start_ms // 1000))
            accumulator = state.builder.accumulators.get(market.condition_id) if state.builder else None
            if accumulator is None:
                continue
            row = _materialize_rows(accumulator, through_second=offset)[offset]
            engine.on_second(row)
            engine.drain_events()
            sampled += 1
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    summary = engine.finish()
    return {
        "status": "SMOKE_OK" if sampled > 0 else "SMOKE_NO_BOOK",
        "slug": slug, "seconds_sampled": sampled, "connections": state.connections,
        "counters": dict(state.counters), "paper_quotes": summary["quotes"],
        "paper_fills": summary["fills"], "paper_pairs": summary["pair_matches"],
        "emergency_fills": summary["emergency_fills"], "final_unmatched_shares": summary["unmatched_shares"],
        "orders_sent": 0, "outcomes_read": 0, "real_money": "BLOQUEADO",
    }


async def _run_v020_locked(*, settings: Settings, prereg_path: str | Path, output_db: str | Path) -> dict[str, Any]:
    prereg = load_and_verify_prereg(prereg_path)
    store = V020Store(output_db)
    store.open(prereg, prereg_path)
    store.mark_interrupted_markets()
    meta = store.meta()
    target_end = _parse_utc(str(meta["target_end_at"])).timestamp()
    run_id = store.start_run()
    status = "RUNNING"
    error: str | None = None
    state = LiveShadowState()
    try:
        while time.time() < target_end:
            now = time.time()
            current = int(now) - int(now) % 300
            target_start = current if now <= current + 15 else current + 300
            if target_start + 300 > target_end:
                await asyncio.sleep(max(0.0, target_end - time.time()))
                break
            await asyncio.sleep(max(0.0, target_start + 1 - time.time()))
            slug = f"btc-updown-5m-{target_start}"
            if store.market_seen(slug):
                await asyncio.sleep(max(1.0, target_start + 300 - time.time()))
                continue
            try:
                market = await _discover(settings, slug, target_start + 30)
            except Exception:
                logging.getLogger("v020").exception("Discovery V0.20 fallo")
                continue
            store.start_market(market)
            state.set_market(market)
            engine = MandatoryHedgePaperEngine(condition_id=market.condition_id, market_start_ms=market.start_ms, config=prereg["strategy"])
            clob_stop = asyncio.Event()

            async def handler(raw: str) -> None:
                state.ingest(source="clob", default_stream="market", raw=raw)
                trade = extract_trade_event(raw, market)
                if trade is not None:
                    engine.on_trade(**trade)
                    store.apply_events(market.condition_id, engine.drain_events())

            clob_task = asyncio.create_task(
                _websocket_feed(
                    name=f"v020-clob-{slug}", endpoint=settings.clob_ws_url,
                    subscription={"assets_ids": [market.up_token_id, market.down_token_id], "type": "market", "custom_feature_enabled": True},
                    heartbeat_text="PING", heartbeat_seconds=10.0, use_proxy=settings.ws_use_proxy,
                    reconnect_max_seconds=settings.reconnect_max_seconds, stop_event=clob_stop, state=state, handler=handler,
                )
            )
            market_completed = False
            try:
                for second in range(first_live_second(target_start, time.time()), 300):
                    if time.time() >= target_end:
                        break
                    await _wait_or_stop(clob_stop, max(0.0, target_start + second + 1.05 - time.time()))
                    accumulator = state.builder.accumulators.get(market.condition_id) if state.builder else None
                    if accumulator is None:
                        continue
                    row = _materialize_rows(accumulator, through_second=second)[second]
                    engine.on_second(row)
                    store.save_second(market.condition_id, row)
                    store.apply_events(market.condition_id, engine.drain_events())
                    if second % 10 == 0:
                        store.save_health(state)
                else:
                    market_completed = True
            finally:
                clob_stop.set()
                clob_task.cancel()
                await asyncio.gather(clob_task, return_exceptions=True)
                summary = engine.finish()
                store.apply_events(market.condition_id, engine.drain_events())
                if market_completed:
                    store.complete_market(summary)
                else:
                    store.exclude_interrupted_market(summary)
                state.connections.pop(f"v020-clob-{slug}", None)
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
        logging.getLogger("v020").exception("V0.20 fallo")
    finally:
        quick_check = store.quick_check()
        store.finish_run(run_id, status, error)
        final_meta = store.meta()
        store.close()
    return {
        "status": status, "error": error, "database": str(Path(output_db).resolve()),
        "quick_check": quick_check, "experiment_started_at": final_meta["experiment_started_at"],
        "target_end_at": final_meta["target_end_at"], "target_hours": final_meta["target_hours"],
        "orders_sent": 0, "wallet_required": False, "outcomes_read": 0, "real_money": "BLOQUEADO",
    }


async def run_v020(*, settings: Settings, prereg_path: str | Path, output_db: str | Path) -> dict[str, Any]:
    lock = V020ProcessLock(f"{Path(output_db).resolve()}.lock")
    lock.acquire()
    try:
        return await _run_v020_locked(settings=settings, prereg_path=prereg_path, output_db=output_db)
    finally:
        lock.release()


def v020_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).resolve()
    if not database.is_file():
        return {"status": "NOT_STARTED", "database": str(database), "outcomes_read": 0, "orders_sent": 0, "real_money": "BLOQUEADO"}
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        meta = {str(k): json.loads(str(v)) for k, v in connection.execute("SELECT key,value FROM v020_meta")}
        market = connection.execute(
            """
            SELECT COUNT(*),SUM(status='COMPLETE'),SUM(status='INTERRUPTED_EXCLUDED'),
             SUM(fill_count>0 AND status='COMPLETE'),SUM(paired_shares>0 AND status='COMPLETE'),
             COALESCE(SUM(fill_count),0),COALESCE(SUM(quote_count),0),COALESCE(SUM(paired_shares),0),
             COALESCE(SUM(paired_cost),0),COALESCE(SUM(cash_deployed),0),COALESCE(SUM(unmatched_shares),0),
             MAX(maximum_pair_set_cost),MAX(cash_deployed),MAX(unmatched_shares),
             COALESCE(SUM(emergency_fills),0),COALESCE(SUM(emergency_shares),0),
             COALESCE(SUM(unmatched_episodes),0),MAX(maximum_unmatched_age_ms),
             SUM(CASE WHEN unmatched_episodes>0 THEN mandatory_hedge_completed ELSE 0 END),
             SUM(CASE WHEN unmatched_episodes>0 THEN 1 ELSE 0 END)
            FROM v020_markets
            """
        ).fetchone()
        fills_real = connection.execute("SELECT COALESCE(SUM(real_money),0) FROM v020_fills").fetchone()[0]
        orders_real = connection.execute("SELECT COALESCE(SUM(real_order),0) FROM v020_quotes").fetchone()[0]
        run = connection.execute("SELECT run_id,started_at,finished_at,status,error FROM v020_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        health = connection.execute("SELECT recorded_at,process_id,connections_json FROM v020_health ORDER BY recorded_at DESC LIMIT 1").fetchone()
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        connection.close()
    target_end = _parse_utc(str(meta["target_end_at"])).timestamp()
    paired_shares = float(market[7] or 0)
    paired_cost = float(market[8] or 0)
    cash = float(market[9] or 0)
    episode_markets = int(market[19] or 0)
    return {
        "status": "COMPLETED" if meta.get("experiment_completed_at") else "RUNNING_OR_RESUMABLE",
        "database": str(database), "experiment_started_at": meta.get("experiment_started_at"),
        "target_end_at": meta.get("target_end_at"), "remaining_hours": max(0.0, target_end-time.time())/3600.0,
        "markets_seen": int(market[0] or 0), "markets_complete": int(market[1] or 0),
        "markets_interrupted_excluded": int(market[2] or 0), "markets_with_fill": int(market[3] or 0),
        "markets_paired": int(market[4] or 0), "paper_fills": int(market[5] or 0), "paper_quotes": int(market[6] or 0),
        "paired_shares": paired_shares, "weighted_complete_set_cost": paired_cost/paired_shares if paired_shares else None,
        "aggregate_paired_capital_fraction": paired_cost/cash if cash else None,
        "final_unmatched_shares": float(market[10] or 0), "maximum_pair_set_cost": float(market[11]) if market[11] is not None else None,
        "maximum_cash_per_market": float(market[12] or 0), "maximum_unmatched_shares_per_market": float(market[13] or 0),
        "emergency_fills": int(market[14] or 0), "emergency_shares": float(market[15] or 0),
        "unmatched_episodes": int(market[16] or 0), "maximum_unmatched_age_ms": int(market[17] or 0),
        "mandatory_hedge_completion_rate": int(market[18] or 0)/episode_markets if episode_markets else None,
        "latest_run": {"run_id": run[0], "started_at": run[1], "finished_at": run[2], "status": run[3], "error": run[4]} if run else None,
        "latest_health": {"recorded_at": health[0], "process_id": health[1], "connections": json.loads(health[2])} if health else None,
        "sqlite_quick_check": quick, "outcomes_read": int(meta.get("outcomes_read",0)),
        "orders_sent": int(orders_real or 0), "real_money_rows": int(fills_real or 0), "real_money": "BLOQUEADO",
        "active_forward_read": False, "active_forward_modified": False,
    }


__all__ = [
    "DATABASE_SCHEMA", "MAXIMUM_HOURS", "PREREG_SCHEMA", "V020ProcessLock", "V020Store",
    "load_and_verify_prereg", "run_smoke", "run_v020", "v020_status",
]
