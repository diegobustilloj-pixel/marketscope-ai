from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping


SIDES = ("UP", "DOWN")


class V018Error(RuntimeError):
    """Raised when the V0.18 paper simulation violates its frozen contract."""


def default_strategy_config() -> dict[str, Any]:
    return {
        "quote_start_second": 10,
        "quote_end_second": 240,
        "quote_interval_seconds": 10,
        "quote_lifetime_seconds": 10,
        "activation_latency_ms": 1_000,
        "order_size_shares": 5.0,
        "paired_target_shares_per_side": 20.0,
        "directional_residual_target_shares": 5.0,
        "maximum_inventory_shares_per_side": 25.0,
        "maximum_directional_residual_shares": 5.0,
        "maximum_cash_per_market": 25.0,
        "maximum_pair_quote_cost": 0.99,
        "maximum_residual_quote_price": 0.70,
        "minimum_quote_price": 0.01,
        "maximum_quote_price": 0.98,
        "maximum_spread": 0.10,
        "queue_ahead_fraction": 0.50,
        "maker_fee_per_share": 0.0,
        "maker_rebate_per_share": 0.0,
        "fills_require_trade_through_or_ask_cross": True,
        "favorite_side_rule": "higher_initial_clob_midpoint",
    }


def validate_strategy_config(config: Mapping[str, Any]) -> dict[str, Any]:
    required = set(default_strategy_config())
    if set(config) != required:
        raise V018Error(
            f"Configuración V0.18 incompatible; missing={sorted(required-set(config))}; "
            f"extra={sorted(set(config)-required)}"
        )
    normalized = dict(config)
    integers = (
        "quote_start_second",
        "quote_end_second",
        "quote_interval_seconds",
        "quote_lifetime_seconds",
        "activation_latency_ms",
    )
    for key in integers:
        normalized[key] = int(normalized[key])
    numeric = required - set(integers) - {
        "fills_require_trade_through_or_ask_cross",
        "favorite_side_rule",
    }
    for key in numeric:
        value = float(normalized[key])
        if not math.isfinite(value):
            raise V018Error(f"Parámetro no finito: {key}")
        normalized[key] = value

    if normalized["quote_start_second"] < 0:
        raise V018Error("quote_start_second inválido")
    if not (
        normalized["quote_start_second"]
        < normalized["quote_end_second"]
        < 300
    ):
        raise V018Error("Ventana de cotización inválida")
    if normalized["quote_interval_seconds"] <= 0:
        raise V018Error("quote_interval_seconds inválido")
    if normalized["quote_lifetime_seconds"] > normalized["quote_interval_seconds"]:
        raise V018Error("Una quote no puede sobrevivir al siguiente reprice")
    if normalized["activation_latency_ms"] < 1_000:
        raise V018Error("V0.18 exige al menos un segundo de latencia")
    if normalized["order_size_shares"] <= 0:
        raise V018Error("order_size_shares inválido")
    paired = normalized["paired_target_shares_per_side"]
    residual = normalized["directional_residual_target_shares"]
    maximum = normalized["maximum_inventory_shares_per_side"]
    if paired <= 0 or residual < 0 or paired + residual > maximum:
        raise V018Error("Targets de inventario incompatibles")
    if normalized["maximum_directional_residual_shares"] != residual:
        raise V018Error("El residual objetivo debe coincidir con su límite")
    if not (0 <= normalized["queue_ahead_fraction"] <= 1):
        raise V018Error("queue_ahead_fraction fuera de rango")
    if normalized["maker_rebate_per_share"] != 0:
        raise V018Error("V0.18 no puede asumir rebates")
    if normalized["fills_require_trade_through_or_ask_cross"] is not True:
        raise V018Error("Los fills deben requerir un trigger observable")
    if normalized["favorite_side_rule"] != "higher_initial_clob_midpoint":
        raise V018Error("Regla direccional no permitida")
    return normalized


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass(slots=True)
class PaperQuote:
    order_id: str
    side: str
    placed_second: int
    active_at_ms: int
    expires_at_ms: int
    price: float
    original_size: float
    remaining_size: float
    queue_ahead_initial: float
    queue_ahead_remaining: float
    status: str = "RESTING"
    filled_size: float = 0.0


