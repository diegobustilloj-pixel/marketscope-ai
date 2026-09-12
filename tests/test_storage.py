import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from polymarket_bot.domain import MarketDefinition, RawEvent
from polymarket_bot.storage import SQLiteStore


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(
            Path(self.temp_dir.name) / "phase1-test.db"
        )
        self.store.open()

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_event_id_is_idempotent(self) -> None:
        event = RawEvent.create(
            source="test",
            default_stream="unit",
            payload_raw='{"ok":true}',
            sequence=1,
            event_id="fixed-id",
        )
        first = self.store.append_events([event])
        second = self.store.append_events([event])
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(self.store.stats()["events"], 1)

    def test_integrity_verification(self) -> None:
        event = RawEvent.create(
            source="test",
            default_stream="unit",
            payload_raw='{"price":"0.50"}',
            sequence=2,
        )
        self.store.append_events([event])
        result = self.store.verify()
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["corrupt"], 0)
        self.assertEqual(result["checked_chunks"], 1)
        self.assertEqual(result["corrupt_chunks"], 0)

    def test_chunk_compression_preserves_every_payload(self) -> None:
        events = [
            RawEvent.create(
                source="clob",
                default_stream="price_change",
                payload_raw=(
                    '{"event_type":"price_change","market":"0xmarket",'
                    f'"asset_id":"up","price":"0.{index % 100:02d}",'
                    '"repeated":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}'
                ),
                sequence=index,
            )
            for index in range(1_000)
        ]
        inserted = self.store.append_events(events)
        stats = self.store.stats()
        replayed = list(self.store.iter_events())

        self.assertEqual(inserted, 1_000)
        self.assertEqual(stats["events"], 1_000)
        self.assertGreater(
            stats["storage"]["raw_bytes"],
            stats["storage"]["compressed_bytes"],
        )
        self.assertEqual(
            [event.payload_raw for event in replayed],
            [event.payload_raw for event in events],
        )

    def test_older_database_is_rejected_without_modification(self) -> None:
        for version in ("1", "2", "3"):
            with self.subTest(version=version):
                old_path = (
                    Path(self.temp_dir.name) / f"phase1-v{version}.db"
                )
                connection = sqlite3.connect(old_path)
                connection.execute(
                    "CREATE TABLE schema_meta "
                    "(key TEXT PRIMARY KEY, value TEXT)"
                )
                connection.execute(
                    "INSERT INTO schema_meta(key, value) "
                    "VALUES('schema_version', ?)",
                    (version,),
                )
                connection.commit()
                connection.close()

                old_store = SQLiteStore(old_path)
                with self.assertRaises(RuntimeError):
                    old_store.open()

                connection = sqlite3.connect(old_path)
                raw_chunks = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM sqlite_master
                    WHERE type='table' AND name='raw_chunks'
                    """
                ).fetchone()[0]
                connection.close()
                self.assertEqual(raw_chunks, 0)

    def test_corrupt_chunk_is_detected(self) -> None:
        event = RawEvent.create(
            source="test",
            default_stream="unit",
            payload_raw='{"price":"0.51"}',
            sequence=3,
        )
        self.store.append_events([event])
        self.store.connection.execute(
            "UPDATE raw_chunks SET payload_blob = ?", (b"not-zlib",)
        )
        self.store.connection.commit()

        result = self.store.verify()
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["corrupt"], 1)
        self.assertEqual(result["corrupt_chunks"], 1)

    def test_fast_verification_detects_blob_tampering(self) -> None:
        event = RawEvent.create(
            source="test",
            default_stream="unit",
            payload_raw='{"price":"0.52"}',
            sequence=4,
        )
        self.store.append_events([event])
        clean = self.store.verify_fast()
        self.assertTrue(clean["ok"])

        self.store.connection.execute(
            "UPDATE raw_chunks SET payload_blob = ?", (b"changed",)
        )
        self.store.connection.commit()
        damaged = self.store.verify_fast()
        self.assertFalse(damaged["ok"])
        self.assertEqual(damaged["corrupt_chunks"], 1)

    def test_audit_accepts_required_feeds_and_real_market(self) -> None:
        events = [
            RawEvent.create(
                source=source,
                default_stream=stream,
                payload_raw=f'{{"source":"{source}","stream":"{stream}"}}',
                sequence=index,
            )
            for index, (source, stream) in enumerate(
                (
                    ("clob", "price_change"),
                    ("gamma", "events_metadata"),
                    ("binance", "aggTrade"),
                    ("rtds", "crypto_prices_chainlink"),
                ),
                start=10,
            )
        ]
        self.store.append_events(events)
        self.store.save_market(
            MarketDefinition(
                slug="btc-updown-5m-1800000000",
                event_id="event-1",
                condition_id="0xreal",
                question="Bitcoin Up or Down",
                start_at="2027-01-15T08:00:00Z",
                end_at="2027-01-15T08:05:00Z",
                resolution_source="Chainlink",
                outcomes=("Up", "Down"),
                token_ids=("up", "down"),
                active=True,
                closed=False,
                accepting_orders=True,
                payload_raw='{"market":"real"}',
            )
        )

        result = self.store.audit(min_hours=0, min_coverage=1.0)
        self.assertTrue(result["passed"])
        self.assertTrue(all(result["observed"]["required_feeds"].values()))

    def test_wall_clock_rollback_is_reported_not_corruption(self) -> None:
        first = replace(
            RawEvent.create(
                source="test",
                default_stream="clock",
                payload_raw='{"tick":1}',
                sequence=100,
            ),
            received_at="2026-07-26T00:01:36.215439+00:00",
            monotonic_ns=1_000,
        )
        second = replace(
            RawEvent.create(
                source="test",
                default_stream="clock",
                payload_raw='{"tick":2}',
                sequence=101,
            ),
            received_at="2026-07-26T00:01:35.827166+00:00",
            monotonic_ns=2_000,
        )
        self.store.append_events((first, second))

        result = self.store.verify_fast()

        self.assertTrue(result["ok"])
        self.assertEqual(result["wall_clock_adjustments"], 1)
        self.assertEqual(result["monotonic_range_errors"], 0)
        self.assertAlmostEqual(
            result["max_clock_rollback_seconds"], 0.388273
        )


if __name__ == "__main__":
    unittest.main()
