from __future__ import annotations

import bisect
import hashlib
import json
import logging
import sqlite3
import time
import urllib.error
import urllib.request
import zlib
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


LOGGER = logging.getLogger("phase2-silver")
SILVER_SCHEMA_VERSION = "1"
RAW_CODEC = "zlib-jsonl-v1"
LATE_EVENT_GRACE_MS = 60_000
TRAINING_COVERAGE_THRESHOLD = 0.95
LABEL_CACHE_SCHEMA_VERSION = 1

QUALITY_MISSING_CHAINLINK = 1
QUALITY_MISSING_UP_BBA = 2
QUALITY_MISSING_DOWN_BBA = 4
QUALITY_MISSING_BINANCE = 8
QUALITY_INVALID_BBA = 16


SILVER_DDL = """
CREATE TABLE IF NOT EXISTS silver_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    event_id TEXT,
    question TEXT NOT NULL,
    market_start_ms INTEGER NOT NULL,
    market_end_ms INTEGER NOT NULL,
    up_token_id TEXT NOT NULL,
    down_token_id TEXT NOT NULL,
    resolution_source TEXT,
    label TEXT NOT NULL,
    label_source TEXT,
    label_verified INTEGER NOT NULL,
    gamma_market_id TEXT,
    gamma_closed INTEGER,
    gamma_resolution_status TEXT,
    price_to_beat REAL,
    outcome_prices_json TEXT,
    resolution_rule TEXT,
    gamma_payload_raw TEXT
);

CREATE TABLE IF NOT EXISTS second_features (
    condition_id TEXT NOT NULL,
    second_offset INTEGER NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    time_remaining_seconds INTEGER NOT NULL,
    chainlink_price REAL,
    chainlink_age_ms INTEGER,
    binance_price REAL,
    binance_age_ms INTEGER,
    up_best_bid REAL,
    up_best_ask REAL,
    up_mid REAL,
    up_spread REAL,
    down_best_bid REAL,
    down_best_ask REAL,
    down_mid REAL,
    down_spread REAL,
    up_bid_depth_1c REAL,
    up_ask_depth_1c REAL,
    up_bid_depth_5c REAL,
    up_ask_depth_5c REAL,
    down_bid_depth_1c REAL,
    down_ask_depth_1c REAL,
    down_bid_depth_5c REAL,
    down_ask_depth_5c REAL,
    up_last_trade_price REAL,
    up_last_trade_size REAL,
    down_last_trade_price REAL,
    down_last_trade_size REAL,
    market_mid_sum REAL,
    market_ask_overround REAL,
    price_change_messages INTEGER NOT NULL,
    book_messages INTEGER NOT NULL,
    best_bid_ask_messages INTEGER NOT NULL,
    polymarket_trade_count INTEGER NOT NULL,
    polymarket_trade_volume REAL NOT NULL,
    chainlink_updates INTEGER NOT NULL,
    binance_trade_count INTEGER NOT NULL,
    binance_trade_volume REAL NOT NULL,
    quality_flags INTEGER NOT NULL,
    PRIMARY KEY(condition_id, second_offset),
    FOREIGN KEY(condition_id) REFERENCES markets(condition_id)
);

CREATE INDEX IF NOT EXISTS idx_silver_timestamp
    ON second_features(timestamp_ms);
"""


@dataclass(frozen=True, slots=True)
class ResolutionInfo:
    label: str = "Unknown"
    source: str | None = None
    verified: bool = False
    gamma_market_id: str | None = None
    closed: bool | None = None
    status: str | None = None
    price_to_beat: float | None = None
    outcome_prices: tuple[float, ...] = ()
    rule: str | None = None
    payload_raw: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SilverMarket:
    condition_id: str
    slug: str
    event_id: str | None
    question: str
    start_ms: int
    end_ms: int
    up_token_id: str
    down_token_id: str
    resolution_source: str | None
    resolution: ResolutionInfo = field(default_factory=ResolutionInfo)

    @property
    def token_outcomes(self) -> dict[str, str]:
        return {
            self.up_token_id: "up",
            self.down_token_id: "down",
        }


