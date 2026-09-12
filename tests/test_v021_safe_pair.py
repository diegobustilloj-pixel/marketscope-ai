import json
import unittest

from polymarket_bot.v021_safe_pair import (
    SafePairOpportunityEngine,
    V021Error,
    default_strategy_config,
    execution_cost,
    validate_strategy_config,
)


START = 1_800_000_000_000


def book(token, timestamp, *, ask, depth=10.0, bid=None):
    return {
        "event_type": "book",
        "asset_id": token,
        "timestamp": str(timestamp),
        "bids": [{"price": str(bid if bid is not None else ask - 0.01), "size": "10"}],
        "asks": [{"price": str(ask), "size": str(depth)}],
    }


class V021SafePairTests(unittest.TestCase):
    def engine(self, config=None):
        return SafePairOpportunityEngine(
            condition_id="condition",
            market_start_ms=START,
            token_sides={"up-token": "UP", "down-token": "DOWN"},
            config=config or default_strategy_config(),
        )

    def test_contract_is_observer_only_and_rejects_single_leg_mode(self):
        config = default_strategy_config()
        self.assertFalse(config["paper_orders_enabled"])
        config["simultaneous_complete_set_only"] = False
        with self.assertRaisesRegex(V021Error, "una sola pata"):
            validate_strategy_config(config)

    def test_execution_cost_uses_depth_slippage_and_fee(self):
        cost = execution_cost(
            {0.40: 2.0, 0.41: 3.0},
            shares=5.0,
            slippage_per_share=0.005,
            fee_rate=0.07,
            minimum_price=0.001,
            maximum_price=0.999,
        )
        self.assertIsNotNone(cost)
        assert cost is not None
        self.assertAlmostEqual(cost["observed_vwap"], 0.406)
        self.assertGreater(cost["total_cost_per_share"], 0.411)
        self.assertIsNone(
            execution_cost(
                {0.40: 4.99}, shares=5.0, slippage_per_share=0.005,
                fee_rate=0.07, minimum_price=0.001, maximum_price=0.999,
            )
        )

    def test_complete_pair_opportunity_never_creates_unilateral_position(self):
        config = default_strategy_config()
        config["taker_slippage_per_share"] = 0.0
        config["taker_fee_rate"] = 0.0
        engine = self.engine(config)
        ts = START + 10_000
        engine.ingest(json.dumps(book("up-token", ts, ask=0.40)), received_timestamp_ms=ts)
        engine.ingest(json.dumps(book("down-token", ts, ask=0.58)), received_timestamp_ms=ts + 1)
        summary = engine.finish(timestamp_ms=ts + 100)
        samples = engine.drain_samples()
        self.assertEqual(summary["opportunity_episodes"], 1)
        self.assertGreater(summary["eligible_observations"], 0)
        self.assertEqual(summary["unilateral_positions"], 0)
        self.assertEqual(summary["paper_orders"], 0)
        self.assertEqual(samples[-1]["eligible"], 1)

    def test_price_change_updates_depth_and_can_close_opportunity(self):
        config = default_strategy_config()
        config["taker_slippage_per_share"] = 0.0
        config["taker_fee_rate"] = 0.0
        engine = self.engine(config)
        ts = START + 10_000
        engine.ingest(json.dumps(book("up-token", ts, ask=0.40)), received_timestamp_ms=ts)
        engine.ingest(json.dumps(book("down-token", ts, ask=0.58)), received_timestamp_ms=ts + 1)
        change = {
            "event_type": "price_change",
            "market": "condition",
            "timestamp": str(ts + 50),
            "price_changes": [
                {"asset_id": "down-token", "side": "SELL", "price": "0.58", "size": "0"},
                {"asset_id": "down-token", "side": "SELL", "price": "0.61", "size": "10"},
            ],
        }
        engine.ingest(json.dumps(change), received_timestamp_ms=ts + 50)
        summary = engine.finish(timestamp_ms=ts + 100)
        self.assertEqual(summary["opportunity_episodes"], 1)
        self.assertEqual(summary["maximum_opportunity_persistence_ms"], 50)

    def test_expensive_complete_set_is_observed_but_not_eligible(self):
        engine = self.engine()
        ts = START + 10_000
        engine.ingest(json.dumps(book("up-token", ts, ask=0.50)), received_timestamp_ms=ts)
        engine.ingest(json.dumps(book("down-token", ts, ask=0.50)), received_timestamp_ms=ts + 1)
        summary = engine.finish(timestamp_ms=ts + 10)
        self.assertEqual(summary["valid_observations"], 1)
        self.assertEqual(summary["eligible_observations"], 0)
        self.assertGreater(summary["minimum_complete_set_cost"], 1.0)


if __name__ == "__main__":
    unittest.main()
