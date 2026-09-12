from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v027_x_profile_express import (
    EXPRESS_SCHEMA,
    build_x_profile_express_diagnostic,
)


class V027XProfileExpressTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        database = root / "source.db"
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            CREATE TABLE shadow_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE shadow_markets(
              condition_id TEXT PRIMARY KEY,market_start_ms INTEGER,label TEXT,
              label_verified INTEGER
            );
            CREATE TABLE shadow_features(
              condition_id TEXT PRIMARY KEY,feature_json TEXT NOT NULL
            );
            CREATE TABLE shadow_twap_ticks(window_s INTEGER);
            """
        )
        meta = {
            "twap_window_seconds": 30,
            "rtds_topics": ["crypto_prices_twap_thirty"],
            "orders_enabled": False,
            "money_real_enabled": False,
            "wallet_required": False,
            "v026b_real_money": "BLOQUEADO",
        }
        connection.executemany(
            "INSERT INTO shadow_meta(key,value) VALUES(?,?)",
            [(key, json.dumps(value)) for key, value in meta.items()],
        )
        rows = [
            ("a", 1_787_241_000_000, "Up", 0.70, 0.71, 0.30),
            ("b", 1_787_244_600_000, "Down", 0.25, 0.26, 0.75),
            ("c", 1_787_248_200_000, "Up", 0.95, 0.96, 0.05),
        ]
        for condition_id, timestamp, label, implied, up_ask, down_ask in rows:
            connection.execute(
                "INSERT INTO shadow_markets VALUES(?,?,?,?)",
                (condition_id, timestamp, label, 1),
            )
            connection.execute(
                "INSERT INTO shadow_features VALUES(?,?)",
                (
                    condition_id,
                    json.dumps(
                        {
                            "implied_up_mid_probability": implied,
                            "up_best_ask": up_ask,
                            "down_best_ask": down_ask,
                            "volatility_regime_ratio": 0.5,
                        }
                    ),
                ),
            )
        connection.executemany(
            "INSERT INTO shadow_twap_ticks VALUES(?)", [(30,), (30,)]
        )
        connection.commit()
        connection.close()

        result = root / "result.json"
        result.write_text(
            json.dumps(
                {
                    "schema": "result_v026b_adaptive_checkpoints_1",
                    "database": str(database.resolve()),
                    "database_sha256": sha256_file(database),
                    "window": {
                        "experiment_started_at": "2026-08-20T16:00:00+00:00",
                        "target_end_at": "2026-08-21T16:00:00+00:00",
                    },
                }
            ),
            encoding="utf-8",
        )
        reconciliation = root / "reconciliation.json"
        reconciliation.write_text(
            json.dumps(
                {
                    "schema": "reconciliation_v026b_terminal_audit_1",
                    "corrected_verdict": "FAIL_REPLICATION",
                }
            ),
            encoding="utf-8",
        )
        return result, reconciliation, database

    def test_builds_read_only_diagnostic_and_detects_twap_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, reconciliation, database = self._fixture(root)
            before = sha256_file(database)
            output = root / "diagnostic.json"
            payload = build_x_profile_express_diagnostic(
                result_path=result,
                reconciliation_path=reconciliation,
                output_path=output,
            )
            self.assertEqual(payload["schema"], EXPRESS_SCHEMA)
            self.assertTrue(payload["contract_check"]["window_mismatch"])
            self.assertEqual(
                payload["application_decision"]["v027_status"],
                "BLOCKED_PENDING_VERSIONED_TWAP_60S_COMPATIBILITY",
            )
            self.assertEqual(
                payload["hypotheses"]["qwinsi_favorite_entry_060_088"]
                ["fill_price_band"]["full_window"]["trades"],
                1,
            )
            self.assertEqual(payload["safety"]["paper_orders"], 0)
            self.assertEqual(payload["safety"]["real_money"], "BLOQUEADO")
            self.assertEqual(sha256_file(database), before)
            self.assertTrue(output.is_file())

    def test_reuses_identical_output_without_rewriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, reconciliation, _ = self._fixture(root)
            output = root / "diagnostic.json"
            first = build_x_profile_express_diagnostic(
                result_path=result,
                reconciliation_path=reconciliation,
                output_path=output,
            )
            before = output.stat().st_mtime_ns
            second = build_x_profile_express_diagnostic(
                result_path=result,
                reconciliation_path=reconciliation,
                output_path=output,
            )
            self.assertEqual(first, second)
            self.assertEqual(output.stat().st_mtime_ns, before)

    def test_rejects_failed_safety_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, reconciliation, database = self._fixture(root)
            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE shadow_meta SET value='true' WHERE key='orders_enabled'"
            )
            connection.commit()
            connection.close()
            payload = json.loads(result.read_text(encoding="utf-8"))
            payload["database_sha256"] = sha256_file(database)
            result.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "seguridad"):
                build_x_profile_express_diagnostic(
                    result_path=result,
                    reconciliation_path=reconciliation,
                )

    def test_source_change_during_analysis_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, reconciliation, database = self._fixture(root)
            real_hash = sha256_file
            calls = 0

            def changing_hash(path: str | Path) -> str:
                nonlocal calls
                calls += 1
                digest = real_hash(path)
                if Path(path).resolve() == database.resolve() and calls > 3:
                    return "0" * 64
                return digest

            with patch(
                "polymarket_bot.v027_x_profile_express.sha256_file",
                side_effect=changing_hash,
            ):
                with self.assertRaisesRegex(RuntimeError, "evidencia"):
                    build_x_profile_express_diagnostic(
                        result_path=result,
                        reconciliation_path=reconciliation,
                    )


if __name__ == "__main__":
    unittest.main()
