from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from polymarket_bot.domain import parse_message_metadata


SIDES = ("UP", "DOWN")


class V021Error(RuntimeError):
    """Raised when the V0.21 safe-pair observation contract is violated."""


def default_strategy_config() -> dict[str, Any]:
    return {
        "quote_start_second": 10,
        "quote_end_second": 240,
        "order_size_shares": 5.0,
        "maximum_complete_set_cost": 0.99,
        "taker_slippage_per_share": 0.005,
        "taker_fee_rate": 0.07,
        "sample_bucket_ms": 250,
        "minimum_price": 0.001,
        "maximum_price": 0.999,
        "simultaneous_complete_set_only": True,
        "paper_orders_enabled": False,
    }


def validate_strategy_config(config: Mapping[str, Any]) -> dict[str, Any]:
    required = set(default_strategy_config())
    if set(config) != required:
        raise V021Error(
            f"Configuracion V0.21 incompatible; missing={sorted(required-set(config))}; "
            f"extra={sorted(set(config)-required)}"
        )
    normalized = dict(config)
    for key in ("quote_start_second", "quote_end_second", "sample_bucket_ms"):
        normalized[key] = int(normalized[key])
    for key in (
        "order_size_shares",
        "maximum_complete_set_cost",
        "taker_slippage_per_share",
        "taker_fee_rate",
        "minimum_price",
        "maximum_price",
    ):
        normalized[key] = float(normalized[key])
        if not math.isfinite(normalized[key]):
            raise V021Error(f"Parametro V0.21 no finito: {key}")
    if not 0 <= normalized["quote_start_second"] < normalized["quote_end_second"] < 300:
        raise V021Error("Ventana V0.21 invalida")
    if normalized["order_size_shares"] <= 0:
        raise V021Error("Tamano V0.21 invalido")
    if not 0 < normalized["maximum_complete_set_cost"] < 1:
        raise V021Error("V0.21 exige coste completo menor que uno")
    if not 0 <= normalized["taker_slippage_per_share"] < 0.1:
        raise V021Error("Slippage V0.21 invalido")
    if not 0 <= normalized["taker_fee_rate"] <= 1:
        raise V021Error("Fee V0.21 invalido")
    if normalized["sample_bucket_ms"] != 250:
        raise V021Error("V0.21 congela resolucion de 250 ms")
    if not 0 < normalized["minimum_price"] < normalized["maximum_price"] < 1:
        raise V021Error("Rango de precios V0.21 invalido")
    if normalized["simultaneous_complete_set_only"] is not True:
        raise V021Error("V0.21 prohibe oportunidades de una sola pata")
    if normalized["paper_orders_enabled"] is not False:
        raise V021Error("V0.21 es observador y no puede simular ordenes atomicas")
    return normalized


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass(slots=True)
class _Book:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    initialized: bool = False
    source_timestamp_ms: int = 0

    def replace(self, payload: Mapping[str, Any], timestamp_ms: int) -> None:
        def levels(name: str) -> dict[float, float]:
            result: dict[float, float] = {}
            raw = payload.get(name, [])
            if not isinstance(raw, list):
                return result
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                price = _number(item.get("price"))
                size = _number(item.get("size"))
                if price is not None and size is not None and size > 0:
                    result[price] = size
            return result

        self.bids = levels("bids")
        self.asks = levels("asks")
        self.initialized = True
        self.source_timestamp_ms = int(timestamp_ms)

    def change(
        self, *, side: str, price: float, size: float, timestamp_ms: int
    ) -> None:
        levels = self.bids if side == "BUY" else self.asks
        if size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        self.source_timestamp_ms = int(timestamp_ms)


def execution_cost(
    asks: Mapping[float, float],
    *,
    shares: float,
    slippage_per_share: float,
    fee_rate: float,
    minimum_price: float,
    maximum_price: float,
) -> dict[str, float] | None:
    remaining = float(shares)
    notional = 0.0
    for price, depth in sorted(asks.items()):
        if price < minimum_price or price > maximum_price or depth <= 0:
            continue
        taken = min(remaining, float(depth))
        notional += taken * float(price)
        remaining -= taken
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return None
    observed_vwap = notional / shares
    fill_price = min(
        maximum_price,
        max(minimum_price, observed_vwap + float(slippage_per_share)),
    )
    fee_per_share = float(fee_rate) * fill_price * (1.0 - fill_price)
    return {
        "observed_vwap": observed_vwap,
        "fill_price": fill_price,
        "fee_per_share": fee_per_share,
        "total_cost_per_share": fill_price + fee_per_share,
    }


