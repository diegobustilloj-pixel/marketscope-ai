import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from polymarket_bot.domain import MarketDefinition, RawEvent
from polymarket_bot.phase2 import (
    GammaResolutionClient,
    ResolutionInfo,
    SilverMarket,
    build_silver_dataset,
    enrich_resolutions,
    silver_status,
)
from polymarket_bot.storage import SQLiteStore


class StubResolutionClient:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, slug: str) -> ResolutionInfo:
        self.calls += 1
        return ResolutionInfo(
            label="Down",
            source="gamma_outcome_prices",
            verified=True,
            gamma_market_id="market-1",
            closed=True,
            status="resolved",
            price_to_beat=64_200.0,
            outcome_prices=(0.0, 1.0),
            rule="Up si final >= inicial; de lo contrario Down.",
            payload_raw=(
                '{"id":"market-1","closed":true,'
                '"outcomes":["Up","Down"],'
                '"outcomePrices":["0","1"],'
                '"umaResolutionStatus":"resolved"}'
            ),
        )


class Phase2Tests(unittest.TestCase):
    def test_gamma_resolution_parser(self) -> None:
        payload = json.dumps(
            {
                "id": "3071553",
                "closed": True,
                "outcomes": '["Up","Down"]',
                "outcomePrices": '["0","1"]',
                "umaResolutionStatus": "resolved",
                "description": "Regla verificable.",
                "events": [
                    {"eventMetadata": {"priceToBeat": 64191.387}}
                ],
            }
        )
        result = GammaResolutionClient.parse(payload)
        self.assertTrue(result.verified)
        self.assertEqual(result.label, "Down")
        self.assertEqual(result.outcome_prices, (0.0, 1.0))
        self.assertAlmostEqual(result.price_to_beat or 0, 64191.387)

    def test_gamma_resolution_parser_reads_top_level_event_metadata(self) -> None:
        payload = json.dumps(
            {
                "id": "3071553",
                "closed": False,
                "outcomes": '["Up","Down"]',
                "outcomePrices": '["0.5","0.5"]',
                "eventMetadata": {"priceToBeat": "65001.25"},
            }
        )
        result = GammaResolutionClient.parse(payload)
        self.assertFalse(result.verified)
        self.assertAlmostEqual(result.price_to_beat or 0, 65001.25)

    def test_verified_gamma_labels_are_reused_from_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "labels.json"
            market = SilverMarket(
                condition_id="0xmarket",
                slug="btc-updown-5m-1784924700",
                event_id="event-1",
                question="Bitcoin Up or Down",
                start_ms=1_784_924_700_000,
                end_ms=1_784_925_000_000,
                up_token_id="up-token",
                down_token_id="down-token",
                resolution_source="Chainlink BTC/USD",
            )
            first_client = StubResolutionClient()
            first = enrich_resolutions(
                [market],
                fetch_labels=True,
                client=first_client,
                cache_path=cache,
            )
            second_client = StubResolutionClient()
            second = enrich_resolutions(
                [market],
                fetch_labels=True,
                client=second_client,
                cache_path=cache,
            )

            self.assertEqual(first_client.calls, 1)
            self.assertEqual(second_client.calls, 0)
            self.assertTrue(first[0].resolution.verified)
            self.assertTrue(second[0].resolution.verified)

    def test_build_silver_from_v4_without_modifying_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw-v4.db"
            output = root / "silver.db"
            store = SQLiteStore(source)
            store.open()
            start_seconds = 1_784_924_700
            condition = "0xmarket"
            up_token = "up-token"
            down_token = "down-token"
            store.save_market(
                MarketDefinition(
                    slug=f"btc-updown-5m-{start_seconds}",
                    event_id="event-1",
                    condition_id=condition,
                    question="Bitcoin Up or Down",
                    start_at=None,
                    end_at=None,
                    resolution_source="Chainlink BTC/USD",
                    outcomes=("Up", "Down"),
                    token_ids=(up_token, down_token),
                    active=True,
                    closed=False,
                    accepting_orders=True,
                    payload_raw='{"market":"synthetic-test"}',
                )
            )
            timestamp_ms = start_seconds * 1000 + 1_000
            received_at = datetime.fromtimestamp(
                timestamp_ms / 1000, timezone.utc
            ).isoformat(timespec="microseconds")
            payloads = [
                (
                    "rtds",
                    "crypto_prices_chainlink",
                    {
                        "topic": "crypto_prices_chainlink",
                        "payload": {
                            "symbol": "btc/usd",
                            "timestamp": timestamp_ms,
                            "value": 64_199.0,
                        },
                    },
                ),
                (
                    "binance",
                    "aggTrade",
                    {
                        "e": "aggTrade",
                        "T": timestamp_ms,
                        "p": "64199.25",
                        "q": "0.10",
                    },
                ),
                (
                    "clob",
                    "book",
                    [
                        {
                            "event_type": "book",
                            "market": condition,
                            "asset_id": up_token,
                            "timestamp": str(timestamp_ms),
                            "bids": [{"price": "0.40", "size": "100"}],
                            "asks": [{"price": "0.42", "size": "90"}],
                        },
                        {
                            "event_type": "book",
                            "market": condition,
                            "asset_id": down_token,
                            "timestamp": str(timestamp_ms),
                            "bids": [{"price": "0.58", "size": "80"}],
                            "asks": [{"price": "0.60", "size": "70"}],
                        },
                    ],
                ),
                (
                    "clob",
                    "last_trade_price",
                    {
                        "event_type": "last_trade_price",
                        "market": condition,
                        "asset_id": up_token,
                        "timestamp": str(timestamp_ms),
                        "price": "0.41",
                        "size": "12",
                    },
                ),
            ]
            events = []
            for sequence, (source_name, stream, payload) in enumerate(
                payloads, start=1
            ):
                event = RawEvent.create(
                    source=source_name,
                    default_stream=stream,
                    payload_raw=json.dumps(
                        payload, separators=(",", ":")
                    ),
                    sequence=sequence,
                )
                events.append(
                    replace(
                        event,
                        received_at=received_at,
                        monotonic_ns=sequence,
                    )
                )
            store.append_events(events)
            store.close()
            source_size = source.stat().st_size

            result = build_silver_dataset(
                source_db=source,
                output_db=output,
                max_markets=1,
                fetch_labels=True,
                resolution_client=StubResolutionClient(),
            )
            status = silver_status(output)

            self.assertTrue(result["passed"])
            self.assertEqual(source.stat().st_size, source_size)
            self.assertEqual(result["labels_verified"], 1)
            self.assertEqual(result["second_rows"], 300)
            self.assertGreater(result["core_coverage"], 0.99)
            self.assertGreater(result["binance_coverage"], 0.99)
            self.assertTrue(result["output_committed"])
            self.assertEqual(result["training_eligible_markets"], 1)
            self.assertEqual(result["training_excluded_markets"], [])
            self.assertFalse(
                output.with_name(f"{output.name}.partial").exists()
            )
            self.assertEqual(status["sqlite_quick_check"], "ok")
            self.assertEqual(status["verified_labels"], 1)
            self.assertEqual(status["training_eligible_markets"], 1)


if __name__ == "__main__":
    unittest.main()
