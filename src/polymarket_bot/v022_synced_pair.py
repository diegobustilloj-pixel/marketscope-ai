from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from polymarket_bot.domain import parse_message_metadata
from polymarket_bot.v021_safe_pair import execution_cost


SIDES = ("UP", "DOWN")


class V022Error(RuntimeError):
    """Raised when the V0.22 synchronized-observer contract is violated."""


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
        "maximum_cross_side_source_skew_ms": 250,
        "maximum_cross_side_receive_skew_ms": 250,
        "maximum_ask_age_ms": 500,
        "minimum_confirmation_ms": 500,
        "both_asks_must_refresh_after_candidate_open": True,
        "reject_out_of_order_ask_updates": True,
        "simultaneous_complete_set_only": True,
        "paper_orders_enabled": False,
    }


def validate_strategy_config(config: Mapping[str, Any]) -> dict[str, Any]:
    required = set(default_strategy_config())
    if set(config) != required:
        raise V022Error(
            f"Configuracion V0.22 incompatible; missing={sorted(required-set(config))}; "
            f"extra={sorted(set(config)-required)}"
        )
    normalized = dict(config)
    integer_keys = (
        "quote_start_second",
        "quote_end_second",
        "sample_bucket_ms",
        "maximum_cross_side_source_skew_ms",
        "maximum_cross_side_receive_skew_ms",
        "maximum_ask_age_ms",
        "minimum_confirmation_ms",
    )
    for key in integer_keys:
        normalized[key] = int(normalized[key])
    float_keys = (
        "order_size_shares",
        "maximum_complete_set_cost",
        "taker_slippage_per_share",
        "taker_fee_rate",
        "minimum_price",
        "maximum_price",
    )
    for key in float_keys:
        normalized[key] = float(normalized[key])
        if not math.isfinite(normalized[key]):
            raise V022Error(f"Parametro V0.22 no finito: {key}")
    if not 0 <= normalized["quote_start_second"] < normalized["quote_end_second"] < 300:
        raise V022Error("Ventana V0.22 invalida")
    if normalized["order_size_shares"] <= 0:
        raise V022Error("Tamano V0.22 invalido")
    if not 0 < normalized["maximum_complete_set_cost"] < 1:
        raise V022Error("V0.22 exige coste completo menor que uno")
    if not 0 <= normalized["taker_slippage_per_share"] < 0.1:
        raise V022Error("Slippage V0.22 invalido")
    if not 0 <= normalized["taker_fee_rate"] <= 1:
        raise V022Error("Fee V0.22 invalido")
    if normalized["sample_bucket_ms"] != 250:
        raise V022Error("V0.22 congela resolucion de 250 ms")
    if not 0 < normalized["minimum_price"] < normalized["maximum_price"] < 1:
        raise V022Error("Rango de precios V0.22 invalido")
    for key in (
        "maximum_cross_side_source_skew_ms",
        "maximum_cross_side_receive_skew_ms",
        "maximum_ask_age_ms",
        "minimum_confirmation_ms",
    ):
        if normalized[key] <= 0:
            raise V022Error(f"Control temporal V0.22 invalido: {key}")
    if normalized["maximum_ask_age_ms"] < normalized["maximum_cross_side_receive_skew_ms"]:
        raise V022Error("La edad maxima V0.22 no puede ser menor al desfase de recepcion")
    if normalized["both_asks_must_refresh_after_candidate_open"] is not True:
        raise V022Error("V0.22 exige renovar ambos asks antes de confirmar")
    if normalized["reject_out_of_order_ask_updates"] is not True:
        raise V022Error("V0.22 exige rechazar updates ask atrasados")
    if normalized["simultaneous_complete_set_only"] is not True:
        raise V022Error("V0.22 prohibe oportunidades de una sola pata")
    if normalized["paper_orders_enabled"] is not False:
        raise V022Error("V0.22 es observador y no puede crear paper orders")
    return normalized


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass(slots=True)
class _AskBook:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    ask_initialized: bool = False
    ask_source_timestamp_ms: int = 0
    ask_received_timestamp_ms: int = 0
    ask_update_sequence: int = 0

    @staticmethod
    def _levels(payload: Mapping[str, Any], name: str) -> dict[float, float]:
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

    def replace(
        self,
        payload: Mapping[str, Any],
        *,
        source_timestamp_ms: int,
        received_timestamp_ms: int,
        reject_out_of_order: bool,
    ) -> tuple[bool, bool]:
        if (
            self.ask_initialized
            and reject_out_of_order
            and int(source_timestamp_ms) < self.ask_source_timestamp_ms
        ):
            return False, True
        self.bids = self._levels(payload, "bids")
        self.asks = self._levels(payload, "asks")
        self.ask_initialized = True
        self.ask_source_timestamp_ms = int(source_timestamp_ms)
        self.ask_received_timestamp_ms = int(received_timestamp_ms)
        self.ask_update_sequence += 1
        return True, False

    def change(
        self,
        *,
        side: str,
        price: float,
        size: float,
        source_timestamp_ms: int,
        received_timestamp_ms: int,
        reject_out_of_order: bool,
    ) -> tuple[bool, bool]:
        if side == "BUY":
            if size <= 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = size
            return False, False
        if (
            reject_out_of_order
            and int(source_timestamp_ms) < self.ask_source_timestamp_ms
        ):
            return False, True
        if size <= 0:
            self.asks.pop(price, None)
        else:
            self.asks[price] = size
        self.ask_source_timestamp_ms = int(source_timestamp_ms)
        self.ask_received_timestamp_ms = int(received_timestamp_ms)
        self.ask_update_sequence += 1
        return True, False


