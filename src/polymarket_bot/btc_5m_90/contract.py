from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


BTC5M_SLUG = re.compile(r"^btc-updown-5m-(?P<epoch>\d{10})$")
TWAP_60S_SOURCE = "https://data.chain.link/streams/btc-usd-twap-60s-streams"

# Esta definición resuelve la contradicción del pedido original. ``ask <= .90``
# sería verdadero para ambos outcomes al abrir (~.50). El evento económico que se
# investiga es que el precio suba hasta el umbral y todavía pueda comprarse a él.
FIRST_TOUCH_SEMANTICS = (
    "first observed best ask exactly equal to the configured threshold; "
    "a jump from below to above is MISSED_SIGNAL and prices above are never chased"
)


class ContractError(ValueError):
    """Metadatos incompatibles con el contrato BTC 5m investigado."""


def _array(value: Any, field: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ContractError(f"{field} no contiene JSON válido") from exc
    if not isinstance(value, list):
        raise ContractError(f"{field} no es una lista")
    return tuple(str(item) for item in value)


def _parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} ausente")
    text = value.strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContractError(f"{field} inválido: {value}") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def slug_start_ms(slug: str) -> int:
    match = BTC5M_SLUG.fullmatch(str(slug))
    if match is None:
        raise ContractError(f"Slug BTC 5m inválido: {slug}")
    return int(match.group("epoch")) * 1000


@dataclass(frozen=True)
class FeeSchedule:
    enabled: bool
    rate: float
    exponent: float
    taker_only: bool
    rebate_rate: float

    def fee_per_share(self, price: float, *, taker: bool = True) -> float:
        if not self.enabled or (self.taker_only and not taker):
            return 0.0
        p = float(price)
        if not 0.0 < p < 1.0:
            raise ValueError("El precio debe estar entre 0 y 1")
        return self.rate * (p * (1.0 - p)) ** self.exponent

    def break_even_win_rate(self, price: float, *, taker: bool = True) -> float:
        return float(price) + self.fee_per_share(price, taker=taker)


@dataclass(frozen=True)
class MarketContract:
    market_id: str
    condition_id: str
    slug: str
    market_start_ms: int
    market_end_ms: int
    closed_at: str | None
    resolution_source: str
    up_token_id: str
    down_token_id: str
    winner: str | None
    tick_size: float
    minimum_order_shares: float
    fee: FeeSchedule
    payload: Mapping[str, Any]


def _winner(payload: Mapping[str, Any], *, require_resolution: bool) -> str | None:
    outcomes = _array(payload.get("outcomes"), "outcomes")
    raw_prices = _array(payload.get("outcomePrices"), "outcomePrices")
    if len(outcomes) != 2 or len(raw_prices) != 2:
        if require_resolution:
            raise ContractError("Outcome prices incompatibles")
        return None
    try:
        prices = tuple(float(value) for value in raw_prices)
    except ValueError as exc:
        raise ContractError("Outcome prices no numéricos") from exc
    winners = [
        outcome for outcome, price in zip(outcomes, prices, strict=True) if price >= 0.99
    ]
    losers = [price for price in prices if price <= 0.01]
    if bool(payload.get("closed")) and len(winners) == 1 and len(losers) == 1:
        if winners[0] not in {"Up", "Down"}:
            raise ContractError(f"Ganador no reconocido: {winners[0]}")
        return winners[0]
    if require_resolution:
        raise ContractError("Mercado sin resolución Gamma verificable")
    return None


