from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.config import Settings
from polymarket_bot.v031_capture import PathBook, V031CaptureState
from polymarket_bot.v033_capture import V033Store, technical_snapshot
from polymarket_bot.v033_runner import (
    V033RunnerError,
    load_and_verify_implementation,
    run_v033,
    v033_status,
)


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "data" / "prereg_v033_fresh_exit_safety_4h.json"
IMPLEMENTATION = ROOT / "data" / "implementation_v033_fresh_exit_safety_4h.json"


class V033RunnerAuditTests(unittest.TestCase):
    def test_sealed_implementation_hashes_verify(self) -> None:
        payload = load_and_verify_implementation(IMPLEMENTATION, project_root=ROOT)
        self.assertEqual(
            payload["status"],
            "BUILT_TESTED_AWAITING_ONE_FRESH_TECHNICAL_LAUNCH_APPROVAL",
        )
        self.assertFalse(payload["economic_strategy_built"])
        self.assertFalse(payload["scheduled_supervision_built"])

    def test_live_runner_cannot_create_database_without_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "blocked.db"
            with self.assertRaises(V033RunnerError):
                asyncio.run(
                    run_v033(
                        settings=Settings.from_env(),
                        prereg_path=PREREG,
                        implementation_path=IMPLEMENTATION,
                        launch_path=Path(temporary) / "missing_approval.json",
                        output_db=database,
                    )
                )
            self.assertFalse(database.exists())

    def test_technical_snapshot_keeps_observed_empty_side_as_complete_v2(self) -> None:
        state = V031CaptureState()
        snapshot_ms = 1_800_000_000_000
        state.latest_chainlink = (100_000.0, snapshot_ms, snapshot_ms)
        state.latest_twap_by_window[60] = (100_000.0, snapshot_ms, snapshot_ms, 60, None)
        state.books["Up"] = PathBook(
            bids={},
            asks={0.55: 12.0},
            initialized=True,
            source_timestamp_ms=snapshot_ms,
            received_timestamp_ms=snapshot_ms,
        )
        state.books["Down"] = PathBook(
            bids={0.44: 12.0},
            asks={0.45: 12.0},
            initialized=True,
            source_timestamp_ms=snapshot_ms,
            received_timestamp_ms=snapshot_ms,
        )
        snapshot = technical_snapshot(
            state,
            condition_id="condition",
            second_offset=0,
            snapshot_timestamp_ms=snapshot_ms,
            official_twap_window_s=60,
            recorded_timestamp_ms=snapshot_ms,
        )
        self.assertTrue(snapshot["complete_v2"])
        self.assertEqual(snapshot["up_bid_depth_top5"], 0.0)
        self.assertNotIn("chainlink_price", snapshot)
        self.assertNotIn("up_best_bid", snapshot)

    def test_store_schema_has_no_prices_outcomes_pnl_or_orders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "v033.db"
            store = V033Store(database)
            store.open(
                preregistration_sha256="p",
                implementation_sha256="i",
                launch_manifest_sha256="l",
                now_timestamp=1_800_000_000.0,
            )
            meta = store.meta()
            self.assertFalse(meta["prices_stored"])
            self.assertEqual(meta["outcomes_read"], 0)
            self.assertFalse(meta["pnl_calculated"])
            store.close()
            connection = sqlite3.connect(database)
            try:
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(v033_snapshots)")
                }
            finally:
                connection.close()
            forbidden = {"price", "outcome", "pnl", "fee", "slippage", "order"}
            self.assertFalse(any(any(word in column for word in forbidden) for column in columns))

    def test_status_before_launch_keeps_all_safety_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status = v033_status(Path(temporary) / "not_started.db")
        self.assertEqual(status["status"], "NOT_STARTED")
        self.assertEqual(status["outcomes_read"], 0)
        self.assertFalse(status["prices_stored"])
        self.assertFalse(status["pnl_calculated"])
        self.assertEqual(status["paper_orders"], 0)
        self.assertFalse(status["wallet_required"])
        self.assertEqual(status["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
