import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from polymarket_bot import analysis24


def module_hash():
    return hashlib.sha256(Path(analysis24.__file__).read_bytes()).hexdigest()


class Analysis24Tests(unittest.TestCase):
    def test_incomplete_forward_reads_zero_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "shadow.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE shadow_meta(key TEXT PRIMARY KEY,value TEXT)")
            connection.execute(
                "INSERT INTO shadow_meta(key,value) VALUES(?,?)",
                ("target_end_at", json.dumps("2026-08-17T00:02:18+00:00")),
            )
            connection.commit()
            connection.close()

            result = analysis24.analyze_last_24h(database)

        self.assertEqual(result["status"], "WAITING_FULL_FORWARD_COMPLETION")
        self.assertEqual(result["outcomes_read"], 0)

    def test_completed_analysis_uses_exact_frozen_24h_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "shadow.db"
            prereg = root / "prereg.json"
            start = 1_800_000_000_000
            end = start + 86_400_000
            from datetime import datetime, timezone

            start_iso = datetime.fromtimestamp(start / 1000, timezone.utc).isoformat()
            end_iso = datetime.fromtimestamp(end / 1000, timezone.utc).isoformat()
            prereg.write_text(
                json.dumps(
                    {
                        "schema": "prereg_analisis_forward_ultimas24h_1",
                        "window_hours": 24.0,
                        "window_start_at": start_iso,
                        "window_end_at": end_iso,
                        "no_outcomes_before_full_completion": True,
                        "analysis_module_sha256": module_hash(),
                        "sources": {},
                        "gates": {
                            "minimum_trades": 20,
                            "maximum_brier_degradation": 0.01,
                            "minimum_market_coverage": 0.0,
                            "minimum_feature_coverage": 0.0,
                            "minimum_resolution_coverage": 0.0,
                        },
                        "real_money": "BLOQUEADO",
                    }
                ),
                encoding="utf-8",
            )
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE shadow_meta(key TEXT PRIMARY KEY,value TEXT)")
            connection.executemany(
                "INSERT INTO shadow_meta(key,value) VALUES(?,?)",
                [
                    ("target_end_at", json.dumps(end_iso)),
                    ("experiment_completed_at", json.dumps(end_iso)),
                ],
            )
            connection.execute(
                """CREATE TABLE shadow_markets(
                    condition_id TEXT PRIMARY KEY,
                    market_start_ms INTEGER,
                    feature_status TEXT,
                    label TEXT,
                    label_verified INTEGER
                )"""
            )
            connection.execute(
                """CREATE TABLE shadow_signals(
                    condition_id TEXT, model_name TEXT, probability_up REAL,
                    side TEXT, would_trade INTEGER, entry_cost REAL,
                    fill_price REAL, fee REAL
                )"""
            )
            rows = [
                ("before", start - 300_000, "SAVED", "Up", 1),
                ("inside-a", start, "SAVED", "Up", 1),
                ("inside-b", end - 300_000, "SAVED", "Down", 1),
                ("at-end", end, "SAVED", "Up", 1),
            ]
            connection.executemany(
                "INSERT INTO shadow_markets VALUES(?,?,?,?,?)", rows
            )
            connection.commit()
            connection.close()

            result = analysis24.analyze_last_24h(
                database,
                prereg_path=prereg,
            )

        self.assertEqual(result["status"], "ANALYZED_AFTER_FULL_COMPLETION")
        self.assertEqual(result["window_hours"], 24.0)
        self.assertEqual(result["markets"], 2)
        self.assertEqual(result["outcomes_read"], 2)
        self.assertFalse(result["diagnostic_candidate"])


if __name__ == "__main__":
    unittest.main()
