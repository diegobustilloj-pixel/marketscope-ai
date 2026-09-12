from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v029_final_postmortem import (
    build_v029_final_postmortem,
    direction_alignment,
    entry_cost_band,
    expected_edge_band,
    selected_probability_band,
    signed_twap_band,
    validation_block,
    volatility_regime,
)


ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "data" / "resultado_v029_high_frequency_holdout.json"
DATABASE = ROOT / "data" / "paper_v029_high_frequency_holdout.db"
STORED = ROOT / "data" / "postmortem_v029_validation_economics_final.json"


class V029FinalPostmortemTests(unittest.TestCase):
    def test_fixed_bucket_boundaries_are_deterministic(self) -> None:
        self.assertEqual(entry_cost_band(0.249999), "cost_lt_025")
        self.assertEqual(entry_cost_band(0.25), "cost_ge_025_lt_050")
        self.assertEqual(entry_cost_band(0.50), "cost_ge_050_lt_075")
        self.assertEqual(entry_cost_band(0.75), "cost_ge_075")
        self.assertEqual(
            selected_probability_band(0.49), "p_selected_lt_050"
        )
        self.assertEqual(
            selected_probability_band(0.65), "p_selected_ge_065_lt_080"
        )
        self.assertEqual(expected_edge_band(0.01), "edge_gt_000_le_001")
        self.assertEqual(expected_edge_band(0.03), "edge_gt_001_le_003")
        self.assertEqual(expected_edge_band(0.05), "edge_gt_003_le_005")
        with self.assertRaises(ValueError):
            expected_edge_band(0.0)

    def test_regime_and_time_boundaries_are_explicit(self) -> None:
        self.assertEqual(signed_twap_band(-10.0), "twap_ge_neg10_lt_neg5")
        self.assertEqual(signed_twap_band(0.0), "twap_ge_0_lt_5")
        self.assertEqual(
            direction_alignment(0.5, "Up"), "neutral_abs_le_1bps"
        )
        self.assertEqual(direction_alignment(-2.0, "Up"), "direction_opposed")
        self.assertEqual(direction_alignment(-2.0, "Down"), "direction_aligned")
        self.assertEqual(volatility_regime(0.75), "vol_ratio_normal_ge_075_lt_125")
        self.assertEqual(volatility_regime(1.25), "vol_ratio_high_ge_125")
        self.assertEqual(validation_block(12.0), "h12_to_h16")
        self.assertEqual(validation_block(16.0), "h16_to_h20")
        self.assertEqual(validation_block(20.0), "h20_to_h24")

    def test_postmortem_reproduces_auditor_and_preserves_sources(self) -> None:
        before = {path: sha256_file(path) for path in (RESULT, DATABASE)}
        calculated = build_v029_final_postmortem(result_path=RESULT)
        after = {path: sha256_file(path) for path in (RESULT, DATABASE)}
        self.assertEqual(before, after)
        self.assertTrue(calculated["reproduction"]["auditor_reproduced_exactly"])
        self.assertEqual(calculated["reproduction"]["training_rows"], 141)
        self.assertEqual(calculated["reproduction"]["validation_rows"], 141)
        self.assertEqual(calculated["reproduction"]["candidate_signals"], 110)
        self.assertEqual(
            calculated["reproduction"]["candidate_metrics"][
                "net_pnl_at_5_shares"
            ],
            -18.59426145,
        )
        self.assertTrue(
            calculated["decisive_findings"][
                "loss_present_before_modeled_execution_cost"
            ]
        )
        self.assertTrue(
            calculated["decisive_findings"]["model_brier_worse_than_market"]
        )
        self.assertIsNone(calculated["branch_decision"]["selected_strategy"])
        stored = json.loads(STORED.read_text(encoding="utf-8"))
        self.assertEqual(calculated["source_hashes"], stored["source_hashes"])
        self.assertEqual(
            calculated["generator_sha256"], stored["generator_sha256"]
        )

    def test_output_is_idempotent_for_same_sealed_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "postmortem.json"
            first = build_v029_final_postmortem(
                result_path=RESULT,
                output_path=output,
            )
            second = build_v029_final_postmortem(
                result_path=RESULT,
                output_path=output,
            )
            self.assertEqual(first, second)

    def test_wrong_verdict_is_rejected_before_database_use(self) -> None:
        payload = json.loads(RESULT.read_text(encoding="utf-8"))
        payload["verdict"] = "HYPOTHESIS_REQUIRES_FRESH_V030_REPLICATION"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                build_v029_final_postmortem(result_path=path)


if __name__ == "__main__":
    unittest.main()
