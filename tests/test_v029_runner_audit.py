from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from polymarket_bot.phase2 import ResolutionInfo, SilverMarket
from polymarket_bot.resolution_contract import resolution_twap_contract
from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v029_audit import audit_v029, evaluate_holdout
from polymarket_bot.v029_forward import (
    V029LiveState,
    V029Store,
    build_v029_feature,
)
from polymarket_bot.v029_runner import (
    V029RunnerError,
    build_implementation_manifest,
    load_and_verify_implementation,
    load_and_verify_launch_approval,
    v029_status,
)
from polymarket_bot.v029_strategy import CANDIDATE_ID


PREREG = ROOT / "data" / "prereg_v029_high_frequency_holdout.json"
SOURCE_30 = "https://data.chain.link/streams/btc-usd-twap-30s-streams"
SOURCE_60 = "https://data.chain.link/streams/btc-usd-twap-60s-streams"


def _market(*, source: str, start_ms: int) -> SilverMarket:
    return SilverMarket(
        condition_id=f"condition-{start_ms}",
        slug=f"btc-updown-5m-{start_ms // 1000}",
        event_id=f"event-{start_ms}",
        question="BTC Up or Down",
        start_ms=start_ms,
        end_ms=start_ms + 300_000,
        up_token_id=f"up-{start_ms}",
        down_token_id=f"down-{start_ms}",
        resolution_source=source,
    )


def _model_row(index: int, *, training: bool, flip_label: bool = False) -> dict[str, object]:
    target_up = index % 2 == 0
    if flip_label:
        target_up = not target_up
    signed = 10.0 if index % 2 == 0 else -10.0
    return {
        "condition_id": f"{'train' if training else 'valid'}-{index}",
        "market_start_ms": index * 300_000,
        "label": "Up" if target_up else "Down",
        "target_up": int(target_up),
        "features": [0.49 if index % 2 == 0 else 0.51, signed, signed],
        "implied_up_mid_probability": 0.49 if index % 2 == 0 else 0.51,
        "up_entry_cost": 0.56,
        "down_entry_cost": 0.56,
    }


