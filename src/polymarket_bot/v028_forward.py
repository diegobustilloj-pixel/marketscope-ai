from __future__ import annotations

import asyncio
import json
import math
import shutil
import sqlite3
import time
from collections import Counter, deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.config import Settings
from polymarket_bot.discovery import DiscoveryError
from polymarket_bot.phase2 import ResolutionInfo, SilverMarket
from polymarket_bot.phase41 import (
    SHADOW_MIN_FREE_GB,
    SHADOW_RTDS_WATCHDOG_SECONDS,
    _build_live_feature,
    _fetch_event_metadata_sync,
    _fetch_market_sync,
    _number,
    _resolve_market,
    _silver_market,
    _wait_or_stop,
    _websocket_feed,
    LiveShadowState,
)
from polymarket_bot.resolution_contract import (
    TWAP_TOPIC_BY_WINDOW,
    ResolutionTwapContract,
    resolution_twap_contract,
)


V028_FORWARD_SCHEMA_VERSION = "1"
V028_FORWARD_CODE_VERSION = "0.9.5a1-v028"
V028_MAXIMUM_HOURS = 24.0
V028_HORIZON_SECONDS = 60
V028_REQUIRED_TWAP_WINDOW_SECONDS = 60
V028_TWAP_MAX_AGE_MS = 5_000
V028_HEALTH_INTERVAL_SECONDS = 60.0
V028_MAX_DATABASE_GB = 1.0
V028_REQUIRED_GLOBAL_FEEDS = (
    "v028-rtds-chainlink",
    "v028-binance",
)


