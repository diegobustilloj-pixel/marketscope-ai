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

from .research import ROOT, sha256_file, utc_now
from .v002 import LateDecision, LateWindowEdgeEngine, exact_book_fill
from .v002_shadow import (
    DATABASE_PATH as V002_DATABASE_PATH,
    PREREG_PATH as V002_PREREG_PATH,
    ProcessLock,
    ShadowError,
    _discover_contract,
    load_and_verify_prereg as load_v002_prereg,
)


PREREG_PATH = ROOT / "data" / "btc5m90_v003" / "prereg.json"
DATABASE_PATH = ROOT / "data" / "btc5m90_v003" / "shadow.db"
PREREG_SCHEMA = "btc5m90_v003_prereg_1"
DATABASE_SCHEMA = "btc5m90_v003_shadow_db_1"
TARGET_QUALITY_MARKETS = 276
MAXIMUM_HOURS = 24.0
SAMPLE_BUCKET_MS = 250
CAPTURE_START_SECONDS = 235
DECISION_START_SECONDS = 240
MARKET_END_SECONDS = 300
EXPECTED_DECISION_BUCKETS = 240
MINIMUM_VALID_DECISION_BUCKETS = 228
MINIMUM_DECISION_COVERAGE = 0.95
MAXIMUM_INVALID_GAP_MS = 1_000
MINIMUM_SIGNALS = 70
MAXIMUM_EVENT_DELAY_MS = 5_000
MAXIMUM_FUTURE_CLOCK_SKEW_MS = 2_000


DDL = """
CREATE TABLE IF NOT EXISTS v003_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v003_runs(
 run_id INTEGER PRIMARY KEY AUTOINCREMENT,started_at TEXT NOT NULL,finished_at TEXT,
 status TEXT NOT NULL,error TEXT
);
CREATE TABLE IF NOT EXISTS v003_markets(
 condition_id TEXT PRIMARY KEY,slug TEXT NOT NULL UNIQUE,market_id TEXT NOT NULL,
 market_start_ms INTEGER NOT NULL,market_end_ms INTEGER NOT NULL,monitor_started_ms INTEGER NOT NULL,
 completed_at TEXT,status TEXT NOT NULL,quality_reason TEXT,resolution_source TEXT NOT NULL,
 tick_size REAL NOT NULL,minimum_order_shares REAL NOT NULL,fee_rate REAL NOT NULL,
 fee_exponent REAL NOT NULL,message_count INTEGER NOT NULL DEFAULT 0,
 malformed_or_control_count INTEGER NOT NULL DEFAULT 0,recognized_event_count INTEGER NOT NULL DEFAULT 0,
 stale_event_count INTEGER NOT NULL DEFAULT 0,
 up_book_snapshot_count INTEGER NOT NULL DEFAULT 0,down_book_snapshot_count INTEGER NOT NULL DEFAULT 0,
 price_change_count INTEGER NOT NULL DEFAULT 0,disconnect_count INTEGER NOT NULL DEFAULT 0,
 sample_count INTEGER NOT NULL DEFAULT 0,decision_window_buckets INTEGER NOT NULL DEFAULT 0,
 valid_decision_buckets INTEGER NOT NULL DEFAULT 0,decision_coverage REAL NOT NULL DEFAULT 0,
 maximum_invalid_gap_ms INTEGER NOT NULL DEFAULT 0,empty_up_buckets INTEGER NOT NULL DEFAULT 0,
 empty_down_buckets INTEGER NOT NULL DEFAULT 0,decision_status TEXT,candidate_price REAL,
 simulated_fill_status TEXT,paper_orders INTEGER NOT NULL DEFAULT 0,
 orders_sent INTEGER NOT NULL DEFAULT 0,outcomes_read INTEGER NOT NULL DEFAULT 0,
 real_money INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS v003_samples(
 condition_id TEXT NOT NULL,bucket_ms INTEGER NOT NULL,received_timestamp_ms INTEGER NOT NULL,
 seconds_elapsed REAL NOT NULL,connected INTEGER NOT NULL,up_initialized INTEGER NOT NULL,
 down_initialized INTEGER NOT NULL,up_best_ask REAL,down_best_ask REAL,
 up_book_empty INTEGER NOT NULL,down_book_empty INTEGER NOT NULL,
 up_source_timestamp_ms INTEGER,down_source_timestamp_ms INTEGER,
 trigger_source_timestamp_ms INTEGER,trigger_event_age_ms INTEGER,valid INTEGER NOT NULL,
 potential_candidate INTEGER NOT NULL,up_asks_json TEXT NOT NULL,down_asks_json TEXT NOT NULL,
 PRIMARY KEY(condition_id,bucket_ms)
);
CREATE TABLE IF NOT EXISTS v003_decisions(
 condition_id TEXT PRIMARY KEY,timestamp_ms INTEGER NOT NULL,seconds_elapsed REAL NOT NULL,
 seconds_remaining REAL NOT NULL,side TEXT,ask REAL,expected_edge_per_share REAL,
 decision_status TEXT NOT NULL,decision_reason TEXT NOT NULL,execution_status TEXT NOT NULL,
 fill_vwap REAL,fill_shares REAL,fee REAL,total_debit REAL,book_source_timestamp_ms INTEGER,
 book_received_timestamp_ms INTEGER,book_levels_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v003_health(
 recorded_at TEXT PRIMARY KEY,process_id INTEGER NOT NULL,connections_json TEXT NOT NULL,
 counters_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v003_markets_start ON v003_markets(market_start_ms);
CREATE INDEX IF NOT EXISTS idx_v003_samples_market ON v003_samples(condition_id,bucket_ms);
"""


