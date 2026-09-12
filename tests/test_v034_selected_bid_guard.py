from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.config import Settings
from polymarket_bot.v034_capture import V034Store
from polymarket_bot.v034_contract import evaluate_probe, load_and_verify_prereg
from polymarket_bot.v034_guard_design import evaluate_relative_guard_probe
from polymarket_bot.v034_runner import (
    V034RunnerError,
    load_and_verify_implementation,
    run_v034,
    v034_status,
)


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "data" / "prereg_v034_selected_bid_guard_4h.json"
IMPLEMENTATION = ROOT / "data" / "implementation_v034_selected_bid_guard_4h.json"
SELECTED_DESIGN = ROOT / "data" / "diagnostico_v034_selected_bid_drawdown_guard.json"


def _row(offset: int, depth: float = 100.0) -> dict:
    return {
        "second_offset": offset, "complete_v2": 1,
        "up_book_fresh": 1, "down_book_fresh": 1,
        "up_bid_depth_top5": depth, "up_ask_depth_top5": depth,
        "down_bid_depth_top5": depth, "down_ask_depth_top5": depth,
    }


def _path() -> dict[int, dict]:
    return {offset: _row(offset) for offset in range(30, 68)}


class V034SelectedBidGuardTests(unittest.TestCase):
    def test_design_selects_eighty_percent_without_changing_other_contract_fields(self) -> None:
        design = json.loads(SELECTED_DESIGN.read_text(encoding="utf-8"))
        self.assertEqual(design["decision"], "PREPARE_ONE_FRESH_V034_SELECTED_BID_REPLICATION")
        self.assertEqual(design["selected_threshold"], 0.8)
        selected = design["candidates"]["0.80"]["overall"]
        self.assertEqual(selected["trapped"], 0)
        self.assertGreaterEqual(selected["scheduled_exit_fraction"], 0.8)
        self.assertEqual(selected["median_holding_seconds"], 30.0)

    def test_prereg_freezes_selected_bid_scope_and_eighty_percent(self) -> None:
        prereg = load_and_verify_prereg(PREREG, project_root=ROOT)
        contract = prereg["probe_contract"]
        self.assertEqual(contract["relative_guard_scope"], "SELECTED_BID_ONLY")
        self.assertEqual(contract["relative_drawdown_threshold_inclusive"], 0.8)
        self.assertFalse(prereg["data_policy"]["prices_stored"])
        self.assertFalse(prereg["data_policy"]["pnl_calculated"])

    def test_eighty_percent_exits_before_known_style_collapse(self) -> None:
        snapshots = _path()
        snapshots[40]["down_bid_depth_top5"] = 16.0
        for offset in range(41, 68):
            snapshots[offset]["down_bid_depth_top5"] = 0.0
        prereg = load_and_verify_prereg(PREREG, project_root=ROOT)
        result = evaluate_probe(
            snapshots, decision_offset=30, outcome="Down", contract=prereg["probe_contract"]
        )
        self.assertEqual(result["status"], "EXITED")
        self.assertEqual(result["exit_offset"], 40)
        self.assertIn("RELATIVE_DRAWDOWN:down_bid_depth_top5", result["guard_reasons"])

    def test_eighty_five_percent_misses_same_warning_and_traps(self) -> None:
        snapshots = _path()
        snapshots[40]["down_bid_depth_top5"] = 16.0
        for offset in range(41, 68):
            snapshots[offset]["down_bid_depth_top5"] = 0.0
        result = evaluate_relative_guard_probe(
            snapshots, decision_offset=30, outcome="Down", drawdown_threshold=0.85,
            relative_guard_scope="SELECTED_BID_ONLY",
        )
        self.assertEqual(result["status"], "TRAPPED")

    def test_selected_bid_scope_does_not_exit_healthy_up_on_transient_down_drawdown(self) -> None:
        snapshots = _path()
        snapshots[40]["down_bid_depth_top5"] = 15.0
        result = evaluate_relative_guard_probe(
            snapshots, decision_offset=30, outcome="Up", drawdown_threshold=0.8,
            relative_guard_scope="SELECTED_BID_ONLY",
        )
        self.assertEqual(result["status"], "EXITED")
        self.assertEqual(result["exit_kind"], "SCHEDULED")
        self.assertEqual(result["exit_offset"], 61)

    def test_direct_one_tick_zero_remains_fail_closed_not_falsely_solved(self) -> None:
        snapshots = _path()
        for offset in range(40, 68):
            snapshots[offset]["down_bid_depth_top5"] = 0.0
        result = evaluate_relative_guard_probe(
            snapshots, decision_offset=30, outcome="Down", drawdown_threshold=0.8,
            relative_guard_scope="SELECTED_BID_ONLY",
        )
        self.assertEqual(result["status"], "TRAPPED")

    def test_store_namespace_and_columns_remain_technical_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "v034.db"
            store = V034Store(database)
            store.open(
                preregistration_sha256="p", implementation_sha256="i",
                launch_manifest_sha256="l", now_timestamp=1_800_000_000.0,
            )
            meta = store.meta()
            self.assertEqual(meta["variant"], "V0.34_FRESH_SELECTED_BID_DRAWDOWN_GUARD_4H")
            self.assertFalse(meta["prices_stored"])
            store.close()
            connection = sqlite3.connect(database)
            try:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                columns = {row[1] for row in connection.execute("PRAGMA table_info(v034_snapshots)")}
            finally:
                connection.close()
            self.assertIn("v034_snapshots", tables)
            self.assertNotIn("v033_snapshots", tables)
            self.assertFalse(any("price" in column or "pnl" in column or "outcome" in column for column in columns))

    def test_status_before_launch_keeps_safety_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status = v034_status(Path(temporary) / "missing.db")
        self.assertEqual(status["status"], "NOT_STARTED")
        self.assertEqual(status["paper_orders"], 0)
        self.assertFalse(status["wallet_required"])
        self.assertEqual(status["real_money"], "BLOQUEADO")

    def test_sealed_implementation_hashes_verify(self) -> None:
        payload = load_and_verify_implementation(IMPLEMENTATION, project_root=ROOT)
        self.assertFalse(payload["economic_strategy_built"])
        self.assertFalse(payload["scheduled_supervision_built"])

    def test_runner_cannot_create_database_without_launch_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "blocked.db"
            with self.assertRaises(V034RunnerError):
                asyncio.run(
                    run_v034(
                        settings=Settings.from_env(), prereg_path=PREREG,
                        implementation_path=IMPLEMENTATION,
                        launch_path=Path(temporary) / "missing.json", output_db=database,
                    )
                )
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
