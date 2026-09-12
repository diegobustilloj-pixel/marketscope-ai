from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.config import Settings
from polymarket_bot.domain import parse_message_metadata
from polymarket_bot.phase41 import LiveShadowState, _websocket_feed

from .contract import MarketContract, validate_gamma_market
from .research import PROTOCOL_PATH, ROOT, ResearchError, _fetch_gamma, sha256_file, utc_now
from .v002 import LateDecision, LateWindowEdgeEngine, exact_book_fill


PREREG_PATH = ROOT / "data" / "btc5m90_v002" / "shadow_prereg.json"
DATABASE_PATH = ROOT / "data" / "btc5m90_v002" / "shadow.db"
PREREG_SCHEMA = "btc5m90_v002_shadow_prereg_1"
DATABASE_SCHEMA = "btc5m90_v002_shadow_db_1"
TARGET_COMPLETE_MARKETS = 96
MAXIMUM_HOURS = 9.0
SAMPLE_BUCKET_MS = 250
MAX_TRIGGER_EVENT_AGE_MS = 5_000
MAX_FUTURE_CLOCK_SKEW_MS = 2_000


DDL = """
CREATE TABLE IF NOT EXISTS shadow_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_runs(
 run_id INTEGER PRIMARY KEY AUTOINCREMENT,started_at TEXT NOT NULL,finished_at TEXT,
 status TEXT NOT NULL,error TEXT
);
CREATE TABLE IF NOT EXISTS shadow_markets(
 condition_id TEXT PRIMARY KEY,slug TEXT NOT NULL UNIQUE,market_id TEXT NOT NULL,
 market_start_ms INTEGER NOT NULL,market_end_ms INTEGER NOT NULL,monitor_started_ms INTEGER NOT NULL,
 completed_at TEXT,status TEXT NOT NULL,resolution_source TEXT NOT NULL,tick_size REAL NOT NULL,
 minimum_order_shares REAL NOT NULL,fee_rate REAL NOT NULL,fee_exponent REAL NOT NULL,
 snapshot_count INTEGER NOT NULL DEFAULT 0,valid_snapshot_count INTEGER NOT NULL DEFAULT 0,
 stale_snapshot_count INTEGER NOT NULL DEFAULT 0,decision_status TEXT,candidate_price REAL,
 simulated_fill_status TEXT,paper_orders INTEGER NOT NULL DEFAULT 0,
 orders_sent INTEGER NOT NULL DEFAULT 0,outcomes_read INTEGER NOT NULL DEFAULT 0,
 real_money INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shadow_snapshots(
 condition_id TEXT NOT NULL,bucket_ms INTEGER NOT NULL,source_timestamp_ms INTEGER NOT NULL,
 received_timestamp_ms INTEGER NOT NULL,seconds_elapsed REAL NOT NULL,up_best_ask REAL,
 down_best_ask REAL,up_source_timestamp_ms INTEGER NOT NULL,down_source_timestamp_ms INTEGER NOT NULL,
 cross_side_skew_ms INTEGER NOT NULL,maximum_book_age_ms INTEGER NOT NULL,
 trigger_event_age_ms INTEGER NOT NULL,valid INTEGER NOT NULL,
 potential_candidate INTEGER NOT NULL,up_asks_json TEXT NOT NULL,down_asks_json TEXT NOT NULL,
 PRIMARY KEY(condition_id,bucket_ms)
);
CREATE TABLE IF NOT EXISTS shadow_decisions(
 condition_id TEXT PRIMARY KEY,timestamp_ms INTEGER NOT NULL,seconds_elapsed REAL NOT NULL,
 seconds_remaining REAL NOT NULL,side TEXT,ask REAL,expected_edge_per_share REAL,
 decision_status TEXT NOT NULL,decision_reason TEXT NOT NULL,execution_status TEXT NOT NULL,
 fill_vwap REAL,fill_shares REAL,fee REAL,total_debit REAL,book_source_timestamp_ms INTEGER,
 book_received_timestamp_ms INTEGER,book_levels_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_health(
 recorded_at TEXT PRIMARY KEY,process_id INTEGER NOT NULL,connections_json TEXT NOT NULL,
 counters_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_markets_start ON shadow_markets(market_start_ms);
CREATE INDEX IF NOT EXISTS idx_shadow_snapshots_condition ON shadow_snapshots(condition_id,bucket_ms);
"""


