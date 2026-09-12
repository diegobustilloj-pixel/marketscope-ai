from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


GOLD_SCHEMA_VERSION = "2"
TRAINING_COVERAGE_THRESHOLD = 0.95
DEFAULT_HORIZONS = (15, 30, 60, 90)
MIN_GOLD_ROW_COVERAGE = 0.95
CORE_QUALITY_MASK = 23
MARKOV_STATE_THRESHOLD_BPS = 0.02


GOLD_DDL = """
CREATE TABLE IF NOT EXISTS gold_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_splits (
    source_dataset TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    market_start_ms INTEGER NOT NULL,
    market_end_ms INTEGER NOT NULL,
    label TEXT NOT NULL,
    y_up INTEGER NOT NULL,
    price_to_beat REAL,
    has_official_strike INTEGER NOT NULL,
    split TEXT NOT NULL,
    core_coverage REAL NOT NULL,
    binance_coverage REAL NOT NULL,
    PRIMARY KEY(source_dataset, condition_id),
    UNIQUE(source_dataset, slug)
);

CREATE TABLE IF NOT EXISTS gold_features (
    source_dataset TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    horizon_seconds INTEGER NOT NULL,
    slug TEXT NOT NULL,
    market_start_ms INTEGER NOT NULL,
    market_end_ms INTEGER NOT NULL,
    decision_offset INTEGER NOT NULL,
    decision_timestamp_ms INTEGER NOT NULL,
    split TEXT NOT NULL,
    label TEXT NOT NULL,
    y_up INTEGER NOT NULL,
    price_to_beat REAL,
    has_official_strike INTEGER NOT NULL,
    chainlink_price REAL NOT NULL,
    binance_price REAL,
    distance_to_strike_bps REAL,
    chainlink_return_5s_bps REAL,
    chainlink_return_15s_bps REAL,
    chainlink_return_30s_bps REAL,
    chainlink_return_60s_bps REAL,
    chainlink_return_120s_bps REAL,
    chainlink_vol_15s_bps REAL,
    chainlink_vol_30s_bps REAL,
    chainlink_vol_60s_bps REAL,
    chainlink_vol_120s_bps REAL,
    chainlink_up_fraction_15s REAL,
    chainlink_up_fraction_30s REAL,
    chainlink_up_fraction_60s REAL,
    chainlink_nonzero_fraction_60s REAL,
    chainlink_state INTEGER NOT NULL,
    chainlink_state_run_length INTEGER NOT NULL,
    markov_p_up_60s REAL NOT NULL,
    volatility_regime_ratio REAL,
    binance_return_5s_bps REAL,
    binance_return_15s_bps REAL,
    binance_return_30s_bps REAL,
    binance_return_60s_bps REAL,
    binance_vol_30s_bps REAL,
    binance_vol_60s_bps REAL,
    up_best_bid REAL NOT NULL,
    up_best_ask REAL NOT NULL,
    up_mid REAL NOT NULL,
    up_spread REAL NOT NULL,
    down_best_bid REAL NOT NULL,
    down_best_ask REAL NOT NULL,
    down_mid REAL NOT NULL,
    down_spread REAL NOT NULL,
    implied_up_mid_probability REAL NOT NULL,
    market_mid_sum REAL,
    market_ask_overround REAL,
    up_bid_depth_1c REAL,
    up_ask_depth_1c REAL,
    up_bid_depth_5c REAL,
    up_ask_depth_5c REAL,
    down_bid_depth_1c REAL,
    down_ask_depth_1c REAL,
    down_bid_depth_5c REAL,
    down_ask_depth_5c REAL,
    up_order_imbalance_1c REAL,
    up_order_imbalance_5c REAL,
    down_order_imbalance_1c REAL,
    down_order_imbalance_5c REAL,
    up_last_trade_price REAL,
    down_last_trade_price REAL,
    polymarket_trade_count_15s INTEGER NOT NULL,
    polymarket_trade_volume_15s REAL NOT NULL,
    polymarket_trade_count_60s INTEGER NOT NULL,
    polymarket_trade_volume_60s REAL NOT NULL,
    price_change_messages_15s INTEGER NOT NULL,
    price_change_messages_60s INTEGER NOT NULL,
    book_messages_15s INTEGER NOT NULL,
    book_messages_60s INTEGER NOT NULL,
    best_bid_ask_messages_15s INTEGER NOT NULL,
    best_bid_ask_messages_60s INTEGER NOT NULL,
    chainlink_updates_15s INTEGER NOT NULL,
    chainlink_updates_60s INTEGER NOT NULL,
    binance_trade_count_15s INTEGER NOT NULL,
    binance_trade_count_60s INTEGER NOT NULL,
    binance_trade_volume_15s REAL NOT NULL,
    binance_trade_volume_60s REAL NOT NULL,
    decision_quality_flags INTEGER NOT NULL,
    PRIMARY KEY(source_dataset, condition_id, horizon_seconds),
    FOREIGN KEY(source_dataset, condition_id)
        REFERENCES market_splits(source_dataset, condition_id)
);

CREATE INDEX IF NOT EXISTS idx_gold_split_horizon
    ON gold_features(split, horizon_seconds);
CREATE INDEX IF NOT EXISTS idx_gold_decision_time
    ON gold_features(decision_timestamp_ms);
"""


