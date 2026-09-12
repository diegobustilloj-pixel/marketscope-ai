import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from polymarket_bot.v021_runner import V021ProcessLock, V021Store, v021_status
from polymarket_bot.v021_safe_pair import default_strategy_config


class V021RunnerTests(unittest.TestCase):
    def test_process_lock_rejects_duplicate_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v021.lock"
            first = V021ProcessLock(path)
            second = V021ProcessLock(path)
            first.acquire()
            try:
                with self.assertRaisesRegex(Exception, "otro monitor V0.21"):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()

    def test_store_persists_observations_without_orders_or_positions(self):
        prereg = {"target_hours": 24.0, "strategy": default_strategy_config()}
        market = SimpleNamespace(
            condition_id="condition", slug="btc-updown-5m-1800000000",
            start_ms=1_800_000_000_000, end_ms=1_800_000_300_000,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prereg_path = root / "prereg.json"
            prereg_path.write_text(json.dumps(prereg), encoding="utf-8")
            database = root / "v021.db"
            store = V021Store(database)
            store.open(prereg, prereg_path)
            store.start_market(market)
            store.save_samples(
                [{
                    "condition_id": "condition", "bucket_ms": 1, "source_timestamp_ms": 1,
                    "received_timestamp_ms": 1, "up_best_ask": 0.4, "down_best_ask": 0.58,
                    "up_ask_depth": 10.0, "down_ask_depth": 10.0, "up_total_cost": 0.4,
                    "down_total_cost": 0.58, "complete_set_cost": 0.98, "eligible": 1,
                    "message_count": 2,
                }]
            )
            store.finalize_market(
                {
                    "condition_id": "condition", "message_count": 2, "valid_observations": 1,
                    "eligible_observations": 1, "opportunity_episodes": 1,
                    "minimum_complete_set_cost": 0.98, "maximum_observed_depth_shares": 10.0,
                    "maximum_opportunity_persistence_ms": 100,
                },
                "COMPLETE",
            )
            store.close()
            status = v021_status(database)
        self.assertEqual(status["markets_complete"], 1)
        self.assertEqual(status["eligible_sample_buckets"], 1)
        self.assertEqual(status["unilateral_positions"], 0)
        self.assertEqual(status["paper_orders"], 0)
        self.assertEqual(status["orders_sent"], 0)
        self.assertEqual(status["outcomes_read"], 0)
        self.assertEqual(status["sqlite_quick_check"], "ok")


if __name__ == "__main__":
    unittest.main()