class MultiFillPaperEngine:
    def __init__(
        self,
        *,
        condition_id: str,
        market_start_ms: int,
        config: Mapping[str, Any],
    ) -> None:
        self.condition_id = str(condition_id)
        self.market_start_ms = int(market_start_ms)
        self.config = validate_strategy_config(config)
        self.favorite_side: str | None = None
        self.inventory = {
            side: {"shares": 0.0, "cost": 0.0} for side in SIDES
        }
        self.quotes: dict[str, PaperQuote | None] = {side: None for side in SIDES}
        self.all_quotes: list[PaperQuote] = []
        self.fills: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self._sequence = 0
        self._finished = False

    @property
    def cash_deployed(self) -> float:
        return sum(float(item["cost"]) for item in self.inventory.values())

    def _event(self, kind: str, **payload: Any) -> None:
        self._events.append({"kind": kind, **payload})

    def drain_events(self) -> list[dict[str, Any]]:
        events = self._events
        self._events = []
        return events

    def _row_values(self, row: Mapping[str, Any], side: str) -> dict[str, float] | None:
        key = side.lower()
        values = {
            "bid": _finite(row.get(f"{key}_best_bid")),
            "ask": _finite(row.get(f"{key}_best_ask")),
            "depth": _finite(row.get(f"{key}_bid_depth_1c")),
        }
        if any(value is None for value in values.values()):
            return None
        bid = float(values["bid"])
        ask = float(values["ask"])
        depth = float(values["depth"])
        if bid > ask or depth < 0:
            return None
        if ask - bid > float(self.config["maximum_spread"]):
            return None
        if not (
            float(self.config["minimum_quote_price"])
            <= bid
            <= float(self.config["maximum_quote_price"])
        ):
            return None
        return {"bid": bid, "ask": ask, "depth": depth, "mid": (bid + ask) / 2}

    def _set_favorite(self, values: dict[str, dict[str, float]]) -> None:
        if self.favorite_side is not None:
            return
        self.favorite_side = (
            "UP" if values["UP"]["mid"] >= values["DOWN"]["mid"] else "DOWN"
        )
        self._event("FAVORITE_SET", side=self.favorite_side)

    def _target(self, side: str) -> float:
        target = float(self.config["paired_target_shares_per_side"])
        if side == self.favorite_side:
            target += float(self.config["directional_residual_target_shares"])
        return target

    def _cancel(self, side: str, *, second: int, reason: str) -> None:
        quote = self.quotes.get(side)
        if quote is None or quote.status not in {"RESTING", "PARTIAL"}:
            return
        quote.status = reason
        self._event(
            "QUOTE_CANCELLED",
            order=asdict(quote),
            cancelled_second=int(second),
            reason=reason,
        )
        self.quotes[side] = None

    def _place(
        self,
        side: str,
        *,
        second: int,
        price: float,
        size: float,
        depth: float,
    ) -> None:
        if size <= 1e-9:
            return
        self._sequence += 1
        active_at = self.market_start_ms + second * 1000 + int(
            self.config["activation_latency_ms"]
        )
        quote = PaperQuote(
            order_id=f"{self.condition_id}:{side}:{second}:{self._sequence}",
            side=side,
            placed_second=int(second),
            active_at_ms=active_at,
            expires_at_ms=(
                self.market_start_ms
                + (second + int(self.config["quote_lifetime_seconds"])) * 1000
            ),
            price=float(price),
            original_size=float(size),
            remaining_size=float(size),
            queue_ahead_initial=float(depth)
            * float(self.config["queue_ahead_fraction"]),
            queue_ahead_remaining=float(depth)
            * float(self.config["queue_ahead_fraction"]),
        )
        self.quotes[side] = quote
        self.all_quotes.append(quote)
        self._event("QUOTE_PLACED", order=asdict(quote))

    def _average_price(self, side: str) -> float | None:
        shares = float(self.inventory[side]["shares"])
        return float(self.inventory[side]["cost"]) / shares if shares else None

    def _quote_sides(
        self,
        values: dict[str, dict[str, float]],
    ) -> list[str]:
        paired_target = float(self.config["paired_target_shares_per_side"])
        shares = {side: float(self.inventory[side]["shares"]) for side in SIDES}
        below = [side for side in SIDES if shares[side] < paired_target - 1e-9]
        if len(below) == 2:
            return (
                list(SIDES)
                if values["UP"]["bid"] + values["DOWN"]["bid"]
                <= float(self.config["maximum_pair_quote_cost"])
                else []
            )
        if len(below) == 1:
            side = below[0]
            opposite = "DOWN" if side == "UP" else "UP"
            opposite_average = self._average_price(opposite)
            return (
                [side]
                if opposite_average is not None
                and values[side]["bid"] + opposite_average
                <= float(self.config["maximum_pair_quote_cost"])
                else []
            )
        if self.favorite_side is None:
            return []
        favorite_shares = shares[self.favorite_side]
        if (
            favorite_shares < self._target(self.favorite_side) - 1e-9
            and values[self.favorite_side]["bid"]
            <= float(self.config["maximum_residual_quote_price"])
        ):
            return [self.favorite_side]
        return []

    def _reprice(self, row: Mapping[str, Any], second: int) -> None:
        values: dict[str, dict[str, float]] = {}
        for side in SIDES:
            parsed = self._row_values(row, side)
            if parsed is None:
                for candidate in SIDES:
                    self._cancel(candidate, second=second, reason="CANCELLED_INVALID_BOOK")
                return
            values[side] = parsed
        self._set_favorite(values)
        for side in SIDES:
            self._cancel(side, second=second, reason="CANCELLED_REPRICE")
        for side in self._quote_sides(values):
            remaining_target = max(
                0.0,
                self._target(side) - float(self.inventory[side]["shares"]),
            )
            cash_remaining = max(
                0.0,
                float(self.config["maximum_cash_per_market"]) - self.cash_deployed,
            )
            affordable = cash_remaining / values[side]["bid"] if values[side]["bid"] else 0
            size = min(
                float(self.config["order_size_shares"]),
                remaining_target,
                affordable,
                float(self.config["maximum_inventory_shares_per_side"])
                - float(self.inventory[side]["shares"]),
            )
            self._place(
                side,
                second=second,
                price=values[side]["bid"],
                size=size,
                depth=values[side]["depth"],
            )

    def _fill(
        self,
        quote: PaperQuote,
        *,
        timestamp_ms: int,
        requested_size: float,
        trigger: str,
        trigger_price: float,
    ) -> float:
        cash_remaining = max(
            0.0,
            float(self.config["maximum_cash_per_market"]) - self.cash_deployed,
        )
        affordable = cash_remaining / quote.price if quote.price else 0.0
        opposite = "DOWN" if quote.side == "UP" else "UP"
        inventory_room = max(
            0.0,
            float(self.inventory[opposite]["shares"])
            + float(self.config["maximum_directional_residual_shares"])
            - float(self.inventory[quote.side]["shares"]),
        )
        size = min(
            float(requested_size), quote.remaining_size, affordable, inventory_room
        )
        if size <= 1e-9:
            return 0.0
        fee = size * float(self.config["maker_fee_per_share"])
        rebate = size * float(self.config["maker_rebate_per_share"])
        cost = size * quote.price + fee - rebate
        quote.remaining_size -= size
        quote.filled_size += size
        quote.status = "FILLED" if quote.remaining_size <= 1e-9 else "PARTIAL"
        self.inventory[quote.side]["shares"] += size
        self.inventory[quote.side]["cost"] += cost
        fill = {
            "order_id": quote.order_id,
            "side": quote.side,
            "timestamp_ms": int(timestamp_ms),
            "second_offset": (int(timestamp_ms) - self.market_start_ms) / 1000.0,
            "fill_price": quote.price,
            "fill_size": size,
            "fee": fee,
            "rebate": rebate,
            "cash_cost": cost,
            "trigger": trigger,
            "trigger_price": float(trigger_price),
            "real_money": 0,
        }
        self.fills.append(fill)
        self._event("FILL", fill=fill, order=asdict(quote))
        if quote.status == "FILLED":
            self.quotes[quote.side] = None
        return size

    def on_trade(
        self,
        *,
        side: str,
        timestamp_ms: int,
        trade_price: float,
        trade_size: float,
    ) -> None:
        normalized = str(side).upper()
        quote = self.quotes.get(normalized)
        if quote is None or quote.status not in {"RESTING", "PARTIAL"}:
            return
        timestamp = int(timestamp_ms)
        price = _finite(trade_price)
        size = _finite(trade_size)
        if price is None or size is None or size <= 0:
            return
        if timestamp < quote.active_at_ms or timestamp >= quote.expires_at_ms:
            return
        if price > quote.price + 1e-12:
            return
        consumed = min(quote.queue_ahead_remaining, size)
        quote.queue_ahead_remaining -= consumed
        executable = size - consumed
        if executable > 0:
            self._fill(
                quote,
                timestamp_ms=timestamp,
                requested_size=executable,
                trigger="TRADE_THROUGH_QUEUE",
                trigger_price=price,
            )

    def on_second(self, row: Mapping[str, Any]) -> None:
        if self._finished:
            raise V018Error("El mercado ya terminó")
        second = int(row.get("second_offset", -1))
        if second < 0 or second >= 300:
            raise V018Error("second_offset inválido")
        timestamp_ms = self.market_start_ms + second * 1000
        for side in SIDES:
            quote = self.quotes.get(side)
            if quote is None or quote.status not in {"RESTING", "PARTIAL"}:
                continue
            if timestamp_ms >= quote.expires_at_ms:
                self._cancel(side, second=second, reason="CANCELLED_EXPIRED")
                continue
            ask = _finite(row.get(f"{side.lower()}_best_ask"))
            if (
                timestamp_ms >= quote.active_at_ms
                and ask is not None
                and ask <= quote.price + 1e-12
            ):
                self._fill(
                    quote,
                    timestamp_ms=timestamp_ms,
                    requested_size=quote.remaining_size,
                    trigger="ASK_CROSS",
                    trigger_price=ask,
                )

        start = int(self.config["quote_start_second"])
        end = int(self.config["quote_end_second"])
        interval = int(self.config["quote_interval_seconds"])
        if start <= second <= end and (second - start) % interval == 0:
            self._reprice(row, second)
        if second >= end + int(self.config["quote_lifetime_seconds"]):
            for side in SIDES:
                self._cancel(side, second=second, reason="CANCELLED_END_WINDOW")

    def finish(self) -> dict[str, Any]:
        if not self._finished:
            for side in SIDES:
                self._cancel(side, second=300, reason="CANCELLED_MARKET_END")
            self._finished = True
        up = self.inventory["UP"]
        down = self.inventory["DOWN"]
        up_shares = float(up["shares"])
        down_shares = float(down["shares"])
        up_average = float(up["cost"]) / up_shares if up_shares else None
        down_average = float(down["cost"]) / down_shares if down_shares else None
        paired = min(up_shares, down_shares)
        complete_set_cost = (
            float(up_average) + float(down_average)
            if up_average is not None and down_average is not None
            else None
        )
        paired_cost = paired * complete_set_cost if complete_set_cost else 0.0
        cash = self.cash_deployed
        return {
            "condition_id": self.condition_id,
            "market_start_ms": self.market_start_ms,
            "favorite_side": self.favorite_side,
            "quotes": len(self.all_quotes),
            "fills": len(self.fills),
            "up_shares": up_shares,
            "down_shares": down_shares,
            "up_cost": float(up["cost"]),
            "down_cost": float(down["cost"]),
            "average_up_price": up_average,
            "average_down_price": down_average,
            "paired_shares": paired,
            "estimated_complete_set_cost": complete_set_cost,
            "estimated_paired_cost": paired_cost,
            "cash_deployed": cash,
            "estimated_paired_capital_fraction": paired_cost / cash if cash else None,
            "residual_side": (
                "UP" if up_shares > down_shares else "DOWN"
                if down_shares > up_shares
                else None
            ),
            "residual_shares": abs(up_shares - down_shares),
            "trade_trigger_fills": sum(
                fill["trigger"] == "TRADE_THROUGH_QUEUE" for fill in self.fills
            ),
            "ask_cross_fills": sum(fill["trigger"] == "ASK_CROSS" for fill in self.fills),
            "real_money": 0,
            "orders_sent": 0,
        }


__all__ = [
    "MultiFillPaperEngine",
    "PaperQuote",
    "SIDES",
    "V018Error",
    "default_strategy_config",
    "validate_strategy_config",
]
