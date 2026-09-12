import json
import unittest

from polymarket_bot.domain import (
    RawEvent,
    parse_message_metadata,
    payload_checksum,
)


class DomainTests(unittest.TestCase):
    def test_rtds_metadata(self) -> None:
        raw = json.dumps(
            {
                "topic": "crypto_prices_chainlink",
                "type": "update",
                "timestamp": 1782753357257,
                "payload": {
                    "symbol": "btc/usd",
                    "timestamp": 1782753357213,
                    "value": "67234.5",
                },
            }
        )
        stream, timestamp, market, token = parse_message_metadata(
            raw, "default"
        )
        self.assertEqual(stream, "crypto_prices_chainlink")
        self.assertEqual(timestamp, 1782753357213)
        self.assertIsNone(market)
        self.assertIsNone(token)

    def test_clob_metadata(self) -> None:
        raw = json.dumps(
            {
                "event_type": "best_bid_ask",
                "market": "0xmarket",
                "asset_id": "123",
                "timestamp": "1782753357257",
            }
        )
        stream, timestamp, market, token = parse_message_metadata(
            raw, "market"
        )
        self.assertEqual(stream, "best_bid_ask")
        self.assertEqual(timestamp, 1782753357257)
        self.assertEqual(market, "0xmarket")
        self.assertEqual(token, "123")

    def test_binance_aggregate_trade_metadata(self) -> None:
        raw = json.dumps(
            {
                "e": "aggTrade",
                "E": 1782753357257,
                "s": "BTCUSDT",
                "a": 987654,
                "p": "67235.10",
                "q": "0.012",
                "T": 1782753357214,
                "m": False,
            }
        )
        stream, timestamp, market, token = parse_message_metadata(
            raw, "binance"
        )
        self.assertEqual(stream, "aggTrade")
        self.assertEqual(timestamp, 1782753357214)
        self.assertIsNone(market)
        self.assertIsNone(token)

    def test_checksum_is_reproducible(self) -> None:
        value = payload_checksum("rtds", "prices", '{"x":1}')
        self.assertEqual(value, payload_checksum("rtds", "prices", '{"x":1}'))
        self.assertNotEqual(
            value, payload_checksum("rtds", "prices", '{"x":2}')
        )

    def test_event_preserves_raw_payload(self) -> None:
        raw = '{"value":"0.5500"}'
        event = RawEvent.create(
            source="test",
            default_stream="raw",
            payload_raw=raw,
            sequence=1,
        )
        self.assertEqual(event.payload_raw, raw)
        self.assertEqual(
            event.checksum,
            payload_checksum(event.source, event.stream, raw),
        )
        self.assertGreater(event.monotonic_ns, 0)

    def test_payload_market_overrides_collector_context(self) -> None:
        raw = json.dumps(
            {
                "event_type": "new_market",
                "market": "0xactual-payload-market",
                "asset_id": "actual-token",
            }
        )
        event = RawEvent.create(
            source="clob",
            default_stream="market",
            payload_raw=raw,
            sequence=2,
            market_id="0xcollector-context",
            token_id="context-token",
        )
        self.assertEqual(event.market_id, "0xactual-payload-market")
        self.assertEqual(event.token_id, "actual-token")


if __name__ == "__main__":
    unittest.main()
