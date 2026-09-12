import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from polymarket_bot.v019_fifo_pair import FifoPairPaperEngine, default_strategy_config
from polymarket_bot.v019_runner import V019ProcessLock, V019Store, v019_status


def book(second: int, *, up_bid: float = 0.55, down_bid: float = 0.40) -> dict:
    return {
        "second_offset": second,
        "up_best_bid": up_bid,
        "up_best_ask": up_bid + 0.01,
        "up_bid_depth_1c": 0.0,
        "down_best_bid": down_bid,
        "down_best_ask": down_bid + 0.01,
        "down_bid_depth_1c": 0.0,
        "polymarket_trade_count": 0,
        "book_messages": 1,
        "price_change_messages": 0,
    }


class V019RunnerTests(unittest.TestCase):
    def test_process_lock_rejects_duplicate_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v019.lock"
            first = V019ProcessLock(path)
            second = V019ProcessLock(path)
            first.acquire()
            try:
                with self.assertRaisesRegex(Exception, "otro monitor V0.19"):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()

    def test_store_persists_fifo_pairs_and_safety_counters(self):
        prereg = {"target_hours": 24.0, "strategy": default_strategy_config()}
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
            database = root / "v019.db"
            store = V019Store(database)
            store.open(prereg, prereg_path)
            store.start_market(market)
            engine = FifoPairPaperEngine(
                condition_id=market.condition_id,
                market_start_ms=market.start_ms,
                config=prereg["strategy"],
            )
            engine.on_second(book(10))
            engine.on_second(book(11, up_bid=0.54, down_bid=0.39))
            store.apply_events(market.condition_id, engine.drain_events())
            summary = engine.finish()
            store.apply_events(market.condition_id, engine.drain_events())
            store.complete_market(summary)
            store.close()

            status = v019_status(database)

        self.assertEqual(status["markets_complete"], 1)
        self.assertEqual(status["markets_paired"], 1)
        self.assertLessEqual(status["maximum_pair_set_cost"], 0.99)
        self.assertEqual(status["outcomes_read"], 0)
        self.assertEqual(status["orders_sent"], 0)
        self.assertEqual(status["real_money_rows"], 0)
        self.assertEqual(status["sqlite_quick_check"], "ok")

    def test_resume_marks_partial_market_excluded(self):
        prereg = {"target_hours": 24.0, "strategy": default_strategy_config()}
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
            database = root / "v019.db"
            store = V019Store(database)
            store.open(prereg, prereg_path)
            store.start_market(market)
            store.mark_interrupted_markets()
            store.close()

            status = v019_status(database)

        self.assertEqual(status["markets_interrupted_excluded"], 1)
        self.assertEqual(status["outcomes_read"], 0)
        self.assertEqual(status["orders_sent"], 0)


if __name__ == "__main__":
    unittest.main()
