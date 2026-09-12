from __future__ import annotations

import unittest

from polymarket_bot.btc_5m_90.contract import FeeSchedule
from polymarket_bot.btc_5m_90.v002 import LateWindowEdgeEngine, exact_book_fill


class V002Tests(unittest.TestCase):
    def _engine(self) -> LateWindowEdgeEngine:
        return LateWindowEdgeEngine(
            condition_id="c",
            market_start_ms=1_000_000,
            market_end_ms=1_300_000,
            fee=FeeSchedule(True, 0.07, 1.0, True, 0.2),
        )

    def test_waits_full_four_minutes(self) -> None:
        engine = self._engine()
        self.assertIsNone(
            engine.observe(timestamp_ms=1_239_999, up_ask=0.90, down_ask=0.11)
        )
        decision = engine.observe(timestamp_ms=1_240_000, up_ask=0.90, down_ask=0.11)
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.status, "SIGNAL")

    def test_first_unprofitable_candidate_rejects_and_locks(self) -> None:
        engine = self._engine()
        decision = engine.observe(timestamp_ms=1_240_000, up_ask=0.92, down_ask=0.09)
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.status, "REJECTED_EDGE")
        self.assertIsNone(
            engine.observe(timestamp_ms=1_241_000, up_ask=0.91, down_ask=0.10)
        )

    def test_approved_prices_are_90_and_91(self) -> None:
        for price in (0.90, 0.91):
            engine = self._engine()
            decision = engine.observe(
                timestamp_ms=1_240_000, up_ask=price, down_ask=round(1.01 - price, 2)
            )
            self.assertIsNotNone(decision)
            assert decision is not None
            self.assertEqual(decision.status, "SIGNAL")
            self.assertGreaterEqual(decision.expected_edge_per_share or 0.0, 0.002)

    def test_full_book_requires_all_five_shares_below_maximum(self) -> None:
        self.assertIsNone(exact_book_fill({0.90: 4.0, 0.91: 10.0}, maximum_price=0.90))
        fill = exact_book_fill({0.89: 2.0, 0.90: 3.0}, maximum_price=0.90)
        self.assertIsNotNone(fill)
        assert fill is not None
        self.assertAlmostEqual(fill[0], 0.896)


if __name__ == "__main__":
    unittest.main()
