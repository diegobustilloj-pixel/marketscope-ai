from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.config import Settings
from polymarket_bot.discovery import DiscoveryError
from polymarket_bot.domain import parse_message_metadata
from polymarket_bot.phase41 import (
    LiveShadowState,
    _fetch_market_sync,
    _materialize_rows,
    _silver_market,
    _wait_or_stop,
    _websocket_feed,
)
from polymarket_bot.v018_multifill import (
    MultiFillPaperEngine,
    V018Error,
    default_strategy_config,
    validate_strategy_config,
)


ROOT = Path(__file__).resolve().parents[2]
PREREG_SCHEMA = "prereg_v018_multifill_paper_1"
DATABASE_SCHEMA = "paper_v018_multifill_1"
MAXIMUM_HOURS = 24.0


DDL = """
CREATE TABLE IF NOT EXISTS v018_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v018_runs(
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS v018_markets(
    condition_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    market_start_ms INTEGER NOT NULL,
    market_end_ms INTEGER NOT NULL,
    discovered_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    favorite_side TEXT,
    quote_count INTEGER NOT NULL DEFAULT 0,
    fill_count INTEGER NOT NULL DEFAULT 0,
    up_shares REAL NOT NULL DEFAULT 0,
    down_shares REAL NOT NULL DEFAULT 0,
    up_cost REAL NOT NULL DEFAULT 0,
    down_cost REAL NOT NULL DEFAULT 0,
    paired_shares REAL NOT NULL DEFAULT 0,
    estimated_complete_set_cost REAL,
    estimated_paired_capital_fraction REAL,
    residual_side TEXT,
    residual_shares REAL NOT NULL DEFAULT 0,
    cash_deployed REAL NOT NULL DEFAULT 0,
    real_money INTEGER NOT NULL DEFAULT 0,
    orders_sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS v018_seconds(
    condition_id TEXT NOT NULL,
    second_offset INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    up_best_bid REAL,
    up_best_ask REAL,
    up_bid_depth_1c REAL,
    down_best_bid REAL,
    down_best_ask REAL,
    down_bid_depth_1c REAL,
    polymarket_trade_count INTEGER NOT NULL,
    book_messages INTEGER NOT NULL,
    price_change_messages INTEGER NOT NULL,
    PRIMARY KEY(condition_id, second_offset)
);
CREATE TABLE IF NOT EXISTS v018_quotes(
    order_id TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL,
    side TEXT NOT NULL,
    placed_second INTEGER NOT NULL,
    active_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    price REAL NOT NULL,
    original_size REAL NOT NULL,
    remaining_size REAL NOT NULL,
    filled_size REAL NOT NULL,
    queue_ahead_initial REAL NOT NULL,
    queue_ahead_remaining REAL NOT NULL,
    status TEXT NOT NULL,
    cancelled_second INTEGER,
    cancel_reason TEXT,
    real_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS v018_fills(
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    side TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    second_offset REAL NOT NULL,
    fill_price REAL NOT NULL,
    fill_size REAL NOT NULL,
    fee REAL NOT NULL,
    rebate REAL NOT NULL,
    cash_cost REAL NOT NULL,
    trigger TEXT NOT NULL,
    trigger_price REAL NOT NULL,
    real_money INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS v018_health(
    recorded_at TEXT PRIMARY KEY,
    process_id INTEGER NOT NULL,
    connections_json TEXT NOT NULL,
    counters_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v018_markets_start ON v018_markets(market_start_ms);
CREATE INDEX IF NOT EXISTS idx_v018_fills_market ON v018_fills(condition_id, timestamp_ms);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_and_verify_prereg(path: str | Path) -> dict[str, Any]:
    prereg_path = Path(path).resolve()
    payload = json.loads(prereg_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != PREREG_SCHEMA:
        raise V018Error("Prerregistro V0.18 incompatible")
    if payload.get("status") != "FROZEN_PAPER_ONLY":
        raise V018Error("V0.18 no está congelado para paper")
    hours = float(payload.get("target_hours", 0))
    if hours <= 0 or hours > MAXIMUM_HOURS:
        raise V018Error("V0.18 debe durar como máximo 24 horas")
    safety = payload.get("safety")
    expected = {
        "wallet_required": False,
        "orders_enabled": False,
        "real_money": "BLOQUEADO",
        "active_forward_read": False,
        "active_forward_modified": False,
        "outcomes_before_completion": False,
        "maximum_hours": 24,
    }
    if not isinstance(safety, dict):
        raise V018Error("Safety V0.18 ausente")
    for key, value in expected.items():
        if safety.get(key) != value:
            raise V018Error(f"Safety V0.18 incumplido: {key}")
    validate_strategy_config(payload.get("strategy", {}))
    code = payload.get("code_hashes")
    if not isinstance(code, dict):
        raise V018Error("Hashes de código ausentes")
    expected_hashes = {
        "engine": sha256_file(ROOT / "src" / "polymarket_bot" / "v018_multifill.py"),
        "runner": sha256_file(Path(__file__)),
        "entrypoint": sha256_file(ROOT / "v018_paper.py"),
    }
    for key, actual in expected_hashes.items():
        if code.get(key) != actual:
            raise V018Error(f"Hash V0.18 no coincide: {key}")
    source = payload.get("wallet_evidence")
    if not isinstance(source, dict):
        raise V018Error("Evidencia de wallet ausente")
    evidence_path = (ROOT / str(source.get("relative_path"))).resolve()
    if not evidence_path.is_file() or sha256_file(evidence_path) != source.get("sha256"):
        raise V018Error("Evidencia de wallet no coincide")
    return payload


class V018Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.connection: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("Base V0.18 cerrada")
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
                raise V018Error("Base V0.18 incompatible")
            if meta.get("prereg_sha256") != prereg_hash:
                raise V018Error("La base pertenece a otro prerregistro")
            if float(meta.get("target_hours", 0)) != float(prereg["target_hours"]):
                raise V018Error("Duración V0.18 incompatible")
            if meta.get("orders_enabled") is not False or meta.get("real_money") != "BLOQUEADO":
                raise V018Error("Base V0.18 insegura")

    def close(self) -> None:
        if self.connection is not None:
            self.db.commit()
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.connection.close()
            self.connection = None

    def set_meta(self, key: str, value: Any, *, commit: bool = True) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO v018_meta(key,value) VALUES(?,?)",
            (str(key), _json(value)),
        )
        if commit:
            self.db.commit()

    def meta(self) -> dict[str, Any]:
        rows = self.db.execute("SELECT key,value FROM v018_meta").fetchall()
        result = {}
        for key, value in rows:
            try:
                result[str(key)] = json.loads(str(value))
            except json.JSONDecodeError:
                result[str(key)] = value
        return result

    def start_run(self) -> int:
        cursor = self.db.execute(
            "INSERT INTO v018_runs(started_at,status) VALUES(?,?)",
            (utc_now(), "RUNNING"),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, error: str | None) -> None:
        self.db.execute(
            "UPDATE v018_runs SET finished_at=?,status=?,error=? WHERE run_id=?",
            (utc_now(), status, error, int(run_id)),
        )
        self.db.commit()

    def mark_interrupted_markets(self) -> None:
        self.db.execute(
            "UPDATE v018_markets SET status='INTERRUPTED_EXCLUDED' WHERE status='COLLECTING'"
        )
        self.db.commit()

    def market_seen(self, slug: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM v018_markets WHERE slug=? LIMIT 1", (slug,)
        ).fetchone() is not None

    def start_market(self, market: Any) -> None:
        self.db.execute(
            """
            INSERT INTO v018_markets(
                condition_id,slug,market_start_ms,market_end_ms,discovered_at,status
            ) VALUES(?,?,?,?,?,'COLLECTING')
            """,
            (
                market.condition_id,
                market.slug,
                int(market.start_ms),
                int(market.end_ms),
                utc_now(),
            ),
        )
        self.db.commit()

    def save_second(self, condition_id: str, row: Mapping[str, Any]) -> None:
        self.db.execute(
            """
            INSERT OR REPLACE INTO v018_seconds VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                condition_id,
                int(row["second_offset"]),
                utc_now(),
                row.get("up_best_bid"),
                row.get("up_best_ask"),
                row.get("up_bid_depth_1c"),
                row.get("down_best_bid"),
                row.get("down_best_ask"),
                row.get("down_bid_depth_1c"),
                int(row.get("polymarket_trade_count") or 0),
                int(row.get("book_messages") or 0),
                int(row.get("price_change_messages") or 0),
            ),
        )
        self.db.commit()

    def apply_events(self, condition_id: str, events: list[dict[str, Any]]) -> None:
        for event in events:
            kind = event["kind"]
            if kind == "FAVORITE_SET":
                self.db.execute(
                    "UPDATE v018_markets SET favorite_side=? WHERE condition_id=?",
                    (event["side"], condition_id),
                )
            elif kind == "QUOTE_PLACED":
                order = event["order"]
                self.db.execute(
                    """
                    INSERT INTO v018_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                    """,
                    (
                        order["order_id"], condition_id, order["side"],
                        order["placed_second"], order["active_at_ms"],
                        order["expires_at_ms"], order["price"],
                        order["original_size"], order["remaining_size"],
                        order["filled_size"], order["queue_ahead_initial"],
                        order["queue_ahead_remaining"], order["status"], None, None,
                    ),
                )
            elif kind == "QUOTE_CANCELLED":
                order = event["order"]
                self.db.execute(
                    """
                    UPDATE v018_quotes SET remaining_size=?,filled_size=?,
                        queue_ahead_remaining=?,status=?,cancelled_second=?,cancel_reason=?
                    WHERE order_id=?
                    """,
                    (
                        order["remaining_size"], order["filled_size"],
                        order["queue_ahead_remaining"], order["status"],
                        event["cancelled_second"], event["reason"], order["order_id"],
                    ),
                )
            elif kind == "FILL":
                fill = event["fill"]
                order = event["order"]
                self.db.execute(
                    """
                    INSERT INTO v018_fills(
                        order_id,condition_id,side,timestamp_ms,second_offset,
                        fill_price,fill_size,fee,rebate,cash_cost,trigger,
                        trigger_price,real_money
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)
                    """,
                    (
                        fill["order_id"], condition_id, fill["side"],
                        fill["timestamp_ms"], fill["second_offset"],
                        fill["fill_price"], fill["fill_size"], fill["fee"],
                        fill["rebate"], fill["cash_cost"], fill["trigger"],
                        fill["trigger_price"],
                    ),
                )
                self.db.execute(
                    """
                    UPDATE v018_quotes SET remaining_size=?,filled_size=?,
                        queue_ahead_remaining=?,status=? WHERE order_id=?
                    """,
                    (
                        order["remaining_size"], order["filled_size"],
                        order["queue_ahead_remaining"], order["status"], order["order_id"],
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
            raise ValueError(f"Estado final V0.18 invalido: {status}")
        self.db.execute(
            """
            UPDATE v018_markets SET completed_at=?,status=?,favorite_side=?,
                quote_count=?,fill_count=?,up_shares=?,down_shares=?,up_cost=?,down_cost=?,
                paired_shares=?,estimated_complete_set_cost=?,
                estimated_paired_capital_fraction=?,residual_side=?,residual_shares=?,
                cash_deployed=?,real_money=0,orders_sent=0
            WHERE condition_id=?
            """,
            (
                utc_now(), status, summary["favorite_side"], summary["quotes"], summary["fills"],
                summary["up_shares"], summary["down_shares"], summary["up_cost"],
                summary["down_cost"], summary["paired_shares"],
                summary["estimated_complete_set_cost"],
                summary["estimated_paired_capital_fraction"], summary["residual_side"],
                summary["residual_shares"], summary["cash_deployed"],
                summary["condition_id"],
            ),
        )
        self.db.commit()

    def save_health(self, state: LiveShadowState) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO v018_health VALUES(?,?,?,?)",
            (utc_now(), os.getpid(), _json(state.connections), _json(dict(state.counters))),
        )
        self.db.commit()

    def quick_check(self) -> str:
        return str(self.db.execute("PRAGMA quick_check").fetchone()[0])


class V018ProcessLock:
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
            raise V018Error("Ya existe otro monitor V0.18 activo") from exc
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


def first_live_second(market_start_s: int, now_s: float) -> int:
    """Return the first second observable live, without replaying elapsed time."""
    return max(0, min(299, int(now_s - market_start_s)))


def extract_trade_event(raw: str, market: Any) -> dict[str, Any] | None:
    try:
        stream, timestamp_ms, _, _ = parse_message_metadata(raw, "market")
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or stream != "last_trade_price":
        return None
    token = str(payload.get("asset_id") or "")
    if token == str(market.up_token_id):
        side = "UP"
    elif token == str(market.down_token_id):
        side = "DOWN"
    else:
        return None
    try:
        price = float(payload["price"])
        size = float(payload["size"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "side": side,
        "timestamp_ms": int(timestamp_ms),
        "trade_price": price,
        "trade_size": size,
    }


async def _discover(settings: Settings, slug: str, deadline: float) -> Any:
    while time.time() < deadline:
        try:
            definition, resolution, _ = await asyncio.to_thread(
                _fetch_market_sync, settings, slug
            )
            return _silver_market(definition, resolution)
        except DiscoveryError:
            await asyncio.sleep(2.0)
    raise V018Error(f"No se pudo descubrir {slug}")


async def run_smoke(settings: Settings, seconds: float) -> dict[str, Any]:
    if seconds <= 0 or seconds > 180:
        raise ValueError("Smoke debe durar entre 0 y 180 segundos")
    now = int(time.time())
    start = now - now % 300
    slug = f"btc-updown-5m-{start}"
    market = await _discover(settings, slug, time.time() + 30)
    state = LiveShadowState()
    state.set_market(market)
    engine = MultiFillPaperEngine(
        condition_id=market.condition_id,
        market_start_ms=market.start_ms,
        config=default_strategy_config(),
    )
    stop = asyncio.Event()

    async def handler(raw: str) -> None:
        state.ingest(source="clob", default_stream="market", raw=raw)
        trade = extract_trade_event(raw, market)
        if trade is not None:
            engine.on_trade(**trade)

    task = asyncio.create_task(
        _websocket_feed(
            name="v018-smoke-clob",
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
            stop_event=stop,
            state=state,
            handler=handler,
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
            rows = _materialize_rows(accumulator, through_second=offset)
            engine.on_second(rows[offset])
            engine.drain_events()
            sampled += 1
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    summary = engine.finish()
    return {
        "status": "SMOKE_OK" if sampled > 0 else "SMOKE_NO_BOOK",
        "slug": slug,
        "seconds_sampled": sampled,
        "connections": state.connections,
        "counters": dict(state.counters),
        "paper_quotes": summary["quotes"],
        "paper_fills": summary["fills"],
        "orders_sent": 0,
        "real_money": "BLOQUEADO",
        "outcomes_read": 0,
    }


async def _run_v018_locked(
    *,
    settings: Settings,
    prereg_path: str | Path,
    output_db: str | Path,
) -> dict[str, Any]:
    prereg = load_and_verify_prereg(prereg_path)
    store = V018Store(output_db)
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
            except V018Error:
                logging.getLogger("v018").exception("Discovery V0.18 falló")
                continue
            store.start_market(market)
            state.set_market(market)
            engine = MultiFillPaperEngine(
                condition_id=market.condition_id,
                market_start_ms=market.start_ms,
                config=prereg["strategy"],
            )
            clob_stop = asyncio.Event()

            async def handler(raw: str) -> None:
                state.ingest(source="clob", default_stream="market", raw=raw)
                trade = extract_trade_event(raw, market)
                if trade is not None:
                    engine.on_trade(**trade)
                    store.apply_events(market.condition_id, engine.drain_events())

            clob_task = asyncio.create_task(
                _websocket_feed(
                    name=f"v018-clob-{slug}",
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
                    state=state,
                    handler=handler,
                )
            )
            market_completed = False
            try:
                first_second = first_live_second(target_start, time.time())
                for second in range(first_second, 300):
                    if time.time() >= target_end:
                        break
                    await _wait_or_stop(
                        clob_stop,
                        max(0.0, target_start + second + 1.05 - time.time()),
                    )
                    accumulator = (
                        state.builder.accumulators.get(market.condition_id)
                        if state.builder else None
                    )
                    if accumulator is None:
                        continue
                    rows = _materialize_rows(accumulator, through_second=second)
                    row = rows[second]
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
                state.connections.pop(f"v018-clob-{slug}", None)
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
        logging.getLogger("v018").exception("V0.18 falló")
    finally:
        quick_check = store.quick_check()
        store.finish_run(run_id, status, error)
        final_meta = store.meta()
        store.close()
    return {
        "status": status,
        "error": error,
        "database": str(Path(output_db).resolve()),
        "quick_check": quick_check,
        "experiment_started_at": final_meta["experiment_started_at"],
        "target_end_at": final_meta["target_end_at"],
        "target_hours": final_meta["target_hours"],
        "orders_sent": 0,
        "wallet_required": False,
        "outcomes_read": 0,
        "real_money": "BLOQUEADO",
    }


async def run_v018(
    *,
    settings: Settings,
    prereg_path: str | Path,
    output_db: str | Path,
) -> dict[str, Any]:
    lock = V018ProcessLock(f"{Path(output_db).resolve()}.lock")
    lock.acquire()
    try:
        return await _run_v018_locked(
            settings=settings,
            prereg_path=prereg_path,
            output_db=output_db,
        )
    finally:
        lock.release()


def v018_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).resolve()
    if not database.is_file():
        return {
            "status": "NOT_STARTED",
            "database": str(database),
            "outcomes_read": 0,
            "orders_sent": 0,
            "real_money": "BLOQUEADO",
        }
    connection = sqlite3.connect(
        f"{database.as_uri()}?mode=ro", uri=True, timeout=5
    )
    try:
        meta = {}
        for key, value in connection.execute("SELECT key,value FROM v018_meta"):
            meta[str(key)] = json.loads(str(value))
        market = connection.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN status='COMPLETE' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status='INTERRUPTED_EXCLUDED' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN fill_count>0 AND status='COMPLETE' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN paired_shares>0 AND status='COMPLETE' THEN 1 ELSE 0 END),
                   COALESCE(SUM(fill_count),0),COALESCE(SUM(quote_count),0),
                   COALESCE(SUM(cash_deployed),0)
            FROM v018_markets
            """
        ).fetchone()
        fills_real = connection.execute(
            "SELECT COALESCE(SUM(real_money),0) FROM v018_fills"
        ).fetchone()[0]
        orders_real = connection.execute(
            "SELECT COALESCE(SUM(real_order),0) FROM v018_quotes"
        ).fetchone()[0]
        run = connection.execute(
            "SELECT run_id,started_at,finished_at,status,error FROM v018_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        health = connection.execute(
            "SELECT recorded_at,process_id,connections_json FROM v018_health ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        connection.close()
    target_end = _parse_utc(str(meta["target_end_at"])).timestamp()
    completed = meta.get("experiment_completed_at") is not None
    return {
        "status": "COMPLETED" if completed else "RUNNING_OR_RESUMABLE",
        "database": str(database),
        "experiment_started_at": meta.get("experiment_started_at"),
        "target_end_at": meta.get("target_end_at"),
        "remaining_hours": max(0.0, target_end - time.time()) / 3600.0,
        "markets_seen": int(market[0] or 0),
        "markets_complete": int(market[1] or 0),
        "markets_interrupted_excluded": int(market[2] or 0),
        "markets_with_fill": int(market[3] or 0),
        "markets_paired": int(market[4] or 0),
        "paper_fills": int(market[5] or 0),
        "paper_quotes": int(market[6] or 0),
        "paper_cash_deployed": float(market[7] or 0),
        "latest_run": {
            "run_id": run[0], "started_at": run[1], "finished_at": run[2],
            "status": run[3], "error": run[4],
        } if run else None,
        "latest_health": {
            "recorded_at": health[0], "process_id": health[1],
            "connections": json.loads(health[2]),
        } if health else None,
        "sqlite_quick_check": quick,
        "outcomes_read": int(meta.get("outcomes_read", 0)),
        "orders_sent": int(orders_real or 0),
        "real_money_rows": int(fills_real or 0),
        "real_money": "BLOQUEADO",
        "active_forward_read": False,
        "active_forward_modified": False,
    }


__all__ = [
    "DATABASE_SCHEMA",
    "MAXIMUM_HOURS",
    "PREREG_SCHEMA",
    "V018Store",
    "V018ProcessLock",
    "extract_trade_event",
    "first_live_second",
    "load_and_verify_prereg",
    "run_smoke",
    "run_v018",
    "sha256_file",
    "v018_status",
]
