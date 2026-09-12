from __future__ import annotations

import unittest

from polymarket_bot.btc_5m_90.backtest import (
    MarketSeries,
    Observation,
    detect_signal,
    fill_signal,
    literal_control,
    metrics,
)
from polymarket_bot.btc_5m_90.contract import FeeSchedule, MarketContract


def _contract(winner: str = "Up") -> MarketContract:
    return MarketContract(
        market_id="1",
        condition_id="c",
        slug="btc-updown-5m-1788786600",
        market_start_ms=1_788_786_600_000,
        market_end_ms=1_788_786_900_000,
        closed_at="2026-09-07 10:35:30+00",
        resolution_source="twap",
        up_token_id="u",
        down_token_id="d",
        winner=winner,
        tick_size=0.001,
        minimum_order_shares=5,
        fee=FeeSchedule(True, 0.07, 1, True, 0.2),
        payload={},
    )


def _series() -> MarketSeries:
    start = 1_788_786_600_000
    return MarketSeries(
        source="synthetic",
        split="TEST",
        condition_id="c",
        slug="btc-updown-5m-1788786600",
        start_ms=start,
        end_ms=start + 300_000,
        sampling_ms=250,
        execution_quality="LEVEL_A_SYNTHETIC",
        observations=(
            Observation(start + 1_000, start + 900, 0.51, 0.50, 20, 20),
            Observation(start + 2_000, start + 1_900, 0.90, 0.11, 20, 20),
            Observation(start + 2_250, start + 2_150, 0.91, 0.10, 20, 20),
            Observation(start + 3_000, start + 2_900, 0.89, 0.12, 20, 20),
        ),
    )


class BacktestTests(unittest.TestCase):
    def test_literal_control_exposes_opening_ambiguity(self) -> None:
        self.assertEqual(
            literal_control(_series()), "AMBIGUOUS_BOTH_AT_FIRST_OBSERVATION"
        )

    def test_latency_can_miss_and_later_better_price_can_fill(self) -> None:
        series = _series()
        contract = _contract()
        signal = detect_signal(series, contract, 0.90)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "Up")
        self.assertIsNone(fill_signal(series, contract, signal, latency_ms=250))
        delayed = fill_signal(series, contract, signal, latency_ms=1000)
        self.assertIsNotNone(delayed)
        assert delayed is not None
        self.assertEqual(delayed.fill_price, 0.89)

    def test_fee_adjusted_pnl_and_metrics(self) -> None:
        series = _series()
        contract = _contract()
        signal = detect_signal(series, contract, 0.90)
        self.assertIsNotNone(signal)
        assert signal is not None
        trade = fill_signal(series, contract, signal, latency_ms=0)
        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(round(trade.fee, 4), 0.0315)
        self.assertEqual(round(trade.net_pnl, 4), 0.4685)
        result = metrics([trade], markets=1, signals=1)
        self.assertEqual(result["wins"], 1)
        self.assertAlmostEqual(result["break_even_win_rate"], 0.9063)


if __name__ == "__main__":
    unittest.main()