class SafePairOpportunityEngine:
    """Track event-level complete-set opportunities without inventing fills."""

    def __init__(
        self,
        *,
        condition_id: str,
        market_start_ms: int,
        token_sides: Mapping[str, str],
        config: Mapping[str, Any],
    ) -> None:
        self.condition_id = str(condition_id)
        self.market_start_ms = int(market_start_ms)
        self.token_sides = {str(token): str(side).upper() for token, side in token_sides.items()}
        if set(self.token_sides.values()) != set(SIDES):
            raise V021Error("V0.21 exige exactamente tokens UP y DOWN")
        self.config = validate_strategy_config(config)
        self.books = {side: _Book() for side in SIDES}
        self.message_count = 0
        self.valid_observations = 0
        self.eligible_observations = 0
        self.opportunity_episodes = 0
        self.minimum_complete_set_cost: float | None = None
        self.maximum_observed_depth_shares = 0.0
        self._eligible_active = False
        self._eligible_started_ms: int | None = None
        self.maximum_opportunity_persistence_ms = 0
        self._pending_bucket: dict[str, Any] | None = None
        self._samples: list[dict[str, Any]] = []

    @staticmethod
    def _timestamp(payload: Any, fallback_ms: int) -> int:
        raw: Any = None
        if isinstance(payload, Mapping):
            raw = payload.get("timestamp")
        elif isinstance(payload, list) and payload and isinstance(payload[0], Mapping):
            raw = payload[0].get("timestamp")
        try:
            return int(raw) if raw is not None else int(fallback_ms)
        except (TypeError, ValueError):
            return int(fallback_ms)

    def _apply_book(self, payload: Any, fallback_ms: int) -> bool:
        items = payload if isinstance(payload, list) else [payload]
        changed = False
        for item in items:
            if not isinstance(item, Mapping):
                continue
            token = str(item.get("asset_id") or "")
            side = self.token_sides.get(token)
            if side is None:
                continue
            timestamp_ms = self._timestamp(item, fallback_ms)
            self.books[side].replace(item, timestamp_ms)
            changed = True
        return changed

    def _apply_price_change(self, payload: Any, fallback_ms: int) -> bool:
        if not isinstance(payload, Mapping):
            return False
        raw_changes = payload.get("price_changes", [])
        if not isinstance(raw_changes, list):
            return False
        timestamp_ms = self._timestamp(payload, fallback_ms)
        changed = False
        for item in raw_changes:
            if not isinstance(item, Mapping):
                continue
            token = str(item.get("asset_id") or "")
            outcome = self.token_sides.get(token)
            side = str(item.get("side") or "").upper()
            price = _number(item.get("price"))
            size = _number(item.get("size"))
            if outcome is None or side not in {"BUY", "SELL"} or price is None or size is None:
                continue
            book = self.books[outcome]
            if not book.initialized:
                continue
            book.change(side=side, price=price, size=size, timestamp_ms=timestamp_ms)
            changed = True
        return changed

    def _close_episode(self, timestamp_ms: int) -> None:
        if self._eligible_started_ms is not None:
            self.maximum_opportunity_persistence_ms = max(
                self.maximum_opportunity_persistence_ms,
                max(0, int(timestamp_ms) - self._eligible_started_ms),
            )
        self._eligible_started_ms = None
        self._eligible_active = False

    def _observe(self, source_timestamp_ms: int, received_timestamp_ms: int) -> None:
        if not all(self.books[side].initialized for side in SIDES):
            return
        offset = (int(source_timestamp_ms) - self.market_start_ms) / 1000.0
        start = int(self.config["quote_start_second"])
        end = int(self.config["quote_end_second"])
        if offset < start or offset > end:
            self._close_episode(source_timestamp_ms)
            return
        size = float(self.config["order_size_shares"])
        costs: dict[str, dict[str, float]] = {}
        for side in SIDES:
            cost = execution_cost(
                self.books[side].asks,
                shares=size,
                slippage_per_share=float(self.config["taker_slippage_per_share"]),
                fee_rate=float(self.config["taker_fee_rate"]),
                minimum_price=float(self.config["minimum_price"]),
                maximum_price=float(self.config["maximum_price"]),
            )
            if cost is None:
                self._close_episode(source_timestamp_ms)
                return
            costs[side] = cost
        complete_cost = sum(costs[side]["total_cost_per_share"] for side in SIDES)
        eligible = complete_cost <= float(self.config["maximum_complete_set_cost"]) + 1e-12
        self.valid_observations += 1
        self.eligible_observations += int(eligible)
        self.minimum_complete_set_cost = (
            complete_cost
            if self.minimum_complete_set_cost is None
            else min(self.minimum_complete_set_cost, complete_cost)
        )
        depth = min(sum(self.books[side].asks.values()) for side in SIDES)
        self.maximum_observed_depth_shares = max(self.maximum_observed_depth_shares, depth)
        if eligible and not self._eligible_active:
            self.opportunity_episodes += 1
            self._eligible_started_ms = int(source_timestamp_ms)
        elif not eligible and self._eligible_active:
            self._close_episode(source_timestamp_ms)
        self._eligible_active = eligible

        bucket_size = int(self.config["sample_bucket_ms"])
        bucket_ms = int(received_timestamp_ms) // bucket_size * bucket_size
        sample = {
            "condition_id": self.condition_id,
            "bucket_ms": bucket_ms,
            "source_timestamp_ms": int(source_timestamp_ms),
            "received_timestamp_ms": int(received_timestamp_ms),
            "up_best_ask": min(self.books["UP"].asks),
            "down_best_ask": min(self.books["DOWN"].asks),
            "up_ask_depth": sum(self.books["UP"].asks.values()),
            "down_ask_depth": sum(self.books["DOWN"].asks.values()),
            "up_total_cost": costs["UP"]["total_cost_per_share"],
            "down_total_cost": costs["DOWN"]["total_cost_per_share"],
            "complete_set_cost": complete_cost,
            "eligible": int(eligible),
            "message_count": 1,
        }
        pending = self._pending_bucket
        if pending is None:
            self._pending_bucket = sample
        elif pending["bucket_ms"] != bucket_ms:
            self._samples.append(pending)
            self._pending_bucket = sample
        else:
            pending["message_count"] += 1
            if complete_cost < float(pending["complete_set_cost"]):
                sample["message_count"] = pending["message_count"]
                self._pending_bucket = sample

    def ingest(self, raw: str, *, received_timestamp_ms: int) -> None:
        self.message_count += 1
        try:
            stream, source_timestamp_ms, _, _ = parse_message_metadata(raw, "market")
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        fallback = int(source_timestamp_ms or received_timestamp_ms)
        changed = False
        if stream == "book":
            changed = self._apply_book(payload, fallback)
        elif stream == "price_change":
            changed = self._apply_price_change(payload, fallback)
        if changed:
            self._observe(fallback, int(received_timestamp_ms))

    def drain_samples(self) -> list[dict[str, Any]]:
        samples = self._samples
        self._samples = []
        return samples

    def finish(self, *, timestamp_ms: int) -> dict[str, Any]:
        if self._pending_bucket is not None:
            self._samples.append(self._pending_bucket)
            self._pending_bucket = None
        self._close_episode(timestamp_ms)
        return {
            "condition_id": self.condition_id,
            "message_count": self.message_count,
            "valid_observations": self.valid_observations,
            "eligible_observations": self.eligible_observations,
            "opportunity_episodes": self.opportunity_episodes,
            "minimum_complete_set_cost": self.minimum_complete_set_cost,
            "maximum_observed_depth_shares": self.maximum_observed_depth_shares,
            "maximum_opportunity_persistence_ms": self.maximum_opportunity_persistence_ms,
            "unilateral_positions": 0,
            "paper_orders": 0,
            "orders_sent": 0,
            "outcomes_read": 0,
            "real_money": 0,
        }


__all__ = [
    "SafePairOpportunityEngine",
    "V021Error",
    "default_strategy_config",
    "execution_cost",
    "validate_strategy_config",
]
