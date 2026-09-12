import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v023_audit import filtered_trade_metrics, signal_passes_filter
from polymarket_bot.v023_runner import load_and_verify_prereg


class V023FourHourTests(unittest.TestCase):
    def prereg(self, evidence: Path, *, hours: float = 4.0) -> dict:
        return {
            "schema": "prereg_v023_filtered_forward_4h_1",
            "status": "FROZEN_FILTERED_DIRECTIONAL_FORWARD_4H",
            "target_hours": hours,
            "strategy": {
                "model_name": "twap_transfer_strike_hgb",
                "minimum_expected_edge_inclusive": 0.10,
                "maximum_expected_edge_exclusive": 0.15,
                "minimum_entry_cost_exclusive": 0.50,
                "paper_shares_per_signal": 5.0,
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
                "runner": sha256_file(ROOT / "src/polymarket_bot/v023_runner.py"),
                "entrypoint": sha256_file(ROOT / "v023_monitor.py"),
                "auditor": sha256_file(ROOT / "src/polymarket_bot/v023_audit.py"),
                "collector_and_model_runtime": sha256_file(
                    ROOT / "src/polymarket_bot/phase41.py"
                ),
            },
        }

    def test_loader_accepts_exact_four_hours(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            evidence = root / "evidence.json"
            evidence.write_text("{}\n", encoding="utf-8")
            prereg_path = root / "prereg.json"
            prereg_path.write_text(
                json.dumps(self.prereg(evidence)), encoding="utf-8"
            )
            loaded = load_and_verify_prereg(prereg_path)
        self.assertEqual(loaded["target_hours"], 4.0)

    def test_loader_rejects_eight_hours(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            evidence = root / "evidence.json"
            evidence.write_text("{}\n", encoding="utf-8")
            prereg_path = root / "prereg.json"
            prereg_path.write_text(
                json.dumps(self.prereg(evidence, hours=8.0)), encoding="utf-8"
            )
            with self.assertRaisesRegex(Exception, "exactamente 4 horas"):
                load_and_verify_prereg(prereg_path)

    def test_filter_boundaries_are_fail_closed(self):
        base = {
            "model_name": "twap_transfer_strike_hgb",
            "would_trade": 1,
            "entry_cost": 0.60,
            "expected_edge": 0.10,
        }
        self.assertTrue(signal_passes_filter(base))
        self.assertFalse(signal_passes_filter({**base, "entry_cost": 0.50}))
        self.assertFalse(signal_passes_filter({**base, "expected_edge": 0.15}))
        self.assertFalse(signal_passes_filter({**base, "would_trade": 0}))

    def test_metrics_use_resolved_pnl_and_five_share_notional(self):
        base = {
            "model_name": "twap_transfer_strike_hgb",
            "would_trade": 1,
            "expected_edge": 0.12,
        }
        metrics = filtered_trade_metrics(
            [
                {**base, "entry_cost": 0.60, "side": "Up", "label": "Up"},
                {**base, "entry_cost": 0.70, "side": "Down", "label": "Up"},
                {**base, "entry_cost": 0.50, "side": "Up", "label": "Up"},
            ]
        )
        self.assertEqual(metrics["trades"], 2)
        self.assertAlmostEqual(metrics["net_pnl_per_share_sequence"], -0.30)
        self.assertAlmostEqual(metrics["net_pnl_at_5_shares"], -1.50)


if __name__ == "__main__":
    unittest.main()
