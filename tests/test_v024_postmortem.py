import unittest

from polymarket_bot.v024_postmortem import attribution_metrics


class V024PostmortemTests(unittest.TestCase):
    def test_attribution_exactly_decomposes_mean_pnl(self):
        rows = [
            {
                "favorite_side": "Up",
                "label": "Up",
                "favorite_probability": 0.70,
                "entry_cost": 0.75,
            },
            {
                "favorite_side": "Down",
                "label": "Up",
                "favorite_probability": 0.60,
                "entry_cost": 0.65,
            },
        ]
        metrics = attribution_metrics(rows)
        self.assertEqual(metrics["trades"], 2)
        self.assertEqual(metrics["wins"], 1)
        self.assertAlmostEqual(metrics["transaction_cost_drag_vs_mid"], 0.05)
        self.assertAlmostEqual(metrics["realization_gap_vs_market_mid"], -0.15)
        self.assertAlmostEqual(
            metrics["reconstructed_mean_pnl_per_share"], -0.20
        )
        self.assertAlmostEqual(metrics["net_pnl_per_share_sequence"], -0.40)
        self.assertAlmostEqual(metrics["net_pnl_at_5_shares"], -2.0)

    def test_empty_attribution_fails_closed(self):
        metrics = attribution_metrics([])
        self.assertEqual(metrics["trades"], 0)
        self.assertIsNone(metrics["average_entry_cost"])
        self.assertEqual(metrics["net_pnl_at_5_shares"], 0.0)


if __name__ == "__main__":
    unittest.main()
