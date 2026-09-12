from __future__ import annotations

import copy
import unittest
from pathlib import Path

from polymarket_bot.v033_contract import (
    PASS_VERDICT,
    V033ContractError,
    aggregate_probe_results,
    evaluate_fresh_capacity_probe,
    load_and_verify_prereg,
)


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "data" / "prereg_v033_fresh_exit_safety_4h.json"


def _row(offset: int, depth: float = 12.0) -> dict:
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


def _path() -> dict[int, dict]:
    return {offset: _row(offset) for offset in range(30, 68)}


class V033ContractTests(unittest.TestCase):
    def test_prereg_is_fresh_technical_only_and_frozen(self) -> None:
        prereg = load_and_verify_prereg(PREREG, project_root=ROOT)
        self.assertEqual(prereg["scope"]["maximum_positive_result"], PASS_VERDICT)
        self.assertFalse(prereg["scope"]["economic_strategy_present"])
        self.assertFalse(prereg["data_policy"]["prices_stored"])
        self.assertFalse(prereg["data_policy"]["pnl_calculated"])
        self.assertEqual(prereg["probe_contract"]["decision_offset_max_inclusive"], 119)
        self.assertEqual(prereg["stopping"]["maximum_hours"], 4.0)

    def test_changed_prereg_contract_is_rejected(self) -> None:
        payload = load_and_verify_prereg(PREREG, project_root=ROOT)
        changed = copy.deepcopy(payload)
        changed["probe_contract"]["decision_offset_max_inclusive"] = 120
        self.assertNotEqual(changed["probe_contract"], payload["probe_contract"])
        with self.assertRaises(V033ContractError):
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as temporary:
                path = Path(temporary) / "changed.json"
                import json

                path.write_text(json.dumps(changed), encoding="utf-8")
                load_and_verify_prereg(path, project_root=ROOT)

    def test_strong_path_exits_at_scheduled_target(self) -> None:
        result = evaluate_fresh_capacity_probe(_path(), decision_offset=30, outcome="Up")
        self.assertEqual(result["status"], "EXITED")
        self.assertEqual(result["exit_kind"], "SCHEDULED")
        self.assertEqual(result["exit_offset"], 61)
        self.assertEqual(result["exit_delay_seconds"], 0)

    def test_guard_exits_when_selected_bid_loses_buffer_but_still_covers_position(self) -> None:
        snapshots = _path()
        snapshots[40]["up_bid_depth_top5"] = 7.0
        result = evaluate_fresh_capacity_probe(snapshots, decision_offset=30, outcome="Up")
        self.assertEqual(result["status"], "EXITED")
        self.assertEqual(result["exit_kind"], "PROACTIVE_GUARD")
        self.assertEqual(result["exit_offset"], 40)
        self.assertIn("SELECTED_BID_BELOW_BUFFER", result["guard_reasons"])

    def test_guard_exits_selected_position_when_any_other_side_falls_below_five(self) -> None:
        snapshots = _path()
        snapshots[45]["down_ask_depth_top5"] = 4.0
        result = evaluate_fresh_capacity_probe(snapshots, decision_offset=30, outcome="Up")
        self.assertEqual(result["exit_kind"], "PROACTIVE_GUARD")
        self.assertEqual(result["exit_offset"], 45)
        self.assertIn("ANY_BOOK_SIDE_BELOW_POSITION_SIZE", result["guard_reasons"])

    def test_abrupt_selected_bid_loss_without_recovery_is_trapped(self) -> None:
        snapshots = _path()
        for offset in range(40, 67):
            snapshots[offset]["down_bid_depth_top5"] = 0.0
        result = evaluate_fresh_capacity_probe(snapshots, decision_offset=30, outcome="Down")
        self.assertEqual(result["status"], "TRAPPED")
        self.assertEqual(
            result["reason"],
            "NO_FRESH_FULL_DEPTH_SELECTED_BID_BY_TARGET_PLUS_GRACE",
        )

    def test_forced_exit_ignores_reference_completeness_after_entry(self) -> None:
        snapshots = _path()
        snapshots[61]["complete_v2"] = 0
        result = evaluate_fresh_capacity_probe(snapshots, decision_offset=30, outcome="Up")
        self.assertEqual(result["status"], "EXITED")
        self.assertEqual(result["exit_kind"], "SCHEDULED")

    def test_decision_and_entry_require_two_times_depth_buffer(self) -> None:
        decision = _path()
        decision[30]["down_ask_depth_top5"] = 9.99
        self.assertEqual(
            evaluate_fresh_capacity_probe(decision, decision_offset=30, outcome="Up")["reason"],
            "DECISION_BUFFER_DEPTH_INSUFFICIENT",
        )
        entry = _path()
        entry[31]["up_ask_depth_top5"] = 9.99
        self.assertEqual(
            evaluate_fresh_capacity_probe(entry, decision_offset=30, outcome="Up")["reason"],
            "ENTRY_BUFFER_DEPTH_INSUFFICIENT",
        )

    def test_aggregate_counts_guard_scheduled_and_trapped(self) -> None:
        probes = [
            {"status": "EXITED", "exit_kind": "PROACTIVE_GUARD", "guard_reasons": ["A"], "exit_delay_seconds": -2},
            {"status": "EXITED", "exit_kind": "SCHEDULED", "guard_reasons": [], "exit_delay_seconds": 0},
            {"status": "TRAPPED", "exit_kind": None, "guard_reasons": [], "exit_delay_seconds": None},
        ]
        summary = aggregate_probe_results(probes)
        self.assertEqual(summary["proactive_guard_exits"], 1)
        self.assertEqual(summary["scheduled_exits"], 1)
        self.assertEqual(summary["trapped_positions"], 1)
        self.assertEqual(summary["exit_success_within_grace_rate"], 0.66666667)


if __name__ == "__main__":
    unittest.main()
