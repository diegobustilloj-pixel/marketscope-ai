from __future__ import annotations

import json
import shutil
import sqlite3
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.phase2 import SilverMarket
from polymarket_bot.resolution_contract import ResolutionTwapContract
from polymarket_bot.v031_capture import V031CaptureState, database_footprint, json_compact, utc_now


V033_SCHEMA_VERSION = "1"
V033_CODE_VERSION = "0.9.5a1-v033-fresh-exit-safety"
V033_CAPTURE_HOURS = 4.0
V033_EXPECTED_MARKETS = 48
V033_MARKET_SECONDS = 300
V033_SUPPORTED_TWAP_WINDOWS = (30, 60)
V033_HEALTH_INTERVAL_SECONDS = 30.0
V033_MAX_DATABASE_GB = 1.0
V033_MINIMUM_FREE_GB = 20.0


V033_DDL = """
CREATE TABLE v033_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE v033_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    error TEXT
);
CREATE TABLE v033_markets (
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
    capture_error TEXT,
    probe_total INTEGER,
    probe_entered INTEGER,
    probe_guard_exits INTEGER,
    probe_exit_successes INTEGER,
    probe_trapped INTEGER
);
CREATE INDEX idx_v033_markets_start ON v033_markets(market_start_ms);
CREATE TABLE v033_snapshots (
    condition_id TEXT NOT NULL,
    second_offset INTEGER NOT NULL,
    snapshot_timestamp_ms INTEGER NOT NULL,
    recorded_timestamp_ms INTEGER NOT NULL,
    chainlink_source_timestamp_ms INTEGER,
    chainlink_received_timestamp_ms INTEGER,
    chainlink_age_ms INTEGER,
    chainlink_fresh INTEGER NOT NULL,
    official_twap_window_s INTEGER NOT NULL,
    official_twap_source_timestamp_ms INTEGER,
    official_twap_received_timestamp_ms INTEGER,
    official_twap_age_ms INTEGER,
    official_twap_fresh INTEGER NOT NULL,
    up_book_source_timestamp_ms INTEGER,
    up_book_received_timestamp_ms INTEGER,
    up_book_age_ms INTEGER,
    up_book_fresh INTEGER NOT NULL,
    down_book_source_timestamp_ms INTEGER,
    down_book_received_timestamp_ms INTEGER,
    down_book_age_ms INTEGER,
    down_book_fresh INTEGER NOT NULL,
    up_bid_depth_top5 REAL NOT NULL,
    up_ask_depth_top5 REAL NOT NULL,
    down_bid_depth_top5 REAL NOT NULL,
    down_ask_depth_top5 REAL NOT NULL,
    complete_v2 INTEGER NOT NULL,
    PRIMARY KEY(condition_id,second_offset),
    FOREIGN KEY(condition_id) REFERENCES v033_markets(condition_id)
);
CREATE INDEX idx_v033_snapshots_time ON v033_snapshots(snapshot_timestamp_ms);
CREATE TABLE v033_health (
    recorded_at TEXT PRIMARY KEY,
    database_bytes INTEGER NOT NULL,
    free_bytes INTEGER NOT NULL,
    counters_json TEXT NOT NULL,
    connections_json TEXT NOT NULL
);
"""


