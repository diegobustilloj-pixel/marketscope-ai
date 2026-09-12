import unittest

from polymarket_bot.v019_fifo_pair import (
    FifoPairPaperEngine,
    V019Error,
    default_strategy_config,
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
    depth=0,
):
    return {
        "second_offset": second,
        "up_best_bid": up_bid,
        "up_best_ask": up_ask,
        "up_bid_depth_1c": depth,
        "down_best_bid": down_bid,
        "down_best_ask": down_ask,
        "down_bid_depth_1c": depth,
    }


class V019FifoPairTests(unittest.TestCase):
    def engine(self):
        return FifoPairPaperEngine(
            condition_id="condition",
            market_start_ms=START,
            config=default_strategy_config(),
        )

    def test_configuration_forbids_directional_residual_and_rebate(self):
        config = default_strategy_config()
        config["directional_residual_target_shares"] = 1
        with self.assertRaisesRegex(V019Error, "residual"):
            validate_strategy_config(config)
        config = default_strategy_config()
        config["maker_rebate_per_share"] = 0.001
        with self.assertRaisesRegex(V019Error, "rebates"):
            validate_strategy_config(config)
        config = default_strategy_config()
        config["maker_fee_per_share"] = 0.001
        with self.assertRaisesRegex(V019Error, "maker fee"):
            validate_strategy_config(config)

    def test_opening_quotes_require_set_at_or_below_099(self):
        engine = self.engine()
        engine.on_second(row(10, up_bid=0.61, down_bid=0.39))
        self.assertEqual(len(engine.all_quotes), 0)
        engine.on_second(row(20, up_bid=0.60, down_bid=0.39))
        self.assertEqual(len(engine.all_quotes), 2)
        self.assertTrue(all(q.purpose == "OPEN_PAIR" for q in engine.all_quotes))

    def test_fill_waits_for_latency_and_clears_queue(self):
        engine = self.engine()
        engine.on_second(row(10, depth=10))
        engine.on_trade(
            side="UP", timestamp_ms=START + 10_500, trade_price=0.60, trade_size=100
        )
        self.assertEqual(len(engine.fills), 0)
        engine.on_trade(
            side="UP", timestamp_ms=START + 11_000, trade_price=0.60, trade_size=6
        )
        self.assertEqual(engine.inventory["UP"]["shares"], 1.0)
        self.assertEqual(engine.unmatched_shares("UP"), 1.0)

    def test_fifo_hedge_uses_actual_unmatched_lot_ceiling(self):
        engine = self.engine()
        engine.on_second(row(10))
        engine.on_second(
            row(11, up_bid=0.59, up_ask=0.60, down_bid=0.39, down_ask=0.41)
        )
        self.assertEqual(engine.unmatched_shares("UP"), 5.0)
        self.assertIsNone(engine.quotes["UP"])
        engine.on_second(row(20, down_bid=0.38))
        hedge = engine.quotes["DOWN"]
        self.assertIsNotNone(hedge)
        self.assertEqual(hedge.purpose, "HEDGE_FIFO")
        self.assertAlmostEqual(hedge.price_ceiling, 0.39)
        engine.on_second(
            row(21, down_bid=0.37, down_ask=0.38, up_bid=0.60, up_ask=0.62)
        )
        summary = engine.finish()
        self.assertEqual(summary["paired_shares"], 5.0)
        self.assertAlmostEqual(summary["weighted_complete_set_cost"], 0.98)
        self.assertEqual(summary["unmatched_shares"], 0.0)

    def test_expensive_hedge_is_not_quoted(self):
        engine = self.engine()
        engine.on_second(row(10))
        engine.on_second(
            row(11, up_bid=0.59, up_ask=0.60, down_bid=0.39, down_ask=0.41)
        )
        engine.on_second(row(20, down_bid=0.40, down_ask=0.42))
        self.assertIsNone(engine.quotes["DOWN"])
        self.assertEqual(engine.unmatched_shares(), 5.0)

    def test_one_sided_inventory_never_exceeds_five(self):
        engine = self.engine()
        engine.on_second(row(10))
        engine.on_second(
            row(11, up_bid=0.59, up_ask=0.60, down_bid=0.39, down_ask=0.41)
        )
        engine.on_second(row(20))
        self.assertIsNone(engine.quotes["UP"])
        engine.on_trade(
            side="UP", timestamp_ms=START + 21_000, trade_price=0.60, trade_size=100
        )
        self.assertEqual(engine.inventory["UP"]["shares"], 5.0)
        self.assertLessEqual(engine.unmatched_shares(), 5.0)

    def test_every_recorded_pair_is_below_frozen_limit(self):
        engine = self.engine()
        engine.on_second(row(10, up_bid=0.55, down_bid=0.40))
        engine.on_second(
            row(11, up_bid=0.54, up_ask=0.55, down_bid=0.39, down_ask=0.40)
        )
        summary = engine.finish()
        self.assertEqual(summary["paired_shares"], 5.0)
        self.assertLessEqual(summary["maximum_pair_set_cost"], 0.99)
        self.assertEqual(summary["directional_residual_target_shares"], 0.0)
        self.assertEqual(summary["orders_sent"], 0)


if __name__ == "__main__":
    unittest.main()
