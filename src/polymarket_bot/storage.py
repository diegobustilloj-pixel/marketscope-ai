from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import threading
import uuid
import zlib
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from polymarket_bot.domain import (
    MarketDefinition,
    RawEvent,
    payload_checksum,
    utc_now_iso,
)


SCHEMA_VERSION = "4"
CODEC = "zlib-jsonl-v1"

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_chunks (
    chunk_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    first_received_at TEXT NOT NULL,
    last_received_at TEXT NOT NULL,
    first_sequence INTEGER NOT NULL,
    last_sequence INTEGER NOT NULL,
    first_monotonic_ns INTEGER NOT NULL,
    last_monotonic_ns INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    codec TEXT NOT NULL,
    raw_bytes INTEGER NOT NULL,
    compressed_bytes INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    compressed_sha256 TEXT NOT NULL,
    payload_blob BLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_received
    ON raw_chunks(first_received_at, last_received_at);

CREATE TABLE IF NOT EXISTS event_counts (
    source TEXT NOT NULL,
    stream TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    PRIMARY KEY(source, stream)
);

CREATE TABLE IF NOT EXISTS event_hourly_counts (
    source TEXT NOT NULL,
    stream TEXT NOT NULL,
    utc_hour TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    PRIMARY KEY(source, stream, utc_hour)
);

CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    event_id TEXT,
    question TEXT NOT NULL,
    start_at TEXT,
    end_at TEXT,
    resolution_source TEXT,
    outcomes_json TEXT NOT NULL,
    token_ids_json TEXT NOT NULL,
    active INTEGER NOT NULL,
    closed INTEGER NOT NULL,
    accepting_orders INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    payload_raw TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collector_runs (
    run_id TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    error TEXT
);
"""


def _event_json(event: RawEvent) -> str:
    return json.dumps(
        asdict(event),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_chunk(blob: bytes, codec: str) -> bytes:
    if codec != CODEC:
        raise ValueError(f"Codec no compatible: {codec}")
    return zlib.decompress(blob)


class SQLiteStore:
    """Catálogo SQLite con eventos raw comprimidos por bloques.

    Los mensajes originales permanecen byte por byte dentro de cada envelope
    JSON. Agruparlos antes de comprimir elimina la repetición de claves y evita
    que los índices de SQLite multipliquen el consumo por cada tick.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path, timeout=30, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        has_schema_meta = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type='table' AND name='schema_meta'
            """
        ).fetchone()
        existing = (
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if has_schema_meta is not None
            else None
        )
        if existing is not None and existing["value"] != SCHEMA_VERSION:
            connection.close()
            raise RuntimeError(
                "La base pertenece a otra versión. Use la ruta "
                "data/polymarket_phase1_v4.db para conservarla intacta."
            )
        connection.executescript(DDL)
        connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )
        connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
            ("raw_codec", CODEC),
        )
        connection.commit()
        self._connection = connection

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("La base de datos no está abierta")
        return self._connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def append_events(self, events: Iterable[RawEvent]) -> int:
        batch = tuple(events)
        if not batch:
            return 0

        raw = ("\n".join(_event_json(event) for event in batch) + "\n").encode(
            "utf-8"
        )
        compressed = zlib.compress(raw, level=6)
        content_sha256 = hashlib.sha256(raw).hexdigest()
        compressed_sha256 = hashlib.sha256(compressed).hexdigest()
        counts = Counter((event.source, event.stream) for event in batch)
        hourly_counts = Counter(
            (event.source, event.stream, event.received_at[:13])
            for event in batch
        )

        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO raw_chunks(
                    chunk_id, created_at, first_received_at, last_received_at,
                    first_sequence, last_sequence,
                    first_monotonic_ns, last_monotonic_ns,
                    event_count, codec,
                    raw_bytes, compressed_bytes, content_sha256,
                    compressed_sha256, payload_blob
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    utc_now_iso(),
                    batch[0].received_at,
                    batch[-1].received_at,
                    batch[0].sequence,
                    batch[-1].sequence,
                    batch[0].monotonic_ns,
                    batch[-1].monotonic_ns,
                    len(batch),
                    CODEC,
                    len(raw),
                    len(compressed),
                    content_sha256,
                    compressed_sha256,
                    compressed,
                ),
            )
            if cursor.rowcount == 0:
                self.connection.rollback()
                return 0
            self.connection.executemany(
                """
                INSERT INTO event_counts(source, stream, event_count)
                VALUES (?, ?, ?)
                ON CONFLICT(source, stream) DO UPDATE SET
                    event_count=event_count + excluded.event_count
                """,
                (
                    (source, stream, count)
                    for (source, stream), count in counts.items()
                ),
            )
            self.connection.executemany(
                """
                INSERT INTO event_hourly_counts(
                    source, stream, utc_hour, event_count
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(source, stream, utc_hour) DO UPDATE SET
                    event_count=event_count + excluded.event_count
                """,
                (
                    (source, stream, utc_hour, count)
                    for (
                        source,
                        stream,
                        utc_hour,
                    ), count in hourly_counts.items()
                ),
            )
            self.connection.commit()
            return len(batch)

    def iter_events(self) -> Iterator[RawEvent]:
        """Reproduce en orden todos los eventos originales."""
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT codec, payload_blob
                FROM raw_chunks
                ORDER BY first_sequence
                """
            ).fetchall()
        for row in rows:
            raw = _decode_chunk(row["payload_blob"], row["codec"])
            for line in raw.splitlines():
                if not line:
                    continue
                yield RawEvent(**json.loads(line))

    def save_market(self, market: MarketDefinition) -> None:
        now = utc_now_iso()
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO markets(
                    condition_id, slug, event_id, question, start_at, end_at,
                    resolution_source, outcomes_json, token_ids_json,
                    active, closed, accepting_orders, first_seen_at,
                    last_seen_at, payload_raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(condition_id) DO UPDATE SET
                    slug=excluded.slug,
                    event_id=excluded.event_id,
                    question=excluded.question,
                    start_at=excluded.start_at,
                    end_at=excluded.end_at,
                    resolution_source=excluded.resolution_source,
                    outcomes_json=excluded.outcomes_json,
                    token_ids_json=excluded.token_ids_json,
                    active=excluded.active,
                    closed=excluded.closed,
                    accepting_orders=excluded.accepting_orders,
                    last_seen_at=excluded.last_seen_at,
                    payload_raw=excluded.payload_raw
                """,
                (
                    market.condition_id,
                    market.slug,
                    market.event_id,
                    market.question,
                    market.start_at,
                    market.end_at,
                    market.resolution_source,
                    json.dumps(market.outcomes, ensure_ascii=False),
                    json.dumps(market.token_ids, ensure_ascii=False),
                    int(market.active),
                    int(market.closed),
                    int(market.accepting_orders),
                    now,
                    now,
                    market.payload_raw,
                ),
            )
            self.connection.commit()

    def start_run(self, component: str) -> str:
        run_id = str(uuid.uuid4())
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO collector_runs(
                    run_id, component, started_at, status
                ) VALUES (?, ?, ?, ?)
                """,
                (run_id, component, utc_now_iso(), "RUNNING"),
            )
            self.connection.commit()
        return run_id

    def finish_run(
        self, run_id: str, status: str, error: str | None = None
    ) -> None:
        with self._lock:
            self.connection.execute(
                """
                UPDATE collector_runs
                SET ended_at=?, status=?, error=?
                WHERE run_id=?
                """,
                (utc_now_iso(), status, error, run_id),
            )
            self.connection.commit()

    def _database_bytes(self) -> int:
        return sum(
            candidate.stat().st_size
            for candidate in (
                self.path,
                Path(f"{self.path}-wal"),
                Path(f"{self.path}-shm"),
            )
            if candidate.exists()
        )

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self.connection.execute(
                "SELECT COALESCE(SUM(event_count), 0) AS count FROM event_counts"
            ).fetchone()["count"]
            markets = self.connection.execute(
                "SELECT COUNT(*) AS count FROM markets"
            ).fetchone()["count"]
            real_markets = self.connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM markets
                WHERE slug NOT LIKE '%synthetic%'
                """
            ).fetchone()["count"]
            bounds = self.connection.execute(
                """
                SELECT MIN(first_received_at) AS first_value,
                       MAX(last_received_at) AS last_value,
                       COUNT(*) AS chunks,
                       COALESCE(SUM(raw_bytes), 0) AS raw_bytes,
                       COALESCE(SUM(compressed_bytes), 0) AS compressed_bytes
                FROM raw_chunks
                """
            ).fetchone()
            by_source = [
                {
                    "source": row["source"],
                    "stream": row["stream"],
                    "count": row["event_count"],
                }
                for row in self.connection.execute(
                    """
                    SELECT source, stream, event_count
                    FROM event_counts
                    ORDER BY source, stream
                    """
                )
            ]
        raw_bytes = int(bounds["raw_bytes"])
        compressed_bytes = int(bounds["compressed_bytes"])
        return {
            "database": str(self.path),
            "schema_version": int(SCHEMA_VERSION),
            "events": int(total),
            "markets": int(markets),
            "real_markets": int(real_markets),
            "first_received_at": bounds["first_value"],
            "last_received_at": bounds["last_value"],
            "by_source": by_source,
            "storage": {
                "chunks": int(bounds["chunks"]),
                "database_bytes": self._database_bytes(),
                "raw_bytes": raw_bytes,
                "compressed_bytes": compressed_bytes,
                "compression_ratio": (
                    round(raw_bytes / compressed_bytes, 3)
                    if compressed_bytes
                    else None
                ),
            },
        }

    def diagnostics(self) -> dict[str, Any]:
        result = self.stats()
        first = result["first_received_at"]
        last = result["last_received_at"]
        observed_seconds = 0.0
        if first and last:
            observed_seconds = max(
                0.0,
                (
                    datetime.fromisoformat(last)
                    - datetime.fromisoformat(first)
                ).total_seconds(),
            )
        storage = result["storage"]
        payload_rate = (
            storage["compressed_bytes"] / observed_seconds
            if observed_seconds > 0
            else 0.0
        )
        result["observation"] = {
            "seconds": round(observed_seconds, 3),
            "events_per_second": (
                round(result["events"] / observed_seconds, 3)
                if observed_seconds > 0
                else None
            ),
            "projected_compressed_gb_24h": (
                round(payload_rate * 86400 / 1_000_000_000, 3)
                if observed_seconds >= 10
                else None
            ),
            "projection_note": (
                "Proyección preliminar; validar con una prueba de al menos 10 minutos."
            ),
        }
        return result

    def verify_fast(self) -> dict[str, Any]:
        """Verificación apta para datasets grandes.

        El SHA-256 del blob comprimido detecta cualquier cambio en los bytes
        persistidos sin decodificar cientos de millones de envelopes. El
        checksum del contenido sin comprimir y los checksums por evento quedan
        disponibles para una auditoría profunda posterior.
        """
        checked_chunks = 0
        corrupt_chunks = 0
        chunk_events = 0
        sequence_range_errors = 0
        monotonic_range_errors = 0
        wall_clock_adjustments = 0
        max_clock_rollback_seconds = 0.0
        previous_last_sequence: int | None = None
        previous_last_monotonic: int | None = None
        previous_last_received: str | None = None
        with self._lock:
            quick_check = self.connection.execute(
                "PRAGMA quick_check"
            ).fetchone()[0]
            rows = self.connection.execute(
                """
                SELECT first_received_at, last_received_at,
                       first_sequence, last_sequence,
                       first_monotonic_ns, last_monotonic_ns,
                       event_count,
                       compressed_sha256, payload_blob
                FROM raw_chunks
                ORDER BY first_sequence
                """
            )
            for row in rows:
                checked_chunks += 1
                chunk_events += int(row["event_count"])
                if (
                    hashlib.sha256(row["payload_blob"]).hexdigest()
                    != row["compressed_sha256"]
                ):
                    corrupt_chunks += 1
                if (
                    row["first_sequence"] > row["last_sequence"]
                    or (
                        previous_last_sequence is not None
                        and row["first_sequence"] <= previous_last_sequence
                    )
                ):
                    sequence_range_errors += 1
                if (
                    row["first_monotonic_ns"] > row["last_monotonic_ns"]
                    or (
                        previous_last_monotonic is not None
                        and row["first_monotonic_ns"]
                        <= previous_last_monotonic
                    )
                ):
                    monotonic_range_errors += 1
                if row["first_received_at"] > row["last_received_at"]:
                    wall_clock_adjustments += 1
                    rollback = (
                        datetime.fromisoformat(row["first_received_at"])
                        - datetime.fromisoformat(row["last_received_at"])
                    ).total_seconds()
                    max_clock_rollback_seconds = max(
                        max_clock_rollback_seconds, rollback
                    )
                if (
                    previous_last_received is not None
                    and row["first_received_at"] < previous_last_received
                ):
                    wall_clock_adjustments += 1
                    rollback = (
                        datetime.fromisoformat(previous_last_received)
                        - datetime.fromisoformat(row["first_received_at"])
                    ).total_seconds()
                    max_clock_rollback_seconds = max(
                        max_clock_rollback_seconds, rollback
                    )
                previous_last_sequence = int(row["last_sequence"])
                previous_last_monotonic = int(row["last_monotonic_ns"])
                previous_last_received = str(row["last_received_at"])
            counted_events = self.connection.execute(
                """
                SELECT COALESCE(SUM(event_count), 0)
                FROM event_counts
                """
            ).fetchone()[0]
        result = {
            "sqlite_quick_check": str(quick_check),
            "checked_chunks": checked_chunks,
            "corrupt_chunks": corrupt_chunks,
            "chunk_events": chunk_events,
            "counted_events": int(counted_events),
            "event_count_consistent": chunk_events == int(counted_events),
            "sequence_range_errors": sequence_range_errors,
            "monotonic_range_errors": monotonic_range_errors,
            "wall_clock_adjustments": wall_clock_adjustments,
            "max_clock_rollback_seconds": round(
                max_clock_rollback_seconds, 6
            ),
        }
        result["ok"] = (
            result["sqlite_quick_check"] == "ok"
            and result["corrupt_chunks"] == 0
            and result["event_count_consistent"]
            and result["sequence_range_errors"] == 0
            and result["monotonic_range_errors"] == 0
        )
        return result

    def audit(self, *, min_hours: float, min_coverage: float) -> dict[str, Any]:
        if min_hours < 0:
            raise ValueError("min_hours no puede ser negativo")
        if not 0 <= min_coverage <= 1:
            raise ValueError("min_coverage debe estar entre 0 y 1")
        diagnostics = self.diagnostics()
        integrity = self.verify_fast()
        counts = {
            (row["source"], row["stream"]): row["count"]
            for row in diagnostics["by_source"]
        }
        first = diagnostics["first_received_at"]
        last = diagnostics["last_received_at"]
        observed_seconds = (
            max(
                0.0,
                (
                    datetime.fromisoformat(last)
                    - datetime.fromisoformat(first)
                ).total_seconds(),
            )
            if first and last
            else 0.0
        )
        expected_markets = (
            math.floor(observed_seconds / 300) + 1
            if observed_seconds > 0
            else 0
        )
        real_markets = diagnostics["real_markets"]
        market_coverage = (
            min(1.0, real_markets / expected_markets)
            if expected_markets
            else 0.0
        )
        expected_hours = (
            math.floor(observed_seconds / 3600) + 1
            if observed_seconds > 0
            else 0
        )

        def feed_quality(
            source: str,
            stream: str,
            minimum_rate: float,
        ) -> dict[str, Any]:
            count = int(counts.get((source, stream), 0))
            with self._lock:
                active_hours = self.connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM event_hourly_counts
                    WHERE source=? AND stream=? AND event_count > 0
                    """,
                    (source, stream),
                ).fetchone()[0]
            rate = count / observed_seconds if observed_seconds > 0 else 0.0
            hourly_coverage = (
                min(1.0, int(active_hours) / expected_hours)
                if expected_hours
                else 0.0
            )
            return {
                "count": count,
                "events_per_second": round(rate, 6),
                "active_hours": int(active_hours),
                "expected_hours": expected_hours,
                "hourly_coverage": round(hourly_coverage, 6),
                "minimum_events_per_second": minimum_rate,
                "passed": (
                    rate >= minimum_rate
                    and hourly_coverage >= 0.95
                ),
            }

        binance_quality = feed_quality("binance", "aggTrade", 0.1)
        chainlink_quality = feed_quality(
            "rtds", "crypto_prices_chainlink", 0.2
        )
        required_feeds = {
            "clob": any(
                source == "clob" and count > 0
                for (source, _), count in counts.items()
            ),
            "gamma": any(
                source == "gamma" and count > 0
                for (source, _), count in counts.items()
            ),
            "binance": bool(binance_quality["passed"]),
            "chainlink": bool(chainlink_quality["passed"]),
        }
        with self._lock:
            failed_runs = self.connection.execute(
                """
                SELECT COUNT(*)
                FROM collector_runs
                WHERE status='FAILED' OR error IS NOT NULL
                """
            ).fetchone()[0]
        checks = {
            "minimum_duration": observed_seconds >= min_hours * 3600,
            "market_coverage": market_coverage >= min_coverage,
            "required_feeds": all(required_feeds.values()),
            "integrity": bool(integrity["ok"]),
            "no_failed_collectors": int(failed_runs) == 0,
        }
        return {
            "passed": all(checks.values()),
            "checks": checks,
            "requirements": {
                "minimum_hours": min_hours,
                "minimum_market_coverage": min_coverage,
                "minimum_feed_hourly_coverage": 0.95,
            },
            "observed": {
                "hours": round(observed_seconds / 3600, 3),
                "real_markets": real_markets,
                "expected_markets": expected_markets,
                "market_coverage": round(market_coverage, 6),
                "required_feeds": required_feeds,
                "feed_quality": {
                    "binance": binance_quality,
                    "chainlink": chainlink_quality,
                },
                "failed_collector_runs": int(failed_runs),
            },
            "integrity": integrity,
            "storage": diagnostics["storage"],
            "events": diagnostics["events"],
        }

    def verify(self) -> dict[str, int]:
        checked = 0
        corrupt = 0
        checked_chunks = 0
        corrupt_chunks = 0
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT event_count, codec, content_sha256, payload_blob
                FROM raw_chunks
                ORDER BY first_sequence
                """
            ).fetchall()
        for row in rows:
            checked_chunks += 1
            try:
                raw = _decode_chunk(row["payload_blob"], row["codec"])
            except (ValueError, zlib.error):
                corrupt_chunks += 1
                corrupt += int(row["event_count"])
                checked += int(row["event_count"])
                continue
            lines = [line for line in raw.splitlines() if line]
            chunk_bad = (
                hashlib.sha256(raw).hexdigest() != row["content_sha256"]
                or len(lines) != row["event_count"]
            )
            for line in lines:
                checked += 1
                try:
                    item = json.loads(line)
                    expected = payload_checksum(
                        item["source"], item["stream"], item["payload_raw"]
                    )
                    if expected != item["checksum"]:
                        corrupt += 1
                        chunk_bad = True
                except (KeyError, TypeError, json.JSONDecodeError):
                    corrupt += 1
                    chunk_bad = True
            if chunk_bad:
                corrupt_chunks += 1
        return {
            "checked": checked,
            "corrupt": corrupt,
            "checked_chunks": checked_chunks,
            "corrupt_chunks": corrupt_chunks,
        }


class AsyncEventWriter:
    _SENTINEL = object()

    def __init__(
        self,
        store: SQLiteStore,
        *,
        batch_size: int = 1_000,
        flush_seconds: float = 0.5,
    ) -> None:
        self.store = store
        self.batch_size = batch_size
        self.flush_seconds = flush_seconds
        self.queue: asyncio.Queue[RawEvent | object] = asyncio.Queue(
            maxsize=50_000
        )
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="sqlite-compressed-chunk-writer"
            )

    async def submit(self, event: RawEvent) -> None:
        await self.queue.put(event)

    async def close(self) -> None:
        if self._task is None:
            return
        await self.queue.put(self._SENTINEL)
        await self._task
        self._task = None

    async def _run(self) -> None:
        batch: list[RawEvent] = []
        stopping = False
        while not stopping:
            try:
                item = await asyncio.wait_for(
                    self.queue.get(), timeout=self.flush_seconds
                )
            except TimeoutError:
                item = None
            if item is self._SENTINEL:
                stopping = True
            elif isinstance(item, RawEvent):
                batch.append(item)
            if batch and (
                len(batch) >= self.batch_size
                or item is None
                or stopping
            ):
                await asyncio.to_thread(self.store.append_events, tuple(batch))
                batch.clear()
