import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v021_audit import audit_v021
from polymarket_bot.v021_runner import V021Store
from polymarket_bot.v021_safe_pair import default_strategy_config


class V021AuditTests(unittest.TestCase):
    def prereg(self, root: Path, evidence: Path) -> dict:
        return {
            "schema": "prereg_v021_safe_pair_observer_1",
            "status": "FROZEN_OBSERVER_ONLY",
            "target_hours": 1 / 12,
            "strategy": default_strategy_config(),
            "safety": {
                "wallet_required": False,
                "orders_enabled": False,
                "paper_orders_enabled": False,
                "real_money": "BLOQUEADO",
                "outcomes_before_completion": False,
                "outcomes_after_completion": False,
                "active_forward_read": False,
                "active_forward_modified": False,
                "maximum_hours": 24,
            },
            "gates": {
                "minimum_complete_markets": 1,
                "minimum_complete_market_fraction": 1.0,
                "maximum_interrupted_market_fraction": 0.0,
                "minimum_valid_observations": 1,
                "minimum_sample_buckets": 1,
                "minimum_opportunity_episodes": 1,
                "minimum_eligible_sample_buckets": 1,
            },
            "design_evidence": {
                "relative_path": str(evidence.relative_to(ROOT)).replace("\\", "/"),
                "sha256": sha256_file(evidence),
            },
            "code_hashes": {
                "engine": sha256_file(ROOT / "src/polymarket_bot/v021_safe_pair.py"),
                "runner": sha256_file(ROOT / "src/polymarket_bot/v021_runner.py"),
                "entrypoint": sha256_file(ROOT / "v021_monitor.py"),
                "auditor": sha256_file(ROOT / "src/polymarket_bot/v021_audit.py"),
                "collector": sha256_file(ROOT / "src/polymarket_bot/phase41.py"),
            },
        }

    def test_completed_safe_observation_is_audited_read_only(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            evidence = root / "evidence.json"
            evidence.write_text("{}\n", encoding="utf-8")
            prereg_path = root / "prereg.json"
            prereg = self.prereg(root, evidence)
            prereg_path.write_text(json.dumps(prereg), encoding="utf-8")
            database = root / "v021.db"
            store = V021Store(database)
            store.open(prereg, prereg_path)
            market = SimpleNamespace(
                condition_id="condition", slug="btc-updown-5m-1800000000",
                start_ms=1_800_000_000_000, end_ms=1_800_000_300_000,
            )
            store.start_market(market)
            store.save_samples(
                [{
                    "condition_id": "condition", "bucket_ms": 1, "source_timestamp_ms": 1,
                    "received_timestamp_ms": 1, "up_best_ask": 0.4, "down_best_ask": 0.58,
                    "up_ask_depth": 10.0, "down_ask_depth": 10.0, "up_total_cost": 0.4,
                    "down_total_cost": 0.58, "complete_set_cost": 0.98, "eligible": 1,
                    "message_count": 2,
                }]
            )
            store.finalize_market(
                {
                    "condition_id": "condition", "message_count": 2, "valid_observations": 1,
                    "eligible_observations": 1, "opportunity_episodes": 1,
                    "minimum_complete_set_cost": 0.98, "maximum_observed_depth_shares": 10.0,
                    "maximum_opportunity_persistence_ms": 100,
                },
                "COMPLETE",
            )
            store.set_meta("experiment_completed_at", "2026-08-18T00:00:00+00:00")
            store.close()
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            result_path = root / "result.json"
            result = audit_v021(
                database=database, prereg_path=prereg_path, result_path=result_path
            )
            after = hashlib.sha256(database.read_bytes()).hexdigest()
            result_exists = result_path.is_file()
        self.assertEqual(result["verdict"], "PASS_OBSERVER_ONLY")
        self.assertFalse(result["forward_candidate"])
        self.assertEqual(before, after)
        self.assertTrue(result_exists)


if __name__ == "__main__":
    unittest.main()