class SyncedPairOpportunityEngine:
    """Observe complete sets only after fresh, ordered and persistent ask updates."""

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
            raise V022Error("V0.22 exige exactamente tokens UP y DOWN")
        self.config = validate_strategy_config(config)
        self.books = {side: _AskBook() for side in SIDES}
        self.message_count = 0
        self.valid_observations = 0
        self.synchronized_observations = 0
        self.unsynchronized_observations = 0
        self.raw_eligible_observations = 0
        self.raw_opportunity_episodes = 0
        self.confirmed_observations = 0
        self.confirmed_opportunity_episodes = 0
        self.minimum_complete_set_cost: float | None = None
        self.minimum_synchronized_complete_set_cost: float | None = None
        self.minimum_raw_eligible_cost: float | None = None
        self.minimum_confirmed_cost: float | None = None
        self.maximum_observed_depth_shares = 0.0
        self.maximum_candidate_persistence_ms = 0
        self.stale_ask_updates_rejected = 0
        self.nonmonotonic_receive_timestamps = 0
        self._last_received_timestamp_ms: int | None = None
        self._candidate_started_received_ms: int | None = None
        self._candidate_last_observed_received_ms: int | None = None
        self._candidate_start_sequences: dict[str, int] = {}
        self._candidate_confirmed = False
        self._pending_bucket: dict[str, Any] | None = None
        self._samples: list[dict[str, Any]] = []
        self._signals: list[dict[str, Any]] = []

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

    @staticmethod
    def _minimum(current: float | None, value: float) -> float:
        return value if current is None else min(current, value)

    def _normalize_received(self, received_timestamp_ms: int) -> int:
        received = int(received_timestamp_ms)
        if self._last_received_timestamp_ms is not None and received < self._last_received_timestamp_ms:
            self.nonmonotonic_receive_timestamps += 1
            received = self._last_received_timestamp_ms
        self._last_received_timestamp_ms = received
        return received

    def _apply_book(self, payload: Any, fallback_ms: int, received_ms: int) -> bool:
        items = payload if isinstance(payload, list) else [payload]
        ask_changed = False
        for item in items:
            if not isinstance(item, Mapping):
                continue
            token = str(item.get("asset_id") or "")
            outcome = self.token_sides.get(token)
            if outcome is None:
                continue
            changed, stale = self.books[outcome].replace(
                item,
                source_timestamp_ms=self._timestamp(item, fallback_ms),
                received_timestamp_ms=received_ms,
                reject_out_of_order=bool(self.config["reject_out_of_order_ask_updates"]),
            )
            ask_changed = ask_changed or changed
            self.stale_ask_updates_rejected += int(stale)
        return ask_changed

    def _apply_price_change(self, payload: Any, fallback_ms: int, received_ms: int) -> bool:
        if not isinstance(payload, Mapping):
            return False
        raw_changes = payload.get("price_changes", [])
        if not isinstance(raw_changes, list):
            return False
        source_ms = self._timestamp(payload, fallback_ms)
        ask_changed = False
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
            if not book.ask_initialized:
                continue
            changed, stale = book.change(
                side=side,
                price=price,
                size=size,
                source_timestamp_ms=source_ms,
                received_timestamp_ms=received_ms,
                reject_out_of_order=bool(self.config["reject_out_of_order_ask_updates"]),
            )
            ask_changed = ask_changed or changed
            self.stale_ask_updates_rejected += int(stale)
        return ask_changed

    def _close_candidate(self) -> None:
        if (
            self._candidate_started_received_ms is not None
            and self._candidate_last_observed_received_ms is not None
        ):
            self.maximum_candidate_persistence_ms = max(
                self.maximum_candidate_persistence_ms,
                max(
                    0,
                    self._candidate_last_observed_received_ms
                    - self._candidate_started_received_ms,
                ),
            )
        self._candidate_started_received_ms = None
        self._candidate_last_observed_received_ms = None
        self._candidate_start_sequences = {}
        self._candidate_confirmed = False

    def tick(self, *, received_timestamp_ms: int) -> None:
        received = self._normalize_received(received_timestamp_ms)
        if self._candidate_started_received_ms is None:
            return
        maximum_age = int(self.config["maximum_ask_age_ms"])
        if any(
            not self.books[side].ask_initialized
            or received - self.books[side].ask_received_timestamp_ms > maximum_age
            for side in SIDES
        ):
            self._close_candidate()

    def _signal(
        self,
        *,
        event_type: str,
        received_ms: int,
        complete_cost: float,
        candidate_age_ms: int,
        source_skew_ms: int,
        receive_skew_ms: int,
        maximum_book_age_ms: int,
    ) -> None:
        self._signals.append(
            {
                "condition_id": self.condition_id,
                "event_type": event_type,
                "received_timestamp_ms": received_ms,
                "complete_set_cost": complete_cost,
                "candidate_age_ms": candidate_age_ms,
                "up_source_timestamp_ms": self.books["UP"].ask_source_timestamp_ms,
                "down_source_timestamp_ms": self.books["DOWN"].ask_source_timestamp_ms,
                "up_received_timestamp_ms": self.books["UP"].ask_received_timestamp_ms,
                "down_received_timestamp_ms": self.books["DOWN"].ask_received_timestamp_ms,
                "source_skew_ms": source_skew_ms,
                "receive_skew_ms": receive_skew_ms,
                "maximum_book_age_ms": maximum_book_age_ms,
            }
        )

    def _accumulate_sample(
        self,
        *,
        received_ms: int,
        complete_cost: float,
        synchronized: bool,
        raw_eligible: bool,
        confirmed: bool,
        source_skew_ms: int,
        receive_skew_ms: int,
        candidate_age_ms: int,
    ) -> None:
        bucket_size = int(self.config["sample_bucket_ms"])
        bucket_ms = received_ms // bucket_size * bucket_size
        sample = {
            "condition_id": self.condition_id,
            "bucket_ms": bucket_ms,
            "valid_observations": 1,
            "synchronized_observations": int(synchronized),
            "raw_eligible_observations": int(raw_eligible),
            "confirmed_observations": int(confirmed),
            "minimum_complete_set_cost": complete_cost,
            "minimum_synchronized_cost": complete_cost if synchronized else None,
            "minimum_raw_eligible_cost": complete_cost if raw_eligible else None,
            "minimum_confirmed_cost": complete_cost if confirmed else None,
            "minimum_source_skew_ms": source_skew_ms if synchronized else None,
            "minimum_receive_skew_ms": receive_skew_ms if synchronized else None,
            "maximum_candidate_age_ms": candidate_age_ms,
        }
        pending = self._pending_bucket
        if pending is None:
            self._pending_bucket = sample
            return
        if int(pending["bucket_ms"]) != bucket_ms:
            self._samples.append(pending)
            self._pending_bucket = sample
            return
        for key in (
            "valid_observations",
            "synchronized_observations",
            "raw_eligible_observations",
            "confirmed_observations",
        ):
            pending[key] = int(pending[key]) + int(sample[key])
        for key in (
            "minimum_complete_set_cost",
            "minimum_synchronized_cost",
            "minimum_raw_eligible_cost",
            "minimum_confirmed_cost",
            "minimum_source_skew_ms",
            "minimum_receive_skew_ms",
        ):
            value = sample[key]
            if value is not None:
                pending[key] = value if pending[key] is None else min(pending[key], value)
        pending["maximum_candidate_age_ms"] = max(
            int(pending["maximum_candidate_age_ms"]), candidate_age_ms
        )

    def _observe(self, received_ms: int) -> None:
        if not all(self.books[side].ask_initialized for side in SIDES):
            self._close_candidate()
            return
        source_ms = max(self.books[side].ask_source_timestamp_ms for side in SIDES)
        offset = (source_ms - self.market_start_ms) / 1000.0
        if not int(self.config["quote_start_second"]) <= offset <= int(
            self.config["quote_end_second"]
        ):
            self._close_candidate()
            return
        costs: dict[str, dict[str, float]] = {}
        size = float(self.config["order_size_shares"])
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
                self._close_candidate()
                return
            costs[side] = cost
        complete_cost = sum(costs[side]["total_cost_per_share"] for side in SIDES)
        self.valid_observations += 1
        self.minimum_complete_set_cost = self._minimum(
            self.minimum_complete_set_cost, complete_cost
        )
        depth = min(sum(self.books[side].asks.values()) for side in SIDES)
        self.maximum_observed_depth_shares = max(self.maximum_observed_depth_shares, depth)

        source_skew = abs(
            self.books["UP"].ask_source_timestamp_ms
            - self.books["DOWN"].ask_source_timestamp_ms
        )
        receive_skew = abs(
            self.books["UP"].ask_received_timestamp_ms
            - self.books["DOWN"].ask_received_timestamp_ms
        )
        ages = {
            side: max(0, received_ms - self.books[side].ask_received_timestamp_ms)
            for side in SIDES
        }
        maximum_book_age = max(ages.values())
        synchronized = (
            source_skew <= int(self.config["maximum_cross_side_source_skew_ms"])
            and receive_skew <= int(self.config["maximum_cross_side_receive_skew_ms"])
            and maximum_book_age <= int(self.config["maximum_ask_age_ms"])
        )
        raw_eligible = synchronized and complete_cost <= float(
            self.config["maximum_complete_set_cost"]
        ) + 1e-12
        confirmed = False
        candidate_age = 0
        if synchronized:
            self.synchronized_observations += 1
            self.minimum_synchronized_complete_set_cost = self._minimum(
                self.minimum_synchronized_complete_set_cost, complete_cost
            )
        else:
            self.unsynchronized_observations += 1

        if not raw_eligible:
            self._close_candidate()
        else:
            self.raw_eligible_observations += 1
            self.minimum_raw_eligible_cost = self._minimum(
                self.minimum_raw_eligible_cost, complete_cost
            )
            if self._candidate_started_received_ms is None:
                self.raw_opportunity_episodes += 1
                self._candidate_started_received_ms = received_ms
                self._candidate_last_observed_received_ms = received_ms
                self._candidate_start_sequences = {
                    side: self.books[side].ask_update_sequence for side in SIDES
                }
                self._candidate_confirmed = False
                self._signal(
                    event_type="RAW_OPEN",
                    received_ms=received_ms,
                    complete_cost=complete_cost,
                    candidate_age_ms=0,
                    source_skew_ms=source_skew,
                    receive_skew_ms=receive_skew,
                    maximum_book_age_ms=maximum_book_age,
                )
            else:
                self._candidate_last_observed_received_ms = received_ms
            candidate_age = max(0, received_ms - self._candidate_started_received_ms)
            refreshed = all(
                self.books[side].ask_update_sequence
                > self._candidate_start_sequences.get(side, self.books[side].ask_update_sequence)
                for side in SIDES
            )
            if (
                not self._candidate_confirmed
                and candidate_age >= int(self.config["minimum_confirmation_ms"])
                and refreshed
            ):
                self._candidate_confirmed = True
                self.confirmed_opportunity_episodes += 1
                self._signal(
                    event_type="CONFIRMED",
                    received_ms=received_ms,
                    complete_cost=complete_cost,
                    candidate_age_ms=candidate_age,
                    source_skew_ms=source_skew,
                    receive_skew_ms=receive_skew,
                    maximum_book_age_ms=maximum_book_age,
                )
            if self._candidate_confirmed:
                confirmed = True
                self.confirmed_observations += 1
                self.minimum_confirmed_cost = self._minimum(
                    self.minimum_confirmed_cost, complete_cost
                )
            self.maximum_candidate_persistence_ms = max(
                self.maximum_candidate_persistence_ms, candidate_age
            )

        self._accumulate_sample(
            received_ms=received_ms,
            complete_cost=complete_cost,
            synchronized=synchronized,
            raw_eligible=raw_eligible,
            confirmed=confirmed,
            source_skew_ms=source_skew,
            receive_skew_ms=receive_skew,
            candidate_age_ms=candidate_age,
        )

    def ingest(self, raw: str, *, received_timestamp_ms: int) -> None:
        self.message_count += 1
        received_ms = self._normalize_received(received_timestamp_ms)
        self.tick(received_timestamp_ms=received_ms)
        try:
            stream, source_timestamp_ms, _, _ = parse_message_metadata(raw, "market")
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        fallback = int(source_timestamp_ms or received_ms)
        changed = False
        if stream == "book":
            changed = self._apply_book(payload, fallback, received_ms)
        elif stream == "price_change":
            changed = self._apply_price_change(payload, fallback, received_ms)
        if changed:
            self._observe(received_ms)

    def drain_samples(self) -> list[dict[str, Any]]:
        samples = self._samples
        self._samples = []
        return samples

    def drain_signals(self) -> list[dict[str, Any]]:
        signals = self._signals
        self._signals = []
        return signals

    def finish(self, *, received_timestamp_ms: int) -> dict[str, Any]:
        self.tick(received_timestamp_ms=received_timestamp_ms)
        self._close_candidate()
        if self._pending_bucket is not None:
            self._samples.append(self._pending_bucket)
            self._pending_bucket = None
        return {
            "condition_id": self.condition_id,
            "message_count": self.message_count,
            "valid_observations": self.valid_observations,
            "synchronized_observations": self.synchronized_observations,
            "unsynchronized_observations": self.unsynchronized_observations,
            "raw_eligible_observations": self.raw_eligible_observations,
            "raw_opportunity_episodes": self.raw_opportunity_episodes,
            "confirmed_observations": self.confirmed_observations,
            "confirmed_opportunity_episodes": self.confirmed_opportunity_episodes,
            "minimum_complete_set_cost": self.minimum_complete_set_cost,
            "minimum_synchronized_complete_set_cost": self.minimum_synchronized_complete_set_cost,
            "minimum_raw_eligible_cost": self.minimum_raw_eligible_cost,
            "minimum_confirmed_cost": self.minimum_confirmed_cost,
            "maximum_observed_depth_shares": self.maximum_observed_depth_shares,
            "maximum_candidate_persistence_ms": self.maximum_candidate_persistence_ms,
            "stale_ask_updates_rejected": self.stale_ask_updates_rejected,
            "nonmonotonic_receive_timestamps": self.nonmonotonic_receive_timestamps,
            "unilateral_positions": 0,
            "paper_orders": 0,
            "orders_sent": 0,
            "outcomes_read": 0,
            "real_money": 0,
        }


__all__ = [
    "SyncedPairOpportunityEngine",
    "V022Error",
    "default_strategy_config",
    "validate_strategy_config",
]
