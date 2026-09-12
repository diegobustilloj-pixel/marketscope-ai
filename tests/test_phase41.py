import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import joblib

from polymarket_bot.config import Settings
from polymarket_bot.discovery import DiscoveryError
from polymarket_bot.phase2 import ResolutionInfo, SilverMarket
from polymarket_bot.phase3 import GoldMarket, _feature_row
from polymarket_bot.phase4 import PHASE4_DDL
from polymarket_bot.phase41 import (
    SHADOW_HYPOTHESES,
    SHADOW_LEGACY_STRIKE_MODEL_ENABLED,
    SHADOW_TWAP_MAX_AGE_MS,
    SHADOW_TWAP_TOPIC,
    LiveShadowState,
    ShadowStore,
    _build_live_feature,
    _event_metadata_details,
    _fetch_gamma_json_sync,
    _fetch_market_sync,
    _materialize_rows,
    _is_fresh_twap_message,
    _websocket_feed,
    audit_shadow_forward,
    prepare_shadow_models,
    shadow_status,
)
from tests.test_phase4 import _create_gold


def _create_phase4(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(PHASE4_DDL)
    values = {
        "schema_version": "1",
        "test_accessed": False,
        "strategy_selected_on_validation": False,
        "phase4_contract_sha256": "synthetic-phase4",
    }
    connection.executemany(
        "INSERT INTO phase4_meta(key,value) VALUES(?,?)",
        [
            (key, json.dumps(value))
            for key, value in values.items()
        ],
    )
    connection.commit()
    connection.close()


class Phase41Tests(unittest.TestCase):
    @staticmethod
    def _settings(root: Path) -> Settings:
        return Settings(
            db_path=root / "unused.db",
            gamma_base_url="https://gamma.example",
            clob_ws_url="wss://clob.example",
            rtds_ws_url="wss://rtds.example",
            binance_ws_url="wss://binance.example",
            http_timeout_seconds=1.0,
            reconnect_max_seconds=2.0,
            ws_use_proxy=False,
            log_level="INFO",
        )

    def test_event_metadata_reads_top_level_strike_and_final_price(self) -> None:
        result = _event_metadata_details(
            {
                "eventMetadata": {
                    "priceToBeat": "101234.5",
                    "finalPrice": "101999.25",
                }
            }
        )
        self.assertEqual(result["strike_fetch_status"], "FOUND")
        self.assertEqual(
            result["strike_source"],
            "gamma_event.eventMetadata",
        )
        self.assertAlmostEqual(result["price_to_beat"], 101234.5)
        self.assertAlmostEqual(result["final_price"], 101999.25)

    def test_event_metadata_reads_nested_market_strike(self) -> None:
        result = _event_metadata_details(
            {
                "events": [
                    {
                        "eventMetadata": {
                            "priceToBeat": 99000,
                        }
                    }
                ]
            }
        )
        self.assertEqual(result["strike_fetch_status"], "FOUND")
        self.assertEqual(
            result["strike_source"],
            "gamma_market.events[0].eventMetadata",
        )
        self.assertEqual(result["price_to_beat"], 99000.0)

    def test_event_metadata_marks_invalid_strike(self) -> None:
        result = _event_metadata_details(
            {"eventMetadata": {"priceToBeat": "not-a-number"}}
        )
        self.assertEqual(
            result["strike_fetch_status"],
            "INVALID_PRICE_TO_BEAT",
        )
        self.assertIsNone(result["price_to_beat"])

    def test_gamma_timeout_is_recoverable_discovery_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            with patch(
                "polymarket_bot.phase41.urllib.request.urlopen",
                side_effect=TimeoutError("read timed out"),
            ):
                with self.assertRaises(DiscoveryError):
                    _fetch_gamma_json_sync(settings, "/events/slug/test")

    def test_shadow_discovery_prefers_event_strike(self) -> None:
        class Response:
            def __init__(self, raw: str) -> None:
                self.raw = raw

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return self.raw.encode("utf-8")

        payload = {
            "id": "event-42",
            "slug": "btc-updown-5m-1800000000",
            "title": "Bitcoin Up or Down",
            "resolutionSource": "Chainlink BTC/USD",
            "eventMetadata": {"priceToBeat": "100123.75"},
            "markets": [
                {
                    "conditionId": "condition-42",
                    "slug": "btc-updown-5m-1800000000",
                    "question": "Bitcoin Up or Down",
                    "outcomes": '["Up","Down"]',
                    "clobTokenIds": '["up","down"]',
                    "active": True,
                    "closed": False,
                    "acceptingOrders": True,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            with patch(
                "polymarket_bot.phase41.urllib.request.urlopen",
                return_value=Response(json.dumps(payload)),
            ) as mocked:
                market, resolution, metadata = _fetch_market_sync(
                    settings,
                    "btc-updown-5m-1800000000",
                )
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(market.event_id, "event-42")
        self.assertAlmostEqual(resolution.price_to_beat or 0, 100123.75)
        self.assertEqual(metadata["strike_fetch_status"], "FOUND")

    def test_reconcile_recovers_running_and_pending_market(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "shadow.db"
            store = ShadowStore(database)
            artifact = {
                "created_at": "2026-08-07T12:00:00+00:00",
                "hypotheses": list(SHADOW_HYPOTHESES),
                "fee_rate": 0.07,
                "slippage_per_share": 0.005,
                "gold_contract_sha256": "gold",
                "phase4_contract_sha256": "phase4",
            }
            store.open(target_hours=1.0, artifact=artifact)
            past = SilverMarket(
                condition_id="past-condition",
                slug="btc-updown-5m-1800000000",
                event_id="past-event",
                question="BTC Up or Down",
                start_ms=1_800_000_000_000,
                end_ms=1_800_000_300_000,
                up_token_id="up-past",
                down_token_id="down-past",
                resolution_source="Chainlink",
            )
            future = SilverMarket(
                condition_id="future-condition",
                slug="btc-updown-5m-1800000300",
                event_id="future-event",
                question="BTC Up or Down",
                start_ms=1_800_000_300_000,
                end_ms=1_800_000_600_000,
                up_token_id="up-future",
                down_token_id="down-future",
                resolution_source="Chainlink",
            )
            store.save_market(past)
            store.save_market(future)
            run_id = store.start_run()
            result = store.reconcile_interrupted_state(
                now_ms=1_800_000_100_000,
            )
            self.assertEqual(result["stale_runs_recovered"], 1)
            self.assertEqual(result["stale_features_recovered"], 1)
            run = store.db.execute(
                "SELECT status,error FROM shadow_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            self.assertEqual(run["status"], "INTERRUPTED")
            self.assertEqual(run["error"], "RECOVERED_STALE_RUNNING")
            statuses = {
                row["condition_id"]: row["feature_status"]
                for row in store.db.execute(
                    "SELECT condition_id,feature_status FROM shadow_markets"
                )
            }
            self.assertEqual(statuses["past-condition"], "FAILED_INTERRUPTED")
            self.assertEqual(statuses["future-condition"], "PENDING")
            store.close()

    def test_diagnostic_features_are_separate_from_official_feature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ShadowStore(root / "shadow.db")
            artifact = {
                "created_at": "2026-08-07T12:00:00+00:00",
                "hypotheses": list(SHADOW_HYPOTHESES),
                "fee_rate": 0.07,
                "slippage_per_share": 0.005,
                "gold_contract_sha256": "gold",
                "phase4_contract_sha256": "phase4",
            }
            store.open(target_hours=1.0, artifact=artifact)
            market = SilverMarket(
                condition_id="diagnostic-condition",
                slug="btc-updown-5m-1800000000",
                event_id="diagnostic-event",
                question="BTC Up or Down",
                start_ms=1_800_000_000_000,
                end_ms=1_800_000_300_000,
                up_token_id="up",
                down_token_id="down",
                resolution_source="Chainlink",
            )
            store.save_market(market)
            feature = {
                "decision_timestamp_ms": 1_800_000_180_000,
                "horizon_seconds": 120,
                "decision_quality_flags": 0,
            }
            store.save_diagnostic_feature(
                condition_id=market.condition_id,
                horizon_seconds=120,
                feature=feature,
            )
            self.assertEqual(
                store.db.execute(
                    "SELECT COUNT(*) FROM shadow_diagnostics"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                store.db.execute(
                    "SELECT COUNT(*) FROM shadow_features"
                ).fetchone()[0],
                0,
            )
            store.close()

    def test_live_state_builds_same_sixty_second_feature(self) -> None:
        start_ms = 1_800_000_000_000
        market = SilverMarket(
            condition_id="condition-live",
            slug="btc-updown-5m-1800000000",
            event_id="event-live",
            question="BTC Up or Down",
            start_ms=start_ms,
            end_ms=start_ms + 300_000,
            up_token_id="up",
            down_token_id="down",
            resolution_source="Chainlink",
            resolution=ResolutionInfo(price_to_beat=100_000.0),
        )
        state = LiveShadowState()
        state.set_market(market)
        for token, bid, ask in (
            ("up", "0.54", "0.55"),
            ("down", "0.44", "0.45"),
        ):
            state.ingest(
                source="clob",
                default_stream="market",
                raw=json.dumps(
                    {
                        "event_type": "book",
                        "market": market.condition_id,
                        "asset_id": token,
                        "timestamp": str(start_ms),
                        "bids": [{"price": bid, "size": "100"}],
                        "asks": [{"price": ask, "size": "90"}],
                    }
                ),
            )
        for second in range(241):
            timestamp = start_ms + second * 1000
            state.ingest(
                source="rtds",
                default_stream="crypto_price",
                raw=json.dumps(
                    {
                        "topic": "crypto_prices_chainlink",
                        "timestamp": timestamp,
                        "payload": {
                            "symbol": "btc/usd",
                            "timestamp": timestamp,
                            "value": str(100_000 + second),
                        },
                    }
                ),
            )
            state.ingest(
                source="binance",
                default_stream="aggTrade",
                raw=json.dumps(
                    {
                        "e": "aggTrade",
                        "E": timestamp,
                        "T": timestamp,
                        "p": str(100_001 + second),
                        "q": "0.1",
                    }
                ),
            )
        builder = state.builder
        self.assertIsNotNone(builder)
        assert builder is not None
        accumulator = builder.accumulators[market.condition_id]
        rows = _materialize_rows(accumulator, through_second=240)
        feature, reason = _feature_row(
            market=GoldMarket(
                source_dataset="shadow",
                source_schema_version=4,
                condition_id=market.condition_id,
                slug=market.slug,
                start_ms=market.start_ms,
                end_ms=market.end_ms,
                label="Down",
                price_to_beat=100_000.0,
                core_coverage=1.0,
                binance_coverage=1.0,
                split="forward",
            ),
            rows=rows,  # type: ignore[arg-type]
            horizon=60,
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(feature)
        assert feature is not None
        self.assertEqual(feature["horizon_seconds"], 60)
        self.assertEqual(feature["decision_quality_flags"], 0)
        self.assertAlmostEqual(
            feature["implied_up_mid_probability"],
            0.545 / 0.99,
        )

    def test_twap_does_not_overwrite_chainlink_spot(self) -> None:
        state = LiveShadowState()
        timestamp = 1_800_000_000_000
        state.ingest(
            source="rtds",
            default_stream="crypto_price",
            raw=json.dumps(
                {
                    "topic": "crypto_prices_chainlink",
                    "timestamp": timestamp,
                    "payload": {
                        "symbol": "btc/usd",
                        "timestamp": timestamp,
                        "value": 100000.0,
                    },
                }
            ),
        )
        state.ingest(
            source="rtds",
            default_stream="crypto_price",
            raw=json.dumps(
                {
                    "topic": SHADOW_TWAP_TOPIC,
                    "timestamp": timestamp,
                    "payload": {
                        "symbol": "btc/usd",
                        "timestamp": timestamp,
                        "value": 99995.0,
                        "full_accuracy_value": "99995000000000000000000",
                        "window_s": 30,
                    },
                }
            ),
        )
        self.assertEqual(state.latest_chainlink, (100000.0, timestamp))
        self.assertIsNotNone(state.latest_twap)
        assert state.latest_twap is not None
        self.assertEqual(state.latest_twap[0], 99995.0)
        self.assertEqual(state.latest_twap[2], 30)


    def test_twap_watchdog_predicate_rejects_pong_and_accepts_valid_update(self) -> None:
        self.assertFalse(_is_fresh_twap_message("PONG"))
        self.assertFalse(
            _is_fresh_twap_message(
                json.dumps(
                    {
                        "topic": "crypto_prices_chainlink",
                        "payload": {
                            "symbol": "btc/usd",
                            "timestamp": 1_800_000_000_000,
                            "value": 100000.0,
                        },
                    }
                )
            )
        )
        self.assertTrue(
            _is_fresh_twap_message(
                json.dumps(
                    {
                        "topic": SHADOW_TWAP_TOPIC,
                        "payload": {
                            "symbol": "btc/usd",
                            "timestamp": 1_800_000_000_000,
                            "window_s": 30,
                            "value": 100000.0,
                        },
                    }
                )
            )
        )

    def test_websocket_watchdog_reconnects_connected_but_stale_feed(self) -> None:
        class FakeWebsocket:
            def __init__(self, messages: list[str]) -> None:
                self.messages = list(messages)
                self.closed = False
                self.sent: list[str] = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                self.closed = True
                return False

            async def send(self, text: str) -> None:
                self.sent.append(text)

            async def close(self) -> None:
                self.closed = True

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.closed:
                    raise StopAsyncIteration
                await asyncio.sleep(0.005)
                if self.messages:
                    return self.messages.pop(0)
                return "PONG"

        stale = FakeWebsocket([])
        valid_message = json.dumps(
            {
                "topic": SHADOW_TWAP_TOPIC,
                "payload": {
                    "symbol": "btc/usd",
                    "timestamp": 1_800_000_000_000,
                    "window_s": 30,
                    "value": 100000.0,
                },
            }
        )
        recovered = FakeWebsocket([valid_message])
        sockets = [stale, recovered]
        stop_event = asyncio.Event()
        state = LiveShadowState()
        seen: list[str] = []

        def fake_connect(*args, **kwargs):
            if not sockets:
                raise AssertionError("watchdog no debio abrir mas de dos sockets")
            return sockets.pop(0)

        async def handler(raw: str) -> None:
            seen.append(raw)
            if _is_fresh_twap_message(raw):
                stop_event.set()

        async def scenario() -> None:
            with patch("polymarket_bot.phase41.connect", side_effect=fake_connect):
                await asyncio.wait_for(
                    _websocket_feed(
                        name="shadow-rtds",
                        endpoint="wss://example",
                        subscription={"action": "subscribe"},
                        heartbeat_text=None,
                        heartbeat_seconds=None,
                        use_proxy=False,
                        reconnect_max_seconds=0.01,
                        stop_event=stop_event,
                        state=state,
                        handler=handler,
                        freshness_timeout_seconds=0.05,
                        freshness_predicate=_is_fresh_twap_message,
                    ),
                    timeout=2.0,
                )

        asyncio.run(scenario())
        self.assertTrue(stale.closed)
        self.assertGreaterEqual(state.counters["rtds:stale_watchdog"], 1)
        self.assertGreaterEqual(state.counters["rtds:stale_reconnects"], 1)
        self.assertTrue(any(_is_fresh_twap_message(raw) for raw in seen))

    def test_twap_history_prevents_future_leakage(self) -> None:
        state = LiveShadowState()
        state.twap_history.append((100.0, 1_000, 30, "100"))
        state.twap_history.append((101.0, 2_000, 30, "101"))
        self.assertIsNone(state.twap_at_or_before(999))
        self.assertEqual(state.twap_at_or_before(1_500), (100.0, 1_000, 30, "100"))
        self.assertEqual(state.twap_at_or_before(2_000), (101.0, 2_000, 30, "101"))

    def test_shadow_store_persists_twap_and_status_reports_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "shadow.db"
            store = ShadowStore(database)
            artifact = {
                "created_at": "2026-08-07T12:00:00+00:00",
                "hypotheses": list(SHADOW_HYPOTHESES),
                "fee_rate": 0.07,
                "slippage_per_share": 0.005,
                "gold_contract_sha256": "gold",
                "phase4_contract_sha256": "phase4",
            }
            store.open(target_hours=1.0, artifact=artifact)
            market = SilverMarket(
                condition_id="twap-condition",
                slug="btc-updown-5m-1800000000",
                event_id="twap-event",
                question="BTC Up or Down",
                start_ms=1_800_000_000_000,
                end_ms=1_800_000_300_000,
                up_token_id="up",
                down_token_id="down",
                resolution_source="Chainlink",
            )
            store.save_market(market)
            self.assertTrue(
                store.save_twap_tick(
                    source_timestamp_ms=1_800_000_001_000,
                    value=100123.5,
                    window_s=30,
                    full_accuracy_value="100123500000000000000000",
                    message_timestamp_ms=1_800_000_001_005,
                )
            )
            self.assertFalse(
                store.save_twap_tick(
                    source_timestamp_ms=1_800_000_001_000,
                    value=100123.5,
                    window_s=30,
                )
            )
            store.close()
            status = shadow_status(database)
            self.assertEqual(status["twap_updates"], 1)
            self.assertEqual(status["markets_with_twap_coverage"], 1)
            self.assertEqual(status["twap_window_seconds_min"], 30)
            self.assertEqual(status["twap_window_seconds_max"], 30)

    def test_live_feature_contains_twap_diagnostics(self) -> None:
        start_ms = 1_800_000_000_000
        market = SilverMarket(
            condition_id="condition-twap-feature",
            slug="btc-updown-5m-1800000000",
            event_id="event-twap-feature",
            question="BTC Up or Down",
            start_ms=start_ms,
            end_ms=start_ms + 300_000,
            up_token_id="up",
            down_token_id="down",
            resolution_source="Chainlink",
        )
        state = LiveShadowState()
        state.set_market(market)
        for token, bid, ask in (
            ("up", "0.54", "0.55"),
            ("down", "0.44", "0.45"),
        ):
            state.ingest(
                source="clob",
                default_stream="market",
                raw=json.dumps(
                    {
                        "event_type": "book",
                        "market": market.condition_id,
                        "asset_id": token,
                        "timestamp": str(start_ms),
                        "bids": [{"price": bid, "size": "100"}],
                        "asks": [{"price": ask, "size": "90"}],
                    }
                ),
            )
        for second in range(241):
            timestamp = start_ms + second * 1000
            state.ingest(
                source="rtds",
                default_stream="crypto_price",
                raw=json.dumps(
                    {
                        "topic": "crypto_prices_chainlink",
                        "timestamp": timestamp,
                        "payload": {
                            "symbol": "btc/usd",
                            "timestamp": timestamp,
                            "value": 100000.0 + second,
                        },
                    }
                ),
            )
            state.ingest(
                source="binance",
                default_stream="aggTrade",
                raw=json.dumps(
                    {
                        "e": "aggTrade",
                        "E": timestamp,
                        "T": timestamp,
                        "p": 100001.0 + second,
                        "q": "0.1",
                    }
                ),
            )
            state.ingest(
                source="rtds",
                default_stream="crypto_price",
                raw=json.dumps(
                    {
                        "topic": SHADOW_TWAP_TOPIC,
                        "timestamp": timestamp,
                        "payload": {
                            "symbol": "btc/usd",
                            "timestamp": timestamp,
                            "value": 99999.0 + second,
                            "full_accuracy_value": str(99999000 + second),
                            "window_s": 30,
                        },
                    }
                ),
            )
        feature, reason = _build_live_feature(
            state=state,
            market=market,
            horizon_seconds=60,
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(feature)
        assert feature is not None
        self.assertEqual(feature["twap_30s_available"], 1)
        self.assertEqual(feature["twap_30s_timestamp_ms"], start_ms + 240_000)
        self.assertEqual(feature["twap_30s_age_ms"], 0)
        self.assertEqual(feature["twap_30s_fresh"], 1)
        self.assertEqual(feature["twap_30s_max_age_ms"], SHADOW_TWAP_MAX_AGE_MS)
        self.assertEqual(feature["twap_30s_window_s"], 30)
        self.assertAlmostEqual(feature["twap_30s_price"], 100239.0)
        self.assertEqual(feature["twap_open_available"], 1)
        self.assertEqual(feature["twap_open_timestamp_ms"], start_ms)
        self.assertEqual(feature["twap_open_age_ms"], 0)
        self.assertEqual(feature["twap_open_fresh"], 1)
        self.assertAlmostEqual(feature["twap_open_price"], 99999.0)
        self.assertAlmostEqual(
            feature["twap_distance_to_open_bps"],
            (100239.0 / 99999.0 - 1.0) * 10000.0,
        )

        # A reconnect gap may leave an old TWAP in memory. Preserve it for
        # diagnostics, but mark it stale so future TWAP models can reject it.
        state.twap_history.clear()
        state.twap_history.append(
            (
                99975.0,
                start_ms + 216_000,
                30,
                "99975000",
            )
        )
        stale_feature, stale_reason = _build_live_feature(
            state=state,
            market=market,
            horizon_seconds=60,
        )
        self.assertIsNone(stale_reason)
        self.assertIsNotNone(stale_feature)
        assert stale_feature is not None
        self.assertEqual(stale_feature["twap_30s_available"], 1)
        self.assertEqual(stale_feature["twap_30s_age_ms"], 24_000)
        self.assertEqual(stale_feature["twap_30s_fresh"], 0)

    def test_legacy_strike_model_is_explicitly_disabled_for_twap_regime(self) -> None:
        self.assertFalse(SHADOW_LEGACY_STRIKE_MODEL_ENABLED)

    def test_prepare_shadow_freezes_transfer_candidate_without_opening_test(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gold = root / "gold.db"
            phase4 = root / "phase4.db"
            model = root / "shadow.joblib"
            _create_gold(gold, markets=400, predictive=True)
            _create_phase4(phase4)
            gold_size = gold.stat().st_size
            phase4_size = phase4.stat().st_size

            result = prepare_shadow_models(
                gold_db=gold,
                phase4_db=phase4,
                output_model=model,
            )
            artifact = joblib.load(model)

            self.assertTrue(result["passed"])
            self.assertFalse(result["historical_test_accessed"])
            self.assertFalse(result["orders_enabled"])
            self.assertEqual(
                set(artifact["models"]),
                {
                    str(item["model_name"])
                    for item in SHADOW_HYPOTHESES
                },
            )
            self.assertFalse(artifact["orders_enabled"])
            self.assertEqual(gold.stat().st_size, gold_size)
            self.assertEqual(phase4.stat().st_size, phase4_size)
            with self.assertRaises(ValueError):
                prepare_shadow_models(
                    gold_db=gold,
                    phase4_db=phase4,
                    output_model=model,
                )

    def test_shadow_store_and_audit_are_lightweight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "shadow.db"
            phase4 = root / "phase4.db"
            _create_phase4(phase4)
            artifact = {
                "created_at": "2026-07-28T12:00:00+00:00",
                "hypotheses": list(SHADOW_HYPOTHESES),
                "fee_rate": 0.07,
                "slippage_per_share": 0.005,
                "gold_contract_sha256": "synthetic-gold-v2",
                "phase4_contract_sha256": "synthetic-phase4",
            }
            store = ShadowStore(database)
            store.open(target_hours=0.001, artifact=artifact)
            market = SilverMarket(
                condition_id="condition-forward",
                slug="btc-updown-5m-1800000000",
                event_id="event-forward",
                question="BTC Up or Down",
                start_ms=1_800_000_000_000,
                end_ms=1_800_000_300_000,
                up_token_id="up",
                down_token_id="down",
                resolution_source="Chainlink",
                resolution=ResolutionInfo(
                    price_to_beat=100_000.0,
                ),
            )
            self.assertTrue(store.save_market(market))
            feature = {
                "decision_timestamp_ms": 1_800_000_240_000,
                "horizon_seconds": 60,
                "decision_quality_flags": 0,
            }
            signals = [
                {
                    "model_name": "market_implied",
                    "scope": "all_markets",
                    "minimum_edge": None,
                    "probability_up": 0.60,
                    "side": None,
                    "expected_edge": None,
                    "would_trade": False,
                    "entry_cost": None,
                    "fill_price": None,
                    "fee": None,
                }
            ]
            for hypothesis in SHADOW_HYPOTHESES:
                signals.append(
                    {
                        "model_name": hypothesis["model_name"],
                        "scope": hypothesis["scope"],
                        "minimum_edge": hypothesis["minimum_edge"],
                        "probability_up": 0.70,
                        "side": "Up",
                        "expected_edge": 0.10,
                        "would_trade": True,
                        "entry_cost": 0.60,
                        "fill_price": 0.59,
                        "fee": 0.01,
                    }
                )
            store.save_feature_and_signals(
                condition_id=market.condition_id,
                feature=feature,
                signals=signals,
            )
            store.save_resolution(
                condition_id=market.condition_id,
                resolution=ResolutionInfo(
                    label="Up",
                    source="gamma_outcome_prices",
                    verified=True,
                ),
            )
            store.set_meta(
                "experiment_completed_at",
                store.meta()["target_end_at"],
            )
            self.assertEqual(store.quick_check(), "ok")
            store.close()

            result = audit_shadow_forward(
                shadow_db=database,
                phase4_db=phase4,
            )
            status = shadow_status(database)

            self.assertTrue(result["technical_passed"])
            self.assertFalse(result["historical_test_still_locked"])
            self.assertFalse(result["forward_candidate"])
            self.assertTrue(result["historical_test_accessed"])
            self.assertFalse(result["historical_test_accessed_by_phase4_pipeline"])
            self.assertFalse(result["twap_retraining_required"])
            self.assertIsNone(result["strategy_validation_blocked_reason"])
            self.assertFalse(result["money_real_candidate"])
            self.assertTrue(result["requires_manual_review_before_live"])
            self.assertEqual(result["markets"], 1)
            self.assertEqual(result["features"], 1)
            self.assertEqual(result["resolved"], 1)
            self.assertEqual(status["sqlite_quick_check"], "ok")
            self.assertFalse(status["orders_created"])


if __name__ == "__main__":
    unittest.main()