def technical_snapshot(
    state: V031CaptureState,
    *,
    condition_id: str,
    second_offset: int,
    snapshot_timestamp_ms: int,
    official_twap_window_s: int,
    recorded_timestamp_ms: int | None = None,
) -> dict[str, Any]:
    raw = state.snapshot(
        condition_id=condition_id,
        second_offset=second_offset,
        snapshot_timestamp_ms=snapshot_timestamp_ms,
        official_twap_window_s=official_twap_window_s,
        recorded_timestamp_ms=recorded_timestamp_ms,
    )
    complete_v2 = bool(
        raw["chainlink_fresh"]
        and raw["official_twap_fresh"]
        and int(raw["official_twap_window_s"]) == int(official_twap_window_s)
        and raw["up_book_source_timestamp_ms"] is not None
        and raw["up_book_received_timestamp_ms"] is not None
        and raw["up_book_fresh"]
        and raw["down_book_source_timestamp_ms"] is not None
        and raw["down_book_received_timestamp_ms"] is not None
        and raw["down_book_fresh"]
    )
    return {
        "condition_id": str(condition_id),
        "second_offset": int(second_offset),
        "snapshot_timestamp_ms": int(snapshot_timestamp_ms),
        "recorded_timestamp_ms": int(raw["recorded_timestamp_ms"]),
        "chainlink_source_timestamp_ms": raw["chainlink_source_timestamp_ms"],
        "chainlink_received_timestamp_ms": raw["chainlink_received_timestamp_ms"],
        "chainlink_age_ms": raw["chainlink_age_ms"],
        "chainlink_fresh": bool(raw["chainlink_fresh"]),
        "official_twap_window_s": int(official_twap_window_s),
        "official_twap_source_timestamp_ms": raw["official_twap_source_timestamp_ms"],
        "official_twap_received_timestamp_ms": raw["official_twap_received_timestamp_ms"],
        "official_twap_age_ms": raw["official_twap_age_ms"],
        "official_twap_fresh": bool(raw["official_twap_fresh"]),
        "up_book_source_timestamp_ms": raw["up_book_source_timestamp_ms"],
        "up_book_received_timestamp_ms": raw["up_book_received_timestamp_ms"],
        "up_book_age_ms": raw["up_book_age_ms"],
        "up_book_fresh": bool(raw["up_book_fresh"]),
        "down_book_source_timestamp_ms": raw["down_book_source_timestamp_ms"],
        "down_book_received_timestamp_ms": raw["down_book_received_timestamp_ms"],
        "down_book_age_ms": raw["down_book_age_ms"],
        "down_book_fresh": bool(raw["down_book_fresh"]),
        "up_bid_depth_top5": float(raw["up_bid_depth_top5"]),
        "up_ask_depth_top5": float(raw["up_ask_depth_top5"]),
        "down_bid_depth_top5": float(raw["down_bid_depth_top5"]),
        "down_ask_depth_top5": float(raw["down_ask_depth_top5"]),
        "complete_v2": complete_v2,
    }


