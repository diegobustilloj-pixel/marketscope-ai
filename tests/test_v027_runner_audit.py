from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from polymarket_bot.phase2 import ResolutionInfo, SilverMarket
from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v027_audit import audit_v027, evaluate_candidate
from polymarket_bot.v027_forward import (
    V027LiveState,
    V027Store,
    build_v027_feature,
)
from polymarket_bot.v027_runner import (
    V027RunnerError,
    _candidate_futility,
    build_implementation_manifest,
    load_and_verify_launch_approval,
)
from polymarket_bot.v027_strategy import UP_LOW_VOL_ID


PREREG = ROOT / "data" / "prereg_v027_regime_replication_tournament.json"
SOURCE_60 = "https://data.chain.link/streams/btc-usd-twap-60s-streams"


def _record(*, hour: float, won: bool, cost: float = 0.65) -> dict:
    return {
        "condition_id": f"row-{hour}-{won}-{cost}",
        "market_start_ms": int(hour * 3_600_000),
        "favorite_side": "Up",
        "label": "Up" if won else "Down",
        "label_verified": 1,
        "entry_cost": cost,
        "volatility_regime_ratio": 0.5,
    }


class V027RunnerAuditTests(unittest.TestCase):
    def test_feature_selects_official_sixty_second_contract(self) -> None:
        start_ms = 1_800_000_000_000
        market = SilverMarket(
            condition_id="condition",
            slug="btc-updown-5m-1800000000",
            event_id="event",
            question="BTC Up or Down",
            start_ms=start_ms,
            end_ms=start_ms + 300_000,
            up_token_id="up",
            down_token_id="down",
            resolution_source=SOURCE_60,
        )
        state = V027LiveState()
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
        feature, reason = build_v027_feature(state=state, market=market)
        self.assertIsNone(reason)
        self.assertIsNotNone(feature)
        assert feature is not None
        self.assertEqual(feature["resolution_twap_window_s"], 60)
        self.assertEqual(feature["resolution_twap_price"], 110240.0)
        self.assertEqual(feature["twap_open_price"], 110000.0)
        self.assertEqual(feature["twap_transfer_model_enabled"], 0)
        self.assertEqual(
            feature["v027_signal_source"],
            "market_implied_only_no_transferred_model",
        )

    def test_store_preserves_same_timestamp_for_both_windows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "v027.db"
            store = V027Store(database)
            store.open(
                preregistration_sha256="prereg",
                launch_manifest_sha256="launch",
            )
            for window, word in ((30, "thirty"), (60, "sixty")):
                outcome = store.save_twap_message(
                    {
                        "topic": f"crypto_prices_twap_{word}",
                        "payload": {
                            "symbol": "btc/usd",
                            "timestamp": 1_800_000_000_000,
                            "window_s": window,
                            "value": 100000 + window,
                        },
                    }
                )
                self.assertEqual(outcome, "SAVED")
            count = store.db.execute(
                "SELECT COUNT(*) FROM v027_twap_ticks"
            ).fetchone()[0]
            self.assertEqual(count, 2)
            store.close()

    def test_candidate_passes_all_frozen_gates_with_stable_rows(self) -> None:
        hours = [
            0.5,
            1.0,
            4.5,
            5.0,
            8.5,
            9.0,
            12.5,
            13.0,
            16.5,
            17.0,
            20.5,
            21.0,
            2.0,
            6.0,
            10.0,
            14.0,
            18.0,
            22.0,
        ]
        candidate = [_record(hour=hour, won=True) for hour in hours]
        parent = [*candidate, *[_record(hour=hour + 0.1, won=False) for hour in hours]]
        result = evaluate_candidate(
            candidate_id=UP_LOW_VOL_ID,
            candidate_rows=candidate,
            parent_rows=parent,
            start_ms=0,
            cutoff_ms=24 * 3_600_000,
            frequency_contract={
                "minimum_final_trades": 15,
                "minimum_first_half_trades": 5,
                "minimum_second_half_trades": 5,
            },
            economic_contract={
                "minimum_evaluable_block_trades": 2,
                "minimum_evaluable_blocks": 4,
                "minimum_positive_evaluable_block_fraction": 0.6,
            },
            candidate_state={"status": "ACTIVE"},
        )
        self.assertTrue(result["frequency_passed"])
        self.assertTrue(result["economic_replication_passed"])
        self.assertTrue(result["full_statistical_passed"])
        self.assertEqual(result["evaluable_blocks"], 6)

    def test_run_stays_blocked_without_launch_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            implementation = Path(directory) / "implementation.json"
            build_implementation_manifest(
                prereg_path=PREREG,
                output_path=implementation,
            )
            with self.assertRaisesRegex(V027RunnerError, "NOT_LAUNCHED"):
                load_and_verify_launch_approval(
                    Path(directory) / "missing-launch.json",
                    prereg_path=PREREG,
                    implementation_path=implementation,
                )

    def test_candidate_futility_is_individual(self) -> None:
        prereg = json.loads(PREREG.read_text(encoding="utf-8"))
        up = _candidate_futility(
            candidate_id="favorite_up_low_vol_lt_075",
            checkpoint_hour=8.0,
            captured_rows=[],
            resolved_rows=[],
            first_half_count=0,
            prereg=prereg,
        )
        down_rows = [
            {
                "entry_cost": 0.75,
                "favorite_side": "Down",
                "label": "Down",
            }
            for _ in range(3)
        ]
        down = _candidate_futility(
            candidate_id="favorite_down_cost_070_080",
            checkpoint_hour=8.0,
            captured_rows=down_rows,
            resolved_rows=down_rows,
            first_half_count=3,
            prereg=prereg,
        )
        self.assertEqual(up["decision"], "FROZEN_FREQUENCY_FUTILITY")
        self.assertEqual(down["decision"], "CONTINUE")

    def test_terminal_auditor_accepts_expected_shutdown_and_fails_frequency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            implementation = root / "implementation.json"
            build_implementation_manifest(
                prereg_path=PREREG,
                output_path=implementation,
            )
            database = root / "v027.db"
            store = V027Store(database)
            store.open(
                preregistration_sha256=sha256_file(PREREG),
                launch_manifest_sha256="synthetic-launch",
            )
            now = int(time.time())
            aligned_now = now - now % 300
            start_seconds = aligned_now - 8 * 3600
            start_iso = time.strftime(
                "%Y-%m-%dT%H:%M:%S+00:00",
                time.gmtime(start_seconds),
            )
            observation_iso = time.strftime(
                "%Y-%m-%dT%H:%M:%S+00:00",
                time.gmtime(aligned_now),
            )
            target_iso = time.strftime(
                "%Y-%m-%dT%H:%M:%S+00:00",
                time.gmtime(start_seconds + 24 * 3600),
            )
            store.set_meta("experiment_started_at", start_iso)
            store.set_meta("target_end_at", target_iso)
            for index in range(96):
                market_start_ms = (start_seconds + index * 300) * 1000
                market = SilverMarket(
                    condition_id=f"condition-{index}",
                    slug=f"btc-updown-5m-{start_seconds + index * 300}",
                    event_id=f"event-{index}",
                    question="BTC Up or Down",
                    start_ms=market_start_ms,
                    end_ms=market_start_ms + 300_000,
                    up_token_id=f"up-{index}",
                    down_token_id=f"down-{index}",
                    resolution_source=SOURCE_60,
                )
                from polymarket_bot.resolution_contract import resolution_twap_contract

                store.save_market(
                    market,
                    discovery_metadata={"strike_fetch_status": "FOUND"},
                    contract=resolution_twap_contract(SOURCE_60),
                )
                store.save_feature(
                    condition_id=market.condition_id,
                    feature={
                        "decision_timestamp_ms": market_start_ms + 240_000,
                        "horizon_seconds": 60,
                        "decision_quality_flags": 0,
                        "implied_up_mid_probability": 0.5,
                        "up_best_ask": 0.51,
                        "down_best_ask": 0.51,
                        "volatility_regime_ratio": 0.5,
                        "resolution_twap_window_s": 60,
                    },
                )
                store.save_resolution(
                    condition_id=market.condition_id,
                    resolution=ResolutionInfo(
                        label="Up" if index % 2 else "Down",
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
                "v027-rtds-chainlink": "CONNECTED",
                "v027-binance": "CONNECTED",
                "v027-rtds-twap-60s": "CONNECTED",
            }
            technical = {
                "passed": True,
                "safety_passed": True,
                "latest_connections": connected,
            }
            frozen_states = {
                candidate_id: {
                    "status": "FROZEN",
                    "frozen_at": observation_iso,
                    "freeze_reason": "FROZEN_FREQUENCY_FUTILITY",
                    "evaluation_cutoff_market_start_ms": aligned_now * 1000,
                }
                for candidate_id in (
                    "favorite_up_low_vol_lt_075",
                    "favorite_down_cost_070_080",
                )
            }
            store.set_meta("v027_candidate_states", frozen_states)
            store.set_meta("v027_completion_reason", "FREEZE_ALL_CANDIDATES_FUTILITY")
            store.set_meta("v027_observation_ended_at", observation_iso)
            store.set_meta(
                "v027_checkpoints",
                [
                    {
                        "checkpoint_hour": 8.0,
                        "attempt": 1,
                        "decision": "FREEZE_ALL_CANDIDATES_FUTILITY",
                        "technical": technical,
                    }
                ],
            )
            run_id = store.start_run()
            cancelled = {
                "v027-rtds-chainlink": "CANCELLED",
                "v027-binance": "CANCELLED",
                "v027-rtds-twap-60s": "CANCELLED",
            }
            store.save_health(counters={}, connections=cancelled)
            store.finish_run(run_id, status="COMPLETED", error=None)
            store.close()

            result = audit_v027(
                database=database,
                prereg_path=PREREG,
                implementation_path=implementation,
            )
            self.assertTrue(result["terminal_feed_shutdown_expected"])
            self.assertTrue(result["technical_passed"])
            self.assertEqual(result["verdict"], "FAIL_ALL_CANDIDATES_FREQUENCY")
            self.assertIsNone(result["selected_strategy"])
            self.assertEqual(result["paper_orders"], 0)
            self.assertEqual(result["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