class V003Error(ShadowError):
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
class BookState:
    asks: dict[float, float] = field(default_factory=dict)
    initialized: bool = False
    source_timestamp_ms: int | None = None
    snapshot_count: int = 0

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
        self.snapshot_count += 1

    def change(self, price: float, size: float, timestamp_ms: int) -> None:
        if size <= 0.0:
            self.asks.pop(price, None)
        else:
            self.asks[price] = size
        self.source_timestamp_ms = int(timestamp_ms)

    def invalidate(self) -> None:
        self.asks.clear()
        self.initialized = False
        self.source_timestamp_ms = None


@dataclass(frozen=True)
class ExecutionRecord:
    decision: LateDecision
    execution_status: str
    fill_vwap: float | None
    fill_shares: float | None
    fee: float | None
    total_debit: float | None
    book_source_timestamp_ms: int | None
    book_received_timestamp_ms: int
    book_levels: Mapping[str, Any]


class QualityMarketEngine:
    """V002 exacto con evidencia explícita de cobertura y libros vacíos."""

    def __init__(self, contract: Any) -> None:
        self.contract = contract
        self.token_sides = {
            contract.up_token_id: "Up",
            contract.down_token_id: "Down",
        }
        self.books = {"Up": BookState(), "Down": BookState()}
        self.detector = LateWindowEdgeEngine(
            condition_id=contract.condition_id,
            market_start_ms=contract.market_start_ms,
            market_end_ms=contract.market_end_ms,
            fee=contract.fee,
        )
        self.samples: dict[int, dict[str, Any]] = {}
        self.execution: ExecutionRecord | None = None
        self.message_count = 0
        self.malformed_or_control_count = 0
        self.recognized_event_count = 0
        self.stale_event_count = 0
        self.price_change_count = 0
        self.disconnect_count = 0
        self.last_event_source_timestamp_ms: int | None = None

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

    @staticmethod
    def _stored_levels(levels: Mapping[float, float]) -> list[list[float]]:
        return [[price, size] for price, size in sorted(levels.items()) if price <= 0.97][:50]

    def invalidate_books(self) -> None:
        for book in self.books.values():
            book.invalidate()
        self.disconnect_count += 1

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
        changes = payload.get("price_changes")
        if not isinstance(changes, list):
            return False
        timestamp_ms = self._source_timestamp(payload, fallback)
        changed = False
        for item in changes:
            if not isinstance(item, Mapping) or str(item.get("side") or "").upper() != "SELL":
                continue
            side = self.token_sides.get(str(item.get("asset_id") or ""))
            price = _number(item.get("price"))
            size = _number(item.get("size"))
            if side is None or price is None or size is None or not self.books[side].initialized:
                continue
            self.books[side].change(price, size, timestamp_ms)
            changed = True
        if changed:
            self.price_change_count += 1
        return changed

    def _observe(
        self,
        received_ms: int,
        *,
        connected: bool,
        trigger_source_timestamp_ms: int | None,
    ) -> None:
        elapsed = (received_ms - self.contract.market_start_ms) / 1000.0
        if elapsed < CAPTURE_START_SECONDS or elapsed >= MARKET_END_SECONDS:
            return
        up = self.books["Up"]
        down = self.books["Down"]
        up_ask = min(up.asks) if up.asks else None
        down_ask = min(down.asks) if down.asks else None
        valid = bool(connected and up.initialized and down.initialized)
        trigger_age = (
            received_ms - trigger_source_timestamp_ms
            if trigger_source_timestamp_ms is not None
            else None
        )
        candidate = any(
            ask is not None and abs(ask - price) <= 1e-9
            for ask in (up_ask, down_ask)
            for price in (0.90, 0.91, 0.92, 0.93, 0.94, 0.95)
        )
        bucket = received_ms // SAMPLE_BUCKET_MS * SAMPLE_BUCKET_MS
        self.samples[bucket] = {
            "condition_id": self.contract.condition_id,
            "bucket_ms": bucket,
            "received_timestamp_ms": received_ms,
            "seconds_elapsed": elapsed,
            "connected": int(connected),
            "up_initialized": int(up.initialized),
            "down_initialized": int(down.initialized),
            "up_best_ask": up_ask,
            "down_best_ask": down_ask,
            "up_book_empty": int(up.initialized and not up.asks),
            "down_book_empty": int(down.initialized and not down.asks),
            "up_source_timestamp_ms": up.source_timestamp_ms,
            "down_source_timestamp_ms": down.source_timestamp_ms,
            "trigger_source_timestamp_ms": trigger_source_timestamp_ms,
            "trigger_event_age_ms": trigger_age,
            "valid": int(valid),
            "potential_candidate": int(candidate),
            "up_asks": self._stored_levels(up.asks),
            "down_asks": self._stored_levels(down.asks),
        }
        if not valid or self.execution is not None:
            return
        decision = self.detector.observe(
            timestamp_ms=received_ms,
            up_ask=up_ask,
            down_ask=down_ask,
        )
        if decision is None:
            return
        levels = {"Up": self._stored_levels(up.asks), "Down": self._stored_levels(down.asks)}
        execution_status = "NOT_ATTEMPTED"
        vwap = shares = fee = total_debit = None
        if decision.status == "SIGNAL" and decision.side and decision.ask is not None:
            fill = exact_book_fill(
                self.books[decision.side].asks,
                maximum_price=decision.ask,
                shares=5.0,
            )
            if fill is None:
                execution_status = "FAILED_INSUFFICIENT_EXACT_DEPTH"
            else:
                vwap, shares = fill
                fee = self.contract.fee.fee_per_share(vwap) * shares
                total_debit = vwap * shares + fee
                execution_status = "SIMULATED_FILL_LEVEL_A"
        timestamps = [
            value
            for value in (up.source_timestamp_ms, down.source_timestamp_ms)
            if value is not None
        ]
        self.execution = ExecutionRecord(
            decision=decision,
            execution_status=execution_status,
            fill_vwap=vwap,
            fill_shares=shares,
            fee=fee,
            total_debit=total_debit,
            book_source_timestamp_ms=max(timestamps) if timestamps else None,
            book_received_timestamp_ms=received_ms,
            book_levels=levels,
        )

    def ingest(self, raw: str, received_ms: int, *, connected: bool = True) -> None:
        self.message_count += 1
        try:
            stream, source_timestamp_ms, _, _ = parse_message_metadata(raw, "market")
            payload = json.loads(raw)
        except (ValueError, TypeError, json.JSONDecodeError):
            self.malformed_or_control_count += 1
            return
        fallback = int(source_timestamp_ms or received_ms)
        event_delay = int(received_ms) - fallback
        if not -MAXIMUM_FUTURE_CLOCK_SKEW_MS <= event_delay <= MAXIMUM_EVENT_DELAY_MS:
            self.stale_event_count += 1
            return
        changed = False
        if stream == "book":
            changed = self._book(payload, fallback)
        elif stream == "price_change":
            changed = self._price_change(payload, fallback)
        if not changed:
            return
        self.recognized_event_count += 1
        self.last_event_source_timestamp_ms = fallback
        self._observe(
            int(received_ms),
            connected=connected,
            trigger_source_timestamp_ms=fallback,
        )

    def tick(self, received_ms: int, *, connected: bool) -> None:
        self._observe(
            int(received_ms),
            connected=connected,
            trigger_source_timestamp_ms=self.last_event_source_timestamp_ms,
        )

    def quality(self, monitor_started_ms: int, end_reached: bool) -> dict[str, Any]:
        start_bucket = (self.contract.market_start_ms + DECISION_START_SECONDS * 1000) // SAMPLE_BUCKET_MS
        expected = [
            (start_bucket + index) * SAMPLE_BUCKET_MS
            for index in range(EXPECTED_DECISION_BUCKETS)
        ]
        valid_flags = [bool(self.samples.get(bucket, {}).get("valid")) for bucket in expected]
        valid_count = sum(valid_flags)
        longest = current = 0
        for valid in valid_flags:
            current = 0 if valid else current + 1
            longest = max(longest, current)
        maximum_gap_ms = longest * SAMPLE_BUCKET_MS
        coverage = valid_count / EXPECTED_DECISION_BUCKETS
        fully_observed = (
            monitor_started_ms <= self.contract.market_start_ms + CAPTURE_START_SECONDS * 1000
            and end_reached
        )
        reasons: list[str] = []
        if not fully_observed:
            reasons.append("timing_incomplete")
        if self.books["Up"].snapshot_count < 1:
            reasons.append("up_book_never_initialized")
        if self.books["Down"].snapshot_count < 1:
            reasons.append("down_book_never_initialized")
        if self.recognized_event_count < 1:
            reasons.append("no_recognized_book_events")
        if valid_count < MINIMUM_VALID_DECISION_BUCKETS:
            reasons.append("decision_coverage_below_95pct")
        if maximum_gap_ms > MAXIMUM_INVALID_GAP_MS:
            reasons.append("invalid_gap_above_1000ms")
        decision_samples = [self.samples.get(bucket) for bucket in expected]
        return {
            "status": "COMPLETE_QUALITY" if not reasons else "DATA_INCOMPLETE",
            "quality_reason": "ok" if not reasons else ";".join(reasons),
            "decision_window_buckets": EXPECTED_DECISION_BUCKETS,
            "valid_decision_buckets": valid_count,
            "decision_coverage": coverage,
            "maximum_invalid_gap_ms": maximum_gap_ms,
            "empty_up_buckets": sum(bool(item and item["up_book_empty"]) for item in decision_samples),
            "empty_down_buckets": sum(bool(item and item["down_book_empty"]) for item in decision_samples),
        }


