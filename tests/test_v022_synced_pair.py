import json
import unittest

from polymarket_bot.v022_synced_pair import (
    SyncedPairOpportunityEngine,
    V022Error,
    default_strategy_config,
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


def change(token, timestamp, *, side, price, size=10.0):
    return {
        "event_type": "price_change",
        "market": "condition",
        "timestamp": str(timestamp),
        "price_changes": [
            {
                "asset_id": token,
                "side": side,
                "price": str(price),
                "size": str(size),
            }
        ],
    }


class V022SyncedPairTests(unittest.TestCase):
    def config(self):
        config = default_strategy_config()
        config["taker_slippage_per_share"] = 0.0
        config["taker_fee_rate"] = 0.0
        return config

    def engine(self, config=None):
        return SyncedPairOpportunityEngine(
            condition_id="condition",
            market_start_ms=START,
            token_sides={"up-token": "UP", "down-token": "DOWN"},
            config=config or self.config(),
        )

    def seed_pair(self, engine, timestamp, *, up=0.40, down=0.58):
        engine.ingest(
            json.dumps(book("up-token", timestamp, ask=up)),
            received_timestamp_ms=timestamp,
        )
        engine.ingest(
            json.dumps(book("down-token", timestamp, ask=down)),
            received_timestamp_ms=timestamp + 1,
        )

    def test_contract_requires_freshness_confirmation_and_no_orders(self):
        config = default_strategy_config()
        self.assertEqual(config["minimum_confirmation_ms"], 500)
        self.assertFalse(config["paper_orders_enabled"])
        config["reject_out_of_order_ask_updates"] = False
        with self.assertRaisesRegex(V022Error, "atrasados"):
            validate_strategy_config(config)

    def test_cross_side_skew_rejects_asynchronous_low_cost(self):
        engine = self.engine()
        ts = START + 10_000
        engine.ingest(
            json.dumps(book("up-token", ts, ask=0.40)),
            received_timestamp_ms=ts,
        )
        engine.ingest(
            json.dumps(book("down-token", ts + 300, ask=0.58)),
            received_timestamp_ms=ts + 300,
        )
        summary = engine.finish(received_timestamp_ms=ts + 301)
        self.assertEqual(summary["valid_observations"], 1)
        self.assertEqual(summary["synchronized_observations"], 0)
        self.assertEqual(summary["raw_opportunity_episodes"], 0)

    def test_confirmation_requires_500ms_and_refresh_of_both_asks(self):
        engine = self.engine()
        ts = START + 10_000
        self.seed_pair(engine, ts)
        engine.ingest(
            json.dumps(change("up-token", ts + 250, side="SELL", price=0.40)),
            received_timestamp_ms=ts + 251,
        )
        engine.ingest(
            json.dumps(change("down-token", ts + 500, side="SELL", price=0.58)),
            received_timestamp_ms=ts + 501,
        )
        summary = engine.finish(received_timestamp_ms=ts + 502)
        signals = engine.drain_signals()
        self.assertEqual(summary["raw_opportunity_episodes"], 1)
        self.assertEqual(summary["confirmed_opportunity_episodes"], 1)
        self.assertGreaterEqual(summary["maximum_candidate_persistence_ms"], 500)
        self.assertEqual([item["event_type"] for item in signals], ["RAW_OPEN", "CONFIRMED"])
        self.assertEqual(summary["unilateral_positions"], 0)
        self.assertEqual(summary["orders_sent"], 0)

    def test_bid_updates_do_not_refresh_or_confirm_asks(self):
        engine = self.engine()
        ts = START + 10_000
        self.seed_pair(engine, ts)
        engine.ingest(
            json.dumps(change("up-token", ts + 250, side="BUY", price=0.39)),
            received_timestamp_ms=ts + 250,
        )
        engine.ingest(
            json.dumps(change("down-token", ts + 500, side="BUY", price=0.57)),
            received_timestamp_ms=ts + 500,
        )
        summary = engine.finish(received_timestamp_ms=ts + 501)
        self.assertEqual(summary["confirmed_opportunity_episodes"], 0)
        self.assertEqual(summary["maximum_candidate_persistence_ms"], 0)

    def test_out_of_order_ask_update_is_rejected(self):
        engine = self.engine()
        ts = START + 10_000
        self.seed_pair(engine, ts, up=0.40, down=0.61)
        engine.ingest(
            json.dumps(change("down-token", ts + 100, side="SELL", price=0.65)),
            received_timestamp_ms=ts + 100,
        )
        engine.ingest(
            json.dumps(change("down-token", ts + 50, side="SELL", price=0.58)),
            received_timestamp_ms=ts + 150,
        )
        summary = engine.finish(received_timestamp_ms=ts + 151)
        self.assertEqual(summary["stale_ask_updates_rejected"], 1)
        self.assertEqual(summary["raw_opportunity_episodes"], 0)

    def test_silence_expires_candidate_without_inventing_persistence(self):
        engine = self.engine()
        ts = START + 10_000
        self.seed_pair(engine, ts)
        engine.tick(received_timestamp_ms=ts + 502)
        summary = engine.finish(received_timestamp_ms=ts + 2_000)
        self.assertEqual(summary["raw_opportunity_episodes"], 1)
        self.assertEqual(summary["confirmed_opportunity_episodes"], 0)
        self.assertEqual(summary["maximum_candidate_persistence_ms"], 0)

    def test_bucket_aggregates_counts_instead_of_promoting_isolated_minimum(self):
        engine = self.engine()
        ts = START + 10_000
        self.seed_pair(engine, ts)
        engine.ingest(
            json.dumps(change("down-token", ts + 50, side="SELL", price=0.58, size=0)),
            received_timestamp_ms=ts + 50,
        )
        engine.ingest(
            json.dumps(change("down-token", ts + 51, side="SELL", price=0.62)),
            received_timestamp_ms=ts + 51,
        )
        summary = engine.finish(received_timestamp_ms=ts + 52)
        samples = engine.drain_samples()
        self.assertEqual(summary["raw_eligible_observations"], 1)
        self.assertEqual(summary["confirmed_observations"], 0)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["raw_eligible_observations"], 1)
        self.assertEqual(samples[0]["confirmed_observations"], 0)


if __name__ == "__main__":
    unittest.main()