GOLD_FEATURE_COLUMNS = (
    "source_dataset",
    "condition_id",
    "horizon_seconds",
    "slug",
    "market_start_ms",
    "market_end_ms",
    "decision_offset",
    "decision_timestamp_ms",
    "split",
    "label",
    "y_up",
    "price_to_beat",
    "has_official_strike",
    "chainlink_price",
    "binance_price",
    "distance_to_strike_bps",
    "chainlink_return_5s_bps",
    "chainlink_return_15s_bps",
    "chainlink_return_30s_bps",
    "chainlink_return_60s_bps",
    "chainlink_return_120s_bps",
    "chainlink_vol_15s_bps",
    "chainlink_vol_30s_bps",
    "chainlink_vol_60s_bps",
    "chainlink_vol_120s_bps",
    "chainlink_up_fraction_15s",
    "chainlink_up_fraction_30s",
    "chainlink_up_fraction_60s",
    "chainlink_nonzero_fraction_60s",
    "chainlink_state",
    "chainlink_state_run_length",
    "markov_p_up_60s",
    "volatility_regime_ratio",
    "binance_return_5s_bps",
    "binance_return_15s_bps",
    "binance_return_30s_bps",
    "binance_return_60s_bps",
    "binance_vol_30s_bps",
    "binance_vol_60s_bps",
    "up_best_bid",
    "up_best_ask",
    "up_mid",
    "up_spread",
    "down_best_bid",
    "down_best_ask",
    "down_mid",
    "down_spread",
    "implied_up_mid_probability",
    "market_mid_sum",
    "market_ask_overround",
    "up_bid_depth_1c",
    "up_ask_depth_1c",
    "up_bid_depth_5c",
    "up_ask_depth_5c",
    "down_bid_depth_1c",
    "down_ask_depth_1c",
    "down_bid_depth_5c",
    "down_ask_depth_5c",
    "up_order_imbalance_1c",
    "up_order_imbalance_5c",
    "down_order_imbalance_1c",
    "down_order_imbalance_5c",
    "up_last_trade_price",
    "down_last_trade_price",
    "polymarket_trade_count_15s",
    "polymarket_trade_volume_15s",
    "polymarket_trade_count_60s",
    "polymarket_trade_volume_60s",
    "price_change_messages_15s",
    "price_change_messages_60s",
    "book_messages_15s",
    "book_messages_60s",
    "best_bid_ask_messages_15s",
    "best_bid_ask_messages_60s",
    "chainlink_updates_15s",
    "chainlink_updates_60s",
    "binance_trade_count_15s",
    "binance_trade_count_60s",
    "binance_trade_volume_15s",
    "binance_trade_volume_60s",
    "decision_quality_flags",
)


@dataclass(frozen=True, slots=True)
class GoldMarket:
    source_dataset: str
    source_schema_version: int
    condition_id: str
    slug: str
    start_ms: int
    end_ms: int
    label: str
    price_to_beat: float | None
    core_coverage: float
    binance_coverage: float
    split: str = ""

    @property
    def y_up(self) -> int:
        return int(self.label == "Up")


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=60,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _read_meta(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        str(row["key"]): json.loads(row["value"])
        for row in connection.execute(
            "SELECT key,value FROM silver_meta ORDER BY key"
        )
    }


def _load_eligible_markets(
    connection: sqlite3.Connection,
    *,
    source_dataset: str,
) -> list[GoldMarket]:
    meta = _read_meta(connection)
    source_schema_version = int(meta.get("source_schema_version", 3))
    expected_binance = source_schema_version >= 4
    rows = connection.execute(
        """
        SELECT
            m.condition_id,
            m.slug,
            m.market_start_ms,
            m.market_end_ms,
            m.label,
            m.label_verified,
            m.price_to_beat,
            AVG(
                CASE
                    WHEN (f.quality_flags & ?) = 0 THEN 1.0
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
        GROUP BY m.condition_id
        ORDER BY m.market_start_ms
        """,
        (CORE_QUALITY_MASK,),
    ).fetchall()
    markets: list[GoldMarket] = []
    for row in rows:
        core_coverage = float(row["core_coverage"] or 0.0)
        binance_coverage = float(row["binance_coverage"] or 0.0)
        eligible = (
            core_coverage >= TRAINING_COVERAGE_THRESHOLD
            and (
                not expected_binance
                or binance_coverage >= TRAINING_COVERAGE_THRESHOLD
            )
        )
        price_to_beat = _number(row["price_to_beat"])
        label = str(row["label"])
        if (
            not eligible
            or int(row["label_verified"]) != 1
            or label not in {"Up", "Down"}
        ):
            continue
        if price_to_beat is not None and price_to_beat <= 0:
            price_to_beat = None
        markets.append(
            GoldMarket(
                source_dataset=source_dataset,
                source_schema_version=source_schema_version,
                condition_id=str(row["condition_id"]),
                slug=str(row["slug"]),
                start_ms=int(row["market_start_ms"]),
                end_ms=int(row["market_end_ms"]),
                label=label,
                price_to_beat=price_to_beat,
                core_coverage=core_coverage,
                binance_coverage=binance_coverage,
            )
        )
    return markets


