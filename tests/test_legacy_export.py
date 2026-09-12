import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from polymarket_bot.legacy_export import export_v3_dataset


class LegacyExportTests(unittest.TestCase):
    def test_v3_export_is_small_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "capture-v3.db"
            output = root / "sample.zip"
            connection = sqlite3.connect(source)
            connection.executescript(
                """
                CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE event_counts(
                    source TEXT, stream TEXT, event_count INTEGER
                );
                CREATE TABLE raw_chunks(
                    codec TEXT, payload_blob BLOB,
                    first_sequence INTEGER, event_count INTEGER,
                    raw_bytes INTEGER, compressed_bytes INTEGER,
                    first_received_at TEXT, last_received_at TEXT
                );
                CREATE TABLE markets(
                    condition_id TEXT, slug TEXT, event_id TEXT,
                    question TEXT, start_at TEXT, end_at TEXT,
                    resolution_source TEXT, outcomes_json TEXT,
                    token_ids_json TEXT, active INTEGER, closed INTEGER,
                    accepting_orders INTEGER, first_seen_at TEXT,
                    last_seen_at TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO schema_meta VALUES('schema_version', '3')"
            )
            connection.execute(
                """
                INSERT INTO event_counts
                VALUES('rtds','crypto_prices_chainlink',1)
                """
            )
            envelope = {
                "source": "rtds",
                "stream": "crypto_prices_chainlink",
                "payload_raw": (
                    '{"topic":"crypto_prices_chainlink","value":"67000.00"}'
                ),
                "sequence": 1,
            }
            raw = (
                json.dumps(envelope, separators=(",", ":")) + "\n"
            ).encode()
            compressed = zlib.compress(raw)
            connection.execute(
                "INSERT INTO raw_chunks VALUES(?,?,?,?,?,?,?,?)",
                (
                    "zlib-jsonl-v1",
                    compressed,
                    1,
                    1,
                    len(raw),
                    len(compressed),
                    "2026-07-24T00:00:00+00:00",
                    "2026-07-24T00:00:01+00:00",
                ),
            )
            connection.execute(
                """
                INSERT INTO markets VALUES(
                    '0x1', 'btc-updown-5m-1', 'event-1',
                    'Bitcoin Up or Down', '2026-07-24T00:00:00Z',
                    '2026-07-24T00:05:00Z', 'Chainlink',
                    '["Up","Down"]', '["up","down"]',
                    1, 0, 1,
                    '2026-07-24T00:00:00Z',
                    '2026-07-24T00:00:01Z'
                )
                """
            )
            connection.commit()
            connection.close()
            before = hashlib.sha256(source.read_bytes()).hexdigest()

            result = export_v3_dataset(
                source_db=source,
                output_zip=output,
                samples_per_stream=1,
            )

            after = hashlib.sha256(source.read_bytes()).hexdigest()
            self.assertTrue(result["ok"])
            self.assertEqual(before, after)
            self.assertTrue(result["source_database_unchanged"])
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {"manifest.json", "markets.jsonl", "samples.jsonl"},
                )
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(
                    manifest["source_database"]["schema_version"], 3
                )
                self.assertEqual(
                    manifest["sample"]["captured"][0]["count"], 1
                )


if __name__ == "__main__":
    unittest.main()
