from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from polymarket_bot.config import Settings
from polymarket_bot.resolution_contract import resolution_twap_contract
from polymarket_bot.v031_audit import audit_v031
from polymarket_bot.v031_capture import V031Store
from polymarket_bot.v031_runner import (
    V031RunnerError,
    load_and_verify_implementation,
    run_v031,
    v031_status,
)


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "data" / "prereg_v031_path_execution_capture.json"
IMPLEMENTATION = ROOT / "data" / "implementation_v031_path_execution_capture.json"
CONTRACT = resolution_twap_contract(
    "https://data.chain.link/streams/btc-usd-twap-60s-streams"
)


def _market(index: int, start_ms: int) -> SimpleNamespace:
    return SimpleNamespace(
        condition_id=f"condition-{index}",
        slug=f"btc-updown-5m-{start_ms // 1000}",
        event_id=f"event-{index}",
        start_ms=start_ms,
        end_ms=start_ms + 300_000,
        up_token_id=f"up-{index}",
        down_token_id=f"down-{index}",
        resolution_source="https://data.chain.link/streams/btc-usd-twap-60s-streams",
    )


def _snapshot(condition_id: str, second: int, timestamp_ms: int) -> dict:
    levels_bid = [[0.49, 10.0], [0.48, 20.0]]
    levels_ask = [[0.50, 10.0], [0.51, 20.0]]
    return {
        "condition_id": condition_id,
        "second_offset": second,
        "snapshot_timestamp_ms": timestamp_ms,
        "recorded_timestamp_ms": timestamp_ms + 10,
        "chainlink_price": 100_000.0,
        "chainlink_source_timestamp_ms": timestamp_ms,
        "chainlink_received_timestamp_ms": timestamp_ms + 1,
        "chainlink_age_ms": 0,
        "chainlink_fresh": True,
        "official_twap_price": 100_000.0,
        "official_twap_window_s": 60,
        "official_twap_source_timestamp_ms": timestamp_ms,
        "official_twap_received_timestamp_ms": timestamp_ms + 2,
        "official_twap_age_ms": 0,
        "official_twap_fresh": True,
        "up_bid_levels": levels_bid,
        "up_ask_levels": levels_ask,
        "down_bid_levels": levels_bid,
        "down_ask_levels": levels_ask,
        "up_book_source_timestamp_ms": timestamp_ms,
        "up_book_received_timestamp_ms": timestamp_ms + 3,
        "up_book_age_ms": 0,
        "up_book_fresh": True,
        "down_book_source_timestamp_ms": timestamp_ms,
        "down_book_received_timestamp_ms": timestamp_ms + 4,
        "down_book_age_ms": 0,
        "down_book_fresh": True,
        "up_best_bid": 0.49,
        "up_best_ask": 0.50,
        "down_best_bid": 0.49,
        "down_best_ask": 0.50,
        "up_bid_depth_top5": 30.0,
        "up_ask_depth_top5": 30.0,
        "down_bid_depth_top5": 30.0,
        "down_ask_depth_top5": 30.0,
        "quality_flags": 0,
        "complete": True,
    }


def _build_terminal_database(path: Path, *, market_count: int, snapshots: int) -> None:
    start_ms = 1_800_000_300_000
    store = V031Store(path)
    store.open(
        preregistration_sha256="a" * 64,
        launch_manifest_sha256="b" * 64,
        now_timestamp=1_800_000_000,
    )
    for index in range(market_count):
        market_start = start_ms + index * 300_000
        market = _market(index, market_start)
        store.save_market(market, contract=CONTRACT)  # type: ignore[arg-type]
        for second in range(snapshots):
            store.save_snapshot(
                _snapshot(
                    market.condition_id,
                    second,
                    market_start + second * 1_000,
                )
            )
        store.finish_market(market.condition_id)
    store.set_meta("completion_reason", "FULL_1H_REACHED")
    store.set_meta("observation_ended_at", "2027-01-15T09:00:00+00:00")
    store.close()


class V031RunnerAuditTests(unittest.TestCase):
    def test_sealed_implementation_hashes_verify(self) -> None:
        payload = load_and_verify_implementation(IMPLEMENTATION, project_root=ROOT)
        self.assertEqual(
            payload["status"], "BUILT_TESTED_AWAITING_LAUNCH_APPROVAL"
        )
        self.assertFalse(payload["launch_approved"])
        self.assertFalse(payload["economic_strategy_built"])

    def test_status_before_launch_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = v031_status(Path(temporary) / "missing.db")
        self.assertEqual(result["status"], "NOT_STARTED")
        self.assertEqual(result["outcomes_read"], 0)
        self.assertEqual(result["orders_created"], 0)
        self.assertEqual(result["real_money"], "BLOQUEADO")

    def test_terminal_full_coverage_fixture_passes_technical_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "capture.db"
            _build_terminal_database(database, market_count=12, snapshots=300)
            with patch(
                "polymarket_bot.v031_audit.load_and_verify_implementation",
                return_value={},
            ):
                result = audit_v031(
                    database=database,
                    prereg_path=PREREG,
                    implementation_path=PREREG,
                )
        self.assertEqual(result["verdict"], "PASS_TECHNICAL_CAPTURE_ONLY")
        self.assertTrue(result["technical_passed"])
        self.assertTrue(result["safety_passed"])
        self.assertFalse(result["economic_edge_evaluated"])
        self.assertEqual(result["outcomes_read"], 0)
        self.assertEqual(result["paper_orders"], 0)

    def test_sparse_terminal_fixture_fails_technical_quality(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "capture.db"
            _build_terminal_database(database, market_count=1, snapshots=100)
            with patch(
                "polymarket_bot.v031_audit.load_and_verify_implementation",
                return_value={},
            ):
                result = audit_v031(
                    database=database,
                    prereg_path=PREREG,
                    implementation_path=PREREG,
                )
        self.assertEqual(result["verdict"], "FAIL_TECHNICAL_QUALITY")
        self.assertFalse(result["technical_gates"]["market_coverage_passed"])
        self.assertFalse(
            result["technical_gates"]["minimum_snapshots_per_market_passed"]
        )

    def test_missing_launch_approval_remains_a_hard_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "approval.json"
            from polymarket_bot.v031_runner import load_and_verify_launch_approval

            with self.assertRaisesRegex(V031RunnerError, "NOT_LAUNCHED"):
                load_and_verify_launch_approval(
                    missing,
                    prereg_path=PREREG,
                    implementation_path=PREREG,
                )

    def test_live_runner_cannot_create_database_without_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "capture.db"
            launch = root / "missing-approval.json"
            with self.assertRaisesRegex(V031RunnerError, "NOT_LAUNCHED"):
                asyncio.run(
                    run_v031(
                        settings=Settings.from_env(),
                        prereg_path=PREREG,
                        implementation_path=IMPLEMENTATION,
                        launch_path=launch,
                        output_db=database,
                    )
                )
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
