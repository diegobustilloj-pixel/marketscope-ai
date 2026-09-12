import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from polymarket_bot.v022_runner import V022ProcessLock, V022Store, v022_status
from polymarket_bot.v022_synced_pair import default_strategy_config


class V022RunnerTests(unittest.TestCase):
    def test_process_lock_rejects_duplicate_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v022.lock"
            first = V022ProcessLock(path)
            second = V022ProcessLock(path)
            first.acquire()
            try:
                with self.assertRaisesRegex(Exception, "otro monitor V0.22"):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()

    def test_store_persists_sync_confirmation_and_safety_counters(self):
        prereg = {"target_hours": 12.0, "strategy": default_strategy_config()}
        market = SimpleNamespace(
            condition_id="condition",
            slug="btc-updown-5m-1800000000",
            start_ms=1_800_000_000_000,
            end_ms=1_800_000_300_000,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prereg_path = root / "prereg.json"
            prereg_path.write_text(json.dumps(prereg), encoding="utf-8")
            database = root / "v022.db"
            store = V022Store(database)
            store.open(prereg, prereg_path)
            store.start_market(market)
            store.save_samples(
                [
                    {
                        "condition_id": "condition",
                        "bucket_ms": 1,
                        "valid_observations": 3,
                        "synchronized_observations": 3,
                        "raw_eligible_observations": 3,
                        "confirmed_observations": 1,
                        "minimum_complete_set_cost": 0.98,
                        "minimum_synchronized_cost": 0.98,
                        "minimum_raw_eligible_cost": 0.98,
                        "minimum_confirmed_cost": 0.985,
                        "minimum_source_skew_ms": 10,
                        "minimum_receive_skew_ms": 8,
                        "maximum_candidate_age_ms": 500,
                    }
                ]
            )
            store.save_signals(
                [
                    {
                        "condition_id": "condition",
                        "event_type": "RAW_OPEN",
                        "received_timestamp_ms": 1,
                        "complete_set_cost": 0.98,
                        "candidate_age_ms": 0,
                        "up_source_timestamp_ms": 1,
                        "down_source_timestamp_ms": 1,
                        "up_received_timestamp_ms": 1,
                        "down_received_timestamp_ms": 1,
                        "source_skew_ms": 0,
                        "receive_skew_ms": 0,
                        "maximum_book_age_ms": 0,
                    },
                    {
                        "condition_id": "condition",
                        "event_type": "CONFIRMED",
                        "received_timestamp_ms": 501,
                        "complete_set_cost": 0.985,
                        "candidate_age_ms": 500,
                        "up_source_timestamp_ms": 500,
                        "down_source_timestamp_ms": 501,
                        "up_received_timestamp_ms": 500,
                        "down_received_timestamp_ms": 501,
                        "source_skew_ms": 1,
                        "receive_skew_ms": 1,
                        "maximum_book_age_ms": 1,
                    },
                ]
            )
            store.finalize_market(
                {
                    "condition_id": "condition",
                    "message_count": 4,
                    "valid_observations": 3,
                    "synchronized_observations": 3,
                    "unsynchronized_observations": 0,
                    "raw_eligible_observations": 3,
                    "raw_opportunity_episodes": 1,
                    "confirmed_observations": 1,
                    "confirmed_opportunity_episodes": 1,
                    "minimum_complete_set_cost": 0.98,
                    "minimum_synchronized_complete_set_cost": 0.98,
                    "minimum_raw_eligible_cost": 0.98,
                    "minimum_confirmed_cost": 0.985,
                    "maximum_observed_depth_shares": 10.0,
                    "maximum_candidate_persistence_ms": 500,
                    "stale_ask_updates_rejected": 2,
                    "nonmonotonic_receive_timestamps": 0,
                },
                "COMPLETE",
            )
            store.close()
            status = v022_status(database)
        self.assertEqual(status["markets_complete"], 1)
        self.assertEqual(status["raw_eligible_sample_buckets"], 1)
        self.assertEqual(status["confirmed_sample_buckets"], 1)
        self.assertEqual(status["confirmed_opportunity_episodes"], 1)
        self.assertEqual(status["stale_ask_updates_rejected"], 2)
        self.assertEqual(status["unilateral_positions"], 0)
        self.assertEqual(status["paper_orders"], 0)
        self.assertEqual(status["orders_sent"], 0)
        self.assertEqual(status["outcomes_read"], 0)
        self.assertEqual(status["sqlite_quick_check"], "ok")


if __name__ == "__main__":
    unittest.main()
