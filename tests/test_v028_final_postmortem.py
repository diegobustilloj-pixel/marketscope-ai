from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v028_final_postmortem import (
    build_v028_final_postmortem,
    outcome_blind_frequency_funnel,
)


ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "data" / "resultado_v028_twap_lt5_replication.json"
DATABASE = ROOT / "data" / "paper_v028_twap_lt5_replication.db"
STORED = ROOT / "data" / "postmortem_v028_twap_lt5_final.json"


class V028FinalPostmortemTests(unittest.TestCase):
    def test_outcome_blind_funnel_reproduces_frequency_bottleneck(self) -> None:
        funnel = outcome_blind_frequency_funnel(DATABASE)
        self.assertEqual(funnel["labels_or_outcomes_read"], 0)
        self.assertEqual(funnel["markets_discovered"], 144)
        self.assertEqual(funnel["features_saved"], 141)
        self.assertEqual(funnel["favorite_up_cost_gt_050_le_090"], 11)
        self.assertEqual(
            funnel["favorite_up_cost_exact_twap60_low_vol_lt_075"], 6
        )
        self.assertEqual(funnel["parent_control"], 6)
        self.assertEqual(funnel["primary_twap_abs_lt_5bps"], 6)
        self.assertEqual(funnel["removed_by_low_vol_filter"], 5)
        self.assertEqual(funnel["removed_incrementally_by_twap_lt5_filter"], 0)

    def test_postmortem_reproduces_stored_artifact_and_preserves_sources(self) -> None:
        before = {path: sha256_file(path) for path in (RESULT, DATABASE)}
        calculated = build_v028_final_postmortem(result_path=RESULT)
        after = {path: sha256_file(path) for path in (RESULT, DATABASE)}
        self.assertEqual(before, after)
        self.assertEqual(calculated["source_verdict"], "FAIL_INSUFFICIENT_FREQUENCY")
        self.assertEqual(
            calculated["branch_decision"]["direct_twap_lt5_replication"],
            "DISCARD",
        )
        self.assertFalse(
            calculated["decisive_findings"][
                "twap_lt5_added_incremental_selection"
            ]
        )
        stored = json.loads(STORED.read_text(encoding="utf-8"))
        self.assertEqual(calculated["source_hashes"], stored["source_hashes"])

    def test_wrong_verdict_is_rejected_before_database_use(self) -> None:
        payload = json.loads(RESULT.read_text(encoding="utf-8"))
        payload["verdict"] = "PASS_SINGLE_HYPOTHESIS_PAPER_CANDIDATE"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                build_v028_final_postmortem(result_path=path)


if __name__ == "__main__":
    unittest.main()