V028_DDL = """
CREATE TABLE v028_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE v028_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    error TEXT
);
CREATE TABLE v028_markets (
    condition_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    event_id TEXT,
    market_start_ms INTEGER NOT NULL,
    market_end_ms INTEGER NOT NULL,
    discovered_at TEXT NOT NULL,
    resolution_source TEXT,
    resolution_contract_status TEXT NOT NULL,
    resolution_twap_window_s INTEGER,
    resolution_twap_topic TEXT,
    price_to_beat REAL,
    final_price REAL,
    strike_source TEXT,
    strike_fetch_status TEXT NOT NULL,
    event_metadata_json TEXT,
    feature_status TEXT NOT NULL,
    feature_error TEXT,
    label TEXT,
    label_verified INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT,
    resolution_error TEXT
);
CREATE INDEX idx_v028_markets_start ON v028_markets(market_start_ms);
CREATE TABLE v028_features (
    condition_id TEXT PRIMARY KEY,
    decision_timestamp_ms INTEGER NOT NULL,
    horizon_seconds INTEGER NOT NULL,
    quality_flags INTEGER NOT NULL,
    feature_json TEXT NOT NULL,
    FOREIGN KEY(condition_id) REFERENCES v028_markets(condition_id)
);
CREATE TABLE v028_twap_ticks (
    source_timestamp_ms INTEGER NOT NULL,
    window_s INTEGER NOT NULL,
    topic TEXT NOT NULL,
    value REAL NOT NULL,
    full_accuracy_value TEXT,
    message_timestamp_ms INTEGER,
    received_at TEXT NOT NULL,
    PRIMARY KEY(source_timestamp_ms,window_s)
);
CREATE INDEX idx_v028_twap_window_time
    ON v028_twap_ticks(window_s,source_timestamp_ms);
CREATE TABLE v028_health (
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


def read_v028_meta(path: str | Path) -> dict[str, Any]:
    connection = open_read_only(path)
    try:
        return {
            str(row[0]): json.loads(str(row[1]))
            for row in connection.execute(
                "SELECT key,value FROM v028_meta ORDER BY key"
            )
        }
    finally:
        connection.close()


class V028Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.connection: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("La base V0.28 no está abierta")
        return self.connection

    def open(
        self,
        *,
        preregistration_sha256: str,
        launch_manifest_sha256: str,
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
            if "v028_meta" not in tables:
                raise ValueError("La base existente no pertenece a V0.28")
        else:
            self.connection.executescript(V028_DDL)
        meta = self.meta()
        if meta:
            if meta.get("schema_version") != V028_FORWARD_SCHEMA_VERSION:
                raise ValueError("Esquema V0.28 incompatible; use una base nueva")
            if meta.get("preregistration_sha256") != preregistration_sha256:
                raise ValueError("La base V0.28 pertenece a otra preinscripción")
            if meta.get("launch_manifest_sha256") != launch_manifest_sha256:
                raise ValueError("La base V0.28 pertenece a otro lanzamiento")
        else:
            now = time.time()
            values = {
                "schema_version": V028_FORWARD_SCHEMA_VERSION,
                "code_version": V028_FORWARD_CODE_VERSION,
                "variant": "V0.28_UP_LOW_VOL_TWAP_LT5_REPLICATION_24H",
                "experiment_started_at": datetime.fromtimestamp(
                    now, timezone.utc
                ).isoformat(timespec="seconds"),
                "target_hours": V028_MAXIMUM_HOURS,
                "target_end_at": datetime.fromtimestamp(
                    now + V028_MAXIMUM_HOURS * 3600,
                    timezone.utc,
                ).isoformat(timespec="seconds"),
                "preregistration_sha256": preregistration_sha256,
                "launch_manifest_sha256": launch_manifest_sha256,
                "supported_twap_windows": [V028_REQUIRED_TWAP_WINDOW_SECONDS],
                "twap_window_selection": "resolution_source_fail_closed_exact_60s",
                "twap_transfer_model_enabled": False,
                "v028_completion_reason": None,
                "v028_observation_ended_at": None,
                "v028_checkpoints": [],
                "v028_candidate_states": {},
                "orders_enabled": False,
                "paper_orders_enabled": False,
                "wallet_required": False,
                "money_real_enabled": False,
                "v028_real_money": "BLOQUEADO",
            }
            self.connection.executemany(
                "INSERT INTO v028_meta(key,value) VALUES(?,?)",
                [(key, json_compact(value)) for key, value in values.items()],
            )
            self.connection.commit()
        self.reconcile_interrupted_runs()

    def meta(self) -> dict[str, Any]:
        return {
            str(row[0]): json.loads(str(row[1]))
            for row in self.db.execute(
                "SELECT key,value FROM v028_meta ORDER BY key"
            )
        }

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO v028_meta(key,value) VALUES(?,?)",
            (key, json_compact(value)),
        )
        self.db.commit()

    def reconcile_interrupted_runs(self) -> None:
        self.db.execute(
            """
            UPDATE v028_runs SET finished_at=?,status='INTERRUPTED',
             error=COALESCE(error,'RECOVERED_STALE_RUNNING')
            WHERE status='RUNNING'
            """,
            (utc_now(),),
        )
        self.db.commit()

    def start_run(self) -> int:
        cursor = self.db.execute(
            "INSERT INTO v028_runs(started_at,status) VALUES(?,'RUNNING')",
            (utc_now(),),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, *, status: str, error: str | None) -> None:
        self.db.execute(
            """
            UPDATE v028_runs SET finished_at=?,status=?,error=? WHERE run_id=?
            """,
            (utc_now(), status, error, run_id),
        )
        self.db.commit()

    def save_market(
        self,
        market: SilverMarket,
        *,
        discovery_metadata: Mapping[str, Any],
        contract: ResolutionTwapContract,
    ) -> bool:
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO v028_markets(
             condition_id,slug,event_id,market_start_ms,market_end_ms,
             discovered_at,resolution_source,resolution_contract_status,
             resolution_twap_window_s,resolution_twap_topic,price_to_beat,
             final_price,strike_source,strike_fetch_status,event_metadata_json,
             feature_status,label_verified
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
            """,
            (
                market.condition_id,
                market.slug,
                market.event_id,
                market.start_ms,
                market.end_ms,
                utc_now(),
                market.resolution_source,
                contract.status,
                contract.window_seconds,
                contract.topic,
                market.resolution.price_to_beat,
                discovery_metadata.get("final_price"),
                discovery_metadata.get("strike_source"),
                str(discovery_metadata.get("strike_fetch_status") or "MISSING"),
                discovery_metadata.get("event_metadata_json"),
                "PENDING",
            ),
        )
        self.db.commit()
        return cursor.rowcount > 0

    def market_exists(self, slug: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM v028_markets WHERE slug=?", (slug,)
            ).fetchone()
            is not None
        )

    def save_feature(
        self,
        *,
        condition_id: str,
        feature: Mapping[str, Any],
    ) -> None:
        self.db.execute(
            """
            INSERT OR REPLACE INTO v028_features VALUES(?,?,?,?,?)
            """,
            (
                condition_id,
                int(feature["decision_timestamp_ms"]),
                int(feature["horizon_seconds"]),
                int(feature["decision_quality_flags"]),
                json_compact(feature),
            ),
        )
        self.db.execute(
            """
            UPDATE v028_markets SET feature_status='SAVED',feature_error=NULL
            WHERE condition_id=?
            """,
            (condition_id,),
        )
        self.db.commit()

    def save_feature_failure(self, condition_id: str, reason: str) -> None:
        self.db.execute(
            """
            UPDATE v028_markets SET feature_status='FAILED',feature_error=?
            WHERE condition_id=?
            """,
            (reason, condition_id),
        )
        self.db.commit()

    def save_twap_message(self, message: Mapping[str, Any]) -> str:
        topic = str(message.get("topic") or "")
        payload = message.get("payload")
        if not isinstance(payload, Mapping):
            return "MALFORMED"
        try:
            window_s = int(payload.get("window_s"))
            source_timestamp_ms = int(payload.get("timestamp"))
            value = float(payload.get("value"))
        except (TypeError, ValueError):
            return "MALFORMED"
        if str(payload.get("symbol") or "").lower() != "btc/usd":
            return "IGNORED_SYMBOL"
        if not math.isfinite(value):
            return "MALFORMED"
        if TWAP_TOPIC_BY_WINDOW.get(window_s) != topic:
            return "TOPIC_WINDOW_MISMATCH"
        message_timestamp = message.get("timestamp")
        try:
            message_timestamp_ms = (
                int(message_timestamp) if message_timestamp is not None else None
            )
        except (TypeError, ValueError):
            message_timestamp_ms = None
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO v028_twap_ticks(
             source_timestamp_ms,window_s,topic,value,full_accuracy_value,
             message_timestamp_ms,received_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                source_timestamp_ms,
                window_s,
                topic,
                value,
                (
                    str(payload.get("full_accuracy_value"))
                    if payload.get("full_accuracy_value") is not None
                    else None
                ),
                message_timestamp_ms,
                utc_now(),
            ),
        )
        self.db.commit()
        return "SAVED" if cursor.rowcount else "DUPLICATE"

    def save_health(
        self,
        *,
        counters: Mapping[str, int],
        connections: Mapping[str, str],
    ) -> None:
        self.db.execute(
            """
            INSERT OR REPLACE INTO v028_health VALUES(?,?,?,?,?)
            """,
            (
                utc_now(),
                database_footprint(self.path),
                shutil.disk_usage(self.path.parent).free,
                json_compact(dict(counters)),
                json_compact(dict(connections)),
            ),
        )
        self.db.commit()

    def save_resolution(
        self,
        *,
        condition_id: str,
        resolution: ResolutionInfo,
    ) -> None:
        self.db.execute(
            """
            UPDATE v028_markets SET label=?,label_verified=?,resolved_at=?,
             resolution_error=? WHERE condition_id=?
            """,
            (
                resolution.label if resolution.verified else None,
                int(resolution.verified),
                utc_now() if resolution.verified else None,
                resolution.error,
                condition_id,
            ),
        )
        self.db.commit()

    def update_event_metadata(
        self,
        *,
        condition_id: str,
        metadata: Mapping[str, Any],
    ) -> None:
        self.db.execute(
            """
            UPDATE v028_markets SET
             price_to_beat=COALESCE(?,price_to_beat),
             final_price=COALESCE(?,final_price),
             strike_source=COALESCE(?,strike_source),
             strike_fetch_status=?,
             event_metadata_json=COALESCE(?,event_metadata_json)
            WHERE condition_id=?
            """,
            (
                metadata.get("price_to_beat"),
                metadata.get("final_price"),
                metadata.get("strike_source"),
                str(metadata.get("strike_fetch_status") or "MISSING"),
                metadata.get("event_metadata_json"),
                condition_id,
            ),
        )
        self.db.commit()

    def pending_resolutions(self) -> list[tuple[str, str, int]]:
        return [
            (str(row[0]), str(row[1]), int(row[2]))
            for row in self.db.execute(
                """
                SELECT condition_id,slug,market_end_ms FROM v028_markets
                WHERE label_verified=0 ORDER BY market_end_ms
                """
            )
        ]

    def quick_check(self) -> str:
        return str(self.db.execute("PRAGMA quick_check").fetchone()[0])

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


