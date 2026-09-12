from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.btc_5m_90.contract import FeeSchedule, MarketContract
from polymarket_bot.btc_5m_90.v002_shadow import load_and_verify_prereg as load_v002_prereg
from polymarket_bot.btc_5m_90.v003_shadow import (
    EXPECTED_DECISION_BUCKETS,
    MINIMUM_VALID_DECISION_BUCKETS,
    QualityMarketEngine,
    V003Store,
    build_prereg,
    status,
)


START = 1_800_000_000_000


def contract() -> MarketContract:
    return MarketContract(
        market_id="market",
        condition_id="condition",
        slug="btc-updown-5m-1800000000",
        market_start_ms=START,
        market_end_ms=START + 300_000,
        closed_at=None,
        resolution_source="https://data.chain.link/streams/btc-usd-twap-60s-streams",
        up_token_id="up-token",
        down_token_id="down-token",
        winner=None,
        tick_size=0.01,
        minimum_order_shares=5.0,
        fee=FeeSchedule(True, 0.25, 2.0, True, 0.0),
        payload={},
    )


def book(token: str, timestamp_ms: int, asks: list[tuple[float, float]]) -> str:
    return json.dumps(
        {
            "event_type": "book",
            "asset_id": token,
            "timestamp": str(timestamp_ms),
            "asks": [{"price": str(price), "size": str(size)} for price, size in asks],
            "bids": [],
        }
    )


def changes(timestamp_ms: int, *rows: tuple[str, float, float]) -> str:
    return json.dumps(
        {
            "event_type": "price_change",
            "timestamp": str(timestamp_ms),
            "price_changes": [
                {
                    "asset_id": token,
                    "side": "SELL",
                    "price": str(price),
                    "size": str(size),
                }
                for token, price, size in rows
            ],
        }
    )


def initialize(engine: QualityMarketEngine, *, up=None, down=None) -> None:
    timestamp = START + 234_000
    engine.ingest(book("up-token", timestamp, [(0.50, 10.0)] if up is None else up), timestamp + 10)
    engine.ingest(book("down-token", timestamp, [(0.50, 10.0)] if down is None else down), timestamp + 20)


def fill_decision_window(engine: QualityMarketEngine, *, connected: bool = True) -> None:
    for index in range(EXPECTED_DECISION_BUCKETS):
        engine.tick(START + 240_000 + index * 250, connected=connected)


class V003QualityEngineTests(unittest.TestCase):
    def test_empty_book_is_explicit_and_can_still_be_quality_complete(self) -> None:
        engine = QualityMarketEngine(contract())
        initialize(engine, up=[], down=[(0.10, 10.0)])
        fill_decision_window(engine)
        quality = engine.quality(START + 1_000, True)
        self.assertEqual(quality["status"], "COMPLETE_QUALITY")
        self.assertEqual(quality["valid_decision_buckets"], EXPECTED_DECISION_BUCKETS)
        self.assertEqual(quality["empty_up_buckets"], EXPECTED_DECISION_BUCKETS)
        sample = engine.samples[START + 240_000]
        self.assertIsNone(sample["up_best_ask"])
        self.assertEqual(sample["up_book_empty"], 1)
        self.assertEqual(sample["valid"], 1)

    def test_missing_second_book_is_data_incomplete_not_no_candidate(self) -> None:
        engine = QualityMarketEngine(contract())
        timestamp = START + 234_000
        engine.ingest(book("up-token", timestamp, [(0.50, 10.0)]), timestamp + 10)
        fill_decision_window(engine)
        quality = engine.quality(START + 1_000, True)
        self.assertEqual(quality["status"], "DATA_INCOMPLETE")
        self.assertIn("down_book_never_initialized", quality["quality_reason"])
        self.assertEqual(quality["valid_decision_buckets"], 0)

    def test_coverage_and_maximum_gap_are_enforced(self) -> None:
        engine = QualityMarketEngine(contract())
        initialize(engine)
        for index in range(MINIMUM_VALID_DECISION_BUCKETS):
            engine.tick(START + 240_000 + index * 250, connected=True)
        quality = engine.quality(START + 1_000, True)
        self.assertEqual(quality["valid_decision_buckets"], MINIMUM_VALID_DECISION_BUCKETS)
        self.assertEqual(quality["status"], "DATA_INCOMPLETE")
        self.assertIn("invalid_gap_above_1000ms", quality["quality_reason"])

    def test_disconnect_invalidates_books_until_new_snapshots(self) -> None:
        engine = QualityMarketEngine(contract())
        initialize(engine)
        engine.invalidate_books()
        engine.tick(START + 240_000, connected=True)
        sample = engine.samples[START + 240_000]
        self.assertEqual(sample["connected"], 1)
        self.assertEqual(sample["up_initialized"], 0)
        self.assertEqual(sample["down_initialized"], 0)
        self.assertEqual(sample["valid"], 0)
        self.assertEqual(engine.disconnect_count, 1)

    def test_stale_event_cannot_mutate_book_or_create_signal(self) -> None:
        engine = QualityMarketEngine(contract())
        initialize(engine)
        received = START + 240_000
        engine.ingest(
            changes(
                received - 10_000,
                ("up-token", 0.50, 0.0),
                ("up-token", 0.90, 5.0),
            ),
            received,
        )
        self.assertEqual(engine.stale_event_count, 1)
        self.assertIsNone(engine.execution)
        self.assertAlmostEqual(min(engine.books["Up"].asks), 0.50)

    def test_fresh_90_signal_requires_exact_five_share_depth(self) -> None:
        engine = QualityMarketEngine(contract())
        initialize(engine)
        received = START + 240_000
        engine.ingest(
            changes(received, ("up-token", 0.50, 0.0), ("up-token", 0.90, 5.0)),
            received + 20,
        )
        self.assertIsNotNone(engine.execution)
        assert engine.execution is not None
        self.assertEqual(engine.execution.decision.status, "SIGNAL")
        self.assertEqual(engine.execution.execution_status, "SIMULATED_FILL_LEVEL_A")
        self.assertAlmostEqual(engine.execution.fill_vwap or 0.0, 0.90)