class ShadowError(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass(slots=True)
class BookSide:
    asks: dict[float, float] = field(default_factory=dict)
    initialized: bool = False
    source_timestamp_ms: int = 0

    def replace(self, raw_asks: Any, timestamp_ms: int) -> None:
        levels: dict[float, float] = {}
        if isinstance(raw_asks, list):
            for item in raw_asks:
                if not isinstance(item, Mapping):
                    continue
                price = _number(item.get("price"))
                size = _number(item.get("size"))
                if price is not None and size is not None and size > 0.0:
                    levels[price] = size
        self.asks = levels
        self.initialized = True
        self.source_timestamp_ms = int(timestamp_ms)

    def change(self, price: float, size: float, timestamp_ms: int) -> None:
        if size <= 0.0:
            self.asks.pop(price, None)
        else:
            self.asks[price] = size
        self.source_timestamp_ms = int(timestamp_ms)


@dataclass(frozen=True)
class ExecutionRecord:
    decision: LateDecision
    execution_status: str
    fill_vwap: float | None
    fill_shares: float | None
    fee: float | None
    total_debit: float | None
    book_source_timestamp_ms: int
    book_received_timestamp_ms: int
    book_levels: Mapping[str, Any]


class ShadowMarketEngine:
    """Reconstruye libros completos y comparte la decisión exacta con V002."""

    def __init__(self, contract: MarketContract) -> None:
        self.contract = contract
        self.token_sides = {
            contract.up_token_id: "Up",
            contract.down_token_id: "Down",
        }
        self.books = {"Up": BookSide(), "Down": BookSide()}
        self.detector = LateWindowEdgeEngine(
            condition_id=contract.condition_id,
            market_start_ms=contract.market_start_ms,
            market_end_ms=contract.market_end_ms,
            fee=contract.fee,
        )
        self.snapshots: dict[int, dict[str, Any]] = {}
        self.execution: ExecutionRecord | None = None
        self.message_count = 0
        self.invalid_message_count = 0
        self.last_event_source_timestamp_ms = 0
        self.last_event_received_timestamp_ms = 0

    @staticmethod
    def _source_timestamp(payload: Any, fallback: int) -> int:
        raw: Any = None
        if isinstance(payload, Mapping):
            raw = payload.get("timestamp")
        elif isinstance(payload, list) and payload and isinstance(payload[0], Mapping):
            raw = payload[0].get("timestamp")
        try:
            return int(raw) if raw is not None else int(fallback)
        except (TypeError, ValueError):
            return int(fallback)

    def _book(self, payload: Any, fallback: int) -> bool:
        items = payload if isinstance(payload, list) else [payload]
        changed = False
        for item in items:
            if not isinstance(item, Mapping):
                continue
            side = self.token_sides.get(str(item.get("asset_id") or ""))
            if side is None:
                continue
            timestamp_ms = self._source_timestamp(item, fallback)
            self.books[side].replace(item.get("asks"), timestamp_ms)
            changed = True
        return changed

    def _price_change(self, payload: Any, fallback: int) -> bool:
        if not isinstance(payload, Mapping):
            return False
        raw_changes = payload.get("price_changes")
        if not isinstance(raw_changes, list):
            return False
        timestamp_ms = self._source_timestamp(payload, fallback)
        changed = False
        for item in raw_changes:
            if not isinstance(item, Mapping) or str(item.get("side") or "").upper() != "SELL":
                continue
            side = self.token_sides.get(str(item.get("asset_id") or ""))
            price = _number(item.get("price"))
            size = _number(item.get("size"))
            if side is None or price is None or size is None or not self.books[side].initialized:
                continue
            self.books[side].change(price, size, timestamp_ms)
            changed = True
        return changed

    @staticmethod
    def _stored_levels(levels: Mapping[float, float]) -> list[list[float]]:
        return [
            [price, size]
            for price, size in sorted(levels.items())
            if price <= 0.97
        ][:50]

    def _observe(self, received_ms: int, trigger_source_timestamp_ms: int) -> None:
        if not all(book.initialized and book.asks for book in self.books.values()):
            return
        elapsed = (received_ms - self.contract.market_start_ms) / 1000.0
        if elapsed < 235.0 or elapsed >= 300.0:
            return
        up = self.books["Up"]
        down = self.books["Down"]
        up_ask = min(up.asks)
        down_ask = min(down.asks)
        skew = abs(up.source_timestamp_ms - down.source_timestamp_ms)
        maximum_age = max(0, received_ms - min(up.source_timestamp_ms, down.source_timestamp_ms))
        trigger_age = received_ms - int(trigger_source_timestamp_ms)
        # Un nivel que no cambia sigue siendo vigente mientras el websocket no haya
        # perdido eventos. La frescura se aplica al evento que provoca la observación;
        # la edad/skew entre patas se conserva sólo como diagnóstico.
        valid = -MAX_FUTURE_CLOCK_SKEW_MS <= trigger_age <= MAX_TRIGGER_EVENT_AGE_MS
        candidate = any(
            abs(ask - price) <= 1e-9
            for ask in (up_ask, down_ask)
            for price in (0.90, 0.91, 0.92, 0.93, 0.94, 0.95)
        )
        bucket = received_ms // SAMPLE_BUCKET_MS * SAMPLE_BUCKET_MS
        snapshot = {
            "condition_id": self.contract.condition_id,
            "bucket_ms": bucket,
            "source_timestamp_ms": max(up.source_timestamp_ms, down.source_timestamp_ms),
            "received_timestamp_ms": received_ms,
            "seconds_elapsed": elapsed,
            "up_best_ask": up_ask,
            "down_best_ask": down_ask,
            "up_source_timestamp_ms": up.source_timestamp_ms,
            "down_source_timestamp_ms": down.source_timestamp_ms,
            "cross_side_skew_ms": skew,
            "maximum_book_age_ms": maximum_age,
            "trigger_event_age_ms": trigger_age,
            "valid": int(valid),
            "potential_candidate": int(candidate),
            "up_asks": self._stored_levels(up.asks),
            "down_asks": self._stored_levels(down.asks),
        }
        self.snapshots[bucket] = snapshot
        if not valid or self.execution is not None:
            return
        decision = self.detector.observe(
            timestamp_ms=received_ms,
            up_ask=up_ask,
            down_ask=down_ask,
        )
        if decision is None:
            return
        levels = {
            "Up": self._stored_levels(up.asks),
            "Down": self._stored_levels(down.asks),
        }
        status = "NOT_ATTEMPTED"
        vwap: float | None = None
        shares: float | None = None
        fee: float | None = None
        debit: float | None = None
        if decision.status == "SIGNAL" and decision.side and decision.ask is not None:
            fill = exact_book_fill(
                self.books[decision.side].asks,
                maximum_price=decision.ask,
                shares=5.0,
            )
            if fill is None:
                status = "FAILED_INSUFFICIENT_EXACT_DEPTH"
            else:
                vwap, shares = fill
                fee = self.contract.fee.fee_per_share(vwap) * shares
                debit = vwap * shares + fee
                status = "SIMULATED_FILL_LEVEL_A"
        self.execution = ExecutionRecord(
            decision=decision,
            execution_status=status,
            fill_vwap=vwap,
            fill_shares=shares,
            fee=fee,
            total_debit=debit,
            book_source_timestamp_ms=max(up.source_timestamp_ms, down.source_timestamp_ms),
            book_received_timestamp_ms=received_ms,
            book_levels=levels,
        )

    def ingest(self, raw: str, received_ms: int) -> None:
        self.message_count += 1
        try:
            stream, source_timestamp_ms, _, _ = parse_message_metadata(raw, "market")
            payload = json.loads(raw)
        except (ValueError, TypeError, json.JSONDecodeError):
            self.invalid_message_count += 1
            return
        fallback = int(source_timestamp_ms or received_ms)
        changed = False
        if stream == "book":
            changed = self._book(payload, fallback)
        elif stream == "price_change":
            changed = self._price_change(payload, fallback)
        if changed:
            self.last_event_source_timestamp_ms = fallback
            self.last_event_received_timestamp_ms = int(received_ms)
            self._observe(int(received_ms), fallback)

    def tick(self, received_ms: int) -> None:
        """Muestrea el estado a 250 ms aunque el libro no cambie justo a los 240 s."""

        if self.last_event_source_timestamp_ms <= 0:
            return
        self._observe(int(received_ms), self.last_event_source_timestamp_ms)

    def summary(self) -> dict[str, Any]:
        snapshots = list(self.snapshots.values())
        return {
            "message_count": self.message_count,
            "invalid_message_count": self.invalid_message_count,
            "snapshot_count": len(snapshots),
            "valid_snapshot_count": sum(item["valid"] for item in snapshots),
            "stale_snapshot_count": sum(not item["valid"] for item in snapshots),
            "decision_status": (
                self.execution.decision.status if self.execution is not None else "NO_CANDIDATE"
            ),
            "candidate_price": (
                self.execution.decision.ask if self.execution is not None else None
            ),
            "simulated_fill_status": (
                self.execution.execution_status if self.execution is not None else "NO_FILL"
            ),
            "paper_orders": 0,
            "orders_sent": 0,
            "outcomes_read": 0,
            "real_money": 0,
        }


class ShadowStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.connection: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("ShadowStore cerrado")
        return self.connection

    def open(
        self,
        prereg: Mapping[str, Any],
        prereg_path: str | Path = PREREG_PATH,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(DDL)
        meta = self.meta()
        if not meta:
            started = time.time()
            values = {
                "schema": DATABASE_SCHEMA,
                "created_at": utc_now(),
                "experiment_started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds"),
                "maximum_end_at": datetime.fromtimestamp(
                    started + float(prereg["maximum_hours"]) * 3600, timezone.utc
                ).isoformat(timespec="seconds"),
                "target_complete_markets": int(prereg["target_complete_markets"]),
                "prereg_sha256": sha256_file(Path(prereg_path).resolve()),
                "wallet_required": False,
                "orders_enabled": False,
                "paper_orders": 0,
                "orders_sent": 0,
                "outcomes_read": 0,
                "real_money": "BLOQUEADO",
            }
            for key, value in values.items():
                self.db.execute("INSERT INTO shadow_meta VALUES(?,?)", (key, _json(value)))
            self.db.commit()
        else:
            if (
                meta.get("schema") != DATABASE_SCHEMA
                or meta.get("prereg_sha256") != sha256_file(Path(prereg_path).resolve())
            ):
                raise ShadowError("Base shadow incompatible con prerregistro")
            if meta.get("orders_enabled") is not False or meta.get("real_money") != "BLOQUEADO":
                raise ShadowError("Contrato de seguridad shadow inválido")

    def meta(self) -> dict[str, Any]:
        if self.connection is None:
            return {}
        return {str(k): json.loads(str(v)) for k, v in self.db.execute("SELECT key,value FROM shadow_meta")}

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO shadow_meta VALUES(?,?)", (key, _json(value)))
        self.db.commit()

    def start_run(self) -> int:
        cursor = self.db.execute(
            "INSERT INTO shadow_runs(started_at,status) VALUES(?,'RUNNING')", (utc_now(),)
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, error: str | None) -> None:
        self.db.execute(
            "UPDATE shadow_runs SET finished_at=?,status=?,error=? WHERE run_id=?",
            (utc_now(), status, error, run_id),
        )
        self.db.commit()

    def market_seen(self, slug: str) -> bool:
        return self.db.execute("SELECT 1 FROM shadow_markets WHERE slug=?", (slug,)).fetchone() is not None

    def completed_markets(self) -> int:
        return int(
            self.db.execute("SELECT COUNT(*) FROM shadow_markets WHERE status='COMPLETE'").fetchone()[0]
        )

    def start_market(self, contract: MarketContract, monitor_started_ms: int) -> None:
        self.db.execute(
            """INSERT INTO shadow_markets(
             condition_id,slug,market_id,market_start_ms,market_end_ms,monitor_started_ms,status,
             resolution_source,tick_size,minimum_order_shares,fee_rate,fee_exponent
             ) VALUES(?,?,?,?,?,?,'COLLECTING',?,?,?,?,?)""",
            (
                contract.condition_id,
                contract.slug,
                contract.market_id,
                contract.market_start_ms,
                contract.market_end_ms,
                monitor_started_ms,
                contract.resolution_source,
                contract.tick_size,
                contract.minimum_order_shares,
                contract.fee.rate,
                contract.fee.exponent,
            ),
        )
        self.db.commit()

    def finish_market(self, engine: ShadowMarketEngine, status: str) -> None:
        summary = engine.summary()
        self.db.executemany(
            "INSERT OR REPLACE INTO shadow_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    item["condition_id"], item["bucket_ms"], item["source_timestamp_ms"],
                    item["received_timestamp_ms"], item["seconds_elapsed"], item["up_best_ask"],
                    item["down_best_ask"], item["up_source_timestamp_ms"],
                    item["down_source_timestamp_ms"], item["cross_side_skew_ms"],
                    item["maximum_book_age_ms"], item["trigger_event_age_ms"], item["valid"],
                    item["potential_candidate"],
                    _json(item["up_asks"]), _json(item["down_asks"]),
                )
                for item in engine.snapshots.values()
            ],
        )
        if engine.execution is not None:
            record = engine.execution
            decision = record.decision
            self.db.execute(
                "INSERT OR REPLACE INTO shadow_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision.condition_id, decision.timestamp_ms, decision.seconds_elapsed,
                    decision.seconds_remaining, decision.side, decision.ask,
                    decision.expected_edge_per_share, decision.status, decision.reason,
                    record.execution_status, record.fill_vwap, record.fill_shares, record.fee,
                    record.total_debit, record.book_source_timestamp_ms,
                    record.book_received_timestamp_ms, _json(record.book_levels),
                ),
            )
        self.db.execute(
            """UPDATE shadow_markets SET completed_at=?,status=?,snapshot_count=?,
             valid_snapshot_count=?,stale_snapshot_count=?,decision_status=?,candidate_price=?,
             simulated_fill_status=?,paper_orders=0,orders_sent=0,outcomes_read=0,real_money=0
             WHERE condition_id=?""",
            (
                utc_now(), status, summary["snapshot_count"], summary["valid_snapshot_count"],
                summary["stale_snapshot_count"], summary["decision_status"],
                summary["candidate_price"], summary["simulated_fill_status"],
                engine.contract.condition_id,
            ),
        )
        self.db.commit()

    def save_health(self, state: LiveShadowState, counters: Mapping[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO shadow_health VALUES(?,?,?,?)",
            (utc_now(), os.getpid(), _json(state.connections), _json(counters)),
        )
        self.db.commit()

    def close(self) -> None:
        if self.connection is not None:
            self.db.commit()
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.close()
            self.connection = None


class ProcessLock:
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
            raise ShadowError("Ya existe un shadow BTC5M90 V002 activo") from exc

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


def load_and_verify_prereg(path: str | Path = PREREG_PATH) -> dict[str, Any]:
    prereg_path = Path(path).resolve()
    payload = json.loads(prereg_path.read_text(encoding="utf-8"))
    if payload.get("schema") != PREREG_SCHEMA or payload.get("status") != "FROZEN_SHADOW_ONLY":
        raise ShadowError("Prerregistro shadow incompatible")
    if int(payload.get("target_complete_markets", 0)) != TARGET_COMPLETE_MARKETS:
        raise ShadowError("Target de mercados cambió")
    if float(payload.get("maximum_hours", 0.0)) != MAXIMUM_HOURS:
        raise ShadowError("Duración máxima cambió")
    expected_safety = {
        "wallet_required": False,
        "orders_enabled": False,
        "paper_orders": False,
        "outcomes_during_capture": False,
        "real_money": "BLOQUEADO",
    }
    if payload.get("safety") != expected_safety:
        raise ShadowError("Safety shadow modificado")
    if payload.get("source_protocol_sha256") != sha256_file(PROTOCOL_PATH):
        raise ShadowError("Protocolo base V002 cambió")
    expected_files = {
        "src/polymarket_bot/btc_5m_90/v002.py",
        "src/polymarket_bot/btc_5m_90/v002_shadow.py",
        "btc5m90_v002_shadow.py",
    }
    if set(payload.get("code_sha256", {})) != expected_files:
        raise ShadowError("Inventario de código shadow incompleto")
    for relative, expected in payload.get("code_sha256", {}).items():
        if sha256_file(ROOT / relative) != expected:
            raise ShadowError(f"Código shadow cambió: {relative}")
    return payload


async def _discover_contract(slug: str, deadline: float) -> MarketContract:
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            raw = await asyncio.to_thread(_fetch_gamma, slug)
            payload = json.loads(raw)
            contract = validate_gamma_market(
                payload, expected_slug=slug, require_resolution=False
            )
            if bool(payload.get("active")) and not bool(payload.get("closed")) and bool(payload.get("acceptingOrders")):
                return contract
            last_error = ShadowError("Mercado aún no acepta órdenes")
        except (ValueError, json.JSONDecodeError, ShadowError, ResearchError) as exc:
            last_error = exc
        await asyncio.sleep(2.0)
    raise ShadowError(f"No se pudo descubrir {slug}: {last_error}")


async def run_smoke(settings: Settings, seconds: float = 20.0) -> dict[str, Any]:
    if seconds <= 0 or seconds > 120:
        raise ValueError("Smoke debe durar 1–120 segundos")
    now = int(time.time())
    start = now - now % 300
    if now >= start + 285:
        start += 300
        await asyncio.sleep(max(0.0, start + 1 - time.time()))
    slug = f"btc-updown-5m-{start}"
    contract = await _discover_contract(slug, time.time() + 30)
    engine = ShadowMarketEngine(contract)
    state = LiveShadowState()
    stop = asyncio.Event()

    async def handler(raw: str) -> None:
        engine.ingest(raw, int(time.time() * 1000))

    task = asyncio.create_task(
        _websocket_feed(
            name="btc5m90-v002-smoke",
            endpoint=settings.clob_ws_url,
            subscription={
                "assets_ids": [contract.up_token_id, contract.down_token_id],
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
    try:
        await asyncio.sleep(seconds)
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    initialized = all(book.initialized and book.asks for book in engine.books.values())
    return {
        "status": "SMOKE_OK" if initialized and engine.message_count > 0 else "SMOKE_NO_BOOK",
        "slug": slug,
        "messages": engine.message_count,
        "both_books_initialized": initialized,
        "wallet_required": False,
        "orders_sent": 0,
        "outcomes_read": 0,
        "real_money": "BLOQUEADO",
    }


async def _run_shadow(
    settings: Settings,
    prereg: Mapping[str, Any],
    prereg_path: Path,
    database: Path,
) -> dict[str, Any]:
    store = ShadowStore(database)
    store.open(prereg, prereg_path)
    run_id = store.start_run()
    meta = store.meta()
    maximum_end = datetime.fromisoformat(str(meta["maximum_end_at"]).replace("Z", "+00:00")).timestamp()
    target = int(meta["target_complete_markets"])
    state = LiveShadowState()
    run_status = "RUNNING"
    error: str | None = None
    counters: dict[str, Any] = {"markets_attempted": 0, "messages": 0}
    try:
        while store.completed_markets() < target and time.time() < maximum_end:
            now = time.time()
            block = int(now) - int(now) % 300
            target_start = block if now < block + 230 else block + 300
            await asyncio.sleep(max(0.0, target_start + 1 - time.time()))
            slug = f"btc-updown-5m-{target_start}"
            if store.market_seen(slug):
                await asyncio.sleep(max(0.0, target_start + 300.1 - time.time()))
                continue
            try:
                contract = await _discover_contract(
                    slug, min(maximum_end, time.time() + 40)
                )
            except ShadowError as exc:
                counters["discovery_failures"] = counters.get("discovery_failures", 0) + 1
                counters["last_discovery_error"] = str(exc)
                store.save_health(state, counters)
                logging.getLogger("btc5m90-v002").warning("%s", exc)
                await asyncio.sleep(max(0.0, target_start + 300.1 - time.time()))
                continue
            monitor_started_ms = int(time.time() * 1000)
            store.start_market(contract, monitor_started_ms)
            counters["markets_attempted"] += 1
            engine = ShadowMarketEngine(contract)
            stop = asyncio.Event()

            async def handler(raw: str) -> None:
                engine.ingest(raw, int(time.time() * 1000))

            task = asyncio.create_task(
                _websocket_feed(
                    name=f"btc5m90-v002-{slug}",
                    endpoint=settings.clob_ws_url,
                    subscription={
                        "assets_ids": [contract.up_token_id, contract.down_token_id],
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
            end = min(maximum_end, contract.market_end_ms / 1000 + 0.2)
            next_health = time.monotonic()
            try:
                while time.time() < end:
                    await asyncio.sleep(min(0.25, max(0.05, end - time.time())))
                    engine.tick(int(time.time() * 1000))
                    counters["messages"] = engine.message_count
                    if time.monotonic() >= next_health:
                        store.save_health(state, counters)
                        next_health = time.monotonic() + 5.0
            finally:
                stop.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            fully_observed = (
                monitor_started_ms <= contract.market_start_ms + 240_000
                and time.time() >= contract.market_end_ms / 1000
            )
            store.finish_market(engine, "COMPLETE" if fully_observed else "INTERRUPTED_EXCLUDED")
        if store.completed_markets() >= target:
            store.set_meta("experiment_completed_at", utc_now())
            run_status = "COMPLETED"
        else:
            store.set_meta("experiment_stopped_at", utc_now())
            run_status = "MAXIMUM_TIME_REACHED"
    except asyncio.CancelledError:
        run_status = "INTERRUPTED"
        raise
    except KeyboardInterrupt:
        run_status = "INTERRUPTED"
    except Exception as exc:
        run_status = "FAILED"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        completed = store.completed_markets()
        quick = str(store.db.execute("PRAGMA quick_check").fetchone()[0])
        store.finish_run(run_id, run_status, error)
        store.close()
    return {
        "status": run_status,
        "error": error,
        "complete_markets": completed,
        "target_complete_markets": target,
        "sqlite_quick_check": quick,
        "wallet_required": False,
        "orders_sent": 0,
        "outcomes_read": 0,
        "real_money": "BLOQUEADO",
    }


async def run_shadow(
    settings: Settings,
    prereg_path: str | Path = PREREG_PATH,
    database: str | Path = DATABASE_PATH,
) -> dict[str, Any]:
    resolved_prereg = Path(prereg_path).resolve()
    prereg = load_and_verify_prereg(resolved_prereg)
    lock = ProcessLock(f"{Path(database).resolve()}.lock")
    lock.acquire()
    try:
        return await _run_shadow(
            settings, prereg, resolved_prereg, Path(database).resolve()
        )
    finally:
        lock.release()


def status(database: str | Path = DATABASE_PATH) -> dict[str, Any]:
    path = Path(database).resolve()
    if not path.is_file():
        return {
            "status": "NOT_STARTED",
            "database": str(path),
            "wallet_required": False,
            "orders_sent": 0,
            "outcomes_read": 0,
            "real_money": "BLOQUEADO",
        }
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        meta = {str(k): json.loads(str(v)) for k, v in connection.execute("SELECT key,value FROM shadow_meta")}
        row = connection.execute(
            """SELECT COUNT(*),SUM(status='COMPLETE'),SUM(status='INTERRUPTED_EXCLUDED'),
             COALESCE(SUM(snapshot_count),0),COALESCE(SUM(valid_snapshot_count),0),
             COALESCE(SUM(stale_snapshot_count),0),COALESCE(SUM(decision_status='SIGNAL'),0),
             COALESCE(SUM(decision_status='REJECTED_EDGE'),0),
             COALESCE(SUM(simulated_fill_status='SIMULATED_FILL_LEVEL_A'),0),
             COALESCE(SUM(simulated_fill_status='FAILED_INSUFFICIENT_EXACT_DEPTH'),0),
             COALESCE(SUM(paper_orders),0),COALESCE(SUM(orders_sent),0),
             COALESCE(SUM(outcomes_read),0),COALESCE(SUM(real_money),0)
             FROM shadow_markets"""
        ).fetchone()
        latest = connection.execute(
            "SELECT started_at,finished_at,status,error FROM shadow_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        connection.close()
    maximum_end = datetime.fromisoformat(str(meta["maximum_end_at"]).replace("Z", "+00:00")).timestamp()
    complete = int(row[1] or 0)
    target = int(meta["target_complete_markets"])
    if meta.get("experiment_completed_at"):
        current_status = "COMPLETED"
    elif latest and latest[2] == "FAILED":
        current_status = "FAILED"
    elif maximum_end <= time.time():
        current_status = "MAXIMUM_TIME_REACHED"
    else:
        current_status = "RUNNING_OR_RESUMABLE"
    return {
        "status": current_status,
        "database": str(path),
        "experiment_started_at": meta.get("experiment_started_at"),
        "maximum_end_at": meta.get("maximum_end_at"),
        "remaining_hours_max": max(0.0, maximum_end - time.time()) / 3600.0,
        "markets_seen": int(row[0] or 0),
        "markets_complete": complete,
        "target_complete_markets": target,
        "markets_remaining": max(0, target - complete),
        "markets_interrupted_excluded": int(row[2] or 0),
        "snapshots": int(row[3] or 0),
        "valid_snapshots": int(row[4] or 0),
        "stale_snapshots": int(row[5] or 0),
        "approved_signals": int(row[6] or 0),
        "rejected_edge": int(row[7] or 0),
        "simulated_level_a_fills": int(row[8] or 0),
        "failed_exact_depth": int(row[9] or 0),
        "paper_orders": int(row[10] or 0),
        "orders_sent": int(row[11] or 0),
        "outcomes_read": int(meta.get("outcomes_read", 0)) + int(row[12] or 0),
        "real_money_rows": int(row[13] or 0),
        "latest_run": (
            {"started_at": latest[0], "finished_at": latest[1], "status": latest[2], "error": latest[3]}
            if latest
            else None
        ),
        "sqlite_quick_check": quick,
        "wallet_required": False,
        "orders_sent_total": 0,
        "real_money": "BLOQUEADO",
    }


def build_prereg() -> dict[str, Any]:
    code_paths = [
        ROOT / "src" / "polymarket_bot" / "btc_5m_90" / "v002.py",
        Path(__file__).resolve(),
        ROOT / "btc5m90_v002_shadow.py",
    ]
    return {
        "schema": PREREG_SCHEMA,
        "status": "FROZEN_SHADOW_ONLY",
        "created_at": utc_now(),
        "target_complete_markets": TARGET_COMPLETE_MARKETS,
        "expected_hours": 8.0,
        "maximum_hours": MAXIMUM_HOURS,
        "strategy": {
            "wait_seconds": 240,
            "candidate_prices": [0.90, 0.91, 0.92, 0.93, 0.94, 0.95],
            "approved_prices": [0.90, 0.91],
            "minimum_expected_edge_per_share": 0.002,
            "shares": 5.0,
            "first_candidate_locks_market": True,
            "hold_to_resolution": True,
        },
        "execution": {
            "full_book_required": True,
            "maximum_trigger_event_age_ms": MAX_TRIGGER_EVENT_AGE_MS,
            "maximum_future_clock_skew_ms": MAX_FUTURE_CLOCK_SKEW_MS,
            "cross_side_age_is_diagnostic_only": True,
            "sample_bucket_ms": SAMPLE_BUCKET_MS,
            "fill_requires_exact_depth": True,
        },
        "gates": {
            "minimum_complete_markets": 96,
            "minimum_signals": 15,
            "minimum_execution_rate": 0.80,
            "minimum_realized_edge_per_share": 0.002,
            "positive_net_pnl": True,
        },
        "safety": {
            "wallet_required": False,
            "orders_enabled": False,
            "paper_orders": False,
            "outcomes_during_capture": False,
            "real_money": "BLOQUEADO",
        },
        "source_protocol_sha256": sha256_file(PROTOCOL_PATH),
        "code_sha256": {
            str(path.relative_to(ROOT)).replace("\\", "/"): sha256_file(path)
            for path in code_paths
        },
    }


def freeze_prereg(path: str | Path = PREREG_PATH) -> dict[str, Any]:
    target = Path(path).resolve()
    if target.exists():
        raise ShadowError("Prerregistro shadow ya existe y no se sobrescribe")
    payload = build_prereg()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    return payload