TwapItem = tuple[float, int, int, str | None]


class V028LiveState(LiveShadowState):
    def __init__(self) -> None:
        super().__init__()
        self.twap_history_by_window: dict[int, deque[TwapItem]] = {
            30: self.twap_history,
            60: deque(maxlen=900),
        }

    def ingest(
        self,
        *,
        source: str,
        default_stream: str,
        raw: str,
    ) -> None:
        if source == "rtds":
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                message = None
            if isinstance(message, dict):
                topic = str(message.get("topic") or "")
                payload = message.get("payload")
                if topic == TWAP_TOPIC_BY_WINDOW[60] and isinstance(payload, dict):
                    try:
                        window_s = int(payload.get("window_s"))
                        timestamp_ms = int(payload.get("timestamp"))
                        price = float(payload.get("value"))
                    except (TypeError, ValueError):
                        pass
                    else:
                        if (
                            window_s == 60
                            and str(payload.get("symbol") or "").lower()
                            == "btc/usd"
                            and math.isfinite(price)
                        ):
                            full_accuracy = payload.get("full_accuracy_value")
                            self.twap_history_by_window[60].append(
                                (
                                    price,
                                    timestamp_ms,
                                    60,
                                    (
                                        str(full_accuracy)
                                        if full_accuracy is not None
                                        else None
                                    ),
                                )
                            )
        super().ingest(source=source, default_stream=default_stream, raw=raw)

    def selected_twap_at_or_before(
        self,
        timestamp_ms: int,
        window_s: int,
    ) -> TwapItem | None:
        history = self.twap_history_by_window.get(int(window_s))
        if history is None:
            return None
        for item in reversed(history):
            if item[1] <= timestamp_ms:
                return item
        return None


