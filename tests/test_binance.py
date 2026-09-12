import tempfile
import unittest
from pathlib import Path

from polymarket_bot.collectors.binance import build_binance_collector
from polymarket_bot.config import Settings
from polymarket_bot.domain import Sequence
from polymarket_bot.storage import AsyncEventWriter, SQLiteStore


class BinanceCollectorTests(unittest.TestCase):
    def test_public_direct_stream_needs_no_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "binance.db")
            store.open()
            writer = AsyncEventWriter(store)
            collector = build_binance_collector(
                Settings.from_env(), store, writer, Sequence()
            )
            try:
                self.assertEqual(collector.spec.source, "binance")
                self.assertEqual(collector.spec.default_stream, "aggTrade")
                self.assertIn(
                    "data-stream.binance.vision",
                    collector.spec.endpoint,
                )
                self.assertIsNone(collector.spec.subscription)
                self.assertIsNone(collector.spec.heartbeat_text)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