def validate_gamma_market(
    payload: Mapping[str, Any],
    *,
    expected_slug: str | None = None,
    expected_condition_id: str | None = None,
    require_resolution: bool = True,
) -> MarketContract:
    """Valida reglas por mercado; no usa ``startDate`` como inicio del intervalo."""

    slug = str(payload.get("slug") or "")
    if expected_slug is not None and slug != expected_slug:
        raise ContractError(f"Gamma devolvió slug {slug}; se esperaba {expected_slug}")
    start_ms = slug_start_ms(slug)
    end_ms = int(_parse_datetime(payload.get("endDate"), "endDate").timestamp() * 1000)
    if end_ms != start_ms + 300_000:
        raise ContractError(
            f"Ventana no es de 5 minutos según slug/endDate: {slug} -> {end_ms}"
        )

    condition_id = str(payload.get("conditionId") or "")
    if not condition_id:
        raise ContractError("conditionId ausente")
    if expected_condition_id is not None and condition_id != expected_condition_id:
        raise ContractError("conditionId no coincide con la captura histórica")

    outcomes = _array(payload.get("outcomes"), "outcomes")
    token_ids = _array(payload.get("clobTokenIds"), "clobTokenIds")
    if outcomes != ("Up", "Down") or len(token_ids) != 2:
        raise ContractError("Se esperaban outcomes/tokens Up, Down")

    source = str(payload.get("resolutionSource") or "").rstrip("/")
    if source != TWAP_60S_SOURCE:
        raise ContractError(f"Fuente de resolución no es TWAP 60s: {source}")
    description = str(payload.get("description") or "").lower()
    required_rule_fragments = ("time-weighted average price", "greater than or equal", "otherwise")
    if any(fragment not in description for fragment in required_rule_fragments):
        raise ContractError("La descripción no confirma la regla TWAP/equality Up")

    fee_raw = payload.get("feeSchedule")
    if not isinstance(fee_raw, Mapping):
        raise ContractError("feeSchedule ausente")
    fee = FeeSchedule(
        enabled=bool(payload.get("feesEnabled")),
        rate=float(fee_raw.get("rate", 0.0)),
        exponent=float(fee_raw.get("exponent", 1.0)),
        taker_only=bool(fee_raw.get("takerOnly", False)),
        rebate_rate=float(fee_raw.get("rebateRate", 0.0)),
    )
    if fee.enabled and (fee.rate < 0.0 or fee.exponent <= 0.0):
        raise ContractError("Fee schedule inválido")
    tick_size = float(payload.get("orderPriceMinTickSize") or 0.0)
    minimum_order_shares = float(payload.get("orderMinSize") or 0.0)
    if tick_size <= 0.0 or minimum_order_shares <= 0.0:
        raise ContractError("Tick size/orderMinSize inválidos")

    return MarketContract(
        market_id=str(payload.get("id") or ""),
        condition_id=condition_id,
        slug=slug,
        market_start_ms=start_ms,
        market_end_ms=end_ms,
        closed_at=(str(payload["closedTime"]) if payload.get("closedTime") else None),
        resolution_source=source,
        up_token_id=token_ids[0],
        down_token_id=token_ids[1],
        winner=_winner(payload, require_resolution=require_resolution),
        tick_size=tick_size,
        minimum_order_shares=minimum_order_shares,
        fee=fee,
        payload=payload,
    )


@dataclass(frozen=True)
class Signal:
    condition_id: str
    side: str | None
    timestamp_ms: int
    threshold: float
    ask: float | None
    status: str


class ThresholdDetector:
    """Detector compartido por backtest y shadow con bloqueo por condition_id."""

    def __init__(self, threshold: float = 0.90, tick_size: float = 0.001) -> None:
        if not 0.0 < threshold < 1.0 or tick_size <= 0.0:
            raise ValueError("Threshold/tick inválidos")
        self.threshold = float(threshold)
        self.tick_size = float(tick_size)
        self._locked: set[str] = set()

    def is_touch(self, ask: float | None) -> bool:
        if ask is None:
            return False
        return abs(float(ask) - self.threshold) <= max(1e-9, self.tick_size / 2.0)

    def observe(
        self,
        condition_id: str,
        timestamp_ms: int,
        up_ask: float | None,
        down_ask: float | None,
    ) -> Signal | None:
        if condition_id in self._locked:
            return None
        touched = [
            side
            for side, ask in (("Up", up_ask), ("Down", down_ask))
            if self.is_touch(ask)
        ]
        if not touched:
            return None
        self._locked.add(condition_id)
        if len(touched) != 1:
            return Signal(
                condition_id, None, int(timestamp_ms), self.threshold, None, "AMBIGUOUS"
            )
        side = touched[0]
        ask = up_ask if side == "Up" else down_ask
        return Signal(
            condition_id, side, int(timestamp_ms), self.threshold, float(ask), "SIGNAL"
        )


def first_touch(
    condition_id: str,
    observations: Sequence[tuple[int, float | None, float | None]],
    *,
    threshold: float = 0.90,
    tick_size: float = 0.001,
) -> Signal | None:
    detector = ThresholdDetector(threshold=threshold, tick_size=tick_size)
    for timestamp_ms, up_ask, down_ask in sorted(observations, key=lambda row: row[0]):
        signal = detector.observe(condition_id, timestamp_ms, up_ask, down_ask)
        if signal is not None:
            return signal
    return None
