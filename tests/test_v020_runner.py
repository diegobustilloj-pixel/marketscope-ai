import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from polymarket_bot.v020_mandatory_hedge import MandatoryHedgePaperEngine, default_strategy_config
from polymarket_bot.v020_runner import V020ProcessLock, V020Store, v020_status


def row(second, *, up_ask=0.62, down_ask=0.41, down_depth=10.0):
    return {
        "second_offset": second,
        "up_best_bid": 0.60,
        "up_best_ask": up_ask,
        "up_bid_depth_1c": 0.0,
        "up_ask_depth_1c": 10.0,
        "down_best_bid": 0.39,
        "down_best_ask": down_ask,
        "down_bid_depth_1c": 0.0,
        "down_ask_depth_1c": down_depth,
        "polymarket_trade_count": 0,
        "book_messages": 1,
        "price_change_messages": 0,
    }


class V020RunnerTests(unittest.TestCase):
    def test_process_lock_rejects_duplicate_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v020.lock"
            first = V020ProcessLock(path)
            second = V020ProcessLock(path)
            first.acquire()
            try:
                with self.assertRaisesRegex(Exception, "otro monitor V0.20"):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()

    def test_store_persists_mandatory_taker_pair_and_safety(self):
        prereg = {"target_hours": 24.0, "strategy": default_strategy_config()}
        market = SimpleNamespace(
            condition_id="condition", slug="btc-updown-5m-1800000000",
            start_ms=1_800_000_000_000, end_ms=1_800_000_300_000,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prereg_path = root / "prereg.json"
            prereg_path.write_text(json.dumps(prereg), encoding="utf-8")
            database = root / "v020.db"
            store = V020Store(database)
            store.open(prereg, prereg_path)
            store.start_market(market)
            engine = MandatoryHedgePaperEngine(
                condition_id=market.condition_id,
                market_start_ms=market.start_ms,
                config=prereg["strategy"],
            )
            for current in (row(10), row(11, up_ask=0.59), row(12, down_ask=0.42)):
                engine.on_second(current)
                store.save_second(market.condition_id, current)
                store.apply_events(market.condition_id, engine.drain_events())
            summary = engine.finish()
            store.apply_events(market.condition_id, engine.drain_events())
            store.complete_market(summary)
            store.close()

            status = v020_status(database)

        self.assertEqual(status["markets_complete"], 1)
        self.assertEqual(status["markets_paired"], 1)
        self.assertEqual(status["emergency_fills"], 1)
        self.assertEqual(status["final_unmatched_shares"], 0.0)
        self.assertEqual(status["mandatory_hedge_completion_rate"], 1.0)
        self.assertEqual(status["orders_sent"], 0)
        self.assertEqual(status["real_money_rows"], 0)
        self.assertEqual(status["outcomes_read"], 0)
        self.assertEqual(status["sqlite_quick_check"], "ok")

    def test_resume_excludes_partial_market(self):
        prereg = {"target_hours": 24.0, "strategy": default_strategy_config()}
        market = SimpleNamespace(
            condition_id="condition", slug="btc-updown-5m-1800000000",
            start_ms=1_800_000_000_000, end_ms=1_800_000_300_000,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prereg_path = root / "prereg.json"
            prereg_path.write_text(json.dumps(prereg), encoding="utf-8")
            database = root / "v020.db"
            store = V020Store(database)
            store.open(prereg, prereg_path)
            store.start_market(market)
            store.mark_interrupted_markets()
            store.close()
            status = v020_status(database)
        self.assertEqual(status["markets_interrupted_excluded"], 1)


if __name__ == "__main__":
    unittest.main()
