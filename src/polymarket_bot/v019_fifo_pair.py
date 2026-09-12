from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Mapping


SIDES = ("UP", "DOWN")


class V019Error(RuntimeError):
    """Raised when the frozen V0.19 paper contract is violated."""


def default_strategy_config() -> dict[str, Any]:
    return {
        "quote_start_second": 10,
        "quote_end_second": 240,
        "quote_interval_seconds": 10,
        "quote_lifetime_seconds": 10,
        "activation_latency_ms": 1_000,
        "order_size_shares": 5.0,
        "paired_target_shares_per_side": 20.0,
        "directional_residual_target_shares": 0.0,
        "maximum_inventory_shares_per_side": 25.0,
        "maximum_unmatched_shares": 5.0,
        "maximum_cash_per_market": 25.0,
        "maximum_matched_set_cost": 0.99,
        "minimum_quote_price": 0.01,
        "maximum_quote_price": 0.98,
        "maximum_spread": 0.10,
        "queue_ahead_fraction": 0.50,
        "maker_fee_per_share": 0.0,
        "maker_rebate_per_share": 0.0,
        "fills_require_trade_through_or_ask_cross": True,
        "pairing_rule": "fifo_actual_fill_cost",
    }


def validate_strategy_config(config: Mapping[str, Any]) -> dict[str, Any]:
    required = set(default_strategy_config())
    if set(config) != required:
        raise V019Error(
            f"Configuracion V0.19 incompatible; missing={sorted(required-set(config))}; "
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
        "pairing_rule",
    }
    for key in numeric:
        value = float(normalized[key])
        if not math.isfinite(value):
            raise V019Error(f"Parametro no finito: {key}")
        normalized[key] = value
    if not (
        0 <= normalized["quote_start_second"]
        < normalized["quote_end_second"]
        < 300
    ):
        raise V019Error("Ventana de cotizacion invalida")
    if normalized["quote_interval_seconds"] <= 0:
        raise V019Error("quote_interval_seconds invalido")
    if not 0 < normalized["quote_lifetime_seconds"] <= normalized["quote_interval_seconds"]:
        raise V019Error("Una quote no puede sobrevivir al siguiente reprice")
    if normalized["activation_latency_ms"] < 1_000:
        raise V019Error("V0.19 exige al menos un segundo de latencia")
    if normalized["order_size_shares"] <= 0:
        raise V019Error("order_size_shares invalido")
    if normalized["paired_target_shares_per_side"] <= 0:
        raise V019Error("paired_target_shares_per_side invalido")
    if normalized["directional_residual_target_shares"] != 0:
        raise V019Error("V0.19 prohibe residual direccional intencional")
    if normalized["maximum_unmatched_shares"] <= 0:
        raise V019Error("maximum_unmatched_shares invalido")
    if normalized["maximum_inventory_shares_per_side"] <= 0:
        raise V019Error("maximum_inventory_shares_per_side invalido")
    if normalized["maximum_cash_per_market"] <= 0:
        raise V019Error("maximum_cash_per_market invalido")
    if (
        normalized["paired_target_shares_per_side"]
        + normalized["maximum_unmatched_shares"]
        > normalized["maximum_inventory_shares_per_side"]
    ):
        raise V019Error("Limites de inventario incompatibles")
    if not 0 < normalized["maximum_matched_set_cost"] < 1:
        raise V019Error("maximum_matched_set_cost debe quedar bajo 1")
    if not (
        0 < normalized["minimum_quote_price"]
        <= normalized["maximum_quote_price"]
        < 1
    ):
        raise V019Error("Rango de precios V0.19 invalido")
    if not 0 < normalized["maximum_spread"] < 1:
        raise V019Error("maximum_spread invalido")
    if not 0 <= normalized["queue_ahead_fraction"] <= 1:
        raise V019Error("queue_ahead_fraction fuera de rango")
    if normalized["maker_fee_per_share"] != 0:
        raise V019Error("V0.19 congela maker fee en cero")
    if normalized["maker_rebate_per_share"] != 0:
        raise V019Error("V0.19 no puede asumir rebates")
    if normalized["fills_require_trade_through_or_ask_cross"] is not True:
        raise V019Error("Los fills deben exigir un trigger observable")
    if normalized["pairing_rule"] != "fifo_actual_fill_cost":
        raise V019Error("Regla FIFO V0.19 incompatible")
    return normalized


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _opposite(side: str) -> str:
    return "DOWN" if side == "UP" else "UP"


