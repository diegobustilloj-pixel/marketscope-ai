import unittest

from polymarket_bot.climate_manual_strategy import _fee_per_share, _metrics, classify_event


class ClimateManualStrategyTests(unittest.TestCase):
    def test_unclosed_near_binary_event_is_only_provisional(self) -> None:
        event = {
            "slug": "x",
            "closed": False,
            "markets": [
                {"id": "a", "groupItemTitle": "A", "outcomePrices": '["0.9995","0.0005"]', "closed": False},
                {"id": "b", "groupItemTitle": "B", "outcomePrices": '["0.0005","0.9995"]', "closed": False},
            ],
        }
        self.assertEqual(classify_event(event)["status"], "PROVISIONAL_CONSENSUS")

    def test_closed_unique_winner_is_official(self) -> None:
        event = {
            "slug": "x",
            "closed": True,
            "markets": [
                {"id": "a", "groupItemTitle": "A", "outcomePrices": '["1","0"]', "closed": True},
                {"id": "b", "groupItemTitle": "B", "outcomePrices": '["0","1"]', "closed": True},
            ],
        }
        result = classify_event(event)
        self.assertEqual(result["status"], "OFFICIAL_RESOLVED")
        self.assertEqual(result["winner_market_id"], "a")

    def test_metrics_use_chronological_drawdown(self) -> None:
        rows = [
            {"entry_at": "1", "slug": "a", "pnl_usdc": 10, "cost_usdc": 25},
            {"entry_at": "2", "slug": "b", "pnl_usdc": -15, "cost_usdc": 25},
            {"entry_at": "3", "slug": "c", "pnl_usdc": 8, "cost_usdc": 25},
        ]
        result = _metrics(rows)
        self.assertEqual(result["net_pnl_usdc"], 3)
        self.assertEqual(result["max_drawdown_usdc"], -15)

    def test_weather_fee_curve_uses_gamma_schedule(self) -> None:
        result = _fee_per_share(
            0.40,
            True,
            {"rate": 0.05, "exponent": 1, "takerOnly": True},
        )
        self.assertAlmostEqual(result, 0.012)

    def test_enabled_fee_without_schedule_is_unverified(self) -> None:
        self.assertIsNone(_fee_per_share(0.40, True, None))


if __name__ == "__main__":
    unittest.main()