def is_fresh_twap_message(raw: str, window_s: int) -> bool:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if (
        not isinstance(message, dict)
        or message.get("topic") != TWAP_TOPIC_BY_WINDOW.get(int(window_s))
    ):
        return False
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return False
    try:
        actual_window = int(payload.get("window_s"))
        timestamp_ms = int(payload.get("timestamp"))
        value = float(payload.get("value"))
    except (TypeError, ValueError):
        return False
    return (
        str(payload.get("symbol") or "").lower() == "btc/usd"
        and actual_window == int(window_s)
        and timestamp_ms > 0
        and math.isfinite(value)
    )


def build_v028_feature(
    *,
    state: V028LiveState,
    market: SilverMarket,
) -> tuple[dict[str, Any] | None, str | None]:
    contract = resolution_twap_contract(market.resolution_source)
    if not contract.verified or contract.window_seconds is None:
        return None, f"resolution_contract_{contract.status.lower()}"
    if int(contract.window_seconds) != V028_REQUIRED_TWAP_WINDOW_SECONDS:
        return None, "resolution_contract_wrong_twap_window"
    feature, reason = _build_live_feature(
        state=state,
        market=market,
        horizon_seconds=V028_HORIZON_SECONDS,
    )
    if feature is None:
        return None, reason
    selected_window = int(contract.window_seconds)
    decision_ms = int(feature["decision_timestamp_ms"])
    selected = state.selected_twap_at_or_before(decision_ms, selected_window)
    opening = state.selected_twap_at_or_before(market.start_ms, selected_window)
    if selected is None:
        return None, "selected_resolution_twap_missing_at_decision"
    if opening is None:
        return None, "selected_resolution_twap_missing_at_open"
    price, timestamp_ms, _, full_accuracy = selected
    open_price, open_timestamp_ms, _, open_full_accuracy = opening
    age_ms = max(0, decision_ms - timestamp_ms)
    open_age_ms = market.start_ms - open_timestamp_ms
    if age_ms > V028_TWAP_MAX_AGE_MS:
        return None, f"selected_resolution_twap_stale_at_decision:{age_ms}ms"
    if not 0 <= open_age_ms <= V028_TWAP_MAX_AGE_MS:
        return None, f"selected_resolution_twap_stale_at_open:{open_age_ms}ms"
    distance = (
        (price / open_price - 1.0) * 10_000.0 if open_price != 0.0 else None
    )
    prefix = f"twap_{selected_window}s"
    feature.update(
        {
            "resolution_source": market.resolution_source,
            "resolution_twap_contract_status": contract.status,
            "resolution_twap_window_s": selected_window,
            "resolution_twap_topic": contract.topic,
            "resolution_twap_available": 1,
            "resolution_twap_price": price,
            "resolution_twap_timestamp_ms": timestamp_ms,
            "resolution_twap_age_ms": age_ms,
            "resolution_twap_fresh": 1,
            "resolution_twap_full_accuracy_value": full_accuracy,
            f"{prefix}_available": 1,
            f"{prefix}_price": price,
            f"{prefix}_timestamp_ms": timestamp_ms,
            f"{prefix}_age_ms": age_ms,
            f"{prefix}_fresh": 1,
            f"{prefix}_window_s": selected_window,
            f"{prefix}_full_accuracy_value": full_accuracy,
            "twap_open_available": 1,
            "twap_open_price": open_price,
            "twap_open_timestamp_ms": open_timestamp_ms,
            "twap_open_age_ms": open_age_ms,
            "twap_open_fresh": 1,
            "twap_open_window_s": selected_window,
            "twap_open_full_accuracy_value": open_full_accuracy,
            "twap_distance_to_open_bps": distance,
            "twap_transfer_model_enabled": 0,
            "twap_transfer_compatible": 0,
            "v028_signal_source": "market_implied_only_no_transferred_model",
        }
    )
    return feature, None


