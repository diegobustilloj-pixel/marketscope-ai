from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from polymarket_bot.v031_quality_reaudit import (
    PASS_VERDICT,
    classify_snapshot,
    load_and_verify_quality_prereg,
    summarize_classifications,
)


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "data" / "prereg_v031_quality_semantics_reaudit_v2.json"


def _row() -> dict:
    bids = json.dumps([[0.49, 10.0], [0.48, 20.0]])
    asks = json.dumps([[0.50, 10.0], [0.51, 20.0]])
    return {
        "condition_id": "condition",
        "second_offset": 10,
        "chainlink_price": 100_000.0,
        "chainlink_source_timestamp_ms": 1_000,
        "chainlink_received_timestamp_ms": 1_010,
        "chainlink_age_ms": 0,
        "chainlink_fresh": 1,
        "official_twap_price": 100_000.0,
        "official_twap_window_s": 60,
        "official_twap_source_timestamp_ms": 1_000,
        "official_twap_received_timestamp_ms": 1_010,
        "official_twap_age_ms": 0,
        "official_twap_fresh": 1,
        "up_bid_levels_json": bids,
        "up_ask_levels_json": asks,
        "down_bid_levels_json": bids,
        "down_ask_levels_json": asks,
        "up_book_source_timestamp_ms": 1_000,
        "up_book_received_timestamp_ms": 1_010,
        "up_book_age_ms": 0,
        "up_book_fresh": 1,
        "down_book_source_timestamp_ms": 1_000,
        "down_book_received_timestamp_ms": 1_010,
        "down_book_age_ms": 0,
        "down_book_fresh": 1,
        "quality_flags": 0,
        "complete": 1,
    }


class V031QualityReauditTests(unittest.TestCase):
    def test_frozen_prereg_preserves_original_and_caps_claim(self) -> None:
        prereg = load_and_verify_quality_prereg(PREREG, project_root=ROOT)
        self.assertTrue(prereg["scope"]["posthoc_diagnostic"])
        self.assertFalse(prereg["scope"]["independent_validation_claim_allowed"])
        self.assertEqual(prereg["scope"]["maximum_positive_result"], PASS_VERDICT)
        self.assertEqual(prereg["data_policy"]["outcomes_read"], 0)
        self.assertFalse(prereg["safety"]["orders_enabled"])

    def test_observed_one_sided_book_is_valid_data_but_blocks_actions(self) -> None:
        row = _row()
        row["up_ask_levels_json"] = "[]"
        row["down_bid_levels_json"] = "[]"
        row["quality_flags"] = 80
        row["complete"] = 0
        classified = classify_snapshot(row, resolution_twap_window_s=60)
        self.assertTrue(classified["data_complete_v2"])
        self.assertTrue(classified["one_sided_observed"])
        self.assertEqual(classified["up"]["state"], "BID_ONLY_OBSERVED")
        self.assertEqual(classified["down"]["state"], "ASK_ONLY_OBSERVED")
        self.assertFalse(classified["actions"]["buy_up"])
        self.assertTrue(classified["actions"]["sell_up"])
        self.assertTrue(classified["actions"]["buy_down"])
        self.assertFalse(classified["actions"]["sell_down"])
        self.assertFalse(classified["actions"]["paired_buy"])
        self.assertFalse(classified["actions"]["paired_sell"])
        self.assertFalse(classified["actions"]["full_round_trip"])

    def test_uninitialized_book_remains_a_data_failure(self) -> None:
        row = _row()
        row["up_book_source_timestamp_ms"] = None
        row["up_book_received_timestamp_ms"] = None
        row["up_book_age_ms"] = None
        row["up_book_fresh"] = 0
        classified = classify_snapshot(row, resolution_twap_window_s=60)
        self.assertFalse(classified["data_complete_v2"])
        self.assertEqual(classified["up"]["state"], "UNINITIALIZED")

    def test_stale_or_misaligned_data_remains_a_failure(self) -> None:
        stale = _row()
        stale["down_book_fresh"] = 0
        stale_result = classify_snapshot(stale, resolution_twap_window_s=60)
        self.assertFalse(stale_result["data_complete_v2"])
        self.assertEqual(stale_result["down"]["state"], "STALE")

        misaligned = _row()
        misaligned["official_twap_window_s"] = 30
        misaligned_result = classify_snapshot(misaligned, resolution_twap_window_s=60)
        self.assertFalse(misaligned_result["data_complete_v2"])
        self.assertFalse(misaligned_result["twap_valid"])

    def test_invalid_level_order_remains_a_data_failure(self) -> None:
        row = _row()
        row["up_bid_levels_json"] = json.dumps([[0.48, 10.0], [0.49, 20.0]])
        classified = classify_snapshot(row, resolution_twap_window_s=60)
        self.assertFalse(classified["data_complete_v2"])
        self.assertEqual(classified["up"]["state"], "INVALID_LEVELS")

    def test_summary_reports_reclassification_and_consecutive_risk(self) -> None:
        classified_rows = []
        for offset in range(5):
            row = _row()
            row["second_offset"] = offset
            if 1 <= offset <= 3:
                row["up_ask_levels_json"] = "[]"
                row["down_bid_levels_json"] = "[]"
                row["quality_flags"] = 80
                row["complete"] = 0
            classified_rows.append(classify_snapshot(row, resolution_twap_window_s=60))
        summary = summarize_classifications(
            classified_rows,
            slug_by_condition={"condition": "btc-updown-5m-test"},
        )
        self.assertEqual(summary["data_complete_v2_snapshots"], 5)
        self.assertEqual(summary["reclassified_valid_from_original_incomplete"], 3)
        self.assertEqual(summary["maximum_consecutive_one_sided_seconds"], 3)
        self.assertEqual(
            summary["action_availability"]["full_round_trip"]["snapshots"], 2
        )


if __name__ == "__main__":
    unittest.main()
