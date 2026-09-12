import unittest

from polymarket_bot import sports_wallet_analysis as analysis


class SportsWalletAnalysisTests(unittest.TestCase):
    def test_fifo_lifecycle_matches_buy_then_sell_and_settlement(self):
        rows = [
            {"timestamp": 1, "side": "BUY", "size": 10, "price": 0.4},
            {"timestamp": 2, "side": "SELL", "size": 4, "price": 0.6},
        ]

        result = analysis.fifo_lifecycle(rows, payout=1.0, resolution_ts=3)

        self.assertAlmostEqual(result["fifo_trade_pnl"], 0.8)
        self.assertAlmostEqual(result["settlement_pnl"], 3.6)
        self.assertAlmostEqual(result["gross_reconstructed_pnl"], 4.4)

    def test_resolved_payout_requires_binary_final_prices(self):
        self.assertEqual(
            analysis.resolved_payouts({"outcome_prices": '["0", "1"]'}),
            {0: 0.0, 1: 1.0},
        )
        self.assertIsNone(
            analysis.resolved_payouts({"outcome_prices": '["0.4", "0.6"]'})
        )

    def test_behavioral_style_does_not_call_both_sides_arbitrage(self):
        rows = [
            {"side": "BUY", "outcome_index": 0, "notional_usd": 60},
            {"side": "BUY", "outcome_index": 1, "notional_usd": 40},
        ]

        self.assertEqual(analysis.behavioral_style(rows), "paired_inventory_hold")

    def test_pnl_summary_uses_resolution_order_for_drawdown(self):
        rows = [
            {"resolution_timestamp": 1, "official_realized_pnl": 10, "official_total_bought_cost": 100},
            {"resolution_timestamp": 2, "official_realized_pnl": -6, "official_total_bought_cost": 50},
            {"resolution_timestamp": 3, "official_realized_pnl": 2, "official_total_bought_cost": 25},
        ]

        result = analysis.summarize_pnl(rows)

        self.assertEqual(result["total_official_realized_pnl"], 6)
        self.assertEqual(result["max_drawdown_by_resolution"], -6)
        self.assertAlmostEqual(result["profit_factor"], 2.0)


if __name__ == "__main__":
    unittest.main()
