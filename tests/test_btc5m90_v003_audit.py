from __future__ import annotations

import unittest

from polymarket_bot.btc_5m_90.v003_audit import _maximum_drawdown, _trade_summary


class V003AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            {
                "condition_id": "a",
                "timestamp_ms": 1,
                "side": "Up",
                "ask": 0.90,
                "fill_shares": 5.0,
                "fee": 0.03,
                "total_debit": 4.53,
            },
            {
                "condition_id": "b",
                "timestamp_ms": 2,
                "side": "Down",
                "ask": 0.91,
                "fill_shares": 5.0,
                "fee": 0.03,
                "total_debit": 4.58,
            },
        ]
        self.winners = {"a": "Up", "b": "Up"}

    def test_summary_uses_exact_debit_and_fees(self) -> None:
        result = _trade_summary(self.rows, self.winners)
        self.assertEqual(result["trades"], 2)
        self.assertEqual(result["wins"], 1)
        self.assertAlmostEqual(result["net_pnl_usdc"], -4.11)
        self.assertAlmostEqual(result["break_even_win_rate"], 0.911)

    def test_drawdown_is_ordered_by_decision_time(self) -> None:
        self.assertAlmostEqual(_maximum_drawdown(reversed(self.rows), self.winners), 4.58)


if __name__ == "__main__":
    unittest.main()
