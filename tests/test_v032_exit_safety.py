from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from polymarket_bot.v032_exit_safety import (
    PASS_VERDICT,
    evaluate_capacity_probe,
    load_and_verify_v032_prereg,
)


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "data" / "prereg_v032_exit_safety_capacity.json"


def _row(offset: int) -> dict:
    timestamp = 1_800_000_000_000 + offset * 1_000
    bids = json.dumps([[0.49, 3.0], [0.48, 3.0]])
    asks = json.dumps([[0.50, 3.0], [0.51, 3.0]])
    return {
        "condition_id": "condition",
        "second_offset": offset,
        "chainlink_price": 100_000.0,
        "chainlink_source_timestamp_ms": timestamp,
        "chainlink_received_timestamp_ms": timestamp + 10,
        "chainlink_age_ms": 0,
        "chainlink_fresh": 1,
        "official_twap_price": 100_000.0,
        "official_twap_window_s": 60,
        "official_twap_source_timestamp_ms": timestamp,
        "official_twap_received_timestamp_ms": timestamp + 10,
        "official_twap_age_ms": 0,
        "official_twap_fresh": 1,
        "up_bid_levels_json": bids,
        "up_ask_levels_json": asks,
        "down_bid_levels_json": bids,
        "down_ask_levels_json": asks,
        "up_book_source_timestamp_ms": timestamp,
        "up_book_received_timestamp_ms": timestamp + 10,
        "up_book_age_ms": 0,
        "up_book_fresh": 1,
        "down_book_source_timestamp_ms": timestamp,
        "down_book_received_timestamp_ms": timestamp + 10,
        "down_book_age_ms": 0,
        "down_book_fresh": 1,
        "quality_flags": 0,
        "complete": 1,
    }


def _path() -> dict[int, dict]:
    return {offset: _row(offset) for offset in range(60, 97)}


class V032ExitSafetyTests(unittest.TestCase):
    def test_prereg_is_technical_only_and_does_not_reopen_pair(self) -> None:
        prereg = load_and_verify_v032_prereg(PREREG, project_root=ROOT)
        self.assertFalse(prereg["family_decision"]["non_atomic_two_leg_pair_reopened"])
        self.assertFalse(prereg["scope"]["economic_strategy_present"])
        self.assertFalse(prereg["data_policy"]["pnl_calculated"])
        self.assertEqual(prereg["scope"]["maximum_positive_result"], PASS_VERDICT)

    def test_scheduled_exit_ignores_reference_feed_outage(self) -> None:
        snapshots = _path()
        snapshots[91]["chainlink_fresh"] = 0
        snapshots[91]["official_twap_fresh"] = 0
        result = evaluate_capacity_probe(
            snapshots,
            decision_offset=60,
            outcome="Up",
            resolution_twap_window_s=60,
        )
        self.assertEqual(result["status"], "EXITED")
        self.assertEqual(result["exit_delay_seconds"], 0)

    def test_missing_scheduled_bid_retries_until_fresh_depth_returns(self) -> None:
        snapshots = _path()
        snapshots[91]["up_bid_levels_json"] = "[]"
        snapshots[92]["up_book_fresh"] = 0
        result = evaluate_capacity_probe(
            snapshots,
            decision_offset=60,
            outcome="Up",
            resolution_twap_window_s=60,
        )
        self.assertEqual(result["status"], "EXITED")
        self.assertEqual(result["exit_delay_seconds"], 2)
        self.assertEqual(result["exit_rejections"]["EXIT_BID_DEPTH_INSUFFICIENT"], 1)
        self.assertEqual(result["exit_rejections"]["EXIT_BOOK_NOT_FRESH"], 1)

    def test_no_bid_during_grace_is_classified_as_trapped(self) -> None:
        snapshots = _path()
        for offset in range(91, 97):
            snapshots[offset]["down_bid_levels_json"] = "[]"
        result = evaluate_capacity_probe(
            snapshots,
            decision_offset=60,
            outcome="Down",
            resolution_twap_window_s=60,
        )
        self.assertEqual(result["status"], "TRAPPED")
        self.assertEqual(result["reason"], "NO_FRESH_FULL_DEPTH_BID_WITHIN_GRACE")

    def test_decision_requires_depth_on_all_four_book_sides(self) -> None:
        snapshots = _path()
        snapshots[60]["down_ask_levels_json"] = "[]"
        snapshots[60]["quality_flags"] = 80
        snapshots[60]["complete"] = 0
        result = evaluate_capacity_probe(
            snapshots,
            decision_offset=60,
            outcome="Up",
            resolution_twap_window_s=60,
        )
        self.assertEqual(result["status"], "DECISION_REJECTED")
        self.assertEqual(result["reason"], "DECISION_ALL_FOUR_DEPTH_INSUFFICIENT")

    def test_entry_requires_fresh_data_and_selected_ask_depth(self) -> None:
        stale = _path()
        stale[61]["official_twap_fresh"] = 0
        stale_result = evaluate_capacity_probe(
            stale,
            decision_offset=60,
            outcome="Up",
            resolution_twap_window_s=60,
        )
        self.assertEqual(stale_result["status"], "ENTRY_REJECTED")
        self.assertEqual(stale_result["reason"], "ENTRY_DATA_INCOMPLETE")

        no_depth = _path()
        no_depth[61]["up_ask_levels_json"] = "[]"
        no_depth[61]["quality_flags"] = 80
        no_depth[61]["complete"] = 0
        no_depth_result = evaluate_capacity_probe(
            no_depth,
            decision_offset=60,
            outcome="Up",
            resolution_twap_window_s=60,
        )
        self.assertEqual(no_depth_result["status"], "ENTRY_REJECTED")
        self.assertEqual(no_depth_result["reason"], "ENTRY_ASK_DEPTH_INSUFFICIENT")


if __name__ == "__main__":
    unittest.main()
