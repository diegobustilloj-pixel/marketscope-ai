from __future__ import annotations

import json
import math
import shutil
import sqlite3
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.phase2 import SilverMarket
from polymarket_bot.resolution_contract import (
    TWAP_TOPIC_BY_WINDOW,
    ResolutionTwapContract,
)


V031_SCHEMA_VERSION = "1"
V031_CODE_VERSION = "0.9.5a1-v031-path-capture"
V031_CAPTURE_HOURS = 1.0
V031_EXPECTED_MARKETS = 12
V031_SNAPSHOT_FREQUENCY_HZ = 1
V031_MARKET_SECONDS = 300
V031_LEVELS = 5
V031_FRESHNESS_MAX_AGE_MS = 5_000
V031_MAX_DATABASE_GB = 0.5
V031_MINIMUM_FREE_GB = 20.0
V031_HEALTH_INTERVAL_SECONDS = 30.0
V031_SUPPORTED_TWAP_WINDOWS = (30, 60)

QUALITY_MISSING_CHAINLINK = 1
QUALITY_STALE_CHAINLINK = 2
QUALITY_MISSING_TWAP = 4
QUALITY_STALE_TWAP = 8
QUALITY_MISSING_UP_BOOK = 16
QUALITY_STALE_UP_BOOK = 32
QUALITY_MISSING_DOWN_BOOK = 64
QUALITY_STALE_DOWN_BOOK = 128
QUALITY_TWAP_WINDOW_MISMATCH = 256


