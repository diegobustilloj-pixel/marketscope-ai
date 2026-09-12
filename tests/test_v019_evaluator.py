import unittest

from polymarket_bot.v019_evaluator import evaluate_performance, final_status


class V019EvaluatorTests(unittest.TestCase):
    def gates(self):
        return {
            "minimum_traded_markets": 1,
            "net_pnl_must_be_positive": True,
            "roi_must_be_positive": True,
            "first_half_net_pnl_must_be_nonnegative": True,
            "second_half_net_pnl_must_be_nonnegative": True,
            "maximum_largest_positive_market_share_of_total_profit": 1.0,
            "no_threshold_rescue": True,
        }

    def test_binary_payout_decomposes_pair_and_residual_exactly(self):
        market = {
            "condition_id": "a",
            "slug": "btc-updown-5m-1000",
            "market_start_ms": 1_000_000,
            "status": "COMPLETE",
            "fill_count": 3,
            "up_shares": 15.0,
            "down_shares": 10.0,
            "up_cost": 7.0,
            "down_cost": 3.0,
            "paired_shares": 10.0,
            "paired_cost": 8.0,
            "unmatched_side": "UP",
            "unmatched_shares": 5.0,
            "unmatched_cost": 2.0,
        }
        result = evaluate_performance(
            [market], {"a": "Up"}, self.gates(), midpoint_ms=1_200_000
        )
        self.assertEqual(result["net_pnl"], 5.0)
        self.assertEqual(result["paired_net_pnl"], 2.0)
        self.assertEqual(result["residual_net_pnl"], 3.0)
        self.assertEqual(result["residual_wins"], 1)

    def test_decision_order_stops_at_behavior_before_performance(self):
        pre = {
            "safety_failures": [],
            "technical_failures": [],
            "frequency_failures": [],
            "behavior_failures": ["PAIRED_CAPITAL_FRACTION_MIN"],
        }
        status, failures = final_status(pre, ["NET_PNL"])
        self.assertEqual(status, "FAIL_BEHAVIOR_REPLICATION")
        self.assertEqual(failures, ["PAIRED_CAPITAL_FRACTION_MIN"])


if __name__ == "__main__":
    unittest.main()