def assign_chronological_splits(
    markets: Iterable[GoldMarket],
) -> list[GoldMarket]:
    ordered = sorted(markets, key=lambda market: market.start_ms)
    if len(ordered) < 10:
        raise ValueError(
            "Se requieren al menos 10 mercados elegibles para dividir Gold"
        )
    slugs = [market.slug for market in ordered]
    if len(slugs) != len(set(slugs)):
        raise ValueError(
            "Hay mercados duplicados entre las fuentes Silver"
        )
    train_end = max(1, int(len(ordered) * 0.60))
    validation_end = max(train_end + 1, int(len(ordered) * 0.80))
    validation_end = min(validation_end, len(ordered) - 1)
    result: list[GoldMarket] = []
    for index, market in enumerate(ordered):
        if index < train_end:
            split = "train"
        elif index < validation_end:
            split = "validation"
        else:
            split = "test"
        result.append(
            GoldMarket(
                source_dataset=market.source_dataset,
                source_schema_version=market.source_schema_version,
                condition_id=market.condition_id,
                slug=market.slug,
                start_ms=market.start_ms,
                end_ms=market.end_ms,
                label=market.label,
                price_to_beat=market.price_to_beat,
                core_coverage=market.core_coverage,
                binance_coverage=market.binance_coverage,
                split=split,
            )
        )
    return result


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _series(
    rows: list[sqlite3.Row],
    field: str,
) -> list[float | None]:
    return [_number(row[field]) for row in rows]


def _return_bps(
    values: list[float | None],
    index: int,
    lookback: int,
) -> float | None:
    earlier_index = index - lookback
    if earlier_index < 0:
        return None
    current = values[index]
    earlier = values[earlier_index]
    if (
        current is None
        or earlier is None
        or current <= 0
        or earlier <= 0
    ):
        return None
    return (current / earlier - 1.0) * 10_000.0


def _one_second_returns(
    values: list[float | None],
    index: int,
    window: int,
) -> list[float]:
    start = max(1, index - window + 1)
    returns: list[float] = []
    for current_index in range(start, index + 1):
        current = values[current_index]
        previous = values[current_index - 1]
        if (
            current is None
            or previous is None
            or current <= 0
            or previous <= 0
        ):
            continue
        returns.append(math.log(current / previous) * 10_000.0)
    return returns


def _volatility_bps(
    values: list[float | None],
    index: int,
    window: int,
) -> float | None:
    returns = _one_second_returns(values, index, window)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(
        returns
    )
    return math.sqrt(variance)


def _trend_metrics(
    values: list[float | None],
    index: int,
    window: int,
) -> tuple[float | None, float]:
    returns = _one_second_returns(values, index, window)
    if not returns:
        return None, 0.0
    nonzero = [
        value
        for value in returns
        if abs(value) >= MARKOV_STATE_THRESHOLD_BPS
    ]
    if not nonzero:
        return None, 0.0
    positive = sum(value > 0 for value in nonzero)
    return positive / len(nonzero), len(nonzero) / len(returns)


def _state(value: float) -> int:
    if value >= MARKOV_STATE_THRESHOLD_BPS:
        return 1
    if value <= -MARKOV_STATE_THRESHOLD_BPS:
        return -1
    return 0


def _markov_metrics(
    values: list[float | None],
    index: int,
    window: int = 60,
) -> tuple[int, int, float]:
    returns = _one_second_returns(values, index, window)
    states = [_state(value) for value in returns]
    if not states:
        return 0, 0, 1.0 / 3.0
    current_state = states[-1]
    run_length = 1
    for value in reversed(states[:-1]):
        if value != current_state:
            break
        run_length += 1
    transitions = Counter(
        (left, right)
        for left, right in zip(states, states[1:])
    )
    outgoing = sum(
        count
        for (left, _), count in transitions.items()
        if left == current_state
    )
    up_count = transitions[(current_state, 1)]
    probability_up = (up_count + 1.0) / (outgoing + 3.0)
    return current_state, run_length, probability_up