def _parse_json_array(value: str) -> list[Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise ValueError("Se esperaba una lista JSON")
    return decoded


def _coerce_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        return _parse_json_array(value)
    return []


def _market_epoch_from_slug(slug: str) -> int:
    prefix = "btc-updown-5m-"
    if not slug.startswith(prefix):
        raise ValueError(f"Slug no compatible con BTC 5m: {slug}")
    value = int(slug.removeprefix(prefix))
    if value % 300 != 0:
        raise ValueError(f"El slug no está alineado a cinco minutos: {slug}")
    return value


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=60,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _source_schema(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        raise ValueError("La base fuente no contiene schema_version")
    version = int(row["value"])
    if version not in {3, 4}:
        raise ValueError(
            f"Solo se admiten capturas V3 o V4; se encontró V{version}."
        )
    return version


def load_silver_markets(
    connection: sqlite3.Connection,
    *,
    max_markets: int | None,
) -> list[SilverMarket]:
    rows = connection.execute(
        """
        SELECT condition_id, slug, event_id, question,
               resolution_source, outcomes_json, token_ids_json
        FROM markets
        WHERE slug LIKE 'btc-updown-5m-%'
          AND slug NOT LIKE '%synthetic%'
        """
    ).fetchall()
    markets: list[SilverMarket] = []
    for row in rows:
        outcomes = [str(item) for item in _parse_json_array(row["outcomes_json"])]
        tokens = [str(item) for item in _parse_json_array(row["token_ids_json"])]
        mapping = dict(zip(outcomes, tokens, strict=False))
        if "Up" not in mapping or "Down" not in mapping:
            raise ValueError(
                f"Mercado sin tokens Up/Down válidos: {row['slug']}"
            )
        start_seconds = _market_epoch_from_slug(str(row["slug"]))
        markets.append(
            SilverMarket(
                condition_id=str(row["condition_id"]),
                slug=str(row["slug"]),
                event_id=(
                    str(row["event_id"])
                    if row["event_id"] is not None
                    else None
                ),
                question=str(row["question"]),
                start_ms=start_seconds * 1000,
                end_ms=(start_seconds + 300) * 1000,
                up_token_id=mapping["Up"],
                down_token_id=mapping["Down"],
                resolution_source=(
                    str(row["resolution_source"])
                    if row["resolution_source"] is not None
                    else None
                ),
            )
        )
    markets.sort(key=lambda market: market.start_ms)
    for previous, current in zip(markets, markets[1:]):
        if current.start_ms <= previous.start_ms:
            raise ValueError("Existen mercados duplicados o fuera de orden")
    if max_markets is not None:
        markets = markets[:max_markets]
    if not markets:
        raise ValueError("No se encontraron mercados BTC Up/Down de 5 minutos")
    return markets


class GammaResolutionClient:
    def __init__(
        self,
        *,
        base_url: str = "https://gamma-api.polymarket.com",
        timeout_seconds: float = 20.0,
        request_pause_seconds: float = 0.10,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.request_pause_seconds = request_pause_seconds

    @staticmethod
    def parse(payload_raw: str) -> ResolutionInfo:
        payload = json.loads(payload_raw)
        if not isinstance(payload, dict):
            raise ValueError("Gamma devolvió un objeto no válido")
        outcomes = _coerce_array(payload.get("outcomes"))
        price_values = _coerce_array(payload.get("outcomePrices"))
        prices = tuple(float(value) for value in price_values)
        closed = bool(payload.get("closed"))
        label = "Unknown"
        verified = False
        if closed and len(outcomes) == len(prices) and prices:
            winners = [
                str(outcome)
                for outcome, price in zip(outcomes, prices, strict=True)
                if price >= 0.99
            ]
            losers = [price for price in prices if price <= 0.01]
            if len(winners) == 1 and len(losers) == len(prices) - 1:
                label = winners[0]
                verified = label in {"Up", "Down"}
        metadata = payload.get("eventMetadata")
        metadata_dict = metadata if isinstance(metadata, dict) else {}
        if not metadata_dict:
            events = payload.get("events")
            first_event = (
                events[0]
                if isinstance(events, list)
                and events
                and isinstance(events[0], dict)
                else {}
            )
            nested_metadata = first_event.get("eventMetadata")
            metadata_dict = (
                nested_metadata
                if isinstance(nested_metadata, dict)
                else {}
            )
        price_to_beat_raw = metadata_dict.get("priceToBeat")
        try:
            price_to_beat = (
                float(price_to_beat_raw)
                if price_to_beat_raw is not None
                else None
            )
        except (TypeError, ValueError):
            price_to_beat = None
        return ResolutionInfo(
            label=label,
            source="gamma_outcome_prices" if verified else None,
            verified=verified,
            gamma_market_id=(
                str(payload["id"]) if payload.get("id") is not None else None
            ),
            closed=closed,
            status=(
                str(payload["umaResolutionStatus"])
                if payload.get("umaResolutionStatus") is not None
                else None
            ),
            price_to_beat=price_to_beat,
            outcome_prices=prices,
            rule=(
                str(payload["description"])
                if payload.get("description") is not None
                else None
            ),
            payload_raw=payload_raw,
        )

    def fetch(self, slug: str) -> ResolutionInfo:
        url = f"{self.base_url}/markets/slug/{slug}"
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "polymarket-quant-bot-phase2/0.6.1",
                    },
                )
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    raw = response.read().decode("utf-8")
                result = self.parse(raw)
                time.sleep(self.request_pause_seconds)
                return result
            except (
                OSError,
                TimeoutError,
                ValueError,
                json.JSONDecodeError,
                urllib.error.URLError,
            ) as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(2**attempt)
        return ResolutionInfo(
            error=(
                f"{type(last_error).__name__}: {last_error}"
                if last_error is not None
                else "Error desconocido"
            )
        )


def _load_resolution_cache(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"No se pudo leer el caché de etiquetas {path}: {exc}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != LABEL_CACHE_SCHEMA_VERSION
        or not isinstance(payload.get("markets"), dict)
    ):
        raise ValueError(f"Formato de caché de etiquetas no válido: {path}")
    return {
        str(slug): str(raw)
        for slug, raw in payload["markets"].items()
        if isinstance(raw, str)
    }


def _save_resolution_cache(path: Path, cache: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    payload = {
        "schema_version": LABEL_CACHE_SCHEMA_VERSION,
        "markets": dict(sorted(cache.items())),
    }
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def enrich_resolutions(
    markets: Iterable[SilverMarket],
    *,
    fetch_labels: bool,
    client: GammaResolutionClient | None = None,
    cache_path: Path | None = None,
) -> list[SilverMarket]:
    market_list = list(markets)
    if not fetch_labels:
        return market_list
    resolver = client or GammaResolutionClient()
    cache = _load_resolution_cache(cache_path)
    enriched: list[SilverMarket] = []
    total = len(market_list)
    cache_hits = 0
    unsaved = 0
    for index, market in enumerate(market_list, start=1):
        resolution = ResolutionInfo()
        cached_raw = cache.get(market.slug)
        if cached_raw is not None:
            try:
                cached = GammaResolutionClient.parse(cached_raw)
            except (ValueError, json.JSONDecodeError):
                cache.pop(market.slug, None)
            else:
                if cached.verified:
                    resolution = cached
                    cache_hits += 1
        if not resolution.verified:
            resolution = resolver.fetch(market.slug)
            if resolution.verified and resolution.payload_raw is not None:
                cache[market.slug] = resolution.payload_raw
                unsaved += 1
        enriched.append(replace(market, resolution=resolution))
        if cache_path is not None and unsaved >= 10:
            _save_resolution_cache(cache_path, cache)
            unsaved = 0
        if (
            cache_path is not None
            and not resolution.verified
            and unsaved > 0
        ):
            _save_resolution_cache(cache_path, cache)
            unsaved = 0
        if index == 1 or index % 10 == 0 or index == total:
            LOGGER.info(
                "Etiquetas Gamma: %d/%d | verificadas=%d | caché=%d",
                index,
                total,
                sum(item.resolution.verified for item in enriched),
                cache_hits,
            )
    if cache_path is not None and unsaved > 0:
        _save_resolution_cache(cache_path, cache)
    return enriched


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _book_metrics(payload: dict[str, Any]) -> dict[str, float | None]:
    bids = [
        (_number(level.get("price")), _number(level.get("size")))
        for level in payload.get("bids", [])
        if isinstance(level, dict)
    ]
    asks = [
        (_number(level.get("price")), _number(level.get("size")))
        for level in payload.get("asks", [])
        if isinstance(level, dict)
    ]
    valid_bids = [
        (price, size)
        for price, size in bids
        if price is not None and size is not None and size >= 0
    ]
    valid_asks = [
        (price, size)
        for price, size in asks
        if price is not None and size is not None and size >= 0
    ]
    best_bid = max((price for price, _ in valid_bids), default=None)
    best_ask = min((price for price, _ in valid_asks), default=None)
    result: dict[str, float | None] = {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_depth_1c": None,
        "ask_depth_1c": None,
        "bid_depth_5c": None,
        "ask_depth_5c": None,
    }
    if best_bid is not None:
        result["bid_depth_1c"] = sum(
            size for price, size in valid_bids if price >= best_bid - 0.01
        )
        result["bid_depth_5c"] = sum(
            size for price, size in valid_bids if price >= best_bid - 0.05
        )
    if best_ask is not None:
        result["ask_depth_1c"] = sum(
            size for price, size in valid_asks if price <= best_ask + 0.01
        )
        result["ask_depth_5c"] = sum(
            size for price, size in valid_asks if price <= best_ask + 0.05
        )
    return result


@dataclass(slots=True)
class SecondBucket:
    values: dict[str, float | int | None] = field(default_factory=dict)
    counters: Counter[str] = field(default_factory=Counter)
    volumes: Counter[str] = field(default_factory=Counter)


class MarketAccumulator:
    def __init__(
        self,
        market: SilverMarket,
        *,
        initial_chainlink: tuple[float, int] | None,
        initial_binance: tuple[float, int] | None,
    ) -> None:
        self.market = market
        self.initial_values: dict[str, float | int | None] = {}
        if initial_chainlink is not None:
            self.initial_values["chainlink_price"] = initial_chainlink[0]
            self.initial_values["chainlink_timestamp_ms"] = initial_chainlink[1]
        if initial_binance is not None:
            self.initial_values["binance_price"] = initial_binance[0]
            self.initial_values["binance_timestamp_ms"] = initial_binance[1]
        self.buckets: dict[int, SecondBucket] = {}

    def _bucket(self, timestamp_ms: int) -> SecondBucket | None:
        offset = (timestamp_ms - self.market.start_ms) // 1000
        if not 0 <= offset < 300:
            return None
        return self.buckets.setdefault(int(offset), SecondBucket())

    def update(
        self,
        timestamp_ms: int,
        values: dict[str, float | int | None],
    ) -> None:
        bucket = self._bucket(timestamp_ms)
        if bucket is not None:
            bucket.values.update(values)

    def count(
        self,
        timestamp_ms: int,
        counter: str,
        *,
        amount: int = 1,
        volume_name: str | None = None,
        volume: float = 0.0,
    ) -> None:
        bucket = self._bucket(timestamp_ms)
        if bucket is None:
            return
        bucket.counters[counter] += amount
        if volume_name is not None:
            bucket.volumes[volume_name] += volume


class SilverWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None
        self.rows_written = 0
        self.core_complete_rows = 0
        self.binance_rows = 0

    def open(self) -> None:
        if self.path.exists():
            raise ValueError(
                f"La salida ya existe y no será sobrescrita: {self.path}"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=60)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SILVER_DDL)
        connection.execute(
            "INSERT INTO silver_meta(key,value) VALUES(?,?)",
            ("schema_version", SILVER_SCHEMA_VERSION),
        )
        connection.commit()
        self.connection = connection

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("La salida Silver no está abierta")
        return self.connection

    def save_meta(self, key: str, value: Any) -> None:
        self.db.execute(
            """
            INSERT OR REPLACE INTO silver_meta(key,value)
            VALUES(?,?)
            """,
            (
                key,
                json.dumps(value, ensure_ascii=False, sort_keys=True),
            ),
        )

    def save_market(self, market: SilverMarket) -> None:
        resolution = market.resolution
        self.db.execute(
            """
            INSERT INTO markets(
                condition_id, slug, event_id, question,
                market_start_ms, market_end_ms,
                up_token_id, down_token_id, resolution_source,
                label, label_source, label_verified,
                gamma_market_id, gamma_closed, gamma_resolution_status,
                price_to_beat, outcome_prices_json, resolution_rule,
                gamma_payload_raw
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                market.condition_id,
                market.slug,
                market.event_id,
                market.question,
                market.start_ms,
                market.end_ms,
                market.up_token_id,
                market.down_token_id,
                market.resolution_source,
                resolution.label,
                resolution.source,
                int(resolution.verified),
                resolution.gamma_market_id,
                (
                    int(resolution.closed)
                    if resolution.closed is not None
                    else None
                ),
                resolution.status,
                resolution.price_to_beat,
                json.dumps(resolution.outcome_prices),
                resolution.rule,
                resolution.payload_raw,
            ),
        )

    @staticmethod
    def _mid(bid: Any, ask: Any) -> float | None:
        if bid is None or ask is None:
            return None
        return (float(bid) + float(ask)) / 2

    @staticmethod
    def _spread(bid: Any, ask: Any) -> float | None:
        if bid is None or ask is None:
            return None
        return float(ask) - float(bid)

    def flush_market(self, accumulator: MarketAccumulator) -> None:
        current = dict(accumulator.initial_values)
        rows: list[tuple[Any, ...]] = []
        for second in range(300):
            bucket = accumulator.buckets.get(second, SecondBucket())
            current.update(bucket.values)
            timestamp_ms = accumulator.market.start_ms + second * 1000
            up_bid = current.get("up_best_bid")
            up_ask = current.get("up_best_ask")
            down_bid = current.get("down_best_bid")
            down_ask = current.get("down_best_ask")
            chainlink = current.get("chainlink_price")
            binance = current.get("binance_price")
            chainlink_ts = current.get("chainlink_timestamp_ms")
            binance_ts = current.get("binance_timestamp_ms")
            up_mid = self._mid(up_bid, up_ask)
            down_mid = self._mid(down_bid, down_ask)
            flags = 0
            if chainlink is None:
                flags |= QUALITY_MISSING_CHAINLINK
            if up_bid is None or up_ask is None:
                flags |= QUALITY_MISSING_UP_BBA
            if down_bid is None or down_ask is None:
                flags |= QUALITY_MISSING_DOWN_BBA
            if binance is None:
                flags |= QUALITY_MISSING_BINANCE
            if (
                (up_bid is not None and up_ask is not None and up_bid > up_ask)
                or (
                    down_bid is not None
                    and down_ask is not None
                    and down_bid > down_ask
                )
            ):
                flags |= QUALITY_INVALID_BBA
            core_complete = flags & (
                QUALITY_MISSING_CHAINLINK
                | QUALITY_MISSING_UP_BBA
                | QUALITY_MISSING_DOWN_BBA
                | QUALITY_INVALID_BBA
            ) == 0
            self.core_complete_rows += int(core_complete)
            self.binance_rows += int(binance is not None)
            rows.append(
                (
                    accumulator.market.condition_id,
                    second,
                    timestamp_ms,
                    300 - second,
                    chainlink,
                    (
                        max(0, timestamp_ms - int(chainlink_ts))
                        if chainlink_ts is not None
                        else None
                    ),
                    binance,
                    (
                        max(0, timestamp_ms - int(binance_ts))
                        if binance_ts is not None
                        else None
                    ),
                    up_bid,
                    up_ask,
                    up_mid,
                    self._spread(up_bid, up_ask),
                    down_bid,
                    down_ask,
                    down_mid,
                    self._spread(down_bid, down_ask),
                    current.get("up_bid_depth_1c"),
                    current.get("up_ask_depth_1c"),
                    current.get("up_bid_depth_5c"),
                    current.get("up_ask_depth_5c"),
                    current.get("down_bid_depth_1c"),
                    current.get("down_ask_depth_1c"),
                    current.get("down_bid_depth_5c"),
                    current.get("down_ask_depth_5c"),
                    current.get("up_last_trade_price"),
                    current.get("up_last_trade_size"),
                    current.get("down_last_trade_price"),
                    current.get("down_last_trade_size"),
                    (
                        up_mid + down_mid
                        if up_mid is not None and down_mid is not None
                        else None
                    ),
                    (
                        float(up_ask) + float(down_ask) - 1
                        if up_ask is not None and down_ask is not None
                        else None
                    ),
                    bucket.counters["price_change_messages"],
                    bucket.counters["book_messages"],
                    bucket.counters["best_bid_ask_messages"],
                    bucket.counters["polymarket_trade_count"],
                    float(bucket.volumes["polymarket_trade_volume"]),
                    bucket.counters["chainlink_updates"],
                    bucket.counters["binance_trade_count"],
                    float(bucket.volumes["binance_trade_volume"]),
                    flags,
                )
            )
        placeholders = ",".join("?" for _ in range(39))
        self.db.executemany(
            f"INSERT INTO second_features VALUES({placeholders})",
            rows,
        )
        self.rows_written += len(rows)
        if self.rows_written % 3_000 == 0:
            self.db.commit()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.commit()
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.connection.close()
            self.connection = None


class SilverBuilder:
    def __init__(
        self,
        markets: list[SilverMarket],
        writer: SilverWriter,
    ) -> None:
        self.markets = markets
        self.writer = writer
        self.starts = [market.start_ms for market in markets]
        self.by_condition = {
            market.condition_id: market for market in markets
        }
        self.token_outcomes = {
            token: (market, outcome)
            for market in markets
            for token, outcome in market.token_outcomes.items()
        }
        self.accumulators: dict[str, MarketAccumulator] = {}
        self.flushed: set[str] = set()
        self.latest_chainlink: tuple[float, int] | None = None
        self.latest_binance: tuple[float, int] | None = None
        self.max_timestamp_ms = 0
        self.late_events = 0
        self.event_counts: Counter[tuple[str, str]] = Counter()

    def _market_for_time(self, timestamp_ms: int) -> SilverMarket | None:
        index = bisect.bisect_right(self.starts, timestamp_ms) - 1
        if index < 0:
            return None
        market = self.markets[index]
        return market if timestamp_ms < market.end_ms else None

    def _accumulator(self, market: SilverMarket) -> MarketAccumulator | None:
        if market.condition_id in self.flushed:
            self.late_events += 1
            return None
        return self.accumulators.setdefault(
            market.condition_id,
            MarketAccumulator(
                market,
                initial_chainlink=self.latest_chainlink,
                initial_binance=self.latest_binance,
            ),
        )

    def _advance(self, timestamp_ms: int) -> None:
        self.max_timestamp_ms = max(self.max_timestamp_ms, timestamp_ms)
        completed = [
            condition_id
            for condition_id, accumulator in self.accumulators.items()
            if accumulator.market.end_ms + LATE_EVENT_GRACE_MS
            < self.max_timestamp_ms
        ]
        for condition_id in completed:
            accumulator = self.accumulators.pop(condition_id)
            self.writer.flush_market(accumulator)
            self.flushed.add(condition_id)

    @staticmethod
    def _payload(payload_raw: str) -> Any:
        return json.loads(payload_raw)

    @staticmethod
    def _timestamp(event: dict[str, Any], payload: Any) -> int | None:
        value = event.get("source_timestamp_ms")
        if value is not None:
            return int(value)
        if isinstance(payload, dict):
            raw = payload.get("timestamp") or payload.get("T") or payload.get("E")
            try:
                return int(raw) if raw is not None else None
            except (TypeError, ValueError):
                pass
        received = event.get("received_at")
        if isinstance(received, str):
            return int(datetime.fromisoformat(received).timestamp() * 1000)
        return None

    def _update_reference(
        self,
        *,
        timestamp_ms: int,
        price: float,
        source: str,
        quantity: float = 0.0,
    ) -> None:
        market = self._market_for_time(timestamp_ms)
        if source == "chainlink":
            self.latest_chainlink = (price, timestamp_ms)
        else:
            self.latest_binance = (price, timestamp_ms)
        if market is None:
            return
        accumulator = self._accumulator(market)
        if accumulator is None:
            return
        if source == "chainlink":
            accumulator.update(
                timestamp_ms,
                {
                    "chainlink_price": price,
                    "chainlink_timestamp_ms": timestamp_ms,
                },
            )
            accumulator.count(
                timestamp_ms, "chainlink_updates"
            )
        else:
            accumulator.update(
                timestamp_ms,
                {
                    "binance_price": price,
                    "binance_timestamp_ms": timestamp_ms,
                },
            )
            accumulator.count(
                timestamp_ms,
                "binance_trade_count",
                volume_name="binance_trade_volume",
                volume=price * quantity,
            )

    def _process_book(
        self,
        payload: Any,
        *,
        fallback_timestamp_ms: int,
    ) -> None:
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            condition = str(item.get("market", ""))
            market = self.by_condition.get(condition)
            token = str(item.get("asset_id", ""))
            outcome_info = self.token_outcomes.get(token)
            if market is None or outcome_info is None:
                continue
            timestamp_ms = int(item.get("timestamp", fallback_timestamp_ms))
            self._advance(timestamp_ms)
            accumulator = self._accumulator(market)
            if accumulator is None:
                continue
            outcome = outcome_info[1]
            metrics = _book_metrics(item)
            accumulator.update(
                timestamp_ms,
                {
                    f"{outcome}_{name}": value
                    for name, value in metrics.items()
                },
            )
            accumulator.count(timestamp_ms, "book_messages")

    def _process_price_change(
        self,
        payload: dict[str, Any],
        *,
        timestamp_ms: int,
    ) -> None:
        market = self.by_condition.get(str(payload.get("market", "")))
        if market is None:
            return
        accumulator = self._accumulator(market)
        if accumulator is None:
            return
        changes = payload.get("price_changes", [])
        for change in changes:
            if not isinstance(change, dict):
                continue
            token = str(change.get("asset_id", ""))
            outcome_info = self.token_outcomes.get(token)
            if outcome_info is None:
                continue
            outcome = outcome_info[1]
            accumulator.update(
                timestamp_ms,
                {
                    f"{outcome}_best_bid": _number(change.get("best_bid")),
                    f"{outcome}_best_ask": _number(change.get("best_ask")),
                },
            )
        accumulator.count(timestamp_ms, "price_change_messages")

    def _process_bba(
        self,
        payload: dict[str, Any],
        *,
        timestamp_ms: int,
    ) -> None:
        token = str(payload.get("asset_id", ""))
        outcome_info = self.token_outcomes.get(token)
        if outcome_info is None:
            return
        market, outcome = outcome_info
        accumulator = self._accumulator(market)
        if accumulator is None:
            return
        accumulator.update(
            timestamp_ms,
            {
                f"{outcome}_best_bid": _number(payload.get("best_bid")),
                f"{outcome}_best_ask": _number(payload.get("best_ask")),
            },
        )
        accumulator.count(timestamp_ms, "best_bid_ask_messages")

    def _process_trade(
        self,
        payload: dict[str, Any],
        *,
        timestamp_ms: int,
    ) -> None:
        token = str(payload.get("asset_id", ""))
        outcome_info = self.token_outcomes.get(token)
        if outcome_info is None:
            return
        market, outcome = outcome_info
        accumulator = self._accumulator(market)
        if accumulator is None:
            return
        price = _number(payload.get("price"))
        size = _number(payload.get("size"))
        accumulator.update(
            timestamp_ms,
            {
                f"{outcome}_last_trade_price": price,
                f"{outcome}_last_trade_size": size,
            },
        )
        accumulator.count(
            timestamp_ms,
            "polymarket_trade_count",
            volume_name="polymarket_trade_volume",
            volume=(price or 0.0) * (size or 0.0),
        )

    def process_event(self, event: dict[str, Any]) -> None:
        source = str(event.get("source", ""))
        stream = str(event.get("stream", ""))
        self.event_counts[(source, stream)] += 1
        if source not in {"clob", "rtds", "binance"}:
            return
        try:
            payload = self._payload(str(event.get("payload_raw", "")))
        except json.JSONDecodeError:
            return
        timestamp_ms = self._timestamp(event, payload)
        if timestamp_ms is None:
            return
        self._advance(timestamp_ms)

        if source == "rtds" and stream == "crypto_prices_chainlink":
            if not isinstance(payload, dict):
                return
            inner = payload.get("payload")
            if not isinstance(inner, dict):
                return
            price = _number(inner.get("value"))
            reference_ts = inner.get("timestamp")
            if price is not None:
                self._update_reference(
                    timestamp_ms=(
                        int(reference_ts)
                        if reference_ts is not None
                        else timestamp_ms
                    ),
                    price=price,
                    source="chainlink",
                )
            return

        if source == "binance" and stream == "aggTrade":
            if not isinstance(payload, dict):
                return
            price = _number(payload.get("p"))
            quantity = _number(payload.get("q"))
            trade_ts = payload.get("T") or payload.get("E")
            if price is not None:
                self._update_reference(
                    timestamp_ms=(
                        int(trade_ts) if trade_ts is not None else timestamp_ms
                    ),
                    price=price,
                    source="binance",
                    quantity=quantity or 0.0,
                )
            return

        if source != "clob":
            return
        if stream == "book":
            self._process_book(
                payload, fallback_timestamp_ms=timestamp_ms
            )
        elif stream == "price_change" and isinstance(payload, dict):
            self._process_price_change(payload, timestamp_ms=timestamp_ms)
        elif stream == "best_bid_ask" and isinstance(payload, dict):
            self._process_bba(payload, timestamp_ms=timestamp_ms)
        elif stream == "last_trade_price" and isinstance(payload, dict):
            self._process_trade(payload, timestamp_ms=timestamp_ms)

    def finish(self) -> None:
        for market in self.markets:
            if market.condition_id in self.flushed:
                continue
            accumulator = self.accumulators.pop(
                market.condition_id,
                MarketAccumulator(
                    market,
                    initial_chainlink=None,
                    initial_binance=None,
                ),
            )
            self.writer.flush_market(accumulator)
            self.flushed.add(market.condition_id)


def _iso_from_ms(value: int) -> str:
    return datetime.fromtimestamp(
        value / 1000, timezone.utc
    ).isoformat(timespec="microseconds")


def _training_market_quality(
    connection: sqlite3.Connection,
    *,
    expected_binance: bool,
) -> tuple[int, list[dict[str, Any]]]:
    rows = connection.execute(
        """
        SELECT
            m.slug,
            AVG(
                CASE
                    WHEN (f.quality_flags & 23) = 0 THEN 1.0
                    ELSE 0.0
                END
            ) AS core_coverage,
            AVG(
                CASE
                    WHEN f.binance_price IS NOT NULL THEN 1.0
                    ELSE 0.0
                END
            ) AS binance_coverage
        FROM markets AS m
        JOIN second_features AS f
          ON f.condition_id = m.condition_id
        GROUP BY m.condition_id, m.slug
        ORDER BY m.market_start_ms
        """
    ).fetchall()
    excluded: list[dict[str, Any]] = []
    eligible = 0
    for row in rows:
        slug = str(row[0])
        core_coverage = float(row[1] or 0.0)
        binance_coverage = float(row[2] or 0.0)
        accepted = (
            core_coverage >= TRAINING_COVERAGE_THRESHOLD
            and (
                not expected_binance
                or binance_coverage >= TRAINING_COVERAGE_THRESHOLD
            )
        )
        if accepted:
            eligible += 1
        else:
            excluded.append(
                {
                    "slug": slug,
                    "core_coverage": round(core_coverage, 6),
                    "binance_coverage": round(binance_coverage, 6),
                    "reason": (
                        "cobertura_core"
                        if core_coverage < TRAINING_COVERAGE_THRESHOLD
                        else "cobertura_binance"
                    ),
                }
            )
    return eligible, excluded


def build_silver_dataset(
    *,
    source_db: str | Path,
    output_db: str | Path,
    max_markets: int | None,
    fetch_labels: bool,
    resolution_client: GammaResolutionClient | None = None,
    label_cache: str | Path | None = None,
) -> dict[str, Any]:
    if max_markets is not None and max_markets <= 0:
        raise ValueError("max_markets debe ser mayor que cero o None")
    source = Path(source_db).expanduser().resolve()
    output = Path(output_db).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"No se encontró la base fuente: {source}")
    if source == output:
        raise ValueError("La base fuente y la salida no pueden ser iguales")
    partial_output = output.with_name(f"{output.name}.partial")
    if output.exists():
        raise ValueError(
            f"La salida ya existe y no será sobrescrita: {output}"
        )
    if partial_output.exists():
        raise ValueError(
            "Existe una salida parcial de un intento anterior: "
            f"{partial_output}. No se eliminó automáticamente."
        )
    cache_path = (
        Path(label_cache).expanduser().resolve()
        if label_cache is not None
        else None
    )
    source_size_before = source.stat().st_size
    started = time.monotonic()
    source_connection = _open_read_only(source)
    writer = SilverWriter(partial_output)
    try:
        schema_version = _source_schema(source_connection)
        expected_binance = schema_version >= 4
        markets = load_silver_markets(
            source_connection, max_markets=max_markets
        )
        LOGGER.info(
            "Fuente V%d en solo lectura | mercados seleccionados=%d",
            schema_version,
            len(markets),
        )
        markets = enrich_resolutions(
            markets,
            fetch_labels=fetch_labels,
            client=resolution_client,
            cache_path=cache_path,
        )
        unresolved = [
            market.slug
            for market in markets
            if not market.resolution.verified
        ]
        if fetch_labels and unresolved:
            raise ValueError(
                "No se verificaron todas las etiquetas Gamma. "
                f"Fallaron {len(unresolved)} de {len(markets)}. "
                "Reintente el proceso; el caché conservará las ya verificadas."
            )
        writer.open()
        writer.save_meta("source_database", str(source))
        writer.save_meta("source_schema_version", schema_version)
        writer.save_meta("source_size_bytes", source_size_before)
        writer.save_meta("selected_markets", len(markets))
        writer.save_meta("fetch_labels", fetch_labels)
        writer.save_meta(
            "created_at",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        for market in markets:
            writer.save_market(market)
        writer.db.commit()

        builder = SilverBuilder(markets, writer)
        window_start = _iso_from_ms(markets[0].start_ms)
        window_end = _iso_from_ms(
            markets[-1].end_ms + LATE_EVENT_GRACE_MS
        )
        rows = source_connection.execute(
            """
            SELECT codec, payload_blob
            FROM raw_chunks
            WHERE last_received_at >= ?
              AND first_received_at <= ?
            ORDER BY first_sequence
            """,
            (window_start, window_end),
        )
        chunks = 0
        envelopes = 0
        malformed = 0
        for row in rows:
            chunks += 1
            try:
                if row["codec"] != RAW_CODEC:
                    raise ValueError(f"Codec no soportado: {row['codec']}")
                raw = zlib.decompress(row["payload_blob"])
            except (ValueError, zlib.error):
                malformed += 1
                continue
            for line in raw.splitlines():
                if not line:
                    continue
                envelopes += 1
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if isinstance(event, dict):
                    builder.process_event(event)
            if chunks % 500 == 0:
                LOGGER.info(
                    "Procesamiento: bloques=%d | envelopes=%d",
                    chunks,
                    envelopes,
                )
        builder.finish()
        elapsed = time.monotonic() - started
        labels_verified = sum(
            market.resolution.verified for market in markets
        )
        labels_failed = [
            {
                "slug": market.slug,
                "error": market.resolution.error,
                "label": market.resolution.label,
            }
            for market in markets
            if not market.resolution.verified
        ]
        total_rows = writer.rows_written
        core_coverage = (
            writer.core_complete_rows / total_rows if total_rows else 0.0
        )
        binance_coverage = (
            writer.binance_rows / total_rows if total_rows else 0.0
        )
        training_eligible, training_excluded = _training_market_quality(
            writer.db,
            expected_binance=expected_binance,
        )
        writer.save_meta(
            "event_counts",
            [
                {
                    "source": source_name,
                    "stream": stream,
                    "count": count,
                }
                for (source_name, stream), count in sorted(
                    builder.event_counts.items()
                )
            ],
        )
        writer.save_meta("chunks_scanned", chunks)
        writer.save_meta("envelopes_scanned", envelopes)
        writer.save_meta("malformed_items", malformed)
        writer.save_meta("late_events", builder.late_events)
        writer.save_meta("labels_verified", labels_verified)
        writer.save_meta("core_coverage", core_coverage)
        writer.save_meta("binance_coverage", binance_coverage)
        writer.save_meta(
            "training_coverage_threshold",
            TRAINING_COVERAGE_THRESHOLD,
        )
        writer.save_meta(
            "training_eligible_markets", training_eligible
        )
        writer.save_meta(
            "training_excluded_markets", training_excluded
        )
        writer.save_meta(
            "late_event_grace_ms", LATE_EVENT_GRACE_MS
        )
        writer.save_meta(
            "label_cache",
            str(cache_path) if cache_path is not None else None,
        )
        writer.save_meta("elapsed_seconds", elapsed)
        writer.db.commit()
        quick_check = str(
            writer.db.execute("PRAGMA quick_check").fetchone()[0]
        )
    finally:
        source_connection.close()
        writer.close()

    source_size_after = source.stat().st_size
    passed = (
        quick_check == "ok"
        and malformed == 0
        and labels_verified == len(markets)
        and core_coverage >= 0.80
        and (not expected_binance or binance_coverage >= 0.80)
        and source_size_after == source_size_before
    )
    if passed:
        partial_output.replace(output)
    committed_output = output if passed else partial_output
    output_bytes = (
        committed_output.stat().st_size
        if committed_output.exists()
        else 0
    )
    result = {
        "passed": passed,
        "source_database": str(source),
        "source_schema_version": schema_version,
        "source_opened_read_only": True,
        "source_size_before": source_size_before,
        "source_size_after": source_size_after,
        "source_size_unchanged": source_size_after == source_size_before,
        "output_database": str(committed_output),
        "requested_output_database": str(output),
        "output_committed": passed,
        "output_bytes": output_bytes,
        "sqlite_quick_check": quick_check,
        "markets_selected": len(markets),
        "labels_verified": labels_verified,
        "labels_failed": labels_failed,
        "second_rows": total_rows,
        "core_complete_rows": writer.core_complete_rows,
        "core_coverage": round(core_coverage, 6),
        "binance_rows": writer.binance_rows,
        "binance_coverage": round(binance_coverage, 6),
        "binance_expected": expected_binance,
        "training_coverage_threshold": TRAINING_COVERAGE_THRESHOLD,
        "training_eligible_markets": training_eligible,
        "training_excluded_markets": training_excluded,
        "chunks_scanned": chunks,
        "envelopes_scanned": envelopes,
        "malformed_items": malformed,
        "late_events": builder.late_events,
        "late_event_grace_ms": LATE_EVENT_GRACE_MS,
        "label_cache": str(cache_path) if cache_path is not None else None,
        "elapsed_seconds": round(elapsed, 3),
        "event_counts": [
            {
                "source": source_name,
                "stream": stream,
                "count": count,
            }
            for (source_name, stream), count in sorted(
                builder.event_counts.items()
            )
        ],
    }
    return result


def silver_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).expanduser().resolve()
    if not database.is_file():
        raise ValueError(f"No se encontró la base Silver: {database}")
    connection = _open_read_only(database)
    try:
        quick_check = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        )
        markets = int(
            connection.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        )
        labels = int(
            connection.execute(
                "SELECT COUNT(*) FROM markets WHERE label_verified=1"
            ).fetchone()[0]
        )
        rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM second_features"
            ).fetchone()[0]
        )
        complete = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM second_features
                WHERE (quality_flags & 23) = 0
                """
            ).fetchone()[0]
        )
        meta = {
            str(row["key"]): json.loads(row["value"])
            for row in connection.execute(
                "SELECT key,value FROM silver_meta ORDER BY key"
            )
        }
        source_schema_version = int(
            meta.get("source_schema_version", 3)
        )
        training_eligible, training_excluded = _training_market_quality(
            connection,
            expected_binance=source_schema_version >= 4,
        )
    finally:
        connection.close()
    return {
        "database": str(database),
        "database_bytes": database.stat().st_size,
        "sqlite_quick_check": quick_check,
        "markets": markets,
        "verified_labels": labels,
        "second_rows": rows,
        "complete_core_rows": complete,
        "complete_core_coverage": (
            round(complete / rows, 6) if rows else 0.0
        ),
        "training_coverage_threshold": TRAINING_COVERAGE_THRESHOLD,
        "training_eligible_markets": training_eligible,
        "training_excluded_markets": training_excluded,
        "meta": meta,
    }
