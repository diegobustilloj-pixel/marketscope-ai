import unittest

from polymarket_bot.v018_multifill import (
    MultiFillPaperEngine,
    V018Error,
    default_strategy_config,
    validate_strategy_config,
)


START = 1_800_000_000_000


def row(second, *, up_bid=0.48, up_ask=0.50, down_bid=0.48, down_ask=0.50, depth=10):
    return {
        "second_offset": second,
        "up_best_bid": up_bid,
        "up_best_ask": up_ask,
        "up_bid_depth_1c": depth,
        "down_best_bid": down_bid,
        "down_best_ask": down_ask,
        "down_bid_depth_1c": depth,
    }


class V018MultiFillTests(unittest.TestCase):
    def test_one_sided_fills_cannot_exceed_residual_cap(self):
        engine = self.engine()
        engine.on_second(row(10))
        quote = engine.quotes["UP"]
        engine.on_trade(
            side="UP",
            timestamp_ms=quote.active_at_ms,
            trade_price=quote.price,
            trade_size=quote.queue_ahead_remaining + 50.0,
        )
        engine.on_second(row(20))
        quote = engine.quotes["UP"]
        engine.on_trade(
            side="UP",
            timestamp_ms=quote.active_at_ms,
            trade_price=quote.price,
            trade_size=quote.queue_ahead_remaining + 50.0,
        )

        self.assertEqual(engine.inventory["UP"]["shares"], 5.0)
        self.assertEqual(engine.inventory["DOWN"]["shares"], 0.0)

    def test_last_quote_at_second_240_remains_live_until_expiry(self):
        engine = self.engine()
        engine.on_second(row(240))

        self.assertIsNotNone(engine.quotes["UP"])
        self.assertEqual(engine.quotes["UP"].status, "RESTING")

        engine.on_second(row(250))
        self.assertIsNone(engine.quotes["UP"])

    def engine(self):
        return MultiFillPaperEngine(
            condition_id="condition",
            market_start_ms=START,
            config=default_strategy_config(),
        )

    def test_configuration_requires_latency_and_no_rebate(self):
        config = default_strategy_config()
        config["activation_latency_ms"] = 999
        with self.assertRaisesRegex(V018Error, "latencia"):
            validate_strategy_config(config)

        config = default_strategy_config()
        config["maker_rebate_per_share"] = 0.001
        with self.assertRaisesRegex(V018Error, "rebates"):
            validate_strategy_config(config)

    def test_places_two_quotes_but_cannot_fill_before_latency(self):
        engine = self.engine()
        engine.on_second(row(10))

        engine.on_trade(
            side="UP",
            timestamp_ms=START + 10_500,
            trade_price=0.48,
            trade_size=100,
        )

        self.assertEqual(len(engine.all_quotes), 2)
        self.assertEqual(len(engine.fills), 0)
        self.assertEqual(engine.favorite_side, "UP")

    def test_trade_must_clear_queue_before_partial_fill(self):
        engine = self.engine()
        engine.on_second(row(10, depth=10))

        engine.on_trade(
            side="UP",
            timestamp_ms=START + 11_000,
            trade_price=0.48,
            trade_size=6,
        )

        self.assertEqual(len(engine.fills), 1)
        self.assertAlmostEqual(engine.fills[0]["fill_size"], 1.0)
        self.assertEqual(engine.fills[0]["trigger"], "TRADE_THROUGH_QUEUE")
        self.assertAlmostEqual(engine.inventory["UP"]["shares"], 1.0)

    def test_reprice_cancels_old_quotes(self):
        engine = self.engine()
        engine.on_second(row(10))
        first_ids = {quote.order_id for quote in engine.all_quotes}
        engine.on_second(row(20, up_bid=0.47, down_bid=0.49))

        self.assertTrue(all(quote.status == "CANCELLED_EXPIRED" for quote in engine.all_quotes[:2]))
        self.assertEqual(len(engine.all_quotes), 4)
        self.assertTrue(first_ids.isdisjoint({quote.order_id for quote in engine.all_quotes[2:]}))

    def test_ask_cross_fills_and_summary_stays_inside_caps(self):
        engine = self.engine()
        for second in (10, 20, 30, 40):
            engine.on_second(row(second, depth=0))
            engine.on_second(
                row(
                    second + 1,
                    up_bid=0.47,
                    up_ask=0.48,
                    down_bid=0.47,
                    down_ask=0.48,
                    depth=0,
                )
            )
        summary = engine.finish()

        self.assertEqual(summary["up_shares"], 20.0)
        self.assertEqual(summary["down_shares"], 20.0)
        self.assertLessEqual(summary["cash_deployed"], 25.0)
        self.assertLessEqual(summary["residual_shares"], 5.0)
        self.assertEqual(summary["orders_sent"], 0)
        self.assertEqual(summary["real_money"], 0)

    def test_invalid_book_cancels_quotes_without_fill(self):
        engine = self.engine()
        engine.on_second(row(10))
        engine.on_second(row(20, up_bid=0.55, up_ask=0.50))
        summary = engine.finish()

        self.assertEqual(summary["fills"], 0)
        self.assertEqual(len(engine.all_quotes), 2)
        self.assertTrue(
            all(quote.status == "CANCELLED_EXPIRED" for quote in engine.all_quotes)
        )


if __name__ == "__main__":
    unittest.main()
