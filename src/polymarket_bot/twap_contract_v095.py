from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from polymarket_bot.config import Settings
from polymarket_bot.discovery import DiscoveryError
from polymarket_bot.phase41 import _fetch_market_sync
from polymarket_bot.resolution_contract import (
    SUPPORTED_TWAP_WINDOWS,
    TWAP_TOPIC_BY_WINDOW,
    ResolutionTwapContract,
    resolution_twap_contract,
)


PROBE_SCHEMA_VERSION = "1"
PROBE_CODE_VERSION = "0.9.5a1"

PROBE_DDL = """
CREATE TABLE probe_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE probe_market (
    slug TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL,
    resolution_source TEXT,
    contract_status TEXT NOT NULL,
    selected_window_s INTEGER,
    selected_topic TEXT
);
CREATE TABLE probe_twap_ticks (
    source_timestamp_ms INTEGER NOT NULL,
    window_s INTEGER NOT NULL,
    topic TEXT NOT NULL,
    value REAL NOT NULL,
    full_accuracy_value TEXT,
    message_timestamp_ms INTEGER,
    received_at TEXT NOT NULL,
    PRIMARY KEY(source_timestamp_ms,window_s)
);
CREATE INDEX idx_probe_twap_window_time
    ON probe_twap_ticks(window_s,source_timestamp_ms);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class TwapContractProbeStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.connection: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("La base técnica no está abierta")
        return self.connection

    def open(self, *, duration_seconds: float) -> None:
        if self.path.exists():
            raise ValueError(
                f"La captura técnica ya existe y no se sobrescribirá: {self.path}"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=60)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(PROBE_DDL)
        meta = {
            "schema_version": PROBE_SCHEMA_VERSION,
            "code_version": PROBE_CODE_VERSION,
            "started_at": _utc_now(),
            "duration_seconds": float(duration_seconds),
            "supported_window_seconds": list(SUPPORTED_TWAP_WINDOWS),
            "topics": [
                "crypto_prices_chainlink",
                *TWAP_TOPIC_BY_WINDOW.values(),
            ],
            "window_selection": "resolution_source_fail_closed",
            "transfer_compatible_window_seconds": [30],
            "orders_enabled": False,
            "wallet_required": False,
            "money_real_enabled": False,
        }
        self.connection.executemany(
            "INSERT INTO probe_meta(key,value) VALUES(?,?)",
            [(key, _json(value)) for key, value in meta.items()],
        )
        self.connection.commit()

    def save_market(
        self,
        *,
        slug: str,
        condition_id: str,
        contract: ResolutionTwapContract,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO probe_market VALUES(?,?,?,?,?,?)
            """,
            (
                slug,
                condition_id,
                contract.source,
                contract.status,
                contract.window_seconds,
                contract.topic,
            ),
        )
        self.db.commit()

    def save_twap_message(self, message: dict[str, Any]) -> str:
        topic = str(message.get("topic") or "")
        payload = message.get("payload")
        if not isinstance(payload, dict):
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
            INSERT OR IGNORE INTO probe_twap_ticks(
                source_timestamp_ms,window_s,topic,value,
                full_accuracy_value,message_timestamp_ms,received_at
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
                _utc_now(),
            ),
        )
        self.db.commit()
        return "SAVED" if cursor.rowcount else "DUPLICATE"

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO probe_meta(key,value) VALUES(?,?)",
            (key, _json(value)),
        )
        self.db.commit()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def _discover_contract_market(
    settings: Settings,
) -> tuple[str, str, ResolutionTwapContract]:
    now = int(time.time())
    current = now - now % 300
    starts = (current, current + 300, current - 300)
    errors: list[str] = []
    for start in starts:
        slug = f"btc-updown-5m-{start}"
        try:
            definition, _, _ = _fetch_market_sync(settings, slug)
        except DiscoveryError as exc:
            errors.append(f"{slug}:{exc}")
            continue
        contract = resolution_twap_contract(definition.resolution_source)
        if contract.verified:
            return definition.slug, definition.condition_id, contract
        errors.append(f"{slug}:contrato_{contract.status.lower()}")
    raise DiscoveryError(" | ".join(errors) or "sin mercado BTC 5m")


def probe_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).expanduser().resolve()
    if not database.is_file():
        raise ValueError(f"No se encontró la captura técnica: {database}")
    connection = sqlite3.connect(
        f"{database.as_uri()}?mode=ro",
        uri=True,
        timeout=60,
    )
    connection.row_factory = sqlite3.Row
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        meta = {
            str(row[0]): json.loads(row[1])
            for row in connection.execute(
                "SELECT key,value FROM probe_meta ORDER BY key"
            )
        }
        market_row = connection.execute("SELECT * FROM probe_market").fetchone()
        counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                """
                SELECT window_s,COUNT(*) FROM probe_twap_ticks
                GROUP BY window_s ORDER BY window_s
                """
            )
        }
    finally:
        connection.close()
    market = dict(market_row) if market_row is not None else None
    selected_window = (
        int(market["selected_window_s"])
        if market is not None and market["selected_window_s"] is not None
        else None
    )
    selected_ticks = counts.get(str(selected_window), 0)
    capture_complete = (
        meta.get("finished_at") is not None
        and isinstance(meta.get("counters"), dict)
    )
    technical_passed = (
        quick_check == "ok"
        and capture_complete
        and market is not None
        and market["contract_status"] == "VERIFIED"
        and selected_ticks > 0
        and all(counts.get(str(window_s), 0) > 0 for window_s in SUPPORTED_TWAP_WINDOWS)
        and int(meta.get("topic_window_mismatches", 0)) == 0
        and meta.get("orders_enabled") is False
        and meta.get("wallet_required") is False
        and meta.get("money_real_enabled") is False
    )
    return {
        "schema": "twap_contract_probe_v095_result_1",
        "database": str(database),
        "sqlite_quick_check": quick_check,
        "market": market,
        "twap_updates_by_window": counts,
        "selected_window_ticks": selected_ticks,
        "capture_complete": capture_complete,
        "all_supported_streams_observed": all(
            counts.get(str(window_s), 0) > 0
            for window_s in SUPPORTED_TWAP_WINDOWS
        ),
        "transfer_model_compatible": selected_window == 30,
        "retraining_required_for_selected_window": selected_window not in {None, 30},
        "technical_passed": technical_passed,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
        "meta": meta,
    }


