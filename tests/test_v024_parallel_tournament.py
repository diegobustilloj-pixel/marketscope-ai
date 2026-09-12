import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v024_audit import evaluate_tournament
from polymarket_bot.v024_runner import load_and_verify_prereg
from polymarket_bot.v024_tournament import (
    eligible_strategy_ids,
    frozen_strategy_config,
    market_record,
    strategy_matches,
    validate_strategy_config,
)


class V024ParallelTournamentTests(unittest.TestCase):
    def prereg(self, evidence: Path, *, hours: float = 4.0) -> dict:
        return {
            "schema": "prereg_v024_parallel_tournament_4h_1",
            "status": "FROZEN_PARALLEL_TOURNAMENT_4H",
            "target_hours": hours,
            "strategies": frozen_strategy_config(),
            "selection": {
                "family_size": 3,
                "family_alpha": 0.05,
                "bonferroni_one_sided_z": 2.128045234184984,
                "minimum_trades_per_strategy": 10,
                "requires_positive_first_half": True,
                "requires_positive_second_half": True,
            },
            "safety": {
                "wallet_required": False,
                "orders_enabled": False,
                "paper_orders_enabled": False,
                "real_money": "BLOQUEADO",
                "active_forward_modified": False,
                "maximum_hours": 4,
            },
            "artifacts": {
                "model": {
                    "relative_path": "data/modelos_twap_transfer_v093.joblib",
                    "sha256": sha256_file(
                        ROOT / "data/modelos_twap_transfer_v093.joblib"
                    ),
                },
                "phase4": {
                    "relative_path": "data/fase4_modelos.db",
                    "sha256": sha256_file(ROOT / "data/fase4_modelos.db"),
                },
            },
            "design_evidence": {
                "relative_path": str(evidence.relative_to(ROOT)).replace("\\", "/"),
                "sha256": sha256_file(evidence),
            },
            "code_hashes": {
                "engine": sha256_file(ROOT / "src/polymarket_bot/v024_tournament.py"),
                "runner": sha256_file(ROOT / "src/polymarket_bot/v024_runner.py"),
                "entrypoint": sha256_file(ROOT / "v024_monitor.py"),
                "auditor": sha256_file(ROOT / "src/polymarket_bot/v024_audit.py"),
                "collector_and_model_runtime": sha256_file(
                    ROOT / "src/polymarket_bot/phase41.py"
                ),
                "cost_runtime": sha256_file(ROOT / "src/polymarket_bot/phase4.py"),
            },
        }

    def test_loader_accepts_four_hours_and_rejects_eight(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            evidence = root / "evidence.json"
            evidence.write_text("{}\n", encoding="utf-8")
            prereg_path = root / "prereg.json"
            prereg_path.write_text(
                json.dumps(self.prereg(evidence)), encoding="utf-8"
            )
            self.assertEqual(load_and_verify_prereg(prereg_path)["target_hours"], 4.0)
            prereg_path.write_text(
                json.dumps(self.prereg(evidence, hours=8.0)), encoding="utf-8"
            )
            with self.assertRaisesRegex(Exception, "exactamente 4 horas"):
                load_and_verify_prereg(prereg_path)

    def test_strategy_contract_rejects_parameter_relaxation(self):
        config = frozen_strategy_config()
        validate_strategy_config(config)
        config[0]["maximum_entry_cost"] = 0.95
        with self.assertRaisesRegex(Exception, "Parametros V0.24 incompatibles"):
            validate_strategy_config(config)

    def test_market_favorite_uses_project_taker_cost_and_model_agreement(self):
        record = market_record(
            feature_json={
                "implied_up_mid_probability": 0.70,
                "up_best_ask": 0.60,
                "down_best_ask": 0.40,
            },
            model_probability_up=0.65,
            label="Up",
            market_start_ms=1,
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["favorite_side"], "Up")
        self.assertTrue(record["model_agrees"])
        expected_fill = 0.605
        expected_cost = expected_fill + 0.07 * expected_fill * (1 - expected_fill)
        self.assertAlmostEqual(record["entry_cost"], expected_cost)
        self.assertIsNone(
            market_record(
                feature_json={
                    "implied_up_mid_probability": 0.5,
                    "up_best_ask": 0.5,
                    "down_best_ask": 0.5,
                },
                model_probability_up=0.5,
            )
        )

    def test_strategy_boundaries_are_exact_and_fail_closed(self):
        strategies = {item["id"]: item for item in frozen_strategy_config()}
        base = {"entry_cost": 0.70, "model_agrees": True}
        self.assertTrue(strategy_matches(base, strategies["agreement_cap_090"]))
        self.assertTrue(strategy_matches(base, strategies["favorite_band_060_080"]))
        self.assertTrue(strategy_matches(base, strategies["favorite_cap_090"]))
        self.assertFalse(
            strategy_matches(
                {"entry_cost": 0.50, "model_agrees": True},
                strategies["favorite_cap_090"],
            )
        )
        self.assertTrue(
            strategy_matches(
                {"entry_cost": 0.90, "model_agrees": True},
                strategies["agreement_cap_090"],
            )
        )
        self.assertFalse(
            strategy_matches(
                {"entry_cost": 0.80, "model_agrees": True},
                strategies["favorite_band_060_080"],
            )
        )
        self.assertEqual(len(eligible_strategy_ids(base, list(strategies.values()))), 3)

    def test_tournament_counts_overlap_and_selects_one_winner(self):
        records = [
            {
                "market_start_ms": index * 100,
                "entry_cost": 0.70,
                "favorite_side": "Up",
                "label": "Up",
                "model_agrees": True,
            }
            for index in range(12)
        ]
        result = evaluate_tournament(
            records, frozen_strategy_config(), midpoint_ms=600
        )
        self.assertEqual(result["selected_strategy"], "agreement_cap_090")
        self.assertEqual(len(result["rescued_strategies"]), 3)
        self.assertEqual(result["overlap_counts"][
            "agreement_cap_090|favorite_cap_090"
        ], 12)
        self.assertEqual(result["overlap_counts"][
            "favorite_band_060_080|favorite_cap_090"
        ], 12)

    def test_negative_second_half_cannot_be_rescued(self):
        records = []
        for index in range(12):
            records.append(
                {
                    "market_start_ms": index * 100,
                    "entry_cost": 0.70,
                    "favorite_side": "Up",
                    "label": "Up" if index < 6 else "Down",
                    "model_agrees": True,
                }
            )
        result = evaluate_tournament(
            records, frozen_strategy_config(), midpoint_ms=600
        )
        self.assertIsNone(result["selected_strategy"])
        self.assertEqual(len(result["discarded_strategies"]), 3)


if __name__ == "__main__":
    unittest.main()