@dataclass(slots=True)
class PaperQuote:
    order_id: str
    side: str
    purpose: str
    placed_second: int
    active_at_ms: int
    expires_at_ms: int
    price: float
    price_ceiling: float
    original_size: float
    remaining_size: float
    queue_ahead_initial: float
    queue_ahead_remaining: float
    status: str = "RESTING"
    filled_size: float = 0.0


@dataclass(slots=True)
class UnmatchedLot:
    lot_id: str
    fill_id: str
    side: str
    timestamp_ms: int
    price: float
    original_size: float
    remaining_size: float


class FifoPairPaperEngine:
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
        self.inventory = {
            side: {"shares": 0.0, "cost": 0.0} for side in SIDES
        }
        self.unmatched: dict[str, deque[UnmatchedLot]] = {
            side: deque() for side in SIDES
        }
        self.quotes: dict[str, PaperQuote | None] = {side: None for side in SIDES}
        self.all_quotes: list[PaperQuote] = []
        self.fills: list[dict[str, Any]] = []
        self.pairs: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self._quote_sequence = 0
        self._fill_sequence = 0
        self._lot_sequence = 0
        self._pair_sequence = 0
        self._finished = False

    @property
    def cash_deployed(self) -> float:
        return sum(float(item["cost"]) for item in self.inventory.values())

    @property
    def paired_shares(self) -> float:
        return sum(float(item["size"]) for item in self.pairs)

    @property
    def paired_cost(self) -> float:
        return sum(float(item["size"]) * float(item["set_cost"]) for item in self.pairs)

    def unmatched_shares(self, side: str | None = None) -> float:
        if side is not None:
            return sum(float(lot.remaining_size) for lot in self.unmatched[side])
        return sum(self.unmatched_shares(candidate) for candidate in SIDES)

    def unmatched_side(self) -> str | None:
        active = [side for side in SIDES if self.unmatched_shares(side) > 1e-9]
        if len(active) > 1:
            raise V019Error("FIFO invalido: hay lotes sin cubrir en ambos lados")
        return active[0] if active else None

    def _event(self, kind: str, **payload: Any) -> None:
        self._events.append({"kind": kind, **payload})

    def drain_events(self) -> list[dict[str, Any]]:
        events = self._events
        self._events = []
        return events

    def _row_values(self, row: Mapping[str, Any], side: str) -> dict[str, float] | None:
        key = side.lower()
        bid = _finite(row.get(f"{key}_best_bid"))
        ask = _finite(row.get(f"{key}_best_ask"))
        depth = _finite(row.get(f"{key}_bid_depth_1c"))
        if bid is None or ask is None or depth is None:
            return None
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
        return {"bid": bid, "ask": ask, "depth": depth}

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
        purpose: str,
        second: int,
        price: float,
        price_ceiling: float,
        size: float,
        depth: float,
    ) -> None:
        if size <= 1e-9:
            return
        if price > price_ceiling + 1e-12:
            raise V019Error("Quote V0.19 supera su ceiling")
        self._quote_sequence += 1
        active_at = self.market_start_ms + second * 1000 + int(
            self.config["activation_latency_ms"]
        )
        quote = PaperQuote(
            order_id=(
                f"{self.condition_id}:{side}:{purpose}:{second}:{self._quote_sequence}"
            ),
            side=side,
            purpose=purpose,
            placed_second=int(second),
            active_at_ms=active_at,
            expires_at_ms=(
                self.market_start_ms
                + (second + int(self.config["quote_lifetime_seconds"])) * 1000
            ),
            price=float(price),
            price_ceiling=float(price_ceiling),
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

    def _eligible_fifo_size(self, side_to_buy: str, price: float) -> float:
        lead = _opposite(side_to_buy)
        eligible = 0.0
        maximum = float(self.config["maximum_matched_set_cost"])
        for lot in self.unmatched[lead]:
            if float(lot.price) + float(price) > maximum + 1e-12:
                break
            eligible += float(lot.remaining_size)
        return eligible

    def _reprice(self, row: Mapping[str, Any], second: int) -> None:
        values: dict[str, dict[str, float]] = {}
        for side in SIDES:
            parsed = self._row_values(row, side)
            if parsed is None:
                for candidate in SIDES:
                    self._cancel(candidate, second=second, reason="CANCELLED_INVALID_BOOK")
                return
            values[side] = parsed
        for side in SIDES:
            self._cancel(side, second=second, reason="CANCELLED_REPRICE")
        lead = self.unmatched_side()
        if lead is not None:
            hedge = _opposite(lead)
            price = float(values[hedge]["bid"])
            eligible = self._eligible_fifo_size(hedge, price)
            if eligible <= 1e-9:
                return
            oldest = self.unmatched[lead][0]
            ceiling = float(self.config["maximum_matched_set_cost"]) - float(
                oldest.price
            )
            size = min(
                float(self.config["order_size_shares"]),
                eligible,
                float(self.config["maximum_inventory_shares_per_side"])
                - float(self.inventory[hedge]["shares"]),
            )
            self._place(
                hedge,
                purpose="HEDGE_FIFO",
                second=second,
                price=price,
                price_ceiling=ceiling,
                size=size,
                depth=float(values[hedge]["depth"]),
            )
            return
        remaining = float(self.config["paired_target_shares_per_side"]) - self.paired_shares
        if remaining <= 1e-9:
            return
        up_price = float(values["UP"]["bid"])
        down_price = float(values["DOWN"]["bid"])
        maximum = float(self.config["maximum_matched_set_cost"])
        if up_price + down_price > maximum + 1e-12:
            return
        size = min(
            float(self.config["order_size_shares"]),
            remaining,
            float(self.config["maximum_unmatched_shares"]),
        )
        for side, opposite in (("UP", "DOWN"), ("DOWN", "UP")):
            price = float(values[side]["bid"])
            self._place(
                side,
                purpose="OPEN_PAIR",
                second=second,
                price=price,
                price_ceiling=maximum - float(values[opposite]["bid"]),
                size=size,
                depth=float(values[side]["depth"]),
            )

    def _compatible_fill_room(self, quote: PaperQuote) -> float:
        side = quote.side
        lead = self.unmatched_side()
        if lead is None:
            unmatched_room = float(self.config["maximum_unmatched_shares"])
        elif lead == side:
            return 0.0
        else:
            unmatched_room = self._eligible_fifo_size(side, quote.price)
        inventory_room = max(
            0.0,
            float(self.config["maximum_inventory_shares_per_side"])
            - float(self.inventory[side]["shares"]),
        )
        return min(unmatched_room, inventory_room)

    def _register_fifo(
        self,
        *,
        fill_id: str,
        side: str,
        timestamp_ms: int,
        price: float,
        size: float,
    ) -> None:
        remaining = float(size)
        opposite = _opposite(side)
        maximum = float(self.config["maximum_matched_set_cost"])
        while remaining > 1e-9 and self.unmatched[opposite]:
            lot = self.unmatched[opposite][0]
            matched = min(remaining, float(lot.remaining_size))
            set_cost = float(price) + float(lot.price)
            if set_cost > maximum + 1e-12:
                raise V019Error("FIFO intento formar un set sobre el limite")
            self._pair_sequence += 1
            pair = {
                "pair_id": f"{self.condition_id}:PAIR:{self._pair_sequence}",
                "timestamp_ms": int(timestamp_ms),
                "size": matched,
                "up_fill_id": fill_id if side == "UP" else lot.fill_id,
                "down_fill_id": fill_id if side == "DOWN" else lot.fill_id,
                "up_price": float(price) if side == "UP" else float(lot.price),
                "down_price": float(price) if side == "DOWN" else float(lot.price),
                "set_cost": set_cost,
            }
            self.pairs.append(pair)
            self._event("PAIR_MATCHED", pair=pair)
            remaining -= matched
            lot.remaining_size -= matched
            if lot.remaining_size <= 1e-9:
                self.unmatched[opposite].popleft()
        if remaining > 1e-9:
            self._lot_sequence += 1
            self.unmatched[side].append(
                UnmatchedLot(
                    lot_id=f"{self.condition_id}:LOT:{self._lot_sequence}",
                    fill_id=fill_id,
                    side=side,
                    timestamp_ms=int(timestamp_ms),
                    price=float(price),
                    original_size=remaining,
                    remaining_size=remaining,
                )
            )

    def _validate_invariants(self) -> None:
        unmatched = self.unmatched_shares()
        if unmatched > float(self.config["maximum_unmatched_shares"]) + 1e-8:
            raise V019Error("V0.19 excedio inventario no pareado")
        up = float(self.inventory["UP"]["shares"])
        down = float(self.inventory["DOWN"]["shares"])
        if abs(abs(up - down) - unmatched) > 1e-7:
            raise V019Error("Ledger FIFO no coincide con inventario")
        if abs(min(up, down) - self.paired_shares) > 1e-7:
            raise V019Error("Pares FIFO no coinciden con inventario")
        if self.cash_deployed > float(self.config["maximum_cash_per_market"]) + 1e-8:
            raise V019Error("V0.19 excedio cash por mercado")
        maximum = float(self.config["maximum_matched_set_cost"])
        if any(float(pair["set_cost"]) > maximum + 1e-12 for pair in self.pairs):
            raise V019Error("V0.19 formo un set caro")

    def _cancel_incompatible_after_fill(self, second: int) -> None:
        lead = self.unmatched_side()
        if lead is not None:
            self._cancel(lead, second=second, reason="CANCELLED_UNMATCHED_CAP")
            hedge = _opposite(lead)
            quote = self.quotes.get(hedge)
            if quote is not None:
                eligible = self._eligible_fifo_size(hedge, quote.price)
                if eligible <= 1e-9:
                    self._cancel(
                        hedge, second=second, reason="CANCELLED_FIFO_INCOMPATIBLE"
                    )
        elif self.paired_shares >= float(
            self.config["paired_target_shares_per_side"]
        ) - 1e-9:
            for side in SIDES:
                self._cancel(side, second=second, reason="CANCELLED_TARGET_REACHED")

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
        room = self._compatible_fill_room(quote)
        size = min(float(requested_size), quote.remaining_size, affordable, room)
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
        self._fill_sequence += 1
        fill_id = f"{self.condition_id}:FILL:{self._fill_sequence}"
        fill = {
            "fill_id": fill_id,
            "order_id": quote.order_id,
            "side": quote.side,
            "purpose": quote.purpose,
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
        self._register_fifo(
            fill_id=fill_id,
            side=quote.side,
            timestamp_ms=timestamp_ms,
            price=quote.price,
            size=size,
        )
        self._event("FILL", fill=fill, order=asdict(quote))
        if quote.status == "FILLED":
            self.quotes[quote.side] = None
        second = max(0, min(300, int((int(timestamp_ms) - self.market_start_ms) / 1000)))
        self._cancel_incompatible_after_fill(second)
        self._validate_invariants()
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
            raise V019Error("El mercado V0.19 ya termino")
        second = int(row.get("second_offset", -1))
        if second < 0 or second >= 300:
            raise V019Error("second_offset invalido")
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
        self._validate_invariants()
        up = self.inventory["UP"]
        down = self.inventory["DOWN"]
        unmatched_side = self.unmatched_side()
        unmatched_shares = self.unmatched_shares()
        weighted_set_cost = (
            self.paired_cost / self.paired_shares if self.paired_shares else None
        )
        return {
            "condition_id": self.condition_id,
            "market_start_ms": self.market_start_ms,
            "quotes": len(self.all_quotes),
            "fills": len(self.fills),
            "pair_matches": len(self.pairs),
            "up_shares": float(up["shares"]),
            "down_shares": float(down["shares"]),
            "up_cost": float(up["cost"]),
            "down_cost": float(down["cost"]),
            "paired_shares": self.paired_shares,
            "paired_cost": self.paired_cost,
            "weighted_complete_set_cost": weighted_set_cost,
            "cash_deployed": self.cash_deployed,
            "paired_capital_fraction": (
                self.paired_cost / self.cash_deployed if self.cash_deployed else None
            ),
            "unmatched_side": unmatched_side,
            "unmatched_shares": unmatched_shares,
            "unmatched_cost": (
                sum(
                    float(lot.remaining_size) * float(lot.price)
                    for lot in self.unmatched[unmatched_side]
                )
                if unmatched_side is not None
                else 0.0
            ),
            "maximum_pair_set_cost": max(
                (float(pair["set_cost"]) for pair in self.pairs), default=None
            ),
            "trade_trigger_fills": sum(
                fill["trigger"] == "TRADE_THROUGH_QUEUE" for fill in self.fills
            ),
            "ask_cross_fills": sum(
                fill["trigger"] == "ASK_CROSS" for fill in self.fills
            ),
            "directional_residual_target_shares": 0.0,
            "real_money": 0,
            "orders_sent": 0,
        }


__all__ = [
    "FifoPairPaperEngine",
    "PaperQuote",
    "SIDES",
    "UnmatchedLot",
    "V019Error",
    "default_strategy_config",
    "validate_strategy_config",
]