def finalize_probe_status(
    *,
    database: str | Path,
    output_json: str | Path,
) -> dict[str, Any]:
    result_path = Path(output_json).expanduser().resolve()
    if result_path.exists():
        raise ValueError(
            f"El resultado técnico ya existe y no se sobrescribirá: {result_path}"
        )
    result = probe_status(database)
    if not result["capture_complete"]:
        raise ValueError("La captura técnica aún no está completa")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


async def run_twap_contract_probe(
    *,
    settings: Settings,
    output_db: str | Path,
    output_json: str | Path,
    duration_seconds: float = 45.0,
) -> dict[str, Any]:
    if duration_seconds <= 0 or duration_seconds > 300:
        raise ValueError("duration_seconds debe estar entre 0 y 300")
    result_path = Path(output_json).expanduser().resolve()
    if result_path.exists():
        raise ValueError(
            f"El resultado técnico ya existe y no se sobrescribirá: {result_path}"
        )
    slug, condition_id, contract = await asyncio.to_thread(
        _discover_contract_market,
        settings,
    )
    store = TwapContractProbeStore(output_db)
    store.open(duration_seconds=duration_seconds)
    store.save_market(
        slug=slug,
        condition_id=condition_id,
        contract=contract,
    )
    counters: Counter[str] = Counter()
    deadline = time.monotonic() + duration_seconds
    attempt = 0
    try:
        while time.monotonic() < deadline:
            heartbeat_task: asyncio.Task[Any] | None = None
            try:
                async with connect(
                    settings.rtds_ws_url,
                    ping_interval=None,
                    open_timeout=12,
                    close_timeout=5,
                    max_size=4 * 1024 * 1024,
                    user_agent_header=(
                        f"polymarket-quant-bot-contract-probe/{PROBE_CODE_VERSION}"
                    ),
                    proxy=True if settings.ws_use_proxy else None,
                ) as websocket:
                    subscription = {
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": "crypto_prices_chainlink",
                                "type": "*",
                                "filters": '{"symbol":"btc/usd"}',
                            },
                            *(
                                {
                                    "topic": topic,
                                    "type": "update",
                                    "filters": '{"symbol":"btc/usd"}',
                                }
                                for topic in TWAP_TOPIC_BY_WINDOW.values()
                            ),
                        ],
                    }
                    await websocket.send(_json(subscription))
                    counters["connections"] += 1
                    attempt = 0

                    async def heartbeat() -> None:
                        while True:
                            await asyncio.sleep(5.0)
                            await websocket.send("PING")

                    heartbeat_task = asyncio.create_task(heartbeat())
                    while time.monotonic() < deadline:
                        remaining = max(0.01, deadline - time.monotonic())
                        try:
                            raw = await asyncio.wait_for(
                                websocket.recv(),
                                timeout=min(5.0, remaining),
                            )
                        except TimeoutError:
                            continue
                        text = (
                            raw.decode("utf-8", errors="replace")
                            if isinstance(raw, bytes)
                            else str(raw)
                        )
                        if text.strip().upper() in {"PING", "PONG"}:
                            counters["heartbeat"] += 1
                            continue
                        try:
                            message = json.loads(text)
                        except json.JSONDecodeError:
                            counters["malformed_json"] += 1
                            continue
                        if not isinstance(message, dict):
                            counters["malformed_json"] += 1
                            continue
                        topic = str(message.get("topic") or "")
                        counters[f"topic:{topic or 'missing'}"] += 1
                        if topic == "crypto_prices_chainlink":
                            continue
                        if topic in TWAP_TOPIC_BY_WINDOW.values():
                            outcome = store.save_twap_message(message)
                            counters[f"twap:{outcome.lower()}"] += 1
            except (ConnectionClosed, OSError, TimeoutError):
                counters["reconnections"] += 1
                attempt += 1
                await asyncio.sleep(min(3.0, 2 ** min(attempt, 2)))
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
    finally:
        store.set_meta("finished_at", _utc_now())
        store.set_meta("counters", dict(counters))
        store.set_meta(
            "topic_window_mismatches",
            int(counters.get("twap:topic_window_mismatch", 0)),
        )
        store.close()

    return finalize_probe_status(
        database=output_db,
        output_json=result_path,
    )
