import unittest

from polymarket_bot.v018_evaluator import evaluate_performance, final_status


class V018EvaluatorTests(unittest.TestCase):
    def gates(self):
        return {
            "minimum_traded_markets": 2,
            "net_pnl_must_be_positive": True,
            "roi_must_be_positive": True,
            "first_half_net_pnl_must_be_nonnegative": True,
            "second_half_net_pnl_must_be_nonnegative": True,
            "maximum_largest_positive_market_share_of_total_profit": 0.8,
            "no_threshold_rescue": True,
        }

    def test_binary_payout_uses_all_shares_on_winning_side(self):
        markets = [
            {
                "condition_id": "a",
                "slug": "btc-updown-5m-1000",
                "market_start_ms": 1_000_000,
                "status": "COMPLETE",
                "fill_count": 2,
                "favorite_side": "UP",
                "up_shares": 25.0,
                "down_shares": 20.0,
                "up_cost": 10.0,
                "down_cost": 10.0,
            },
            {
                "condition_id": "b",
                "slug": "btc-updown-5m-1300",
                "market_start_ms": 1_300_000,
                "status": "COMPLETE",
                "fill_count": 2,
                "favorite_side": "DOWN",
                "up_shares": 20.0,
                "down_shares": 25.0,
                "up_cost": 10.0,
                "down_cost": 10.0,
            },
        ]
        result = evaluate_performance(
            markets,
            {"a": "Up", "b": "Down"},
            self.gates(),
            midpoint_ms=1_200_000,
        )
        self.assertEqual(result["capital_deployed"], 40.0)
        self.assertEqual(result["net_pnl"], 10.0)
        self.assertEqual(result["first_half_net_pnl"], 5.0)
        self.assertEqual(result["second_half_net_pnl"], 5.0)
        self.assertEqual(result["failures"], [])

    def test_decision_order_stops_at_behavior_before_performance(self):
        pre = {
            "safety_failures": [],
            "technical_failures": [],
            "frequency_failures": [],
            "behavior_failures": ["WEIGHTED_COMPLETE_SET_COST"],
        }
        status, failures = final_status(pre, ["NET_PNL"])
        self.assertEqual(status, "FAIL_BEHAVIOR_REPLICATION")
        self.assertEqual(failures, ["WEIGHTED_COMPLETE_SET_COST"])


if __name__ == "__main__":
    unittest.main()
