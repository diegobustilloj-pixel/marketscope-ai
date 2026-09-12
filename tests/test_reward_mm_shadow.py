import unittest

from polymarket_bot.reward_mm_shadow import (
    competition_metrics,
    midpoint_from_book,
    order_score,
    proposed_quote,
    qmin_score,
)


class RewardMMShadowTests(unittest.TestCase):
    def test_official_quadratic_order_score(self) -> None:
        self.assertAlmostEqual(order_score(4.0, 1.0, 100.0), 56.25)
        self.assertEqual(order_score(4.0, 4.0, 100.0), 0.0)
        self.assertEqual(order_score(4.0, 4.1, 100.0), 0.0)

    def test_central_single_side_is_divided_by_three(self) -> None:
        self.assertAlmostEqual(qmin_score(90.0, 0.0, 0.50), 30.0)
        self.assertAlmostEqual(qmin_score(90.0, 60.0, 0.50), 60.0)

    def test_extreme_price_requires_two_sides(self) -> None:
        self.assertEqual(qmin_score(90.0, 0.0, 0.05), 0.0)
        self.assertEqual(qmin_score(90.0, 60.0, 0.95), 60.0)

    def test_competition_pairs_yes_and_no_books(self) -> None:
        yes = {"bids": [{"price": "0.49", "size": "100"}], "asks": []}
        no = {"bids": [{"price": "0.49", "size": "100"}], "asks": []}
        result = competition_metrics(yes, no, yes_midpoint=0.50, max_spread_cents=4.0)
        self.assertAlmostEqual(result["aggregate_q_one"], 56.25)
        self.assertAlmostEqual(result["aggregate_q_two"], 56.25)
        self.assertAlmostEqual(result["aggregate_qmin_proxy"], 56.25)

    def test_midpoint_excludes_levels_below_reward_minimum(self) -> None:
        midpoint, source = midpoint_from_book(
            {
                "bids": [{"price": "0.49", "size": "5"}, {"price": "0.47", "size": "20"}],
                "asks": [{"price": "0.51", "size": "5"}, {"price": "0.55", "size": "20"}],
            },
            fallback=0.50,
            minimum_size=20.0,
        )
        self.assertAlmostEqual(midpoint, 0.51)
        self.assertEqual(source, "CLOB_SIZE_FILTERED_LEVEL_PROXY")

    def test_two_prefunded_bids_use_real_capital(self) -> None:
        result = proposed_quote(
            yes_midpoint=0.50,
            desired_distance_cents=1.0,
            tick=0.01,
            size=100.0,
            max_spread_cents=4.0,
            yes_book={"asks": [{"price": "0.52", "size": "10"}]},
            no_book={"asks": [{"price": "0.52", "size": "10"}]},
            competitor_q_proxy=100.0,
            daily_reward=50.0,
            active_hours_remaining=12.0,
        )
        assert result is not None
        self.assertAlmostEqual(result["capital_required_usdc"], 98.0)
        self.assertAlmostEqual(result["gross_reward_remaining_proxy_usdc"], result["gross_reward_daily_proxy_usdc"] / 2)
        self.assertIsNone(result["net_expected_pnl"])


if __name__ == "__main__":
    unittest.main()
