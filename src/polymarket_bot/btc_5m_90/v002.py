from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .contract import FeeSchedule


WAIT_SECONDS = 240
MARKET_SECONDS = 300
PRICE_GRID = (0.90, 0.91, 0.92, 0.93, 0.94, 0.95)
MIN_EXPECTED_EDGE = 0.002  # 0.2 centavos de USDC por share.
ORDER_SIZE_SHARES = 5.0


@dataclass(frozen=True)
class CalibrationRow:
    price: float
    train_count: int
    train_wins: int
    validation_count: int
    validation_wins: int
    conservative_expected_edge: float
    approved: bool


# Desarrollo histórico anterior al shadow V002. TEST V001 ya se había abierto, por
# lo que no participa en esta selección. Un precio sólo se aprueba si TRAIN y
# VALIDATION tienen >=30/>=10 casos y EV >= 0.002 por share por separado.
CALIBRATION: Mapping[float, CalibrationRow] = {
    0.90: CalibrationRow(0.90, 86, 81, 15, 15, 0.0355604651162790, True),
    0.91: CalibrationRow(0.91, 46, 43, 15, 14, 0.0176000000000000, True),
    0.92: CalibrationRow(0.92, 37, 35, 17, 14, -0.1016225882352941, False),
    0.93: CalibrationRow(0.93, 39, 36, 9, 8, -0.0456681111111111, False),
    0.94: CalibrationRow(0.94, 30, 28, 10, 10, -0.0106146666666667, False),
    0.95: CalibrationRow(0.95, 34, 33, 23, 21, -0.0402815217391304, False),
}


class V002Error(ValueError):
    pass


@dataclass(frozen=True)
class LateDecision:
    condition_id: str
    timestamp_ms: int
    seconds_elapsed: float
    seconds_remaining: float
    side: str | None
    ask: float | None
    expected_edge_per_share: float | None
    status: str
    reason: str


class LateWindowEdgeEngine:
    """Primera decisión 90–95c después de 4m, con lock aun si se rechaza."""

    def __init__(
        self,
        *,
        condition_id: str,
        market_start_ms: int,
        market_end_ms: int,
        fee: FeeSchedule,
    ) -> None:
        if market_end_ms - market_start_ms != MARKET_SECONDS * 1000:
            raise V002Error("V002 exige exactamente cinco minutos")
        self.condition_id = str(condition_id)
        self.market_start_ms = int(market_start_ms)
        self.market_end_ms = int(market_end_ms)
        self.fee = fee
        self.locked = False

    @staticmethod
    def _grid_price(ask: float | None) -> float | None:
        if ask is None:
            return None
        value = float(ask)
        return next((price for price in PRICE_GRID if abs(value - price) <= 1e-9), None)

    def observe(
        self,
        *,
        timestamp_ms: int,
        up_ask: float | None,
        down_ask: float | None,
    ) -> LateDecision | None:
        if self.locked:
            return None
        elapsed = (int(timestamp_ms) - self.market_start_ms) / 1000.0
        if elapsed < WAIT_SECONDS:
            return None
        if int(timestamp_ms) >= self.market_end_ms:
            self.locked = True
            return LateDecision(
                self.condition_id,
                int(timestamp_ms),
                elapsed,
                0.0,
                None,
                None,
                None,
                "EXPIRED",
                "market_window_closed",
            )

        candidates = [
            (side, price)
            for side, ask in (("Up", up_ask), ("Down", down_ask))
            if (price := self._grid_price(ask)) is not None
        ]
        if not candidates:
            return None
        self.locked = True
        remaining = max(0.0, (self.market_end_ms - int(timestamp_ms)) / 1000.0)
        if len(candidates) != 1:
            return LateDecision(
                self.condition_id,
                int(timestamp_ms),
                elapsed,
                remaining,
                None,
                None,
                None,
                "AMBIGUOUS",
                "both_outcomes_hit_candidate_grid_same_observation",
            )

        side, price = candidates[0]
        calibration = CALIBRATION[price]
        current_fee_edge_floor = calibration.conservative_expected_edge
        approved = calibration.approved and current_fee_edge_floor >= MIN_EXPECTED_EDGE
        return LateDecision(
            self.condition_id,
            int(timestamp_ms),
            elapsed,
            remaining,
            side,
            price,
            current_fee_edge_floor,
            "SIGNAL" if approved else "REJECTED_EDGE",
            "approved_historical_development_gate"
            if approved
            else "price_failed_train_validation_edge_gate",
        )


def exact_book_fill(
    asks: Mapping[float, float],
    *,
    maximum_price: float,
    shares: float = ORDER_SIZE_SHARES,
) -> tuple[float, float] | None:
    """Devuelve VWAP y shares si el libro completo prueba capacidad bajo el límite."""

    remaining = float(shares)
    notional = 0.0
    for price, size in sorted((float(p), float(q)) for p, q in asks.items()):
        if price > maximum_price + 1e-12 or size <= 0.0:
            continue
        taken = min(remaining, size)
        remaining -= taken
        notional += taken * price
        if remaining <= 1e-9:
            return notional / shares, shares
    return None
