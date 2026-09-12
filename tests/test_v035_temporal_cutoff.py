from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.config import Settings
from polymarket_bot.v035_capture import V035Store
from polymarket_bot.v035_contract import (
    evaluate_market_probes,
    evaluate_probe,
    load_and_verify_prereg,
)
from polymarket_bot.v035_runner import (
    V035RunnerError,
    load_and_verify_implementation,
    run_v035,
    v035_status,
)


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "data" / "prereg_v035_temporal_cutoff_4h.json"
IMPLEMENTATION = ROOT / "data" / "implementation_v035_temporal_cutoff_4h.json"
DESIGN = ROOT / "data" / "diagnostico_v035_temporal_exposure_cutoff.json"


def _row(offset: int, depth: float = 100.0) -> dict:
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
    return {offset: _row(offset) for offset in range(0, end + 1)}


class V035TemporalCutoffTests(unittest.TestCase):
    def test_closed_diagnostic_selects_largest_cap_with_required_margin(self) -> None:
        design = json.loads(DESIGN.read_text(encoding="utf-8"))
        self.assertEqual(
            design["decision"],
            "PREPARE_ONE_FRESH_V035_TEMPORAL_CUTOFF_REPLICATION",
        )
        self.assertEqual(design["selected_decision_cap"], 105)
        selected = design["candidates"]["105"]
        self.assertEqual(selected["overall"]["trapped_positions"], 0)
        self.assertEqual(selected["latest_scheduled_exit_offset"], 136)
        self.assertEqual(
            selected["margin_before_earliest_irreducible_exit_seconds"], 10
        )
        self.assertGreaterEqual(selected["probe_retention_vs_30_119"], 0.8)

    def test_prereg_freezes_preventive_cap_and_safety_blocks(self) -> None:
        prereg = load_and_verify_prereg(PREREG, project_root=ROOT)
        contract = prereg["probe_contract"]
        self.assertEqual(contract["decision_offset_max_inclusive"], 105)
        self.assertEqual(contract["decision_offset_count"], 76)
        self.assertEqual(contract["latest_scheduled_exit_offset"], 136)
        self.assertEqual(
            contract["required_margin_before_earliest_irreducible_exit_seconds"], 10
        )
        self.assertFalse(prereg["data_policy"]["prices_stored"])
        self.assertFalse(prereg["data_policy"]["pnl_calculated"])

    def test_latest_allowed_decision_exits_before_known_collapse_zone(self) -> None:
        snapshots = _path()
        for offset in range(146, 161):
            snapshots[offset]["up_bid_depth_top5"] = 0.0
        contract = load_and_verify_prereg(PREREG, project_root=ROOT)["probe_contract"]
        result = evaluate_probe(
            snapshots, decision_offset=105, outcome="Up", contract=contract
        )
        self.assertEqual(result["status"], "EXITED")
        self.assertEqual(result["exit_kind"], "SCHEDULED")
        self.assertEqual(result["exit_offset"], 136)

    def test_market_probe_generator_never_opens_after_second_105(self) -> None:
        contract = load_and_verify_prereg(PREREG, project_root=ROOT)["probe_contract"]
        probes = evaluate_market_probes(
            _path(), condition_id="c", slug="s", contract=contract
        )
        self.assertEqual(len(probes), 152)
        self.assertEqual(min(int(probe["decision_offset"]) for probe in probes), 30)
        self.assertEqual(max(int(probe["decision_offset"]) for probe in probes), 105)
        self.assertNotIn(106, {int(probe["decision_offset"]) for probe in probes})

    def test_relative_guard_remains_active_inside_shortened_window(self) -> None:
        snapshots = _path()
        snapshots[40]["down_bid_depth_top5"] = 16.0
        for offset in range(41, 68):
            snapshots[offset]["down_bid_depth_top5"] = 0.0
        contract = load_and_verify_prereg(PREREG, project_root=ROOT)["probe_contract"]
        result = evaluate_probe(
            snapshots, decision_offset=30, outcome="Down", contract=contract
        )
        self.assertEqual(result["status"], "EXITED")
        self.assertEqual(result["exit_offset"], 40)
        self.assertIn(
            "RELATIVE_DRAWDOWN:down_bid_depth_top5", result["guard_reasons"]
        )

    def test_unseen_earlier_instant_collapse_still_fails_closed(self) -> None:
        snapshots = _path()
        for offset in range(130, 161):
            snapshots[offset]["up_bid_depth_top5"] = 0.0
        contract = load_and_verify_prereg(PREREG, project_root=ROOT)["probe_contract"]
        result = evaluate_probe(
            snapshots, decision_offset=105, outcome="Up", contract=contract
        )
        self.assertEqual(result["status"], "TRAPPED")

    def test_store_namespace_and_columns_remain_technical_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "v035.db"
            store = V035Store(database)
            store.open(
                preregistration_sha256="p",
                implementation_sha256="i",
                launch_manifest_sha256="l",
                now_timestamp=1_800_000_000.0,
            )
            meta = store.meta()
            self.assertEqual(meta["variant"], "V0.35_FRESH_TEMPORAL_EXPOSURE_CUTOFF_4H")
            self.assertFalse(meta["prices_stored"])
            store.close()
            connection = sqlite3.connect(database)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(v035_snapshots)")
                }
            finally:
                connection.close()
            self.assertIn("v035_snapshots", tables)
            self.assertNotIn("v034_snapshots", tables)
            self.assertFalse(
                any(
                    "price" in column or "pnl" in column or "outcome" in column
                    for column in columns
                )
            )

    def test_status_before_launch_keeps_safety_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status = v035_status(Path(temporary) / "missing.db")
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
            with self.assertRaises(V035RunnerError):
                asyncio.run(
                    run_v035(
                        settings=Settings.from_env(),
                        prereg_path=PREREG,
                        implementation_path=IMPLEMENTATION,
                        launch_path=Path(temporary) / "missing.json",
                        output_db=database,
                    )
                )
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
