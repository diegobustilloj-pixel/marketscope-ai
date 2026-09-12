import unittest

from polymarket_bot.sports_final_report import (
    capital_metrics,
    price_bucket,
    summarize_rows,
    timing_bucket,
)


class SportsFinalReportTests(unittest.TestCase):
    def test_price_buckets_cover_requested_ranges(self):
        self.assertEqual(price_bucket(0.05), "0.01-0.10")
        self.assertEqual(price_bucket(0.10), "0.10-0.20")
        self.assertEqual(price_bucket(0.97), "0.95-0.99")

    def test_timing_bucket_separates_pregame_and_live(self):
        self.assertEqual(timing_bucket(25 * 3600), "PREGAME_GT_24H")
        self.assertEqual(timing_bucket(300), "PREGAME_0_10M")
        self.assertEqual(timing_bucket(-60), "LIVE_EARLY_0_30M")

    def test_summary_uses_chronological_drawdown(self):
        rows = [
            {"pnl": 10, "cost": 100, "time": 1},
            {"pnl": -15, "cost": 100, "time": 2},
            {"pnl": 8, "cost": 100, "time": 3},
        ]
        result = summarize_rows(rows, pnl_field="pnl", cost_field="cost", time_field="time")
        self.assertEqual(result["net_pnl_usd"], 3)
        self.assertEqual(result["max_drawdown_usd"], -15)

    def test_capital_metrics_are_time_weighted(self):
        rows = [
            {
                "condition_id": "a",
                "first_trade_timestamp": 1,
                "resolution_timestamp": 11,
                "official_total_bought_cost": 100,
                "official_realized_pnl": 10,
            },
            {
                "condition_id": "b",
                "first_trade_timestamp": 6,
                "resolution_timestamp": 16,
                "official_total_bought_cost": 50,
                "official_realized_pnl": 5,
            },
        ]
        result = capital_metrics(rows)
        self.assertEqual(result["peak_concurrent_capital_proxy_usd"], 150)
        self.assertAlmostEqual(result["average_concurrent_capital_proxy_usd"], 100)


if __name__ == "__main__":
    unittest.main()