class V003ContractTests(unittest.TestCase):
    def test_strategy_is_identical_but_quality_contract_is_explicit(self) -> None:
        payload = build_prereg()
        self.assertEqual(payload["strategy"], load_v002_prereg()["strategy"])
        quality = payload["quality_contract"]
        self.assertEqual(quality["minimum_decision_coverage"], 0.95)
        self.assertEqual(quality["maximum_invalid_gap_ms"], 1_000)
        self.assertTrue(quality["empty_books_are_explicit_valid_states"])
        self.assertEqual(quality["missing_evidence_status"], "DATA_INCOMPLETE")

    def test_duration_and_safety_are_frozen(self) -> None:
        payload = build_prereg()
        self.assertEqual(payload["target_quality_markets"], 276)
        self.assertEqual(payload["expected_capture_hours"], 23.0)
        self.assertEqual(payload["maximum_hours"], 24.0)
        self.assertEqual(payload["recovery_reserve_hours"], 1.0)
        self.assertEqual(payload["gates"]["minimum_signals"], 70)
        self.assertFalse(payload["safety"]["wallet_required"])
        self.assertFalse(payload["safety"]["orders_enabled"])
        self.assertFalse(payload["safety"]["outcomes_during_capture"])
        self.assertEqual(payload["safety"]["real_money"], "BLOQUEADO")

    def test_missing_database_status_is_unambiguously_not_started(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = status(Path(temporary) / "missing.db")
        self.assertEqual(result["status"], "NOT_STARTED")
        self.assertEqual(result["quality_markets"], 0)
        self.assertEqual(result["orders_sent"], 0)
        self.assertEqual(result["real_money"], "BLOQUEADO")

    def test_store_round_trip_preserves_quality_and_safety(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prereg_path = root / "prereg.json"
            prereg_path.write_text("{}\n", encoding="utf-8")
            database = root / "shadow.db"
            store = V003Store(database)
            store.open(
                {"maximum_hours": 24.0, "target_quality_markets": 276},
                prereg_path,
            )
            run_id = store.start_run()
            engine = QualityMarketEngine(contract())
            store.start_market(engine.contract, START + 1_000)
            initialize(engine, up=[], down=[(0.10, 10.0)])
            fill_decision_window(engine)
            store.finish_market(engine, START + 1_000, True)
            store.finish_run(run_id, "TEST_COMPLETE", None)
            store.close()

            result = status(database)
            self.assertEqual(result["quality_markets"], 1)
            self.assertEqual(result["data_incomplete"], 0)
            self.assertEqual(result["average_decision_coverage"], 1.0)
            self.assertEqual(result["minimum_decision_coverage"], 1.0)
            self.assertEqual(result["maximum_invalid_gap_ms"], 0)
            self.assertEqual(result["paper_orders"], 0)
            self.assertEqual(result["orders_sent"], 0)
            self.assertEqual(result["outcomes_read"], 0)
            self.assertEqual(result["sqlite_quick_check"], "ok")


if __name__ == "__main__":
    unittest.main()
