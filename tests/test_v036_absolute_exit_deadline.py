from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.config import Settings
from polymarket_bot.v036_capture import V036Store
from polymarket_bot.v036_contract import (
    evaluate_market_probes,
    evaluate_probe,
    load_and_verify_prereg,
)
from polymarket_bot.v036_runner import (
    V036RunnerError,
    load_and_verify_implementation,
    run_v036,
    v036_status,
)


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "data" / "prereg_v036_absolute_exit_deadline_4h.json"
IMPLEMENTATION = ROOT / "data" / "implementation_v036_absolute_exit_deadline_4h.json"
DESIGN = ROOT / "data" / "diagnostico_v036_absolute_exit_deadline.json"


def _row(offset: int, depth: float = 1000.0) -> dict:
    return {
        "second_offset": offset,
        "complete_v2": 1,
        "up_book_fresh": 1,
        "down_book_fresh": 1,
        "up_bid_depth_top5": depth,
        "up_ask_depth_top5": depth,
        "down_bid_depth_top5": depth,
        "down_ask_depth_top5": depth,
    }


def _path(end: int = 160) -> dict[int, dict]:
    return {offset: _row(offset) for offset in range(end + 1)}


class V036AbsoluteExitDeadlineTests(unittest.TestCase):
    def test_design_derives_deadline_without_grid_and_closes_all_evidence(self) -> None:
        design = json.loads(DESIGN.read_text(encoding="utf-8"))
        self.assertEqual(
            design["decision"],
            "PREPARE_ONE_FRESH_V036_ABSOLUTE_EXIT_DEADLINE_REPLICATION",
        )
        self.assertFalse(design["derivation"]["parameter_grid_used"])
        self.assertEqual(design["derivation"]["absolute_exit_deadline"], 116)
        self.assertEqual(design["derivation"]["derived_decision_max_inclusive"], 95)
        self.assertTrue(design["closed_evidence"]["all_gates_passed"])
        self.assertEqual(design["closed_evidence"]["overall"]["trapped_positions"], 0)
        self.assertEqual(design["closed_evidence"]["overall"]["exit_successes_within_grace"], 3960)

    def test_prereg_freezes_four_hours_and_6336_expected_probes(self) -> None:
        prereg = load_and_verify_prereg(PREREG, project_root=ROOT)
        self.assertEqual(prereg["stopping"]["maximum_hours"], 4.0)
        self.assertEqual(prereg["duration_rationale"]["expected_capacity_probes"], 6336)
        self.assertEqual(prereg["probe_contract"]["absolute_exit_deadline_offset"], 116)
        self.assertFalse(prereg["data_policy"]["prices_stored"])
        self.assertFalse(prereg["data_policy"]["pnl_calculated"])

    def test_early_decision_keeps_thirty_seconds(self) -> None:
        contract = load_and_verify_prereg(PREREG, project_root=ROOT)["probe_contract"]
        result = evaluate_probe(_path(), decision_offset=85, outcome="Down", contract=contract)
        self.assertEqual(result["status"], "EXITED")
        self.assertEqual(result["target_exit_offset"], 116)
        self.assertEqual(result["planned_holding_seconds"], 30)

    def test_last_decision_tapers_to_twenty_seconds_and_exits_at_deadline(self) -> None:
        snapshots = _path()
        for offset in range(126, 161):
            snapshots[offset]["down_bid_depth_top5"] = 0.0
        contract = load_and_verify_prereg(PREREG, project_root=ROOT)["probe_contract"]
        result = evaluate_probe(
            snapshots, decision_offset=95, outcome="Down", contract=contract
        )
        self.assertEqual(result["status"], "EXITED")
        self.assertEqual(result["exit_kind"], "SCHEDULED")
        self.assertEqual(result["target_exit_offset"], 116)
        self.assertEqual(result["planned_holding_seconds"], 20)

    def test_generator_never_opens_after_second_95(self) -> None:
        contract = load_and_verify_prereg(PREREG, project_root=ROOT)["probe_contract"]
        probes = evaluate_market_probes(
            _path(), condition_id="c", slug="s", contract=contract
        )
        offsets = {int(probe["decision_offset"]) for probe in probes}
        self.assertEqual(len(probes), 132)
        self.assertEqual(min(offsets), 30)
        self.assertEqual(max(offsets), 95)
        self.assertNotIn(96, offsets)

    def test_future_earlier_collapse_still_fails_closed(self) -> None:
        snapshots = _path()
        for offset in range(116, 161):
            snapshots[offset]["down_bid_depth_top5"] = 0.0
        contract = load_and_verify_prereg(PREREG, project_root=ROOT)["probe_contract"]
        result = evaluate_probe(
            snapshots, decision_offset=95, outcome="Down", contract=contract
        )
        self.assertEqual(result["status"], "TRAPPED")

    def test_store_namespace_and_columns_remain_technical_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "v036.db"
            store = V036Store(database)
            store.open(
                preregistration_sha256="p", implementation_sha256="i",
                launch_manifest_sha256="l", now_timestamp=1_800_000_000.0,
            )
            meta = store.meta()
            self.assertEqual(meta["variant"], "V0.36_FRESH_ABSOLUTE_EXIT_DEADLINE_4H")
            self.assertFalse(meta["prices_stored"])
            store.close()
            connection = sqlite3.connect(database)
            try:
                tables = {
                    row[0]
                    for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(v036_snapshots)")
                }
            finally:
                connection.close()
            self.assertIn("v036_snapshots", tables)
            self.assertNotIn("v035_snapshots", tables)
            self.assertFalse(any("price" in col or "pnl" in col or "outcome" in col for col in columns))

    def test_status_before_launch_keeps_safety_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status = v036_status(Path(temporary) / "missing.db")
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
            with self.assertRaises(V036RunnerError):
                asyncio.run(
                    run_v036(
                        settings=Settings.from_env(), prereg_path=PREREG,
                        implementation_path=IMPLEMENTATION,
                        launch_path=Path(temporary) / "missing.json", output_db=database,
                    )
                )
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
