import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from polymarket_bot.v018_runner import (
    V018ProcessLock,
    V018Store,
    extract_trade_event,
    first_live_second,
    v018_status,
)


class V018RunnerTests(unittest.TestCase):
    def test_process_lock_rejects_duplicate_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v018.lock"
            first = V018ProcessLock(path)
            second = V018ProcessLock(path)
            first.acquire()
            try:
                with self.assertRaisesRegex(Exception, "otro monitor"):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()

    def test_first_live_second_never_replays_elapsed_seconds(self):
        self.assertEqual(first_live_second(1_000, 1_000.2), 0)
        self.assertEqual(first_live_second(1_000, 1_030.9), 30)
        self.assertEqual(first_live_second(1_000, 1_400.0), 299)
    def test_extracts_only_trade_for_known_token(self):
        market = SimpleNamespace(up_token_id="up-token", down_token_id="down-token")
        raw = json.dumps(
            {
                "event_type": "last_trade_price",
                "asset_id": "up-token",
                "timestamp": "1800000011000",
                "price": "0.48",
                "size": "7",
            }
        )

        trade = extract_trade_event(raw, market)

        self.assertEqual(trade["side"], "UP")
        self.assertEqual(trade["timestamp_ms"], 1_800_000_011_000)
        self.assertEqual(trade["trade_size"], 7.0)
        self.assertIsNone(extract_trade_event('{"event_type":"book"}', market))

    def test_store_is_resumable_and_marks_partial_market_excluded(self):
        prereg = {
            "target_hours": 24.0,
            "strategy": {"name": "frozen"},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prereg_path = root / "prereg.json"
            prereg_path.write_text(json.dumps(prereg), encoding="utf-8")
            database = root / "v018.db"
            market = SimpleNamespace(
                condition_id="condition",
                slug="btc-updown-5m-1800000000",
                start_ms=1_800_000_000_000,
                end_ms=1_800_000_300_000,
            )
            store = V018Store(database)
            store.open(prereg, prereg_path)
            store.start_market(market)
            store.mark_interrupted_markets()
            store.close()

            status = v018_status(database)

        self.assertEqual(status["markets_interrupted_excluded"], 1)
        self.assertEqual(status["outcomes_read"], 0)
        self.assertEqual(status["orders_sent"], 0)
        self.assertEqual(status["sqlite_quick_check"], "ok")


if __name__ == "__main__":
    unittest.main()