class V003Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.connection: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("V003Store cerrado")
        return self.connection

    def open(self, prereg: Mapping[str, Any], prereg_path: Path) -> None:
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
                "target_quality_markets": int(prereg["target_quality_markets"]),
                "prereg_sha256": sha256_file(prereg_path),
                "wallet_required": False,
                "orders_enabled": False,
                "paper_orders": 0,
                "orders_sent": 0,
                "outcomes_read": 0,
                "real_money": "BLOQUEADO",
            }
            for key, value in values.items():
                self.db.execute("INSERT INTO v003_meta VALUES(?,?)", (key, _json(value)))
            self.db.commit()
        else:
            if meta.get("schema") != DATABASE_SCHEMA:
                raise V003Error("Base V003 incompatible")
            if meta.get("prereg_sha256") != sha256_file(prereg_path):
                raise V003Error("Base V003 no coincide con su prerregistro")
            if meta.get("orders_enabled") is not False or meta.get("real_money") != "BLOQUEADO":
                raise V003Error("Contrato de seguridad V003 inválido")

    def meta(self) -> dict[str, Any]:
        if self.connection is None:
            return {}
        return {str(k): json.loads(str(v)) for k, v in self.db.execute("SELECT key,value FROM v003_meta")}

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO v003_meta VALUES(?,?)", (key, _json(value)))
        self.db.commit()

    def start_run(self) -> int:
        self.db.execute(
            "UPDATE v003_runs SET finished_at=?,status='ABORTED_UNCLEAN' WHERE status='RUNNING'",
            (utc_now(),),
        )
        self.db.execute(
            "UPDATE v003_markets SET completed_at=?,status='ABORTED_UNCLEAN',quality_reason='process_ended_before_market_close' WHERE status='COLLECTING'",
            (utc_now(),),
        )
        cursor = self.db.execute(
            "INSERT INTO v003_runs(started_at,status) VALUES(?,'RUNNING')", (utc_now(),)
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, error: str | None) -> None:
        self.db.execute(
            "UPDATE v003_runs SET finished_at=?,status=?,error=? WHERE run_id=?",
            (utc_now(), status, error, run_id),
        )
        self.db.commit()

    def quality_markets(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM v003_markets WHERE status='COMPLETE_QUALITY'").fetchone()[0])

    def market_seen(self, slug: str) -> bool:
        return self.db.execute("SELECT 1 FROM v003_markets WHERE slug=?", (slug,)).fetchone() is not None

    def start_market(self, contract: Any, monitor_started_ms: int) -> None:
        self.db.execute(
            """INSERT INTO v003_markets(
             condition_id,slug,market_id,market_start_ms,market_end_ms,monitor_started_ms,status,
             resolution_source,tick_size,minimum_order_shares,fee_rate,fee_exponent
             ) VALUES(?,?,?,?,?,?,'COLLECTING',?,?,?,?,?)""",
            (
                contract.condition_id, contract.slug, contract.market_id,
                contract.market_start_ms, contract.market_end_ms, monitor_started_ms,
                contract.resolution_source, contract.tick_size, contract.minimum_order_shares,
                contract.fee.rate, contract.fee.exponent,
            ),
        )
        self.db.commit()

    def finish_market(
        self,
        engine: QualityMarketEngine,
        monitor_started_ms: int,
        end_reached: bool,
    ) -> None:
        quality = engine.quality(monitor_started_ms, end_reached)
        self.db.executemany(
            "INSERT OR REPLACE INTO v003_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    item["condition_id"], item["bucket_ms"], item["received_timestamp_ms"],
                    item["seconds_elapsed"], item["connected"], item["up_initialized"],
                    item["down_initialized"], item["up_best_ask"], item["down_best_ask"],
                    item["up_book_empty"], item["down_book_empty"],
                    item["up_source_timestamp_ms"], item["down_source_timestamp_ms"],
                    item["trigger_source_timestamp_ms"], item["trigger_event_age_ms"],
                    item["valid"], item["potential_candidate"], _json(item["up_asks"]),
                    _json(item["down_asks"]),
                )
                for item in engine.samples.values()
            ],
        )
        decision_status = "NO_CANDIDATE"
        candidate_price = None
        fill_status = "NO_FILL"
        if engine.execution is not None:
            record = engine.execution
            decision = record.decision
            decision_status = decision.status
            candidate_price = decision.ask
            fill_status = record.execution_status
            self.db.execute(
                "INSERT OR REPLACE INTO v003_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            """UPDATE v003_markets SET completed_at=?,status=?,quality_reason=?,message_count=?,
             malformed_or_control_count=?,recognized_event_count=?,stale_event_count=?,up_book_snapshot_count=?,
             down_book_snapshot_count=?,price_change_count=?,disconnect_count=?,sample_count=?,
             decision_window_buckets=?,valid_decision_buckets=?,decision_coverage=?,
             maximum_invalid_gap_ms=?,empty_up_buckets=?,empty_down_buckets=?,decision_status=?,
             candidate_price=?,simulated_fill_status=?,paper_orders=0,orders_sent=0,
             outcomes_read=0,real_money=0 WHERE condition_id=?""",
            (
                utc_now(), quality["status"], quality["quality_reason"], engine.message_count,
                engine.malformed_or_control_count, engine.recognized_event_count,
                engine.stale_event_count,
                engine.books["Up"].snapshot_count, engine.books["Down"].snapshot_count,
                engine.price_change_count, engine.disconnect_count, len(engine.samples),
                quality["decision_window_buckets"], quality["valid_decision_buckets"],
                quality["decision_coverage"], quality["maximum_invalid_gap_ms"],
                quality["empty_up_buckets"], quality["empty_down_buckets"],
                decision_status, candidate_price, fill_status, engine.contract.condition_id,
            ),
        )
        self.db.commit()

    def save_health(self, state: LiveShadowState, counters: Mapping[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO v003_health VALUES(?,?,?,?)",
            (utc_now(), os.getpid(), _json(state.connections), _json(counters)),
        )
        self.db.commit()

    def close(self) -> None:
        if self.connection is not None:
            self.db.commit()
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.close()
            self.connection = None


def _code_paths() -> tuple[Path, ...]:
    return (
        ROOT / "src" / "polymarket_bot" / "btc_5m_90" / "v002.py",
        ROOT / "src" / "polymarket_bot" / "btc_5m_90" / "v002_shadow.py",
        Path(__file__).resolve(),
        ROOT / "btc5m90_v003_shadow.py",
    )


def build_prereg() -> dict[str, Any]:
    base = load_v002_prereg(V002_PREREG_PATH)
    return {
        "schema": PREREG_SCHEMA,
        "status": "FROZEN_V003_SHADOW_ONLY",
        "created_at": utc_now(),
        "target_quality_markets": TARGET_QUALITY_MARKETS,
        "expected_capture_hours": 23.0,
        "maximum_hours": MAXIMUM_HOURS,
        "recovery_reserve_hours": 1.0,
        "strategy": base["strategy"],
        "execution": {
            "full_book_required": True,
            "fill_requires_exact_depth": True,
            "shares": 5.0,
            "maximum_event_delay_ms": MAXIMUM_EVENT_DELAY_MS,
            "maximum_future_clock_skew_ms": MAXIMUM_FUTURE_CLOCK_SKEW_MS,
            "persistent_book_valid_while_connection_is_continuous": True,
        },
        "quality_contract": {
            "capture_window_seconds": [CAPTURE_START_SECONDS, MARKET_END_SECONDS],
            "decision_window_seconds": [DECISION_START_SECONDS, MARKET_END_SECONDS],
            "sample_bucket_ms": SAMPLE_BUCKET_MS,
            "expected_decision_buckets": EXPECTED_DECISION_BUCKETS,
            "minimum_valid_decision_buckets": MINIMUM_VALID_DECISION_BUCKETS,
            "minimum_decision_coverage": MINIMUM_DECISION_COVERAGE,
            "maximum_invalid_gap_ms": MAXIMUM_INVALID_GAP_MS,
            "both_books_must_initialize": True,
            "empty_books_are_explicit_valid_states": True,
            "missing_evidence_status": "DATA_INCOMPLETE",
            "data_incomplete_excluded_from_primary_analysis": True,
        },
        "gates": {
            "minimum_quality_markets": TARGET_QUALITY_MARKETS,
            "minimum_signals": MINIMUM_SIGNALS,
            "minimum_execution_rate": 0.80,
            "minimum_realized_edge_per_share": 0.002,
            "positive_net_pnl": True,
            "pooled_wilson_95_low_above_pooled_break_even": True,
        },
        "evaluation": {
            "outcomes_opened_only_after_capture": True,
            "evaluate_v003_separately": True,
            "pool_only_exact_depth_trades_from_quality_markets": True,
            "price_and_side_slices_diagnostic_only": True,
            "no_posthoc_rule_change": True,
            "no_automatic_live_promotion": True,
        },
        "safety": {
            "wallet_required": False,
            "orders_enabled": False,
            "paper_orders": False,
            "outcomes_during_capture": False,
            "real_money": "BLOQUEADO",
        },
        "sources": {
            "v002_prereg_sha256": sha256_file(V002_PREREG_PATH),
            "v002_database_sha256": sha256_file(V002_DATABASE_PATH),
            "lesson": "V002 COMPLETE did not prove decision-window coverage; V003 does.",
        },
        "code_sha256": {
            str(path.relative_to(ROOT)).replace("\\", "/"): sha256_file(path)
            for path in _code_paths()
        },
    }


def freeze_prereg(path: str | Path = PREREG_PATH) -> dict[str, Any]:
    target = Path(path).resolve()
    if target.exists():
        raise V003Error("El prerregistro V003 ya existe y no se sobrescribe")
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


def load_and_verify_prereg(path: str | Path = PREREG_PATH) -> dict[str, Any]:
    target = Path(path).resolve()
    payload = json.loads(target.read_text(encoding="utf-8"))
    if payload.get("schema") != PREREG_SCHEMA or payload.get("status") != "FROZEN_V003_SHADOW_ONLY":
        raise V003Error("Prerregistro V003 incompatible")
    if int(payload.get("target_quality_markets", 0)) != TARGET_QUALITY_MARKETS:
        raise V003Error("Cambió el target V003")
    if float(payload.get("maximum_hours", 0.0)) != MAXIMUM_HOURS:
        raise V003Error("Cambió el máximo de 24 horas")
    expected_quality = build_prereg()["quality_contract"]
    if payload.get("quality_contract") != expected_quality:
        raise V003Error("Cambió el contrato de calidad V003")
    expected_safety = build_prereg()["safety"]
    if payload.get("safety") != expected_safety:
        raise V003Error("Cambió el contrato de seguridad V003")
    expected_files = {
        "src/polymarket_bot/btc_5m_90/v002.py",
        "src/polymarket_bot/btc_5m_90/v002_shadow.py",
        "src/polymarket_bot/btc_5m_90/v003_shadow.py",
        "btc5m90_v003_shadow.py",
    }
    if set(payload.get("code_sha256", {})) != expected_files:
        raise V003Error("Inventario de código V003 incompleto")
    for relative, expected in payload["code_sha256"].items():
        if sha256_file(ROOT / relative) != expected:
            raise V003Error(f"Código V003 cambió: {relative}")
    if payload.get("strategy") != load_v002_prereg(V002_PREREG_PATH).get("strategy"):
        raise V003Error("La estrategia dejó de ser idéntica a V002")
    expected_sources = build_prereg()["sources"]
    if payload.get("sources") != expected_sources:
        raise V003Error("Cambió una fuente congelada de V003")
    return payload


async def _run(settings: Settings, prereg: Mapping[str, Any], prereg_path: Path, database: Path) -> dict[str, Any]:
    store = V003Store(database)
    store.open(prereg, prereg_path)
    run_id = store.start_run()
    meta = store.meta()
    maximum_end = datetime.fromisoformat(str(meta["maximum_end_at"]).replace("Z", "+00:00")).timestamp()
    target = int(meta["target_quality_markets"])
    state = LiveShadowState()
    run_status = "RUNNING"
    error: str | None = None
    counters: dict[str, Any] = {"markets_attempted": 0, "messages": 0, "data_incomplete": 0}
    try:
        while store.quality_markets() < target and time.time() < maximum_end:
            now = time.time()
            block = int(now) - int(now) % 300
            target_start = block if now < block + 225 else block + 300
            await asyncio.sleep(max(0.0, target_start + 1 - time.time()))
            slug = f"btc-updown-5m-{target_start}"
            if store.market_seen(slug):
                await asyncio.sleep(max(0.0, target_start + 300.1 - time.time()))
                continue
            try:
                contract = await _discover_contract(slug, min(maximum_end, time.time() + 40))
            except ShadowError as exc:
                counters["discovery_failures"] = counters.get("discovery_failures", 0) + 1
                counters["last_discovery_error"] = str(exc)
                store.save_health(state, counters)
                logging.getLogger("btc5m90-v003").warning("%s", exc)
                await asyncio.sleep(max(0.0, target_start + 300.1 - time.time()))
                continue
            monitor_started_ms = int(time.time() * 1000)
            store.start_market(contract, monitor_started_ms)
            counters["markets_attempted"] += 1
            engine = QualityMarketEngine(contract)
            stop = asyncio.Event()
            name = f"btc5m90-v003-{slug}"

            async def handler(raw: str) -> None:
                engine.ingest(raw, int(time.time() * 1000), connected=True)

            task = asyncio.create_task(
                _websocket_feed(
                    name=name,
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
            previous_connected = False
            next_health = time.monotonic()
            try:
                while time.time() < end:
                    await asyncio.sleep(min(0.25, max(0.05, end - time.time())))
                    connected = state.connections.get(name) == "CONNECTED"
                    if previous_connected and not connected:
                        engine.invalidate_books()
                    previous_connected = connected
                    engine.tick(int(time.time() * 1000), connected=connected)
                    counters["messages"] = engine.message_count
                    if time.monotonic() >= next_health:
                        store.save_health(state, counters)
                        next_health = time.monotonic() + 5.0
            finally:
                stop.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            end_reached = time.time() >= contract.market_end_ms / 1000
            store.finish_market(engine, monitor_started_ms, end_reached)
            latest_status = store.db.execute(
                "SELECT status FROM v003_markets WHERE condition_id=?",
                (contract.condition_id,),
            ).fetchone()[0]
            if latest_status == "DATA_INCOMPLETE":
                counters["data_incomplete"] += 1
        if store.quality_markets() >= target:
            store.set_meta("experiment_completed_at", utc_now())
            run_status = "COMPLETED"
        else:
            store.set_meta("experiment_stopped_at", utc_now())
            run_status = "MAXIMUM_TIME_REACHED"
    except asyncio.CancelledError:
        run_status = "INTERRUPTED"
        error = "CancelledError: ejecución interrumpida"
        raise
    except Exception as exc:
        run_status = "FAILED"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        quality_markets = store.quality_markets()
        quick = str(store.db.execute("PRAGMA quick_check").fetchone()[0])
        store.finish_run(run_id, run_status, error)
        store.close()
    return {
        "status": run_status,
        "error": error,
        "quality_markets": quality_markets,
        "target_quality_markets": target,
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
        return await _run(settings, prereg, resolved_prereg, Path(database).resolve())
    finally:
        lock.release()


def status(database: str | Path = DATABASE_PATH) -> dict[str, Any]:
    path = Path(database).resolve()
    if not path.is_file():
        return {
            "status": "NOT_STARTED", "database": str(path), "quality_markets": 0,
            "wallet_required": False, "orders_sent": 0, "outcomes_read": 0,
            "real_money": "BLOQUEADO",
        }
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        meta = {str(k): json.loads(str(v)) for k, v in connection.execute("SELECT key,value FROM v003_meta")}
        counts = dict(connection.execute(
            """SELECT COUNT(*) markets_seen,
             COALESCE(SUM(status='COMPLETE_QUALITY'),0) quality_markets,
             COALESCE(SUM(status='DATA_INCOMPLETE'),0) data_incomplete,
             COALESCE(SUM(status='ABORTED_UNCLEAN'),0) aborted_unclean,
             COALESCE(SUM(status='COLLECTING'),0) collecting,
             COALESCE(SUM(decision_status='SIGNAL' AND status='COMPLETE_QUALITY'),0) approved_signals,
             COALESCE(SUM(decision_status='REJECTED_EDGE' AND status='COMPLETE_QUALITY'),0) rejected_edge,
             COALESCE(SUM(simulated_fill_status='SIMULATED_FILL_LEVEL_A' AND status='COMPLETE_QUALITY'),0) level_a_fills,
             COALESCE(SUM(simulated_fill_status='FAILED_INSUFFICIENT_EXACT_DEPTH' AND status='COMPLETE_QUALITY'),0) failed_depth,
             COALESCE(SUM(paper_orders),0) paper_orders,COALESCE(SUM(orders_sent),0) orders_sent,
             COALESCE(SUM(outcomes_read),0) outcomes_read,COALESCE(SUM(real_money),0) real_money,
             COALESCE(AVG(CASE WHEN status='COMPLETE_QUALITY' THEN decision_coverage END),0) avg_coverage,
             COALESCE(MIN(CASE WHEN status='COMPLETE_QUALITY' THEN decision_coverage END),0) min_coverage,
             COALESCE(MAX(maximum_invalid_gap_ms),0) max_invalid_gap_ms
             FROM v003_markets"""
        ).fetchone())
        latest_run = connection.execute(
            "SELECT started_at,finished_at,status,error FROM v003_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        latest_health = connection.execute(
            "SELECT recorded_at,process_id,connections_json,counters_json FROM v003_health ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        connection.close()
    maximum_end = datetime.fromisoformat(str(meta["maximum_end_at"]).replace("Z", "+00:00")).timestamp()
    health_age = None
    if latest_health:
        health_age = time.time() - datetime.fromisoformat(str(latest_health[0]).replace("Z", "+00:00")).timestamp()
    if meta.get("experiment_completed_at"):
        current_status = "COMPLETED"
    elif latest_run and latest_run[2] == "FAILED":
        current_status = "FAILED"
    elif maximum_end <= time.time():
        current_status = "MAXIMUM_TIME_REACHED"
    elif latest_run and latest_run[2] == "RUNNING" and health_age is not None and health_age <= 20:
        current_status = "RUNNING_HEALTHY"
    elif latest_run and latest_run[2] == "RUNNING":
        current_status = "STOPPED_UNCLEAN"
    else:
        current_status = "RESUMABLE"
    target = int(meta["target_quality_markets"])
    quality_markets = int(counts["quality_markets"])
    return {
        "status": current_status,
        "database": str(path),
        "experiment_started_at": meta.get("experiment_started_at"),
        "maximum_end_at": meta.get("maximum_end_at"),
        "remaining_hours_max": max(0.0, maximum_end - time.time()) / 3600.0,
        "markets_seen": int(counts["markets_seen"]),
        "quality_markets": quality_markets,
        "target_quality_markets": target,
        "quality_markets_remaining": max(0, target - quality_markets),
        "data_incomplete": int(counts["data_incomplete"]),
        "aborted_unclean": int(counts["aborted_unclean"]),
        "collecting": int(counts["collecting"]),
        "approved_signals": int(counts["approved_signals"]),
        "rejected_edge": int(counts["rejected_edge"]),
        "simulated_level_a_fills": int(counts["level_a_fills"]),
        "failed_exact_depth": int(counts["failed_depth"]),
        "average_decision_coverage": float(counts["avg_coverage"]),
        "minimum_decision_coverage": float(counts["min_coverage"]),
        "maximum_invalid_gap_ms": int(counts["max_invalid_gap_ms"]),
        "health_age_seconds": health_age,
        "latest_health": (
            {
                "recorded_at": latest_health[0], "process_id": latest_health[1],
                "connections": json.loads(latest_health[2]), "counters": json.loads(latest_health[3]),
            }
            if latest_health else None
        ),
        "latest_run": (
            {"started_at": latest_run[0], "finished_at": latest_run[1], "status": latest_run[2], "error": latest_run[3]}
            if latest_run else None
        ),
        "sqlite_quick_check": quick,
        "wallet_required": False,
        "paper_orders": int(counts["paper_orders"]),
        "orders_sent": int(counts["orders_sent"]),
        "outcomes_read": int(counts["outcomes_read"]),
        "real_money_rows": int(counts["real_money"]),
        "real_money": "BLOQUEADO",
    }


async def run_smoke(settings: Settings, seconds: float = 15.0) -> dict[str, Any]:
    if seconds <= 0 or seconds > 120:
        raise ValueError("Smoke debe durar 1–120 segundos")
    now = int(time.time())
    start = now - now % 300
    if now >= start + 285:
        start += 300
        await asyncio.sleep(max(0.0, start + 1 - time.time()))
    slug = f"btc-updown-5m-{start}"
    contract = await _discover_contract(slug, time.time() + 30)
    engine = QualityMarketEngine(contract)
    state = LiveShadowState()
    stop = asyncio.Event()
    name = "btc5m90-v003-smoke"

    async def handler(raw: str) -> None:
        engine.ingest(raw, int(time.time() * 1000), connected=True)

    task = asyncio.create_task(
        _websocket_feed(
            name=name, endpoint=settings.clob_ws_url,
            subscription={"assets_ids": [contract.up_token_id, contract.down_token_id], "type": "market", "custom_feature_enabled": True},
            heartbeat_text="PING", heartbeat_seconds=10.0,
            use_proxy=settings.ws_use_proxy, reconnect_max_seconds=settings.reconnect_max_seconds,
            stop_event=stop, state=state, handler=handler,
        )
    )
    try:
        await asyncio.sleep(seconds)
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    both_initialized = all(book.snapshot_count > 0 for book in engine.books.values())
    return {
        "status": "SMOKE_OK" if both_initialized and engine.recognized_event_count > 0 else "SMOKE_NO_BOOK",
        "slug": slug,
        "messages": engine.message_count,
        "recognized_events": engine.recognized_event_count,
        "up_book_snapshots": engine.books["Up"].snapshot_count,
        "down_book_snapshots": engine.books["Down"].snapshot_count,
        "both_books_initialized": both_initialized,
        "wallet_required": False, "orders_sent": 0, "outcomes_read": 0,
        "real_money": "BLOQUEADO",
    }
