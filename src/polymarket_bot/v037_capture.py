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
from polymarket_bot.v031_capture import database_footprint, json_compact, utc_now
from polymarket_bot.v034_capture import V034_DDL


V037_CAPTURE_HOURS = 4.0
V037_EXPECTED_MARKETS = 48
V037_MARKET_SECONDS = 300
V037_SUPPORTED_TWAP_WINDOWS = (30, 60)
V037_HEALTH_INTERVAL_SECONDS = 30.0
V037_DDL = V034_DDL.replace("v034", "v037") + """
CREATE TABLE v037_clob_watchdog_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT,
 condition_id TEXT NOT NULL,
 slug TEXT NOT NULL,
 incident_id INTEGER,
 generation INTEGER NOT NULL,
 event_type TEXT NOT NULL,
 recorded_timestamp_ms INTEGER NOT NULL,
 second_offset INTEGER NOT NULL,
 up_book_age_ms INTEGER,
 down_book_age_ms INTEGER,
 recovery_ms INTEGER,
 FOREIGN KEY(condition_id) REFERENCES v037_markets(condition_id)
);
CREATE INDEX idx_v037_watchdog_market ON v037_clob_watchdog_events(condition_id,event_id);
"""


class V037Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.connection: sqlite3.Connection | None = None
        self.pending = 0

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("Base V0.37 cerrada")
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
                row[0]
                for row in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "v037_meta" not in tables or "v037_clob_watchdog_events" not in tables:
                raise ValueError("Base no pertenece a V0.37")
        else:
            self.connection.executescript(V037_DDL)
        meta = self.meta()
        if not meta:
            now = float(now_timestamp if now_timestamp is not None else time.time())
            start = (int(now) // 300 + 1) * 300
            values = {
                "schema_version": "1",
                "variant": "V0.37_FRESH_CLOB_RECOVERY_4H",
                "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                "capture_start_at": datetime.fromtimestamp(start, timezone.utc).isoformat(timespec="seconds"),
                "target_end_at": datetime.fromtimestamp(start + 14400, timezone.utc).isoformat(timespec="seconds"),
                "target_hours": 4.0,
                "expected_markets": 48,
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
            self.db.executemany(
                "INSERT INTO v037_meta(key,value) VALUES(?,?)",
                [(key, json_compact(value)) for key, value in values.items()],
            )
            self.db.commit()
            meta = self.meta()
        expected = {
            "variant": "V0.37_FRESH_CLOB_RECOVERY_4H",
            "preregistration_sha256": preregistration_sha256,
            "implementation_sha256": implementation_sha256,
            "launch_manifest_sha256": launch_manifest_sha256,
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
            "outcomes_read": 0,
            "prices_stored": False,
            "pnl_calculated": False,
        }
        for key, value in expected.items():
            if meta.get(key) != value:
                raise ValueError(f"Base V0.37 incompatible: {key}")
        self.db.execute(
            "UPDATE v037_runs SET finished_at=?,status='INTERRUPTED',error=COALESCE(error,'RECOVERED_STALE_RUNNING') WHERE status='RUNNING'",
            (utc_now(),),
        )
        self.db.execute(
            "UPDATE v037_markets SET capture_status='INTERRUPTED',capture_error=COALESCE(capture_error,'RECOVERED_PARTIAL_MARKET') WHERE capture_status='CAPTURING'"
        )
        self.db.commit()

    def meta(self) -> dict[str, Any]:
        return {
            str(row[0]): json.loads(str(row[1]))
            for row in self.db.execute("SELECT key,value FROM v037_meta")
        }

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO v037_meta(key,value) VALUES(?,?)",
            (key, json_compact(value)),
        )
        self.db.commit()

    def start_run(self) -> int:
        cursor = self.db.execute(
            "INSERT INTO v037_runs(started_at,status) VALUES(?,'RUNNING')", (utc_now(),)
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, *, status: str, error: str | None) -> None:
        self.db.execute(
            "UPDATE v037_runs SET finished_at=?,status=?,error=? WHERE run_id=?",
            (utc_now(), status, error, run_id),
        )
        self.db.commit()

    def save_market(self, market: SilverMarket, *, contract: ResolutionTwapContract) -> None:
        self.db.execute(
            """INSERT OR IGNORE INTO v037_markets(condition_id,slug,event_id,market_start_ms,
            market_end_ms,up_token_id,down_token_id,discovered_at,resolution_source,
            resolution_contract_status,resolution_twap_window_s,resolution_twap_topic,capture_status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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

    def market_exists(self, slug: str) -> bool:
        return self.db.execute("SELECT 1 FROM v037_markets WHERE slug=?", (slug,)).fetchone() is not None

    def save_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        values = (
            snapshot["condition_id"], snapshot["second_offset"], snapshot["snapshot_timestamp_ms"],
            snapshot["recorded_timestamp_ms"], snapshot["chainlink_source_timestamp_ms"],
            snapshot["chainlink_received_timestamp_ms"], snapshot["chainlink_age_ms"],
            int(snapshot["chainlink_fresh"]), snapshot["official_twap_window_s"],
            snapshot["official_twap_source_timestamp_ms"], snapshot["official_twap_received_timestamp_ms"],
            snapshot["official_twap_age_ms"], int(snapshot["official_twap_fresh"]),
            snapshot["up_book_source_timestamp_ms"], snapshot["up_book_received_timestamp_ms"],
            snapshot["up_book_age_ms"], int(snapshot["up_book_fresh"]),
            snapshot["down_book_source_timestamp_ms"], snapshot["down_book_received_timestamp_ms"],
            snapshot["down_book_age_ms"], int(snapshot["down_book_fresh"]),
            snapshot["up_bid_depth_top5"], snapshot["up_ask_depth_top5"],
            snapshot["down_bid_depth_top5"], snapshot["down_ask_depth_top5"],
            int(snapshot["complete_v2"]),
        )
        self.db.execute(
            f"INSERT OR REPLACE INTO v037_snapshots VALUES({','.join('?' for _ in values)})",
            values,
        )
        self.pending += 1
        if self.pending >= 10:
            self.db.commit()
            self.pending = 0

    def save_watchdog_event(
        self,
        *,
        condition_id: str,
        slug: str,
        incident_id: int | None,
        generation: int,
        event_type: str,
        recorded_timestamp_ms: int,
        second_offset: int,
        up_book_age_ms: int | None,
        down_book_age_ms: int | None,
        recovery_ms: int | None,
    ) -> None:
        if event_type not in {"GENERATION_FRESH", "STALE_TRIGGER", "RECOVERED"}:
            raise ValueError("Evento watchdog V0.37 invalido")
        self.db.execute(
            """INSERT INTO v037_clob_watchdog_events(
            condition_id,slug,incident_id,generation,event_type,recorded_timestamp_ms,
            second_offset,up_book_age_ms,down_book_age_ms,recovery_ms)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                condition_id,
                slug,
                incident_id,
                generation,
                event_type,
                recorded_timestamp_ms,
                second_offset,
                up_book_age_ms,
                down_book_age_ms,
                recovery_ms,
            ),
        )
        self.db.commit()
        self.pending = 0

    def load_market_snapshots(self, condition_id: str) -> dict[int, dict[str, Any]]:
        self.db.commit()
        return {
            int(row["second_offset"]): dict(row)
            for row in self.db.execute(
                "SELECT * FROM v037_snapshots WHERE condition_id=? ORDER BY second_offset",
                (condition_id,),
            )
        }

    def finish_market(
        self, condition_id: str, *, error: str | None, probe_summary: Mapping[str, Any]
    ) -> None:
        self.db.execute(
            """UPDATE v037_markets SET capture_status=?,capture_error=?,probe_total=?,
            probe_entered=?,probe_relative_guard_exits=?,probe_exit_successes=?,probe_trapped=?
            WHERE condition_id=?""",
            (
                "COMPLETED" if error is None else "FAILED",
                error,
                probe_summary["total_capacity_probes"],
                probe_summary["entry_eligible_probes"],
                probe_summary["relative_guard_exits"],
                probe_summary["exit_successes_within_grace"],
                probe_summary["trapped_positions"],
                condition_id,
            ),
        )
        self.db.commit()

    def save_health(self, *, counters: Mapping[str, int], connections: Mapping[str, str]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO v037_health VALUES(?,?,?,?,?)",
            (
                utc_now(),
                database_footprint(self.path),
                shutil.disk_usage(self.path.parent).free,
                json_compact(dict(counters)),
                json_compact(dict(connections)),
            ),
        )
        self.db.commit()
        self.pending = 0

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
    "V037_CAPTURE_HOURS",
    "V037_EXPECTED_MARKETS",
    "V037_HEALTH_INTERVAL_SECONDS",
    "V037_MARKET_SECONDS",
    "V037_SUPPORTED_TWAP_WINDOWS",
    "V037Store",
]
