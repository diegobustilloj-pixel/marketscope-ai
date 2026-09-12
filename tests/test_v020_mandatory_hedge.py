import unittest

from polymarket_bot.v020_mandatory_hedge import (
    MandatoryHedgePaperEngine,
    V020Error,
    default_strategy_config,
    taker_cost_per_share,
    validate_strategy_config,
)


START = 1_800_000_000_000


def row(
    second,
    *,
    up_bid=0.60,
    up_ask=0.62,
    down_bid=0.39,
    down_ask=0.41,
    bid_depth=0.0,
    up_ask_depth=10.0,
    down_ask_depth=10.0,
):
    return {
        "second_offset": second,
        "up_best_bid": up_bid,
        "up_best_ask": up_ask,
        "up_bid_depth_1c": bid_depth,
        "up_ask_depth_1c": up_ask_depth,
        "down_best_bid": down_bid,
        "down_best_ask": down_ask,
        "down_bid_depth_1c": bid_depth,
        "down_ask_depth_1c": down_ask_depth,
    }


class V020MandatoryHedgeTests(unittest.TestCase):
    def engine(self):
        return MandatoryHedgePaperEngine(
            condition_id="condition",
            market_start_ms=START,
            config=default_strategy_config(),
        )

    def test_configuration_requires_mandatory_hedge_and_realistic_taker_cost(self):
        config = default_strategy_config()
        config["mandatory_hedge"] = False
        with self.assertRaisesRegex(V020Error, "obligatoria"):
            validate_strategy_config(config)
        fill, fee, total = taker_cost_per_share(
            0.40, slippage_per_share=0.005, fee_rate=0.07
        )
        self.assertAlmostEqual(fill, 0.405)
        self.assertAlmostEqual(fee, 0.07 * 0.405 * 0.595)
        self.assertAlmostEqual(total, fill + fee)

    def test_opening_requires_observable_depth_for_both_emergency_directions(self):
        engine = self.engine()
        engine.on_second(row(10, down_ask_depth=4.99))
        self.assertEqual(engine.all_quotes, [])
        engine.on_second(row(20))
        self.assertEqual(len(engine.all_quotes), 2)

    def test_unilateral_fill_is_mandatorily_hedged_after_one_second(self):
        engine = self.engine()
        engine.on_second(row(10))
        engine.on_second(row(11, up_ask=0.59))
        self.assertEqual(engine.unmatched_shares("UP"), 5.0)
        engine.on_second(row(12, down_ask=0.42))
        summary = engine.finish()
        self.assertEqual(summary["unmatched_shares"], 0.0)
        self.assertEqual(summary["emergency_fills"], 1)
        self.assertEqual(summary["emergency_shares"], 5.0)
        self.assertTrue(summary["mandatory_hedge_completed"])
        self.assertGreater(summary["maximum_pair_set_cost"], 0.99)

    def test_partial_depth_is_taken_and_retried_without_new_risk(self):
        engine = self.engine()
        engine.on_second(row(10))
        engine.on_second(row(11, up_ask=0.59))
        engine.on_second(row(12, down_ask=0.42, down_ask_depth=2.0))
        self.assertEqual(engine.unmatched_shares("UP"), 3.0)
        engine.on_second(row(13, down_ask=0.43, down_ask_depth=3.0))
        summary = engine.finish()
        self.assertEqual(summary["unmatched_shares"], 0.0)
        self.assertEqual(summary["emergency_fills"], 2)
        self.assertEqual(summary["emergency_shares"], 5.0)

    def test_passive_second_leg_inside_grace_avoids_taker(self):
        engine = self.engine()
        engine.on_second(row(10))
        engine.on_second(row(11, up_ask=0.59))
        engine.on_second(row(12, down_ask=0.38))
        summary = engine.finish()
        self.assertEqual(summary["unmatched_shares"], 0.0)
        self.assertEqual(summary["emergency_fills"], 0)
        self.assertGreater(summary["passive_pair_matches"], 0)
        self.assertLessEqual(summary["maximum_pair_set_cost"], 0.99)

    def test_no_new_quotes_while_unmatched_and_cash_stays_capped(self):
        engine = self.engine()
        engine.on_second(row(10))
        engine.on_second(row(11, up_ask=0.59))
        quote_count = len(engine.all_quotes)
        engine.on_second(row(20, down_ask_depth=0.0))
        self.assertEqual(len(engine.all_quotes), quote_count)
        self.assertLessEqual(
            engine.cash_deployed, engine.config["maximum_cash_per_market"]
        )


if __name__ == "__main__":
    unittest.main()