V031_DDL = """
CREATE TABLE v031_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE v031_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    error TEXT
);
CREATE TABLE v031_markets (
    condition_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    event_id TEXT,
    market_start_ms INTEGER NOT NULL,
    market_end_ms INTEGER NOT NULL,
    up_token_id TEXT NOT NULL,
    down_token_id TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    resolution_source TEXT,
    resolution_contract_status TEXT NOT NULL,
    resolution_twap_window_s INTEGER,
    resolution_twap_topic TEXT,
    capture_status TEXT NOT NULL,
    capture_error TEXT
);
CREATE INDEX idx_v031_markets_start ON v031_markets(market_start_ms);
CREATE TABLE v031_snapshots (
    condition_id TEXT NOT NULL,
    second_offset INTEGER NOT NULL,
    snapshot_timestamp_ms INTEGER NOT NULL,
    recorded_timestamp_ms INTEGER NOT NULL,
    chainlink_price REAL,
    chainlink_source_timestamp_ms INTEGER,
    chainlink_received_timestamp_ms INTEGER,
    chainlink_age_ms INTEGER,
    chainlink_fresh INTEGER NOT NULL,
    official_twap_price REAL,
    official_twap_window_s INTEGER,
    official_twap_source_timestamp_ms INTEGER,
    official_twap_received_timestamp_ms INTEGER,
    official_twap_age_ms INTEGER,
    official_twap_fresh INTEGER NOT NULL,
    up_bid_levels_json TEXT NOT NULL,
    up_ask_levels_json TEXT NOT NULL,
    down_bid_levels_json TEXT NOT NULL,
    down_ask_levels_json TEXT NOT NULL,
    up_book_source_timestamp_ms INTEGER,
    up_book_received_timestamp_ms INTEGER,
    up_book_age_ms INTEGER,
    up_book_fresh INTEGER NOT NULL,
    down_book_source_timestamp_ms INTEGER,
    down_book_received_timestamp_ms INTEGER,
    down_book_age_ms INTEGER,
    down_book_fresh INTEGER NOT NULL,
    up_best_bid REAL,
    up_best_ask REAL,
    down_best_bid REAL,
    down_best_ask REAL,
    up_bid_depth_top5 REAL NOT NULL,
    up_ask_depth_top5 REAL NOT NULL,
    down_bid_depth_top5 REAL NOT NULL,
    down_ask_depth_top5 REAL NOT NULL,
    quality_flags INTEGER NOT NULL,
    complete INTEGER NOT NULL,
    PRIMARY KEY(condition_id,second_offset),
    FOREIGN KEY(condition_id) REFERENCES v031_markets(condition_id)
);
CREATE INDEX idx_v031_snapshots_time
    ON v031_snapshots(snapshot_timestamp_ms);
CREATE TABLE v031_twap_ticks (
    source_timestamp_ms INTEGER NOT NULL,
    window_s INTEGER NOT NULL,
    topic TEXT NOT NULL,
    value REAL NOT NULL,
    full_accuracy_value TEXT,
    message_timestamp_ms INTEGER,
    received_timestamp_ms INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY(source_timestamp_ms,window_s)
);
CREATE TABLE v031_health (
    recorded_at TEXT PRIMARY KEY,
    database_bytes INTEGER NOT NULL,
    free_bytes INTEGER NOT NULL,
    counters_json TEXT NOT NULL,
    connections_json TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_compact(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def database_footprint(path: Path) -> int:
    return sum(
        item.stat().st_size
        for item in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if item.exists()
    )


def open_read_only(path: str | Path) -> sqlite3.Connection:
    database = Path(path).expanduser().resolve()
    connection = sqlite3.connect(
        f"{database.as_uri()}?mode=ro",
        uri=True,
        timeout=60,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def read_v031_meta(path: str | Path) -> dict[str, Any]:
    connection = open_read_only(path)
    try:
        return {
            str(row[0]): json.loads(str(row[1]))
            for row in connection.execute(
                "SELECT key,value FROM v031_meta ORDER BY key"
            )
        }
    finally:
        connection.close()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any, fallback: int) -> int:
    try:
        return int(value) if value is not None else int(fallback)
    except (TypeError, ValueError):
        return int(fallback)


@dataclass(slots=True)
class PathBook:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    initialized: bool = False
    source_timestamp_ms: int = 0
    received_timestamp_ms: int = 0
    out_of_order_updates: int = 0

    @staticmethod
    def _levels(payload: Mapping[str, Any], name: str) -> dict[float, float]:
        result: dict[float, float] = {}
        raw = payload.get(name, [])
        if not isinstance(raw, list):
            return result
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            price = _number(item.get("price"))
            size = _number(item.get("size"))
            if (
                price is not None
                and size is not None
                and 0.0 < price < 1.0
                and size > 0.0
            ):
                result[price] = size
        return result

    def replace(
        self,
        payload: Mapping[str, Any],
        *,
        source_timestamp_ms: int,
        received_timestamp_ms: int,
    ) -> bool:
        if self.initialized and source_timestamp_ms < self.source_timestamp_ms:
            self.out_of_order_updates += 1
            return False
        self.bids = self._levels(payload, "bids")
        self.asks = self._levels(payload, "asks")
        self.source_timestamp_ms = int(source_timestamp_ms)
        self.received_timestamp_ms = int(received_timestamp_ms)
        self.initialized = True
        return True

    def change(
        self,
        *,
        side: str,
        price: float,
        size: float,
        source_timestamp_ms: int,
        received_timestamp_ms: int,
    ) -> bool:
        if not self.initialized:
            return False
        if source_timestamp_ms < self.source_timestamp_ms:
            self.out_of_order_updates += 1
            return False
        target = self.bids if side == "BUY" else self.asks
        if size <= 0.0:
            target.pop(price, None)
        elif 0.0 < price < 1.0:
            target[price] = size
        self.source_timestamp_ms = int(source_timestamp_ms)
        self.received_timestamp_ms = int(received_timestamp_ms)
        return True

    def top(self, side: str, count: int = V031_LEVELS) -> list[list[float]]:
        source = self.bids if side == "bid" else self.asks
        ordered = sorted(source.items(), reverse=side == "bid")[:count]
        return [[float(price), float(size)] for price, size in ordered]


TwapItem = tuple[float, int, int, int, str | None]
ReferenceItem = tuple[float, int, int]


class V031CaptureState:
    def __init__(self) -> None:
        self.counters: Counter[str] = Counter()
        self.connections: dict[str, str] = {}
        self.latest_chainlink: ReferenceItem | None = None
        self.latest_twap_by_window: dict[int, TwapItem] = {}
        self.condition_id: str | None = None
        self.token_sides: dict[str, str] = {}
        self.books: dict[str, PathBook] = {
            "Up": PathBook(),
            "Down": PathBook(),
        }

    def activate_market(self, market: SilverMarket) -> None:
        self.condition_id = str(market.condition_id)
        self.token_sides = {
            str(market.up_token_id): "Up",
            str(market.down_token_id): "Down",
        }
        self.books = {"Up": PathBook(), "Down": PathBook()}

    def clear_market(self) -> None:
        self.condition_id = None
        self.token_sides = {}
        self.books = {"Up": PathBook(), "Down": PathBook()}

    def ingest_rtds(
        self, raw: str, *, received_timestamp_ms: int | None = None
    ) -> Mapping[str, Any] | None:
        received = int(received_timestamp_ms or time.time() * 1000)
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            self.counters["rtds:malformed"] += 1
            return None
        if not isinstance(message, Mapping):
            return None
        topic = str(message.get("topic") or "")
        payload = message.get("payload")
        if not isinstance(payload, Mapping):
            return None
        if str(payload.get("symbol") or "").lower() != "btc/usd":
            return None
        price = _number(payload.get("value"))
        if price is None or price <= 0.0:
            return None
        source_ms = _timestamp(payload.get("timestamp"), received)
        if topic == "crypto_prices_chainlink":
            self.latest_chainlink = (price, source_ms, received)
            self.counters["rtds:chainlink_updates"] += 1
            return None
        try:
            window_s = int(payload.get("window_s"))
        except (TypeError, ValueError):
            return None
        if TWAP_TOPIC_BY_WINDOW.get(window_s) != topic:
            self.counters["rtds:twap_topic_window_mismatch"] += 1
            return None
        full_accuracy = payload.get("full_accuracy_value")
        self.latest_twap_by_window[window_s] = (
            price,
            source_ms,
            received,
            window_s,
            str(full_accuracy) if full_accuracy is not None else None,
        )
        self.counters[f"rtds:twap_{window_s}s_updates"] += 1
        return message

    def _book_item(
        self,
        item: Mapping[str, Any],
        *,
        fallback_source_ms: int,
        received_ms: int,
    ) -> None:
        token = str(item.get("asset_id") or "")
        outcome = self.token_sides.get(token)
        if outcome is None:
            return
        source_ms = _timestamp(item.get("timestamp"), fallback_source_ms)
        if self.books[outcome].replace(
            item,
            source_timestamp_ms=source_ms,
            received_timestamp_ms=received_ms,
        ):
            self.counters[f"clob:{outcome.lower()}_book_updates"] += 1
        else:
            self.counters["clob:out_of_order_rejected"] += 1

    def ingest_clob(
        self, raw: str, *, received_timestamp_ms: int | None = None
    ) -> None:
        received = int(received_timestamp_ms or time.time() * 1000)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.counters["clob:malformed"] += 1
            return
        items = payload if isinstance(payload, list) else [payload]
        if all(isinstance(item, Mapping) and item.get("event_type") == "book" for item in items):
            for item in items:
                assert isinstance(item, Mapping)
                self._book_item(
                    item,
                    fallback_source_ms=received,
                    received_ms=received,
                )
            return
        if not isinstance(payload, Mapping):
            return
        if str(payload.get("event_type") or "") != "price_change":
            return
        if self.condition_id and str(payload.get("market") or "") not in {
            "",
            self.condition_id,
        }:
            return
        source_ms = _timestamp(payload.get("timestamp"), received)
        changes = payload.get("price_changes", [])
        if not isinstance(changes, list):
            return
        for item in changes:
            if not isinstance(item, Mapping):
                continue
            token = str(item.get("asset_id") or "")
            outcome = self.token_sides.get(token)
            side = str(item.get("side") or "").upper()
            price = _number(item.get("price"))
            size = _number(item.get("size"))
            if (
                outcome is None
                or side not in {"BUY", "SELL"}
                or price is None
                or size is None
            ):
                continue
            if self.books[outcome].change(
                side=side,
                price=price,
                size=size,
                source_timestamp_ms=source_ms,
                received_timestamp_ms=received,
            ):
                self.counters[f"clob:{outcome.lower()}_price_changes"] += 1
            elif self.books[outcome].out_of_order_updates:
                self.counters["clob:out_of_order_rejected"] += 1

    @staticmethod
    def _age(snapshot_ms: int, source_ms: int | None) -> int | None:
        return max(0, snapshot_ms - source_ms) if source_ms is not None else None

    def snapshot(
        self,
        *,
        condition_id: str,
        second_offset: int,
        snapshot_timestamp_ms: int,
        official_twap_window_s: int,
        recorded_timestamp_ms: int | None = None,
    ) -> dict[str, Any]:
        recorded = int(recorded_timestamp_ms or time.time() * 1000)
        flags = 0
        chainlink = self.latest_chainlink
        chainlink_age = self._age(
            snapshot_timestamp_ms,
            chainlink[1] if chainlink is not None else None,
        )
        chainlink_fresh = chainlink_age is not None and chainlink_age <= V031_FRESHNESS_MAX_AGE_MS
        if chainlink is None:
            flags |= QUALITY_MISSING_CHAINLINK
        elif not chainlink_fresh:
            flags |= QUALITY_STALE_CHAINLINK

        twap = self.latest_twap_by_window.get(int(official_twap_window_s))
        twap_age = self._age(
            snapshot_timestamp_ms,
            twap[1] if twap is not None else None,
        )
        twap_fresh = twap_age is not None and twap_age <= V031_FRESHNESS_MAX_AGE_MS
        if twap is None:
            flags |= QUALITY_MISSING_TWAP
        elif twap[3] != official_twap_window_s:
            flags |= QUALITY_TWAP_WINDOW_MISMATCH
        elif not twap_fresh:
            flags |= QUALITY_STALE_TWAP

        level_payload: dict[str, Any] = {}
        for outcome, missing_flag, stale_flag in (
            ("Up", QUALITY_MISSING_UP_BOOK, QUALITY_STALE_UP_BOOK),
            ("Down", QUALITY_MISSING_DOWN_BOOK, QUALITY_STALE_DOWN_BOOK),
        ):
            book = self.books[outcome]
            bids = book.top("bid")
            asks = book.top("ask")
            age = self._age(
                snapshot_timestamp_ms,
                book.source_timestamp_ms if book.initialized else None,
            )
            fresh = age is not None and age <= V031_FRESHNESS_MAX_AGE_MS
            if not book.initialized or not bids or not asks:
                flags |= missing_flag
            elif not fresh:
                flags |= stale_flag
            prefix = outcome.lower()
            level_payload.update(
                {
                    f"{prefix}_bid_levels": bids,
                    f"{prefix}_ask_levels": asks,
                    f"{prefix}_book_source_timestamp_ms": (
                        book.source_timestamp_ms if book.initialized else None
                    ),
                    f"{prefix}_book_received_timestamp_ms": (
                        book.received_timestamp_ms if book.initialized else None
                    ),
                    f"{prefix}_book_age_ms": age,
                    f"{prefix}_book_fresh": fresh,
                    f"{prefix}_best_bid": bids[0][0] if bids else None,
                    f"{prefix}_best_ask": asks[0][0] if asks else None,
                    f"{prefix}_bid_depth_top5": sum(level[1] for level in bids),
                    f"{prefix}_ask_depth_top5": sum(level[1] for level in asks),
                }
            )
        return {
            "condition_id": str(condition_id),
            "second_offset": int(second_offset),
            "snapshot_timestamp_ms": int(snapshot_timestamp_ms),
            "recorded_timestamp_ms": recorded,
            "chainlink_price": chainlink[0] if chainlink is not None else None,
            "chainlink_source_timestamp_ms": chainlink[1] if chainlink is not None else None,
            "chainlink_received_timestamp_ms": chainlink[2] if chainlink is not None else None,
            "chainlink_age_ms": chainlink_age,
            "chainlink_fresh": chainlink_fresh,
            "official_twap_price": twap[0] if twap is not None else None,
            "official_twap_window_s": twap[3] if twap is not None else official_twap_window_s,
            "official_twap_source_timestamp_ms": twap[1] if twap is not None else None,
            "official_twap_received_timestamp_ms": twap[2] if twap is not None else None,
            "official_twap_age_ms": twap_age,
            "official_twap_fresh": twap_fresh,
            **level_payload,
            "quality_flags": flags,
            "complete": flags == 0,
        }


class V031Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.connection: sqlite3.Connection | None = None
        self._pending_writes = 0

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("La base V0.31 no esta abierta")
        return self.connection

    def open(
        self,
        *,
        preregistration_sha256: str,
        launch_manifest_sha256: str,
        now_timestamp: float | None = None,
    ) -> None:
        existed = self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=60)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        if existed:
            tables = {
                str(row[0])
                for row in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "v031_meta" not in tables:
                raise ValueError("La base existente no pertenece a V0.31")
        else:
            self.connection.executescript(V031_DDL)
        meta = self.meta()
        if meta:
            if meta.get("schema_version") != V031_SCHEMA_VERSION:
                raise ValueError("Esquema V0.31 incompatible; use una base nueva")
            if meta.get("preregistration_sha256") != preregistration_sha256:
                raise ValueError("La base V0.31 pertenece a otra preinscripcion")
            if meta.get("launch_manifest_sha256") != launch_manifest_sha256:
                raise ValueError("La base V0.31 pertenece a otro lanzamiento")
            expected_safety = {
                "orders_enabled": False,
                "paper_orders_enabled": False,
                "wallet_required": False,
                "money_real_enabled": False,
                "real_money": "BLOQUEADO",
                "outcomes_read": 0,
                "pnl_calculated": False,
                "signals_generated": False,
            }
            for key, expected in expected_safety.items():
                if meta.get(key) != expected:
                    raise ValueError(f"Base V0.31 insegura: {key}")
        else:
            now = float(now_timestamp if now_timestamp is not None else time.time())
            capture_start = (int(now) // 300 + 1) * 300
            target_end = capture_start + int(V031_CAPTURE_HOURS * 3600)
            values = {
                "schema_version": V031_SCHEMA_VERSION,
                "code_version": V031_CODE_VERSION,
                "variant": "V0.31_PATH_EXECUTION_CAPTURE_ONLY_TECHNICAL_1H",
                "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                "capture_start_at": datetime.fromtimestamp(capture_start, timezone.utc).isoformat(timespec="seconds"),
                "target_hours": V031_CAPTURE_HOURS,
                "target_end_at": datetime.fromtimestamp(target_end, timezone.utc).isoformat(timespec="seconds"),
                "expected_markets": V031_EXPECTED_MARKETS,
                "preregistration_sha256": preregistration_sha256,
                "launch_manifest_sha256": launch_manifest_sha256,
                "supported_twap_windows": list(V031_SUPPORTED_TWAP_WINDOWS),
                "completion_reason": None,
                "observation_ended_at": None,
                "orders_enabled": False,
                "paper_orders_enabled": False,
                "wallet_required": False,
                "money_real_enabled": False,
                "real_money": "BLOQUEADO",
                "outcomes_read": 0,
                "pnl_calculated": False,
                "signals_generated": False,
            }
            self.connection.executemany(
                "INSERT INTO v031_meta(key,value) VALUES(?,?)",
                [(key, json_compact(value)) for key, value in values.items()],
            )
            self.connection.commit()
        self.reconcile_interrupted_runs()

    def meta(self) -> dict[str, Any]:
        return {
            str(row[0]): json.loads(str(row[1]))
            for row in self.db.execute("SELECT key,value FROM v031_meta ORDER BY key")
        }

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO v031_meta(key,value) VALUES(?,?)",
            (key, json_compact(value)),
        )
        self.db.commit()

    def reconcile_interrupted_runs(self) -> None:
        self.db.execute(
            """
            UPDATE v031_runs SET finished_at=?,status='INTERRUPTED',
             error=COALESCE(error,'RECOVERED_STALE_RUNNING')
            WHERE status='RUNNING'
            """,
            (utc_now(),),
        )
        self.db.execute(
            """
            UPDATE v031_markets SET capture_status='INTERRUPTED',
             capture_error=COALESCE(capture_error,'RECOVERED_PARTIAL_MARKET')
            WHERE capture_status='CAPTURING'
            """
        )
        self.db.commit()

    def start_run(self) -> int:
        cursor = self.db.execute(
            "INSERT INTO v031_runs(started_at,status) VALUES(?,'RUNNING')",
            (utc_now(),),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, *, status: str, error: str | None) -> None:
        self.db.execute(
            "UPDATE v031_runs SET finished_at=?,status=?,error=? WHERE run_id=?",
            (utc_now(), status, error, run_id),
        )
        self.db.commit()

    def save_market(
        self,
        market: SilverMarket,
        *,
        contract: ResolutionTwapContract,
    ) -> bool:
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO v031_markets(
             condition_id,slug,event_id,market_start_ms,market_end_ms,
             up_token_id,down_token_id,discovered_at,resolution_source,
             resolution_contract_status,resolution_twap_window_s,
             resolution_twap_topic,capture_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                market.condition_id,
                market.slug,
                market.event_id,
                market.start_ms,
                market.end_ms,
                market.up_token_id,
                market.down_token_id,
                utc_now(),
                market.resolution_source,
                contract.status,
                contract.window_seconds,
                contract.topic,
                "CAPTURING" if contract.verified else "REJECTED_CONTRACT",
            ),
        )
        self.db.commit()
        return cursor.rowcount > 0

    def market_exists(self, slug: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM v031_markets WHERE slug=?", (slug,)
        ).fetchone() is not None

    def finish_market(self, condition_id: str, *, error: str | None = None) -> None:
        self.db.execute(
            """
            UPDATE v031_markets SET capture_status=?,capture_error=?
            WHERE condition_id=?
            """,
            ("COMPLETED" if error is None else "FAILED", error, condition_id),
        )
        self.db.commit()

    def save_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        values = (
            snapshot["condition_id"], snapshot["second_offset"],
            snapshot["snapshot_timestamp_ms"], snapshot["recorded_timestamp_ms"],
            snapshot["chainlink_price"], snapshot["chainlink_source_timestamp_ms"],
            snapshot["chainlink_received_timestamp_ms"], snapshot["chainlink_age_ms"],
            int(snapshot["chainlink_fresh"]), snapshot["official_twap_price"],
            snapshot["official_twap_window_s"], snapshot["official_twap_source_timestamp_ms"],
            snapshot["official_twap_received_timestamp_ms"], snapshot["official_twap_age_ms"],
            int(snapshot["official_twap_fresh"]),
            json_compact(snapshot["up_bid_levels"]), json_compact(snapshot["up_ask_levels"]),
            json_compact(snapshot["down_bid_levels"]), json_compact(snapshot["down_ask_levels"]),
            snapshot["up_book_source_timestamp_ms"], snapshot["up_book_received_timestamp_ms"],
            snapshot["up_book_age_ms"], int(snapshot["up_book_fresh"]),
            snapshot["down_book_source_timestamp_ms"], snapshot["down_book_received_timestamp_ms"],
            snapshot["down_book_age_ms"], int(snapshot["down_book_fresh"]),
            snapshot["up_best_bid"], snapshot["up_best_ask"],
            snapshot["down_best_bid"], snapshot["down_best_ask"],
            snapshot["up_bid_depth_top5"], snapshot["up_ask_depth_top5"],
            snapshot["down_bid_depth_top5"], snapshot["down_ask_depth_top5"],
            snapshot["quality_flags"], int(snapshot["complete"]),
        )
        placeholders = ",".join("?" for _ in values)
        self.db.execute(
            f"INSERT OR REPLACE INTO v031_snapshots VALUES({placeholders})",
            values,
        )
        self._pending_writes += 1
        if self._pending_writes >= 10:
            self.db.commit()
            self._pending_writes = 0

    def save_twap_message(
        self,
        message: Mapping[str, Any],
        *,
        received_timestamp_ms: int | None = None,
    ) -> str:
        topic = str(message.get("topic") or "")
        payload = message.get("payload")
        if not isinstance(payload, Mapping):
            return "MALFORMED"
        try:
            window_s = int(payload.get("window_s"))
            source_ms = int(payload.get("timestamp"))
            value = float(payload.get("value"))
        except (TypeError, ValueError):
            return "MALFORMED"
        if (
            str(payload.get("symbol") or "").lower() != "btc/usd"
            or not math.isfinite(value)
            or TWAP_TOPIC_BY_WINDOW.get(window_s) != topic
        ):
            return "REJECTED"
        message_timestamp = message.get("timestamp")
        try:
            message_ms = int(message_timestamp) if message_timestamp is not None else None
        except (TypeError, ValueError):
            message_ms = None
        received = int(received_timestamp_ms or time.time() * 1000)
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO v031_twap_ticks VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                source_ms, window_s, topic, value,
                str(payload.get("full_accuracy_value")) if payload.get("full_accuracy_value") is not None else None,
                message_ms, received, utc_now(),
            ),
        )
        self._pending_writes += int(cursor.rowcount > 0)
        if self._pending_writes >= 10:
            self.db.commit()
            self._pending_writes = 0
        return "SAVED" if cursor.rowcount else "DUPLICATE"

    def save_health(
        self,
        *,
        counters: Mapping[str, int],
        connections: Mapping[str, str],
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO v031_health VALUES(?,?,?,?,?)",
            (
                utc_now(),
                database_footprint(self.path),
                shutil.disk_usage(self.path.parent).free,
                json_compact(dict(counters)),
                json_compact(dict(connections)),
            ),
        )
        self.db.commit()
        self._pending_writes = 0

    def quick_check(self) -> str:
        self.db.commit()
        return str(self.db.execute("PRAGMA quick_check").fetchone()[0])

    def close(self) -> None:
        if self.connection is not None:
            self.connection.commit()
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.connection.close()
            self.connection = None


__all__ = [
    "PathBook",
    "V031CaptureState",
    "V031Store",
    "V031_CAPTURE_HOURS",
    "V031_DDL",
    "V031_EXPECTED_MARKETS",
    "V031_FRESHNESS_MAX_AGE_MS",
    "V031_LEVELS",
    "V031_MARKET_SECONDS",
    "V031_MAX_DATABASE_GB",
    "V031_MINIMUM_FREE_GB",
    "V031_SUPPORTED_TWAP_WINDOWS",
    "database_footprint",
    "open_read_only",
    "parse_utc",
    "read_v031_meta",
    "utc_now",
]
