from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict
from typing import Any, Mapping

from polymarket_bot.v019_fifo_pair import (
    FifoPairPaperEngine,
    SIDES,
    UnmatchedLot,
    V019Error,
    default_strategy_config as v019_default_strategy_config,
)


class V020Error(V019Error):
    """Raised when the V0.20 mandatory-hedge contract is violated."""


def default_strategy_config() -> dict[str, Any]:
    config = v019_default_strategy_config()
    config.update(
        {
            "paired_target_shares_per_side": 10.0,
            "maximum_inventory_shares_per_side": 15.0,
            "maximum_projected_entry_set_cost": 1.05,
            "maximum_emergency_pair_cost": 2.0,
            "emergency_hedge_delay_ms": 1_000,
            "minimum_opposite_ask_depth_shares": 5.0,
            "taker_slippage_per_share": 0.005,
            "taker_fee_rate": 0.07,
            "mandatory_hedge": True,
        }
    )
    return config


def validate_strategy_config(config: Mapping[str, Any]) -> dict[str, Any]:
    required = set(default_strategy_config())
    if set(config) != required:
        raise V020Error(
            f"Configuracion V0.20 incompatible; missing={sorted(required-set(config))}; "
            f"extra={sorted(set(config)-required)}"
        )
    normalized = dict(config)
    base_keys = set(v019_default_strategy_config())
    base = {key: normalized[key] for key in base_keys}
    from polymarket_bot.v019_fifo_pair import validate_strategy_config as validate_v019

    normalized.update(validate_v019(base))
    for key in (
        "maximum_projected_entry_set_cost",
        "maximum_emergency_pair_cost",
        "minimum_opposite_ask_depth_shares",
        "taker_slippage_per_share",
        "taker_fee_rate",
    ):
        value = float(normalized[key])
        if not math.isfinite(value):
            raise V020Error(f"Parametro V0.20 no finito: {key}")
        normalized[key] = value
    normalized["emergency_hedge_delay_ms"] = int(
        normalized["emergency_hedge_delay_ms"]
    )
    if normalized["mandatory_hedge"] is not True:
        raise V020Error("V0.20 exige cobertura obligatoria")
    if normalized["emergency_hedge_delay_ms"] < 0 or normalized[
        "emergency_hedge_delay_ms"
    ] > 1_000:
        raise V020Error("La pata unilateral no puede esperar mas de un segundo")
    if normalized["minimum_opposite_ask_depth_shares"] < normalized[
        "order_size_shares"
    ]:
        raise V020Error("La profundidad reservada no cubre la orden completa")
    if not 1.0 <= normalized["maximum_projected_entry_set_cost"] <= 1.10:
        raise V020Error("Cap proyectado V0.20 fuera de rango")
    if not 1.0 < normalized["maximum_emergency_pair_cost"] <= 2.0:
        raise V020Error("Cap de seguridad V0.20 fuera de rango")
    if not 0 <= normalized["taker_slippage_per_share"] < 0.10:
        raise V020Error("Slippage taker V0.20 invalido")
    if not 0 <= normalized["taker_fee_rate"] <= 1:
        raise V020Error("Fee taker V0.20 invalido")
    if normalized["paired_target_shares_per_side"] != 10.0:
        raise V020Error("V0.20 congela el objetivo en 10 shares por lado")
    return normalized


def taker_cost_per_share(
    ask: float, *, slippage_per_share: float, fee_rate: float
) -> tuple[float, float, float]:
    price = float(ask)
    if not math.isfinite(price):
        raise V020Error("Ask taker no finita")
    fill = min(0.999, max(0.001, price + float(slippage_per_share)))
    fee = float(fee_rate) * fill * (1.0 - fill)
    return fill, fee, fill + fee


def _opposite(side: str) -> str:
    return "DOWN" if side == "UP" else "UP"


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