class V033Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.connection: sqlite3.Connection | None = None
        self._pending_writes = 0

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("La base V0.33 no esta abierta")
        return self.connection

    def open(
        self,
        *,
        preregistration_sha256: str,
        implementation_sha256: str,
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
            if "v033_meta" not in tables:
                raise ValueError("La base existente no pertenece a V0.33")
        else:
            self.connection.executescript(V033_DDL)
        meta = self.meta()
        if meta:
            expected = {
                "schema_version": V033_SCHEMA_VERSION,
                "preregistration_sha256": preregistration_sha256,
                "implementation_sha256": implementation_sha256,
                "launch_manifest_sha256": launch_manifest_sha256,
                "orders_enabled": False,
                "paper_orders_enabled": False,
                "wallet_required": False,
                "money_real_enabled": False,
                "real_money": "BLOQUEADO",
                "outcomes_read": 0,
                "prices_stored": False,
                "pnl_calculated": False,
                "signals_generated": False,
                "trades_generated": False,
            }
            for key, value in expected.items():
                if meta.get(key) != value:
                    raise ValueError(f"Base V0.33 incompatible o insegura: {key}")
        else:
            now = float(now_timestamp if now_timestamp is not None else time.time())
            capture_start = (int(now) // V033_MARKET_SECONDS + 1) * V033_MARKET_SECONDS
            target_end = capture_start + int(V033_CAPTURE_HOURS * 3600)
            values = {
                "schema_version": V033_SCHEMA_VERSION,
                "code_version": V033_CODE_VERSION,
                "variant": "V0.33_FRESH_EXIT_SAFETY_REPLICATION_4H",
                "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                "capture_start_at": datetime.fromtimestamp(capture_start, timezone.utc).isoformat(timespec="seconds"),
                "target_hours": V033_CAPTURE_HOURS,
                "target_end_at": datetime.fromtimestamp(target_end, timezone.utc).isoformat(timespec="seconds"),
                "expected_markets": V033_EXPECTED_MARKETS,
                "preregistration_sha256": preregistration_sha256,
                "implementation_sha256": implementation_sha256,
                "launch_manifest_sha256": launch_manifest_sha256,
                "completion_reason": None,
                "observation_ended_at": None,
                "orders_enabled": False,
                "paper_orders_enabled": False,
                "wallet_required": False,
                "money_real_enabled": False,
                "real_money": "BLOQUEADO",
                "outcomes_read": 0,
                "prices_stored": False,
                "pnl_calculated": False,
                "signals_generated": False,
                "trades_generated": False,
            }
            self.connection.executemany(
                "INSERT INTO v033_meta(key,value) VALUES(?,?)",
                [(key, json_compact(value)) for key, value in values.items()],
            )
            self.connection.commit()
        self.reconcile_interrupted_runs()

    def meta(self) -> dict[str, Any]:
        return {
            str(row[0]): json.loads(str(row[1]))
            for row in self.db.execute("SELECT key,value FROM v033_meta ORDER BY key")
        }

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO v033_meta(key,value) VALUES(?,?)",
            (key, json_compact(value)),
        )
        self.db.commit()

    def reconcile_interrupted_runs(self) -> None:
        self.db.execute(
            """
            UPDATE v033_runs SET finished_at=?,status='INTERRUPTED',
             error=COALESCE(error,'RECOVERED_STALE_RUNNING') WHERE status='RUNNING'
            """,
            (utc_now(),),
        )
        self.db.execute(
            """
            UPDATE v033_markets SET capture_status='INTERRUPTED',
             capture_error=COALESCE(capture_error,'RECOVERED_PARTIAL_MARKET')
            WHERE capture_status='CAPTURING'
            """
        )
        self.db.commit()

    def start_run(self) -> int:
        cursor = self.db.execute(
            "INSERT INTO v033_runs(started_at,status) VALUES(?,'RUNNING')",
            (utc_now(),),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, *, status: str, error: str | None) -> None:
        self.db.execute(
            "UPDATE v033_runs SET finished_at=?,status=?,error=? WHERE run_id=?",
            (utc_now(), status, error, run_id),
        )
        self.db.commit()

    def save_market(self, market: SilverMarket, *, contract: ResolutionTwapContract) -> bool:
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO v033_markets(
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
        return self.db.execute("SELECT 1 FROM v033_markets WHERE slug=?", (slug,)).fetchone() is not None

    def finish_market(
        self,
        condition_id: str,
        *,
        error: str | None,
        probe_summary: Mapping[str, Any] | None = None,
    ) -> None:
        summary = probe_summary or {}
        self.db.execute(
            """
            UPDATE v033_markets SET capture_status=?,capture_error=?,probe_total=?,
             probe_entered=?,probe_guard_exits=?,probe_exit_successes=?,probe_trapped=?
            WHERE condition_id=?
            """,
            (
                "COMPLETED" if error is None else "FAILED",
                error,
                summary.get("total_capacity_probes"),
                summary.get("entry_eligible_probes"),
                summary.get("proactive_guard_exits"),
                summary.get("exit_successes_within_grace"),
                summary.get("trapped_positions"),
                condition_id,
            ),
        )
        self.db.commit()

    def save_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        values = (
            snapshot["condition_id"], snapshot["second_offset"],
            snapshot["snapshot_timestamp_ms"], snapshot["recorded_timestamp_ms"],
            snapshot["chainlink_source_timestamp_ms"], snapshot["chainlink_received_timestamp_ms"],
            snapshot["chainlink_age_ms"], int(snapshot["chainlink_fresh"]),
            snapshot["official_twap_window_s"], snapshot["official_twap_source_timestamp_ms"],
            snapshot["official_twap_received_timestamp_ms"], snapshot["official_twap_age_ms"],
            int(snapshot["official_twap_fresh"]), snapshot["up_book_source_timestamp_ms"],
            snapshot["up_book_received_timestamp_ms"], snapshot["up_book_age_ms"],
            int(snapshot["up_book_fresh"]), snapshot["down_book_source_timestamp_ms"],
            snapshot["down_book_received_timestamp_ms"], snapshot["down_book_age_ms"],
            int(snapshot["down_book_fresh"]), snapshot["up_bid_depth_top5"],
            snapshot["up_ask_depth_top5"], snapshot["down_bid_depth_top5"],
            snapshot["down_ask_depth_top5"], int(snapshot["complete_v2"]),
        )
        placeholders = ",".join("?" for _ in values)
        self.db.execute(f"INSERT OR REPLACE INTO v033_snapshots VALUES({placeholders})", values)
        self._pending_writes += 1
        if self._pending_writes >= 10:
            self.db.commit()
            self._pending_writes = 0

    def load_market_snapshots(self, condition_id: str) -> dict[int, dict[str, Any]]:
        self.db.commit()
        return {
            int(row["second_offset"]): dict(row)
            for row in self.db.execute(
                "SELECT * FROM v033_snapshots WHERE condition_id=? ORDER BY second_offset",
                (condition_id,),
            )
        }

    def save_health(
        self,
        *,
        counters: Mapping[str, int],
        connections: Mapping[str, str],
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO v033_health VALUES(?,?,?,?,?)",
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
    "V033_CAPTURE_HOURS",
    "V033_EXPECTED_MARKETS",
    "V033_HEALTH_INTERVAL_SECONDS",
    "V033_MARKET_SECONDS",
    "V033_MAX_DATABASE_GB",
    "V033_MINIMUM_FREE_GB",
    "V033_SUPPORTED_TWAP_WINDOWS",
    "V033Store",
    "technical_snapshot",
]