def _imbalance(bid_depth: Any, ask_depth: Any) -> float | None:
    bid = _number(bid_depth)
    ask = _number(ask_depth)
    if bid is None or ask is None or bid + ask <= 0:
        return None
    return (bid - ask) / (bid + ask)


def _window_sum(
    rows: list[sqlite3.Row],
    field: str,
    index: int,
    window: int,
) -> float:
    start = max(0, index - window + 1)
    return sum(
        float(row[field] or 0.0)
        for row in rows[start : index + 1]
    )


def _feature_row(
    *,
    market: GoldMarket,
    rows: list[sqlite3.Row],
    horizon: int,
) -> tuple[dict[str, Any] | None, str | None]:
    decision_offset = 300 - horizon
    if not 0 <= decision_offset < len(rows):
        return None, "horizonte_fuera_de_rango"
    decision = rows[decision_offset]
    if int(decision["second_offset"]) != decision_offset:
        return None, "segundos_no_contiguos"
    if int(decision["quality_flags"]) & CORE_QUALITY_MASK:
        return None, "core_incompleto_en_decision"
    chainlink = _number(decision["chainlink_price"])
    up_bid = _number(decision["up_best_bid"])
    up_ask = _number(decision["up_best_ask"])
    up_mid = _number(decision["up_mid"])
    up_spread = _number(decision["up_spread"])
    down_bid = _number(decision["down_best_bid"])
    down_ask = _number(decision["down_best_ask"])
    down_mid = _number(decision["down_mid"])
    down_spread = _number(decision["down_spread"])
    required = (
        chainlink,
        up_bid,
        up_ask,
        up_mid,
        up_spread,
        down_bid,
        down_ask,
        down_mid,
        down_spread,
    )
    if any(value is None for value in required):
        return None, "core_incompleto_en_decision"
    assert chainlink is not None
    assert up_bid is not None and up_ask is not None and up_mid is not None
    assert down_bid is not None and down_ask is not None
    assert down_mid is not None
    mid_total = up_mid + down_mid
    if mid_total <= 0:
        return None, "probabilidad_implicita_invalida"

    chainlink_values = _series(rows, "chainlink_price")
    binance_values = _series(rows, "binance_price")
    trend_15, _ = _trend_metrics(
        chainlink_values, decision_offset, 15
    )
    trend_30, _ = _trend_metrics(
        chainlink_values, decision_offset, 30
    )
    trend_60, nonzero_60 = _trend_metrics(
        chainlink_values, decision_offset, 60
    )
    state, run_length, markov_p_up = _markov_metrics(
        chainlink_values, decision_offset
    )
    vol_15 = _volatility_bps(
        chainlink_values, decision_offset, 15
    )
    vol_60 = _volatility_bps(
        chainlink_values, decision_offset, 60
    )
    volatility_regime = (
        vol_15 / vol_60
        if vol_15 is not None and vol_60 not in {None, 0.0}
        else None
    )
    feature: dict[str, Any] = {
        "source_dataset": market.source_dataset,
        "condition_id": market.condition_id,
        "horizon_seconds": horizon,
        "slug": market.slug,
        "market_start_ms": market.start_ms,
        "market_end_ms": market.end_ms,
        "decision_offset": decision_offset,
        "decision_timestamp_ms": int(decision["timestamp_ms"]),
        "split": market.split,
        "label": market.label,
        "y_up": market.y_up,
        "price_to_beat": market.price_to_beat,
        "has_official_strike": int(
            market.price_to_beat is not None
        ),
        "chainlink_price": chainlink,
        "binance_price": _number(decision["binance_price"]),
        "distance_to_strike_bps": (
            (chainlink / market.price_to_beat - 1.0) * 10_000.0
            if market.price_to_beat is not None
            else None
        ),
        "chainlink_return_5s_bps": _return_bps(
            chainlink_values, decision_offset, 5
        ),
        "chainlink_return_15s_bps": _return_bps(
            chainlink_values, decision_offset, 15
        ),
        "chainlink_return_30s_bps": _return_bps(
            chainlink_values, decision_offset, 30
        ),
        "chainlink_return_60s_bps": _return_bps(
            chainlink_values, decision_offset, 60
        ),
        "chainlink_return_120s_bps": _return_bps(
            chainlink_values, decision_offset, 120
        ),
        "chainlink_vol_15s_bps": vol_15,
        "chainlink_vol_30s_bps": _volatility_bps(
            chainlink_values, decision_offset, 30
        ),
        "chainlink_vol_60s_bps": vol_60,
        "chainlink_vol_120s_bps": _volatility_bps(
            chainlink_values, decision_offset, 120
        ),
        "chainlink_up_fraction_15s": trend_15,
        "chainlink_up_fraction_30s": trend_30,
        "chainlink_up_fraction_60s": trend_60,
        "chainlink_nonzero_fraction_60s": nonzero_60,
        "chainlink_state": state,
        "chainlink_state_run_length": run_length,
        "markov_p_up_60s": markov_p_up,
        "volatility_regime_ratio": volatility_regime,
        "binance_return_5s_bps": _return_bps(
            binance_values, decision_offset, 5
        ),
        "binance_return_15s_bps": _return_bps(
            binance_values, decision_offset, 15
        ),
        "binance_return_30s_bps": _return_bps(
            binance_values, decision_offset, 30
        ),
        "binance_return_60s_bps": _return_bps(
            binance_values, decision_offset, 60
        ),
        "binance_vol_30s_bps": _volatility_bps(
            binance_values, decision_offset, 30
        ),
        "binance_vol_60s_bps": _volatility_bps(
            binance_values, decision_offset, 60
        ),
        "up_best_bid": up_bid,
        "up_best_ask": up_ask,
        "up_mid": up_mid,
        "up_spread": up_spread,
        "down_best_bid": down_bid,
        "down_best_ask": down_ask,
        "down_mid": down_mid,
        "down_spread": down_spread,
        "implied_up_mid_probability": up_mid / mid_total,
        "market_mid_sum": _number(decision["market_mid_sum"]),
        "market_ask_overround": _number(
            decision["market_ask_overround"]
        ),
        "up_bid_depth_1c": _number(decision["up_bid_depth_1c"]),
        "up_ask_depth_1c": _number(decision["up_ask_depth_1c"]),
        "up_bid_depth_5c": _number(decision["up_bid_depth_5c"]),
        "up_ask_depth_5c": _number(decision["up_ask_depth_5c"]),
        "down_bid_depth_1c": _number(
            decision["down_bid_depth_1c"]
        ),
        "down_ask_depth_1c": _number(
            decision["down_ask_depth_1c"]
        ),
        "down_bid_depth_5c": _number(
            decision["down_bid_depth_5c"]
        ),
        "down_ask_depth_5c": _number(
            decision["down_ask_depth_5c"]
        ),
        "up_order_imbalance_1c": _imbalance(
            decision["up_bid_depth_1c"],
            decision["up_ask_depth_1c"],
        ),
        "up_order_imbalance_5c": _imbalance(
            decision["up_bid_depth_5c"],
            decision["up_ask_depth_5c"],
        ),
        "down_order_imbalance_1c": _imbalance(
            decision["down_bid_depth_1c"],
            decision["down_ask_depth_1c"],
        ),
        "down_order_imbalance_5c": _imbalance(
            decision["down_bid_depth_5c"],
            decision["down_ask_depth_5c"],
        ),
        "up_last_trade_price": _number(
            decision["up_last_trade_price"]
        ),
        "down_last_trade_price": _number(
            decision["down_last_trade_price"]
        ),
        "polymarket_trade_count_15s": int(
            _window_sum(
                rows,
                "polymarket_trade_count",
                decision_offset,
                15,
            )
        ),
        "polymarket_trade_volume_15s": _window_sum(
            rows,
            "polymarket_trade_volume",
            decision_offset,
            15,
        ),
        "polymarket_trade_count_60s": int(
            _window_sum(
                rows,
                "polymarket_trade_count",
                decision_offset,
                60,
            )
        ),
        "polymarket_trade_volume_60s": _window_sum(
            rows,
            "polymarket_trade_volume",
            decision_offset,
            60,
        ),
        "price_change_messages_15s": int(
            _window_sum(
                rows,
                "price_change_messages",
                decision_offset,
                15,
            )
        ),
        "price_change_messages_60s": int(
            _window_sum(
                rows,
                "price_change_messages",
                decision_offset,
                60,
            )
        ),
        "book_messages_15s": int(
            _window_sum(
                rows, "book_messages", decision_offset, 15
            )
        ),
        "book_messages_60s": int(
            _window_sum(
                rows, "book_messages", decision_offset, 60
            )
        ),
        "best_bid_ask_messages_15s": int(
            _window_sum(
                rows,
                "best_bid_ask_messages",
                decision_offset,
                15,
            )
        ),
        "best_bid_ask_messages_60s": int(
            _window_sum(
                rows,
                "best_bid_ask_messages",
                decision_offset,
                60,
            )
        ),
        "chainlink_updates_15s": int(
            _window_sum(
                rows, "chainlink_updates", decision_offset, 15
            )
        ),
        "chainlink_updates_60s": int(
            _window_sum(
                rows, "chainlink_updates", decision_offset, 60
            )
        ),
        "binance_trade_count_15s": int(
            _window_sum(
                rows,
                "binance_trade_count",
                decision_offset,
                15,
            )
        ),
        "binance_trade_count_60s": int(
            _window_sum(
                rows,
                "binance_trade_count",
                decision_offset,
                60,
            )
        ),
        "binance_trade_volume_15s": _window_sum(
            rows,
            "binance_trade_volume",
            decision_offset,
            15,
        ),
        "binance_trade_volume_60s": _window_sum(
            rows,
            "binance_trade_volume",
            decision_offset,
            60,
        ),
        "decision_quality_flags": int(decision["quality_flags"]),
    }
    return feature, None


class GoldWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None

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
        connection.executescript(GOLD_DDL)
        connection.execute(
            "INSERT INTO gold_meta(key,value) VALUES(?,?)",
            ("schema_version", GOLD_SCHEMA_VERSION),
        )
        connection.commit()
        self.connection = connection

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("La salida Gold no está abierta")
        return self.connection

    def save_meta(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO gold_meta(key,value) VALUES(?,?)",
            (
                key,
                json.dumps(value, ensure_ascii=False, sort_keys=True),
            ),
        )

    def save_market(self, market: GoldMarket) -> None:
        self.db.execute(
            """
            INSERT INTO market_splits VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                market.source_dataset,
                market.condition_id,
                market.slug,
                market.start_ms,
                market.end_ms,
                market.label,
                market.y_up,
                market.price_to_beat,
                int(market.price_to_beat is not None),
                market.split,
                market.core_coverage,
                market.binance_coverage,
            ),
        )

    def save_feature(self, feature: dict[str, Any]) -> None:
        columns = ",".join(GOLD_FEATURE_COLUMNS)
        placeholders = ",".join("?" for _ in GOLD_FEATURE_COLUMNS)
        self.db.execute(
            f"INSERT INTO gold_features({columns}) VALUES({placeholders})",
            tuple(feature[column] for column in GOLD_FEATURE_COLUMNS),
        )

    def close(self) -> None:
        if self.connection is not None:
            self.connection.commit()
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.connection.close()
            self.connection = None


def _split_summary(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    return [
        {
            "split": str(row[0]),
            "markets": int(row[1]),
            "up": int(row[2]),
            "down": int(row[3]),
            "first_market_ms": int(row[4]),
            "last_market_ms": int(row[5]),
        }
        for row in connection.execute(
            """
            SELECT
                split,
                COUNT(*),
                SUM(y_up),
                COUNT(*) - SUM(y_up),
                MIN(market_start_ms),
                MAX(market_start_ms)
            FROM market_splits
            GROUP BY split
            ORDER BY MIN(market_start_ms)
            """
        )
    ]


def _null_summary(
    connection: sqlite3.Connection,
) -> dict[str, int]:
    fields = (
        "price_to_beat",
        "distance_to_strike_bps",
        "chainlink_return_60s_bps",
        "chainlink_vol_60s_bps",
        "chainlink_up_fraction_60s",
        "binance_price",
        "up_bid_depth_5c",
        "down_bid_depth_5c",
        "up_last_trade_price",
        "down_last_trade_price",
    )
    expression = ",".join(
        f"SUM(CASE WHEN {field} IS NULL THEN 1 ELSE 0 END)"
        for field in fields
    )
    row = connection.execute(
        f"SELECT {expression} FROM gold_features"
    ).fetchone()
    return {
        field: int(row[index] or 0)
        for index, field in enumerate(fields)
    }


def _chronological_split_order(
    split_summary: list[dict[str, Any]],
) -> bool:
    by_name = {item["split"]: item for item in split_summary}
    if set(by_name) != {"train", "validation", "test"}:
        return False
    return (
        by_name["train"]["last_market_ms"]
        < by_name["validation"]["first_market_ms"]
        <= by_name["validation"]["last_market_ms"]
        < by_name["test"]["first_market_ms"]
    )


def _contract_hash(horizons: tuple[int, ...]) -> str:
    contract = json.dumps(
        {
            "columns": GOLD_FEATURE_COLUMNS,
            "horizons": horizons,
            "split": "chronological_60_20_20",
            "target": "y_up",
            "future_features_forbidden": True,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(contract.encode("utf-8")).hexdigest()


def build_gold_dataset(
    *,
    v3_silver_db: str | Path,
    v4_silver_db: str | Path,
    output_db: str | Path,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
) -> dict[str, Any]:
    horizon_values = tuple(sorted(set(int(value) for value in horizons)))
    if not horizon_values or any(
        value <= 0 or value >= 300 for value in horizon_values
    ):
        raise ValueError(
            "Los horizontes deben estar entre 1 y 299 segundos"
        )
    sources = {
        "v3": Path(v3_silver_db).expanduser().resolve(),
        "v4": Path(v4_silver_db).expanduser().resolve(),
    }
    for name, path in sources.items():
        if not path.is_file():
            raise ValueError(
                f"No se encontró la base Silver {name.upper()}: {path}"
            )
    output = Path(output_db).expanduser().resolve()
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
    if output in sources.values():
        raise ValueError("La salida Gold no puede ser una base Silver")

    source_sizes_before = {
        name: path.stat().st_size for name, path in sources.items()
    }
    connections: dict[str, sqlite3.Connection] = {}
    writer = GoldWriter(partial_output)
    started = time.monotonic()
    try:
        source_checks: dict[str, str] = {}
        declared_eligible_by_source: dict[str, int] = {}
        markets: list[GoldMarket] = []
        for name, path in sources.items():
            connection = _open_read_only(path)
            connections[name] = connection
            source_checks[name] = str(
                connection.execute("PRAGMA quick_check").fetchone()[0]
            )
            meta = _read_meta(connection)
            if str(meta.get("schema_version")) != "1":
                raise ValueError(
                    f"Silver {name.upper()} tiene un esquema no compatible"
                )
            loaded_markets = _load_eligible_markets(
                connection,
                source_dataset=name,
            )
            markets.extend(loaded_markets)
            declared_eligible_by_source[name] = int(
                meta.get(
                    "training_eligible_markets",
                    len(loaded_markets),
                )
            )
        markets = assign_chronological_splits(markets)
        declared_eligible_total = sum(
            declared_eligible_by_source.values()
        )
        eligible_count_matches_silver = (
            len(markets) == declared_eligible_total
        )
        official_strike_markets = sum(
            market.price_to_beat is not None for market in markets
        )
        missing_strike_markets = (
            len(markets) - official_strike_markets
        )
        writer.open()
        writer.save_meta(
            "created_at",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        writer.save_meta(
            "source_databases",
            {name: str(path) for name, path in sources.items()},
        )
        writer.save_meta("source_sizes_bytes", source_sizes_before)
        writer.save_meta("source_quick_checks", source_checks)
        writer.save_meta(
            "declared_eligible_by_source",
            declared_eligible_by_source,
        )
        writer.save_meta(
            "declared_eligible_total", declared_eligible_total
        )
        writer.save_meta(
            "eligible_count_matches_silver",
            eligible_count_matches_silver,
        )
        writer.save_meta(
            "official_strike_markets", official_strike_markets
        )
        writer.save_meta(
            "missing_strike_markets", missing_strike_markets
        )
        writer.save_meta(
            "missing_strike_policy",
            "conservar_mercado_y_dejar_strike_distance_en_NULL",
        )
        writer.save_meta("horizons_seconds", horizon_values)
        writer.save_meta(
            "training_coverage_threshold",
            TRAINING_COVERAGE_THRESHOLD,
        )
        writer.save_meta("split_policy", "chronological_60_20_20")
        writer.save_meta(
            "feature_time_policy",
            "solo_datos_con_timestamp_hasta_decision_inclusive",
        )
        writer.save_meta("target", "y_up")
        writer.save_meta(
            "data_contract_sha256", _contract_hash(horizon_values)
        )
        for market in markets:
            writer.save_market(market)
        writer.db.commit()

        dropped = Counter()
        rows_written = 0
        for index, market in enumerate(markets, start=1):
            connection = connections[market.source_dataset]
            second_rows = connection.execute(
                """
                SELECT *
                FROM second_features
                WHERE condition_id=?
                ORDER BY second_offset
                """,
                (market.condition_id,),
            ).fetchall()
            if len(second_rows) != 300:
                dropped["mercado_sin_300_segundos"] += len(
                    horizon_values
                )
                continue
            for horizon in horizon_values:
                feature, reason = _feature_row(
                    market=market,
                    rows=second_rows,
                    horizon=horizon,
                )
                if feature is None:
                    dropped[reason or "motivo_desconocido"] += 1
                    continue
                writer.save_feature(feature)
                rows_written += 1
            if index % 100 == 0:
                writer.db.commit()

        expected_rows = len(markets) * len(horizon_values)
        row_coverage = (
            rows_written / expected_rows if expected_rows else 0.0
        )
        split_summary = _split_summary(writer.db)
        split_order_valid = _chronological_split_order(split_summary)
        null_summary = _null_summary(writer.db)
        leakage_violations = int(
            writer.db.execute(
                """
                SELECT COUNT(*)
                FROM gold_features
                WHERE decision_timestamp_ms >= market_end_ms
                   OR decision_timestamp_ms < market_start_ms
                   OR decision_offset != 300 - horizon_seconds
                """
            ).fetchone()[0]
        )
        orphan_features = int(
            writer.db.execute(
                """
                SELECT COUNT(*)
                FROM gold_features AS f
                LEFT JOIN market_splits AS m
                  ON m.source_dataset=f.source_dataset
                 AND m.condition_id=f.condition_id
                WHERE m.condition_id IS NULL
                """
            ).fetchone()[0]
        )
        markets_without_features = int(
            writer.db.execute(
                """
                SELECT COUNT(*)
                FROM market_splits AS m
                LEFT JOIN gold_features AS f
                  ON m.source_dataset=f.source_dataset
                 AND m.condition_id=f.condition_id
                WHERE f.condition_id IS NULL
                """
            ).fetchone()[0]
        )
        writer.save_meta("eligible_markets", len(markets))
        writer.save_meta("expected_feature_rows", expected_rows)
        writer.save_meta("feature_rows", rows_written)
        writer.save_meta("feature_row_coverage", row_coverage)
        writer.save_meta("dropped_feature_rows", dict(dropped))
        writer.save_meta("split_summary", split_summary)
        writer.save_meta("split_order_valid", split_order_valid)
        writer.save_meta("feature_null_counts", null_summary)
        writer.save_meta("leakage_violations", leakage_violations)
        writer.save_meta("orphan_features", orphan_features)
        writer.save_meta(
            "markets_without_features", markets_without_features
        )
        writer.save_meta(
            "elapsed_seconds", time.monotonic() - started
        )
        writer.db.commit()
        quick_check = str(
            writer.db.execute("PRAGMA quick_check").fetchone()[0]
        )
    finally:
        for connection in connections.values():
            connection.close()
        writer.close()

    source_sizes_after = {
        name: path.stat().st_size for name, path in sources.items()
    }
    source_sizes_unchanged = (
        source_sizes_before == source_sizes_after
    )
    split_names = {item["split"] for item in split_summary}
    passed = (
        all(value == "ok" for value in source_checks.values())
        and quick_check == "ok"
        and source_sizes_unchanged
        and row_coverage >= MIN_GOLD_ROW_COVERAGE
        and leakage_violations == 0
        and orphan_features == 0
        and markets_without_features == 0
        and split_names == {"train", "validation", "test"}
        and split_order_valid
        and eligible_count_matches_silver
    )
    if passed:
        partial_output.replace(output)
    committed_output = output if passed else partial_output
    elapsed = time.monotonic() - started
    return {
        "passed": passed,
        "source_databases": {
            name: str(path) for name, path in sources.items()
        },
        "source_opened_read_only": True,
        "source_quick_checks": source_checks,
        "source_sizes_before": source_sizes_before,
        "source_sizes_after": source_sizes_after,
        "source_sizes_unchanged": source_sizes_unchanged,
        "output_database": str(committed_output),
        "requested_output_database": str(output),
        "output_committed": passed,
        "output_bytes": (
            committed_output.stat().st_size
            if committed_output.exists()
            else 0
        ),
        "sqlite_quick_check": quick_check,
        "eligible_markets": len(markets),
        "declared_eligible_by_source": declared_eligible_by_source,
        "declared_eligible_total": declared_eligible_total,
        "eligible_count_matches_silver": eligible_count_matches_silver,
        "official_strike_markets": official_strike_markets,
        "missing_strike_markets": missing_strike_markets,
        "horizons_seconds": list(horizon_values),
        "expected_feature_rows": expected_rows,
        "feature_rows": rows_written,
        "feature_row_coverage": round(row_coverage, 6),
        "dropped_feature_rows": dict(dropped),
        "split_summary": split_summary,
        "split_order_valid": split_order_valid,
        "feature_null_counts": null_summary,
        "leakage_violations": leakage_violations,
        "orphan_features": orphan_features,
        "markets_without_features": markets_without_features,
        "data_contract_sha256": _contract_hash(horizon_values),
        "elapsed_seconds": round(elapsed, 3),
    }


def gold_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).expanduser().resolve()
    if not database.is_file():
        raise ValueError(f"No se encontró la base Gold: {database}")
    connection = _open_read_only(database)
    try:
        quick_check = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        )
        markets = int(
            connection.execute(
                "SELECT COUNT(*) FROM market_splits"
            ).fetchone()[0]
        )
        rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM gold_features"
            ).fetchone()[0]
        )
        leakage = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM gold_features
                WHERE decision_timestamp_ms >= market_end_ms
                   OR decision_timestamp_ms < market_start_ms
                   OR decision_offset != 300 - horizon_seconds
                """
            ).fetchone()[0]
        )
        split_summary = _split_summary(connection)
        split_order_valid = _chronological_split_order(split_summary)
        official_strike_markets = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM market_splits
                WHERE has_official_strike=1
                """
            ).fetchone()[0]
        )
        null_summary = _null_summary(connection)
        meta = {
            str(row["key"]): json.loads(row["value"])
            for row in connection.execute(
                "SELECT key,value FROM gold_meta ORDER BY key"
            )
        }
    finally:
        connection.close()
    return {
        "database": str(database),
        "database_bytes": database.stat().st_size,
        "sqlite_quick_check": quick_check,
        "markets": markets,
        "official_strike_markets": official_strike_markets,
        "missing_strike_markets": markets - official_strike_markets,
        "feature_rows": rows,
        "leakage_violations": leakage,
        "split_summary": split_summary,
        "split_order_valid": split_order_valid,
        "feature_null_counts": null_summary,
        "meta": meta,
    }
