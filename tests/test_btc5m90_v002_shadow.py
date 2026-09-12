from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.btc_5m_90.contract import FeeSchedule, MarketContract
from polymarket_bot.btc_5m_90.v002_shadow import (
    ShadowMarketEngine,
    ShadowStore,
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


def book(token: str, timestamp_ms: int, ask: float, size: float = 10.0) -> str:
    return json.dumps(
        {
            "event_type": "book",
            "asset_id": token,
            "timestamp": str(timestamp_ms),
            "asks": [{"price": str(ask), "size": str(size)}],
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


class V002ShadowEngineTests(unittest.TestCase):
    def engine_at_fifty_fifty(self) -> ShadowMarketEngine:
        engine = ShadowMarketEngine(contract())
        engine.ingest(book("up-token", START + 1_000, 0.50), START + 1_010)
        engine.ingest(book("down-token", START + 1_000, 0.50), START + 1_020)
        return engine

    def test_approved_exact_depth_creates_level_a_fill_without_order(self) -> None:
        engine = self.engine_at_fifty_fifty()
        timestamp = START + 240_000
        engine.ingest(
            changes(timestamp, ("up-token", 0.50, 0.0), ("up-token", 0.90, 5.0)),
            timestamp + 20,
        )
        self.assertIsNotNone(engine.execution)
        assert engine.execution is not None
        self.assertEqual(engine.execution.decision.status, "SIGNAL")
        self.assertEqual(engine.execution.execution_status, "SIMULATED_FILL_LEVEL_A")
        self.assertAlmostEqual(engine.execution.fill_vwap or 0.0, 0.90)
        self.assertAlmostEqual(engine.execution.fill_shares or 0.0, 5.0)
        self.assertEqual(engine.summary()["paper_orders"], 0)
        self.assertEqual(engine.summary()["orders_sent"], 0)

    def test_unchanged_opposite_book_is_diagnostic_not_false_stale_gate(self) -> None:
        engine = self.engine_at_fifty_fifty()
        timestamp = START + 240_000
        engine.ingest(
            changes(timestamp, ("up-token", 0.50, 0.0), ("up-token", 0.91, 5.0)),
            timestamp + 20,
        )
        self.assertIsNotNone(engine.execution)
        snapshot = engine.snapshots[max(engine.snapshots)]
        self.assertGreater(snapshot["cross_side_skew_ms"], 2_000)
        self.assertEqual(snapshot["valid"], 1)

    def test_tick_observes_price_present_at_exact_four_minute_boundary(self) -> None:
        engine = self.engine_at_fifty_fifty()
        before = START + 239_000
        engine.ingest(
            changes(before, ("up-token", 0.50, 0.0), ("up-token", 0.90, 5.0)),
            before + 10,
        )
        self.assertIsNone(engine.execution)
        engine.tick(START + 240_000)
        self.assertIsNotNone(engine.execution)
        assert engine.execution is not None
        self.assertEqual(engine.execution.decision.seconds_elapsed, 240.0)

    def test_rejected_first_candidate_locks_market(self) -> None:
        engine = self.engine_at_fifty_fifty()
        timestamp = START + 240_000
        engine.ingest(
            changes(timestamp, ("up-token", 0.50, 0.0), ("up-token", 0.92, 5.0)),
            timestamp + 10,
        )
        assert engine.execution is not None
        self.assertEqual(engine.execution.decision.status, "REJECTED_EDGE")
        self.assertEqual(engine.execution.execution_status, "NOT_ATTEMPTED")
        engine.ingest(
            changes(timestamp + 1_000, ("up-token", 0.92, 0.0), ("up-token", 0.90, 5.0)),
            timestamp + 1_010,
        )
        assert engine.execution is not None
        self.assertAlmostEqual(engine.execution.decision.ask or 0.0, 0.92)

    def test_exact_price_without_five_shares_is_not_a_level_a_fill(self) -> None:
        engine = self.engine_at_fifty_fifty()
        timestamp = START + 240_000
        engine.ingest(
            changes(timestamp, ("up-token", 0.50, 0.0), ("up-token", 0.90, 4.99)),
            timestamp + 10,
        )
        assert engine.execution is not None
        self.assertEqual(
            engine.execution.execution_status, "FAILED_INSUFFICIENT_EXACT_DEPTH"
        )

    def test_stale_trigger_does_not_lock_then_fresh_trigger_can_decide(self) -> None:
        engine = self.engine_at_fifty_fifty()
        timestamp = START + 240_000
        engine.ingest(
            changes(timestamp - 10_000, ("up-token", 0.50, 0.0), ("up-token", 0.90, 5.0)),
            timestamp,
        )
        self.assertIsNone(engine.execution)
        engine.ingest(
            changes(timestamp + 100, ("up-token", 0.90, 6.0)), timestamp + 110
        )
        self.assertIsNotNone(engine.execution)


class V002ShadowContractTests(unittest.TestCase):
    def test_prereg_is_bound_to_base_protocol_and_disables_money(self) -> None:
        payload = build_prereg()
        self.assertTrue(payload["source_protocol_sha256"])
        self.assertEqual(payload["safety"]["real_money"], "BLOQUEADO")
        self.assertFalse(payload["safety"]["wallet_required"])
        self.assertFalse(payload["safety"]["orders_enabled"])
        self.assertFalse(payload["safety"]["paper_orders"])
        self.assertEqual(
            set(payload["code_sha256"]),
            {
                "src/polymarket_bot/btc_5m_90/v002.py",
                "src/polymarket_bot/btc_5m_90/v002_shadow.py",
                "btc5m90_v002_shadow.py",
            },
        )

    def test_store_honors_custom_prereg_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prereg_path = root / "prereg.json"
            prereg_path.write_text("{}\n", encoding="utf-8")
            store = ShadowStore(root / "shadow.db")
            store.open(
                {"maximum_hours": 9.0, "target_complete_markets": 96}, prereg_path
            )
            try:
                self.assertEqual(store.meta()["real_money"], "BLOQUEADO")
                self.assertEqual(store.meta()["paper_orders"], 0)
            finally:
                store.close()

    def test_missing_database_status_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = status(Path(temporary) / "missing.db")
        self.assertEqual(result["status"], "NOT_STARTED")
        self.assertFalse(result["wallet_required"])
        self.assertEqual(result["orders_sent"], 0)
        self.assertEqual(result["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