class V029RunnerAuditTests(unittest.TestCase):
    def test_feature_requires_exact_fresh_official_twap60(self) -> None:
        start_ms = 1_800_000_000_000
        market = _market(source=SOURCE_60, start_ms=start_ms)
        state = V029LiveState()
        state.set_market(market)
        for token, bid, ask in (
            (market.up_token_id, "0.54", "0.55"),
            (market.down_token_id, "0.44", "0.45"),
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
                        "asks": [{"price": ask, "size": "100"}],
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
                        "payload": {
                            "symbol": "btc/usd",
                            "timestamp": timestamp,
                            "value": 100000 + second,
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
                        "T": timestamp,
                        "p": str(100001 + second),
                        "q": "0.1",
                    }
                ),
            )
            state.ingest(
                source="rtds",
                default_stream="crypto_price",
                raw=json.dumps(
                    {
                        "topic": "crypto_prices_twap_sixty",
                        "payload": {
                            "symbol": "btc/usd",
                            "timestamp": timestamp,
                            "window_s": 60,
                            "value": 110000 + second,
                        },
                    }
                ),
            )
        feature, reason = build_v029_feature(state=state, market=market)
        self.assertIsNone(reason)
        assert feature is not None
        self.assertEqual(feature["resolution_twap_window_s"], 60)
        self.assertEqual(feature["v029_signal_source"], "market_implied_only_no_transferred_model")
        rejected, rejected_reason = build_v029_feature(
            state=V029LiveState(),
            market=_market(source=SOURCE_30, start_ms=start_ms + 300_000),
        )
        self.assertIsNone(rejected)
        self.assertEqual(rejected_reason, "resolution_contract_wrong_twap_window")

    def test_database_namespace_and_public_status_are_v029_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "v029.db"
            store = V029Store(database)
            store.open(
                preregistration_sha256="p" * 64,
                launch_manifest_sha256="l" * 64,
            )
            store.close()
            connection = sqlite3.connect(database)
            try:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                connection.close()
            self.assertIn("v029_markets", tables)
            self.assertNotIn("v028_markets", tables)
            status = v029_status(database)
            self.assertFalse(status["intermediate_outcome_metrics_exposed"])
            self.assertFalse(status["model_fit"])
            self.assertNotIn("wins", status)
            self.assertNotIn("pnl", status)

    def test_validation_labels_never_change_fitted_model(self) -> None:
        prereg = json.loads(PREREG.read_text(encoding="utf-8"))
        training = [_model_row(index, training=True) for index in range(120)]
        validation = [_model_row(index, training=False) for index in range(120)]
        flipped = [
            _model_row(index, training=False, flip_label=True)
            for index in range(120)
        ]
        first = evaluate_holdout(
            training=training,
            validation=validation,
            prereg=prereg,
        )
        second = evaluate_holdout(
            training=training,
            validation=flipped,
            prereg=prereg,
        )
        self.assertTrue(first["model_fit"])
        self.assertEqual(first["model_artifact"], second["model_artifact"])
        self.assertFalse(first["training_pnl_computed"])
        self.assertNotEqual(
            first["candidate_metrics"]["net_pnl_at_5_shares"],
            second["candidate_metrics"]["net_pnl_at_5_shares"],
        )

    def test_launch_and_manifest_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            implementation = Path(directory) / "implementation.json"
            payload = build_implementation_manifest(
                prereg_path=PREREG,
                output_path=implementation,
            )
            with self.assertRaisesRegex(V029RunnerError, "NOT_LAUNCHED"):
                load_and_verify_launch_approval(
                    Path(directory) / "missing-launch.json",
                    prereg_path=PREREG,
                    implementation_path=implementation,
                )
            payload["safety"]["orders_enabled"] = True
            implementation.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(V029RunnerError, "safety"):
                load_and_verify_implementation(implementation)

    def test_terminal_auditor_passes_synthetic_sealed_holdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            implementation = root / "implementation.json"
            build_implementation_manifest(
                prereg_path=PREREG,
                output_path=implementation,
            )
            database = root / "v029.db"
            store = V029Store(database)
            store.open(
                preregistration_sha256=sha256_file(PREREG),
                launch_manifest_sha256="synthetic-launch",
            )
            observation_seconds = int(time.time())
            observation_seconds -= observation_seconds % 300
            start_seconds = observation_seconds - 24 * 3600
            start_iso = time.strftime(
                "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(start_seconds)
            )
            observation_iso = time.strftime(
                "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(observation_seconds)
            )
            store.set_meta("experiment_started_at", start_iso)
            store.set_meta("target_end_at", observation_iso)
            for index in range(288):
                start_ms = (start_seconds + index * 300) * 1000
                market = _market(source=SOURCE_60, start_ms=start_ms)
                store.save_market(
                    market,
                    discovery_metadata={"strike_fetch_status": "FOUND"},
                    contract=resolution_twap_contract(SOURCE_60),
                )
                target_up = index % 2 == 0
                signed = 10.0 if target_up else -10.0
                store.save_feature(
                    condition_id=market.condition_id,
                    feature={
                        "decision_timestamp_ms": start_ms + 240_000,
                        "horizon_seconds": 60,
                        "decision_quality_flags": 0,
                        "implied_up_mid_probability": 0.49 if target_up else 0.51,
                        "up_best_ask": 0.54,
                        "down_best_ask": 0.54,
                        "twap_distance_to_open_bps": signed,
                        "binance_return_60s_bps": signed,
                        "resolution_twap_window_s": 60,
                    },
                )
                store.save_resolution(
                    condition_id=market.condition_id,
                    resolution=ResolutionInfo(
                        label="Up" if target_up else "Down",
                        verified=True,
                    ),
                )
            store.save_twap_message(
                {
                    "topic": "crypto_prices_twap_sixty",
                    "payload": {
                        "symbol": "btc/usd",
                        "timestamp": start_seconds * 1000,
                        "window_s": 60,
                        "value": 100000,
                    },
                }
            )
            connected = {
                "v029-rtds-chainlink": "CONNECTED",
                "v029-binance": "CONNECTED",
                "v029-rtds-twap-60s": "CONNECTED",
            }
            store.set_meta("v029_completion_reason", "FULL_24H_REACHED")
            store.set_meta("v029_observation_ended_at", observation_iso)
            store.set_meta(
                "v029_checkpoints",
                [
                    {
                        "checkpoint_hour": 20.0,
                        "attempt": 1,
                        "decision": "CONTINUE",
                        "technical": {
                            "passed": True,
                            "safety_passed": True,
                            "latest_connections": connected,
                        },
                    }
                ],
            )
            run_id = store.start_run()
            store.save_health(
                counters={},
                connections={key: "CANCELLED" for key in connected},
            )
            store.finish_run(run_id, status="COMPLETED", error=None)
            store.close()

            result_path = root / "result.json"
            before = sha256_file(database)
            result = audit_v029(
                database=database,
                prereg_path=PREREG,
                implementation_path=implementation,
                result_path=result_path,
            )
            self.assertEqual(
                result["verdict"],
                "HYPOTHESIS_REQUIRES_FRESH_V030_REPLICATION",
            )
            self.assertEqual(result["generated_hypothesis"], CANDIDATE_ID)
            self.assertIsNone(result["selected_strategy"])
            self.assertFalse(result["paper_forward_candidate"])
            self.assertTrue(result["technical_passed"])
            self.assertTrue(result["terminal_feed_shutdown_expected"])
            self.assertTrue(result["holdout"]["validation_economics_passed"])
            self.assertGreaterEqual(result["holdout"]["candidate_signal_count"], 20)
            self.assertEqual(result["database_sha256"], before)
            self.assertEqual(sha256_file(database), before)
            self.assertEqual(result["orders_created"], 0)
            self.assertEqual(result["paper_orders"], 0)
            self.assertFalse(result["wallet_required"])
            self.assertEqual(result["real_money"], "BLOQUEADO")

            repeated = audit_v029(
                database=database,
                prereg_path=PREREG,
                implementation_path=implementation,
                result_path=result_path,
            )
            self.assertEqual(repeated, result)


if __name__ == "__main__":
    unittest.main()
