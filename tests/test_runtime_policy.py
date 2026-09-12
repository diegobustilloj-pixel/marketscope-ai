import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from polymarket_bot.cli import _parser
from polymarket_bot.runtime_policy import (
    BacktestRuntimeExceeded,
    backtest_runtime_guard,
    enforce_backtest_runtime_hours,
    enforce_forward_duration,
    load_duration_policy,
)


class RuntimePolicyTests(unittest.TestCase):
    def test_real_policy_is_frozen_at_24_hours(self):
        policy = load_duration_policy()

        self.assertEqual(policy["maximum_new_experiment_hours"], 24.0)
        self.assertEqual(enforce_backtest_runtime_hours(24), 24.0)
        with self.assertRaises(ValueError):
            enforce_backtest_runtime_hours(24.01)

    def test_new_forward_over_24_hours_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                enforce_forward_duration(25, "new.db", root=directory)

    def test_only_exact_existing_forward_can_use_grandfathering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            database = data / "existing.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE shadow_meta(key TEXT PRIMARY KEY,value TEXT)")
            meta = {
                "target_hours": 168.0,
                "experiment_started_at": "2026-08-10T00:02:18+00:00",
                "target_end_at": "2026-08-17T00:02:18+00:00",
                "orders_enabled": False,
                "money_real_enabled": False,
                "wallet_required": False,
            }
            connection.executemany(
                "INSERT INTO shadow_meta(key,value) VALUES(?,?)",
                [(key, json.dumps(value)) for key, value in meta.items()],
            )
            connection.commit()
            connection.close()
            policy_path = data / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "schema": "policy_experiment_duration_24h_1",
                        "maximum_new_experiment_hours": 24.0,
                        "maximum_backtest_runtime_hours": 24.0,
                        "grandfathered_experiments": [
                            {
                                "kind": "forward_shadow",
                                "relative_output_db": "data/existing.db",
                                "target_hours": 168.0,
                                "experiment_started_at": meta["experiment_started_at"],
                                "target_end_at": meta["target_end_at"],
                            }
                        ],
                        "real_money": "BLOQUEADO",
                    }
                ),
                encoding="utf-8",
            )

            decision = enforce_forward_duration(
                168,
                database,
                policy_path=policy_path,
                root=root,
            )

        self.assertEqual(decision["status"], "GRANDFATHERED_EXISTING_FORWARD")

    def test_run_shadow_default_is_24_hours(self):
        args = _parser().parse_args(
            [
                "run-shadow",
                "--model-file",
                "model.joblib",
                "--output-db",
                "shadow.db",
            ]
        )
        self.assertEqual(args.hours, 24.0)

    def test_runtime_guard_interrupts_at_its_configured_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "schema": "policy_experiment_duration_24h_1",
                        "maximum_new_experiment_hours": 24.0,
                        "maximum_backtest_runtime_hours": 24.0,
                        "grandfathered_experiments": [],
                        "real_money": "BLOQUEADO",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(BacktestRuntimeExceeded):
                with backtest_runtime_guard(
                    0.00001,
                    policy_path=policy_path,
                ):
                    time.sleep(0.2)


if __name__ == "__main__":
    unittest.main()
