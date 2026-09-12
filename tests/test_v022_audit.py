import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v022_audit import audit_v022
from polymarket_bot.v022_runner import V022Store
from polymarket_bot.v022_synced_pair import default_strategy_config


class V022AuditTests(unittest.TestCase):
    def prereg(self, root: Path, evidence: Path) -> dict:
        return {
            "schema": "prereg_v022_synced_persistent_observer_1",
            "status": "FROZEN_SYNCHRONIZED_OBSERVER_ONLY",
            "target_hours": 12.0,
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
                "maximum_hours": 12,
            },
            "gates": {
                "minimum_complete_markets": 1,
                "minimum_complete_market_fraction": 0.0,
                "maximum_interrupted_market_fraction": 0.0,
                "minimum_valid_observations": 1,
                "minimum_synchronized_observations": 1,
                "minimum_sample_buckets": 1,
                "maximum_nonmonotonic_receive_timestamps": 0,
                "minimum_raw_opportunity_episodes": 1,
                "minimum_raw_eligible_sample_buckets": 1,
                "minimum_confirmed_opportunity_episodes": 1,
                "minimum_confirmed_sample_buckets": 1,
            },
            "design_evidence": {
                "relative_path": str(evidence.relative_to(ROOT)).replace("\\", "/"),
                "sha256": sha256_file(evidence),
            },
            "code_hashes": {
                "engine": sha256_file(ROOT / "src/polymarket_bot/v022_synced_pair.py"),
                "runner": sha256_file(ROOT / "src/polymarket_bot/v022_runner.py"),
                "entrypoint": sha256_file(ROOT / "v022_monitor.py"),
                "auditor": sha256_file(ROOT / "src/polymarket_bot/v022_audit.py"),
                "collector": sha256_file(ROOT / "src/polymarket_bot/phase41.py"),
                "cost_model": sha256_file(ROOT / "src/polymarket_bot/v021_safe_pair.py"),
            },
        }

    @staticmethod
    def summary(*, confirmed: bool) -> dict:
        return {
            "condition_id": "condition",
            "message_count": 3,
            "valid_observations": 2,
            "synchronized_observations": 2,
            "unsynchronized_observations": 0,
            "raw_eligible_observations": 2,
            "raw_opportunity_episodes": 1,
            "confirmed_observations": int(confirmed),
            "confirmed_opportunity_episodes": int(confirmed),
            "minimum_complete_set_cost": 0.98,
            "minimum_synchronized_complete_set_cost": 0.98,
            "minimum_raw_eligible_cost": 0.98,
            "minimum_confirmed_cost": 0.985 if confirmed else None,
            "maximum_observed_depth_shares": 10.0,
            "maximum_candidate_persistence_ms": 500 if confirmed else 0,
            "stale_ask_updates_rejected": 0,
            "nonmonotonic_receive_timestamps": 0,
        }

    def build_evidence(self, root: Path, *, confirmed: bool):
        evidence = root / "evidence.json"
        evidence.write_text("{}\n", encoding="utf-8")
        prereg_path = root / "prereg.json"
        prereg = self.prereg(root, evidence)
        prereg_path.write_text(json.dumps(prereg), encoding="utf-8")
        database = root / "v022.db"
        store = V022Store(database)
        store.open(prereg, prereg_path)
        market = SimpleNamespace(
            condition_id="condition",
            slug="btc-updown-5m-1800000000",
            start_ms=1_800_000_000_000,
            end_ms=1_800_000_300_000,
        )
        store.start_market(market)
        store.save_samples(
            [
                {
                    "condition_id": "condition",
                    "bucket_ms": 1,
                    "valid_observations": 2,
                    "synchronized_observations": 2,
                    "raw_eligible_observations": 2,
                    "confirmed_observations": int(confirmed),
                    "minimum_complete_set_cost": 0.98,
                    "minimum_synchronized_cost": 0.98,
                    "minimum_raw_eligible_cost": 0.98,
                    "minimum_confirmed_cost": 0.985 if confirmed else None,
                    "minimum_source_skew_ms": 0,
                    "minimum_receive_skew_ms": 0,
                    "maximum_candidate_age_ms": 500 if confirmed else 0,
                }
            ]
        )
        raw = {
            "condition_id": "condition",
            "event_type": "RAW_OPEN",
            "received_timestamp_ms": 1,
            "complete_set_cost": 0.98,
            "candidate_age_ms": 0,
            "up_source_timestamp_ms": 1,
            "down_source_timestamp_ms": 1,
            "up_received_timestamp_ms": 1,
            "down_received_timestamp_ms": 1,
            "source_skew_ms": 0,
            "receive_skew_ms": 0,
            "maximum_book_age_ms": 0,
        }
        signals = [raw]
        if confirmed:
            signals.append({**raw, "event_type": "CONFIRMED", "received_timestamp_ms": 501,
                            "complete_set_cost": 0.985, "candidate_age_ms": 500})
        store.save_signals(signals)
        store.finalize_market(self.summary(confirmed=confirmed), "COMPLETE")
        store.set_meta("experiment_completed_at", "2026-08-19T00:00:00+00:00")
        store.close()
        return database, prereg_path

    def test_completed_confirmed_observation_is_audited_read_only(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            database, prereg_path = self.build_evidence(root, confirmed=True)
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            result = audit_v022(
                database=database,
                prereg_path=prereg_path,
                result_path=root / "result.json",
            )
            after = hashlib.sha256(database.read_bytes()).hexdigest()
        self.assertEqual(result["verdict"], "PASS_SYNCHRONIZED_OBSERVER_ONLY")
        self.assertTrue(result["execution_study_candidate"])
        self.assertFalse(result["forward_candidate"])
        self.assertEqual(before, after)

    def test_raw_but_unconfirmed_observation_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            database, prereg_path = self.build_evidence(root, confirmed=False)
            result = audit_v022(
                database=database,
                prereg_path=prereg_path,
                result_path=root / "result.json",
            )
        self.assertEqual(result["verdict"], "FAIL_UNCONFIRMED_TRANSIENTS")
        self.assertFalse(result["execution_study_candidate"])


if __name__ == "__main__":
    unittest.main()