async def run_v028_forward(
    *,
    settings: Settings,
    output_db: str | Path,
    preregistration_sha256: str,
    launch_manifest_sha256: str,
    stop_event: asyncio.Event,
    maximum_database_gb: float = V028_MAX_DATABASE_GB,
    minimum_free_gb: float = SHADOW_MIN_FREE_GB,
) -> dict[str, Any]:
    database = Path(output_db).expanduser().resolve()
    store = V028Store(database)
    store.open(
        preregistration_sha256=preregistration_sha256,
        launch_manifest_sha256=launch_manifest_sha256,
    )
    meta = store.meta()
    target_end = parse_utc(str(meta["target_end_at"])).timestamp()
    run_id = store.start_run()
    state = V028LiveState()
    resolution_tasks: set[asyncio.Task[Any]] = set()
    status = "COMPLETED"
    error: str | None = None
    safety_stop_reason: str | None = None

    async def ingest_rtds(raw: str) -> None:
        state.ingest(source="rtds", default_stream="crypto_price", raw=raw)
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(message, dict):
            return
        if message.get("topic") in TWAP_TOPIC_BY_WINDOW.values():
            outcome = store.save_twap_message(message)
            state.counters[f"twap:{outcome.lower()}"] += 1

    async def ingest_binance(raw: str) -> None:
        state.ingest(source="binance", default_stream="aggTrade", raw=raw)

    global_tasks = [
        asyncio.create_task(
            _websocket_feed(
                name="v028-rtds-chainlink",
                endpoint=settings.rtds_ws_url,
                subscription={
                    "action": "subscribe",
                    "subscriptions": [
                        {
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": '{"symbol":"btc/usd"}',
                        }
                    ],
                },
                heartbeat_text="PING",
                heartbeat_seconds=5.0,
                use_proxy=settings.ws_use_proxy,
                reconnect_max_seconds=settings.reconnect_max_seconds,
                stop_event=stop_event,
                state=state,
                handler=ingest_rtds,
            )
        ),
        asyncio.create_task(
            _websocket_feed(
                name="v028-binance",
                endpoint=settings.binance_ws_url,
                subscription=None,
                heartbeat_text=None,
                heartbeat_seconds=None,
                use_proxy=settings.ws_use_proxy,
                reconnect_max_seconds=settings.reconnect_max_seconds,
                stop_event=stop_event,
                state=state,
                handler=ingest_binance,
            )
        ),
    ]
    for window_s in (V028_REQUIRED_TWAP_WINDOW_SECONDS,):
        topic = TWAP_TOPIC_BY_WINDOW[window_s]
        feed_name = f"v028-rtds-twap-{window_s}s"
        global_tasks.append(
            asyncio.create_task(
                _websocket_feed(
                    name=feed_name,
                    endpoint=settings.rtds_ws_url,
                    subscription={
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": topic,
                                "type": "update",
                                "filters": '{"symbol":"btc/usd"}',
                            }
                        ],
                    },
                    heartbeat_text="PING",
                    heartbeat_seconds=5.0,
                    use_proxy=settings.ws_use_proxy,
                    reconnect_max_seconds=settings.reconnect_max_seconds,
                    stop_event=stop_event,
                    state=state,
                    handler=ingest_rtds,
                    freshness_timeout_seconds=SHADOW_RTDS_WATCHDOG_SECONDS,
                    freshness_predicate=(
                        lambda raw, expected=window_s: is_fresh_twap_message(
                            raw, expected
                        )
                    ),
                )
            )
        )

    async def health_monitor() -> None:
        nonlocal safety_stop_reason
        while not stop_event.is_set():
            store.save_health(
                counters=state.counters,
                connections=state.connections,
            )
            if database_footprint(database) >= maximum_database_gb * 1_000_000_000:
                safety_stop_reason = "DATABASE_LIMIT_REACHED"
                store.set_meta("v028_completion_reason", "FREEZE_COLLECTOR_SAFETY")
                store.set_meta("v028_observation_ended_at", utc_now())
                stop_event.set()
                return
            if shutil.disk_usage(database.parent).free <= minimum_free_gb * 1_000_000_000:
                safety_stop_reason = "INSUFFICIENT_FREE_SPACE"
                store.set_meta("v028_completion_reason", "FREEZE_COLLECTOR_SAFETY")
                store.set_meta("v028_observation_ended_at", utc_now())
                stop_event.set()
                return
            await _wait_or_stop(stop_event, V028_HEALTH_INTERVAL_SECONDS)

    health_task = asyncio.create_task(health_monitor())
    try:
        for condition_id, slug, end_ms in store.pending_resolutions():
            task = asyncio.create_task(
                _resolve_market(
                    settings=settings,
                    store=store,  # type: ignore[arg-type]
                    condition_id=condition_id,
                    slug=slug,
                    market_end_ms=end_ms,
                )
            )
            resolution_tasks.add(task)
            task.add_done_callback(resolution_tasks.discard)

        while time.time() < target_end and not stop_event.is_set():
            now = time.time()
            current_start = int(now) - int(now) % 300
            target_start = current_start if now <= current_start + 15 else current_start + 300
            if target_start + 240 > target_end:
                await _wait_or_stop(stop_event, max(0.0, target_end - time.time()))
                break
            await _wait_or_stop(stop_event, max(0.0, target_start + 1 - time.time()))
            if stop_event.is_set():
                break
            slug = f"btc-updown-5m-{target_start}"
            if store.market_exists(slug):
                await _wait_or_stop(
                    stop_event,
                    max(1.0, target_start + 300 - time.time()),
                )
                continue
            discovered = None
            deadline = target_start + 30
            while time.time() < deadline and not stop_event.is_set():
                try:
                    discovered = await asyncio.to_thread(
                        _fetch_market_sync,
                        settings,
                        slug,
                    )
                    break
                except DiscoveryError:
                    await _wait_or_stop(stop_event, 2.0)
            if discovered is None:
                state.counters["market_discovery_missed"] += 1
                continue
            definition, initial_resolution, metadata = discovered
            market = _silver_market(definition, initial_resolution)
            contract = resolution_twap_contract(market.resolution_source)
            store.save_market(
                market,
                discovery_metadata=metadata,
                contract=contract,
            )
            if not contract.verified:
                store.save_feature_failure(
                    market.condition_id,
                    f"resolution_contract_{contract.status.lower()}",
                )
                continue
            state.set_market(market)

            async def ingest_clob(raw: str) -> None:
                state.ingest(source="clob", default_stream="market", raw=raw)

            clob_stop = asyncio.Event()
            clob_task = asyncio.create_task(
                _websocket_feed(
                    name=f"v028-clob-{market.slug}",
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
                    handler=ingest_clob,
                )
            )
            decision_at = market.end_ms / 1000 - V028_HORIZON_SECONDS
            await _wait_or_stop(stop_event, max(0.0, decision_at - time.time()))
            active_market = market
            if not stop_event.is_set() and active_market.resolution.price_to_beat is None:
                try:
                    refreshed = await asyncio.to_thread(
                        _fetch_event_metadata_sync,
                        settings,
                        active_market.slug,
                    )
                except DiscoveryError:
                    state.counters["strike_refresh_failed"] += 1
                else:
                    store.update_event_metadata(
                        condition_id=active_market.condition_id,
                        metadata=refreshed,
                    )
                    if refreshed.get("price_to_beat") is not None:
                        active_market = replace(
                            active_market,
                            resolution=replace(
                                active_market.resolution,
                                price_to_beat=float(refreshed["price_to_beat"]),
                            ),
                        )
            if not stop_event.is_set():
                feature, reason = build_v028_feature(
                    state=state,
                    market=active_market,
                )
                if feature is None:
                    store.save_feature_failure(
                        active_market.condition_id,
                        reason or "invalid_feature",
                    )
                else:
                    store.save_feature(
                        condition_id=active_market.condition_id,
                        feature=feature,
                    )
            await _wait_or_stop(
                stop_event,
                max(0.0, market.end_ms / 1000 + 2 - time.time()),
            )
            clob_stop.set()
            clob_task.cancel()
            await asyncio.gather(clob_task, return_exceptions=True)
            state.connections.pop(f"v028-clob-{market.slug}", None)
            state.clear_market()
            task = asyncio.create_task(
                _resolve_market(
                    settings=settings,
                    store=store,  # type: ignore[arg-type]
                    condition_id=market.condition_id,
                    slug=market.slug,
                    market_end_ms=market.end_ms,
                )
            )
            resolution_tasks.add(task)
            task.add_done_callback(resolution_tasks.discard)

        current_meta = store.meta()
        if safety_stop_reason is not None:
            status = "COMPLETED"
        elif current_meta.get("v028_completion_reason") is not None:
            status = "COMPLETED"
        elif time.time() >= target_end:
            store.set_meta("v028_completion_reason", "FULL_24H_REACHED")
            store.set_meta("v028_observation_ended_at", utc_now())
            status = "COMPLETED"
        else:
            status = "INTERRUPTED"
    except asyncio.CancelledError:
        status = "INTERRUPTED"
        raise
    except Exception as exc:
        status = "FAILED"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        stop_event.set()
        for task in global_tasks:
            task.cancel()
        health_task.cancel()
        await asyncio.gather(*global_tasks, health_task, return_exceptions=True)
        if status == "COMPLETED" and resolution_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(resolution_tasks), return_exceptions=True),
                    timeout=960,
                )
            except TimeoutError:
                pass
        else:
            for task in resolution_tasks:
                task.cancel()
            await asyncio.gather(*list(resolution_tasks), return_exceptions=True)
        store.save_health(counters=state.counters, connections=state.connections)
        store.finish_run(run_id, status=status, error=error)
        quick_check = store.quick_check()
        result_meta = store.meta()
        store.close()
    return {
        "status": status,
        "error": error,
        "safety_stop_reason": safety_stop_reason,
        "database": str(database),
        "database_bytes": database_footprint(database),
        "quick_check": quick_check,
        "experiment_started_at": result_meta["experiment_started_at"],
        "target_end_at": result_meta["target_end_at"],
        "completion_reason": result_meta.get("v028_completion_reason"),
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


__all__ = [
    "V028_DDL",
    "V028_FORWARD_CODE_VERSION",
    "V028_FORWARD_SCHEMA_VERSION",
    "V028_REQUIRED_TWAP_WINDOW_SECONDS",
    "V028LiveState",
    "V028Store",
    "build_v028_feature",
    "database_footprint",
    "is_fresh_twap_message",
    "open_read_only",
    "parse_utc",
    "read_v028_meta",
    "run_v028_forward",
]