class MandatoryHedgePaperEngine(FifoPairPaperEngine):
    def __init__(
        self,
        *,
        condition_id: str,
        market_start_ms: int,
        config: Mapping[str, Any],
    ) -> None:
        normalized = validate_strategy_config(config)
        base_keys = set(v019_default_strategy_config())
        super().__init__(
            condition_id=condition_id,
            market_start_ms=market_start_ms,
            config={key: normalized[key] for key in base_keys},
        )
        self.config = normalized
        self.emergency_attempts = 0
        self.emergency_fills = 0
        self.emergency_shares = 0.0
        self.emergency_cost = 0.0
        self.unmatched_episodes = 0
        self.maximum_unmatched_age_ms = 0
        self._active_unmatched_started_ms: int | None = None

    def _taker_components(self, ask: float) -> tuple[float, float, float]:
        return taker_cost_per_share(
            ask,
            slippage_per_share=float(self.config["taker_slippage_per_share"]),
            fee_rate=float(self.config["taker_fee_rate"]),
        )

    def _ask_depth(self, row: Mapping[str, Any], side: str) -> float | None:
        depth = _finite(row.get(f"{side.lower()}_ask_depth_1c"))
        return depth if depth is not None and depth >= 0 else None

    def _register_fifo(
        self,
        *,
        fill_id: str,
        side: str,
        timestamp_ms: int,
        price: float,
        size: float,
    ) -> None:
        was_flat = self.unmatched_side() is None
        remaining = float(size)
        opposite = _opposite(side)
        purpose = str(self.fills[-1].get("purpose")) if self.fills else ""
        mode = "MANDATORY_TAKER" if purpose == "MANDATORY_HEDGE" else "PASSIVE_FIFO"
        maximum = float(
            self.config[
                "maximum_emergency_pair_cost"
                if mode == "MANDATORY_TAKER"
                else "maximum_matched_set_cost"
            ]
        )
        while remaining > 1e-9 and self.unmatched[opposite]:
            lot = self.unmatched[opposite][0]
            matched = min(remaining, float(lot.remaining_size))
            set_cost = float(price) + float(lot.price)
            if set_cost > maximum + 1e-12:
                raise V020Error("Cobertura V0.20 excedio su limite matematico")
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
                "pairing_mode": mode,
            }
            self.pairs.append(pair)
            self._event("PAIR_MATCHED", pair=pair)
            remaining -= matched
            lot.remaining_size -= matched
            if lot.remaining_size <= 1e-9:
                self.unmatched[opposite].popleft()
        if remaining > 1e-9:
            if purpose == "MANDATORY_HEDGE":
                raise V020Error("Una cobertura taker no puede crear una pata nueva")
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
        is_flat = self.unmatched_side() is None
        if was_flat and not is_flat:
            self.unmatched_episodes += 1
            self._active_unmatched_started_ms = int(timestamp_ms)
        elif not was_flat and is_flat and self._active_unmatched_started_ms is not None:
            self.maximum_unmatched_age_ms = max(
                self.maximum_unmatched_age_ms,
                int(timestamp_ms) - self._active_unmatched_started_ms,
            )
            self._active_unmatched_started_ms = None

    def _validate_invariants(self) -> None:
        unmatched = self.unmatched_shares()
        if unmatched > float(self.config["maximum_unmatched_shares"]) + 1e-8:
            raise V020Error("V0.20 excedio inventario unilateral")
        up = float(self.inventory["UP"]["shares"])
        down = float(self.inventory["DOWN"]["shares"])
        if abs(abs(up - down) - unmatched) > 1e-7:
            raise V020Error("Ledger V0.20 no coincide con inventario")
        if abs(min(up, down) - self.paired_shares) > 1e-7:
            raise V020Error("Pares V0.20 no coinciden con inventario")
        if self.cash_deployed > float(self.config["maximum_cash_per_market"]) + 1e-8:
            raise V020Error("V0.20 excedio cash por mercado")
        for pair in self.pairs:
            cap = float(
                self.config[
                    "maximum_emergency_pair_cost"
                    if pair.get("pairing_mode") == "MANDATORY_TAKER"
                    else "maximum_matched_set_cost"
                ]
            )
            if float(pair["set_cost"]) > cap + 1e-12:
                raise V020Error("Par V0.20 excedio su cap")

    def _reprice(self, row: Mapping[str, Any], second: int) -> None:
        if self.unmatched_side() is not None:
            return
        values: dict[str, dict[str, float]] = {}
        for side in SIDES:
            parsed = self._row_values(row, side)
            depth = self._ask_depth(row, side)
            if parsed is None or depth is None:
                for candidate in SIDES:
                    self._cancel(candidate, second=second, reason="CANCELLED_INVALID_BOOK")
                return
            values[side] = {**parsed, "ask_depth": depth}
        for side in SIDES:
            self._cancel(side, second=second, reason="CANCELLED_REPRICE")
        remaining = float(self.config["paired_target_shares_per_side"]) - self.paired_shares
        if remaining <= 1e-9:
            return
        size = min(float(self.config["order_size_shares"]), remaining)
        if any(
            values[side]["ask_depth"]
            < max(size, float(self.config["minimum_opposite_ask_depth_shares"]))
            for side in SIDES
        ):
            return
        up_bid = values["UP"]["bid"]
        down_bid = values["DOWN"]["bid"]
        if up_bid + down_bid > float(self.config["maximum_matched_set_cost"]) + 1e-12:
            return
        _, _, down_taker = self._taker_components(values["DOWN"]["ask"])
        _, _, up_taker = self._taker_components(values["UP"]["ask"])
        projected_up = up_bid + down_taker
        projected_down = down_bid + up_taker
        projected_cap = float(self.config["maximum_projected_entry_set_cost"])
        if max(projected_up, projected_down) > projected_cap + 1e-12:
            return
        worst_cash = size * max(projected_up, projected_down)
        if self.cash_deployed + worst_cash > float(self.config["maximum_cash_per_market"]) + 1e-12:
            return
        self._place(
            "UP",
            purpose="OPEN_PAIR",
            second=second,
            price=up_bid,
            price_ceiling=float(self.config["maximum_matched_set_cost"]) - down_bid,
            size=size,
            depth=values["UP"]["depth"],
        )
        self._place(
            "DOWN",
            purpose="OPEN_PAIR",
            second=second,
            price=down_bid,
            price_ceiling=float(self.config["maximum_matched_set_cost"]) - up_bid,
            size=size,
            depth=values["DOWN"]["depth"],
        )

    def _execute_mandatory_hedge(
        self, row: Mapping[str, Any], *, timestamp_ms: int, second: int
    ) -> float:
        lead = self.unmatched_side()
        if lead is None:
            return 0.0
        hedge = _opposite(lead)
        ask = _finite(row.get(f"{hedge.lower()}_best_ask"))
        depth = self._ask_depth(row, hedge)
        self.emergency_attempts += 1
        if ask is None or depth is None or depth <= 1e-9:
            return 0.0
        fill_price, fee_per_share, total_per_share = self._taker_components(ask)
        cash_room = max(
            0.0,
            float(self.config["maximum_cash_per_market"]) - self.cash_deployed,
        )
        size = min(
            self.unmatched_shares(lead),
            depth,
            cash_room / total_per_share if total_per_share > 0 else 0.0,
        )
        if size <= 1e-9:
            return 0.0
        for candidate in SIDES:
            self._cancel(candidate, second=second, reason="CANCELLED_MANDATORY_HEDGE")
        self._fill_sequence += 1
        fill_id = f"{self.condition_id}:FILL:{self._fill_sequence}"
        fee = size * fee_per_share
        cost = size * total_per_share
        fill = {
            "fill_id": fill_id,
            "order_id": f"{self.condition_id}:PAPER_TAKER:{self._fill_sequence}",
            "side": hedge,
            "purpose": "MANDATORY_HEDGE",
            "timestamp_ms": int(timestamp_ms),
            "second_offset": (int(timestamp_ms) - self.market_start_ms) / 1000.0,
            "fill_price": fill_price,
            "fill_size": size,
            "fee": fee,
            "rebate": 0.0,
            "cash_cost": cost,
            "trigger": "OBSERVED_ASK_DEPTH_TAKER",
            "trigger_price": ask,
            "real_money": 0,
        }
        self.fills.append(fill)
        self.inventory[hedge]["shares"] += size
        self.inventory[hedge]["cost"] += cost
        self._register_fifo(
            fill_id=fill_id,
            side=hedge,
            timestamp_ms=timestamp_ms,
            price=total_per_share,
            size=size,
        )
        self._event("FILL", fill=fill, order=None)
        self.emergency_fills += 1
        self.emergency_shares += size
        self.emergency_cost += cost
        self._cancel_incompatible_after_fill(second)
        self._validate_invariants()
        return size

    def on_second(self, row: Mapping[str, Any]) -> None:
        if self._finished:
            raise V020Error("El mercado V0.20 ya termino")
        second = int(row.get("second_offset", -1))
        if second < 0 or second >= 300:
            raise V020Error("second_offset V0.20 invalido")
        timestamp_ms = self.market_start_ms + second * 1000
        for side in SIDES:
            quote = self.quotes.get(side)
            if quote is None or quote.status not in {"RESTING", "PARTIAL"}:
                continue
            if timestamp_ms >= quote.expires_at_ms:
                self._cancel(side, second=second, reason="CANCELLED_EXPIRED")
                continue
            ask = _finite(row.get(f"{side.lower()}_best_ask"))
            if timestamp_ms >= quote.active_at_ms and ask is not None and ask <= quote.price + 1e-12:
                self._fill(
                    quote,
                    timestamp_ms=timestamp_ms,
                    requested_size=quote.remaining_size,
                    trigger="ASK_CROSS",
                    trigger_price=ask,
                )
        lead = self.unmatched_side()
        if lead is not None:
            oldest = self.unmatched[lead][0]
            if timestamp_ms >= int(oldest.timestamp_ms) + int(
                self.config["emergency_hedge_delay_ms"]
            ):
                self._execute_mandatory_hedge(
                    row, timestamp_ms=timestamp_ms, second=second
                )
        start = int(self.config["quote_start_second"])
        end = int(self.config["quote_end_second"])
        interval = int(self.config["quote_interval_seconds"])
        if (
            self.unmatched_side() is None
            and start <= second <= end
            and (second - start) % interval == 0
        ):
            self._reprice(row, second)
        if second >= end + int(self.config["quote_lifetime_seconds"]):
            for side in SIDES:
                self._cancel(side, second=second, reason="CANCELLED_END_WINDOW")

    def finish(self) -> dict[str, Any]:
        summary = super().finish()
        if self._active_unmatched_started_ms is not None:
            self.maximum_unmatched_age_ms = max(
                self.maximum_unmatched_age_ms,
                self.market_start_ms + 300_000 - self._active_unmatched_started_ms,
            )
        emergency_pairs = [
            pair for pair in self.pairs if pair.get("pairing_mode") == "MANDATORY_TAKER"
        ]
        passive_pairs = [
            pair for pair in self.pairs if pair.get("pairing_mode") == "PASSIVE_FIFO"
        ]
        summary.update(
            {
                "mandatory_hedge": True,
                "emergency_attempts": self.emergency_attempts,
                "emergency_fills": self.emergency_fills,
                "emergency_shares": self.emergency_shares,
                "emergency_cash_cost": self.emergency_cost,
                "emergency_pair_matches": len(emergency_pairs),
                "passive_pair_matches": len(passive_pairs),
                "emergency_weighted_set_cost": (
                    sum(float(p["size"]) * float(p["set_cost"]) for p in emergency_pairs)
                    / sum(float(p["size"]) for p in emergency_pairs)
                    if emergency_pairs
                    else None
                ),
                "passive_weighted_set_cost": (
                    sum(float(p["size"]) * float(p["set_cost"]) for p in passive_pairs)
                    / sum(float(p["size"]) for p in passive_pairs)
                    if passive_pairs
                    else None
                ),
                "unmatched_episodes": self.unmatched_episodes,
                "maximum_unmatched_age_ms": self.maximum_unmatched_age_ms,
                "mandatory_hedge_completed": summary["unmatched_shares"] <= 1e-9,
            }
        )
        return summary


__all__ = [
    "MandatoryHedgePaperEngine",
    "V020Error",
    "default_strategy_config",
    "taker_cost_per_share",
    "validate_strategy_config",
]
