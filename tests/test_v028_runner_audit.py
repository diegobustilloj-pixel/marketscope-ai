from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from polymarket_bot.phase2 import ResolutionInfo, SilverMarket
from polymarket_bot.resolution_contract import resolution_twap_contract
from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v028_audit import audit_v028, evaluate_primary
from polymarket_bot.v028_forward import (
    V028LiveState,
    V028Store,
    build_v028_feature,
)
from polymarket_bot.v028_runner import (
    V028RunnerError,
    _candidate_futility,
    build_implementation_manifest,
    load_and_verify_implementation,
    load_and_verify_launch_approval,
)
from polymarket_bot.v028_strategy import PARENT_CONTROL_ID, PRIMARY_ID


PREREG = ROOT / "data" / "prereg_v028_twap_lt5_replication.json"
SOURCE_30 = "https://data.chain.link/streams/btc-usd-twap-30s-streams"
SOURCE_60 = "https://data.chain.link/streams/btc-usd-twap-60s-streams"


def _record(
    *,
    hour: float,
    won: bool,
    cost: float = 0.65,
    distance: float = 1.0,
) -> dict[str, object]:
    return {
        "condition_id": f"row-{hour}-{won}-{cost}-{distance}",
        "market_start_ms": int(hour * 3_600_000),
        "favorite_side": "Up",
        "label": "Up" if won else "Down",
        "label_verified": 1,
        "entry_cost": cost,
        "volatility_regime_ratio": 0.5,
        "twap_distance_to_open_bps": distance,
        "resolution_twap_window_s": 60,
    }


def _market(*, source: str, start_ms: int = 1_800_000_000_000) -> SilverMarket:
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


class V028RunnerAuditTests(unittest.TestCase):
    def test_feature_requires_exact_official_sixty_second_contract(self) -> None:
        start_ms = 1_800_000_000_000
        market = _market(source=SOURCE_60, start_ms=start_ms)
        state = V028LiveState()
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
        feature, reason = build_v028_feature(state=state, market=market)
        self.assertIsNone(reason)
        self.assertIsNotNone(feature)
        assert feature is not None
        self.assertEqual(feature["resolution_twap_window_s"], 60)
        self.assertEqual(feature["twap_open_price"], 110000.0)
        self.assertAlmostEqual(
            feature["twap_distance_to_open_bps"],
            (110240.0 / 110000.0 - 1.0) * 10_000.0,
        )
        self.assertEqual(feature["twap_transfer_model_enabled"], 0)
        self.assertEqual(
            feature["v028_signal_source"],
            "market_implied_only_no_transferred_model",
        )

        rejected, rejected_reason = build_v028_feature(
            state=V028LiveState(),
            market=_market(source=SOURCE_30, start_ms=start_ms + 300_000),
        )
        self.assertIsNone(rejected)
        self.assertEqual(rejected_reason, "resolution_contract_wrong_twap_window")

    def test_primary_passes_all_frozen_gates_and_control_is_not_selected(self) -> None:
        candidate = [
            _record(hour=block * 4 + offset, won=True)
            for block in range(6)
            for offset in (0.5, 1.0, 1.5, 2.0)
        ]
        parent = [
            *candidate,
            *[
                _record(hour=block * 4 + offset, won=False, distance=6.0)
                for block in range(6)
                for offset in (2.25, 2.5, 2.75, 3.0)
            ],
        ]
        prereg = json.loads(PREREG.read_text(encoding="utf-8"))
        result = evaluate_primary(
            candidate_id=PRIMARY_ID,
            candidate_rows=candidate,
            parent_rows=parent,
            start_ms=0,
            cutoff_ms=24 * 3_600_000,
            frequency_contract=prereg["candidate_frequency"],
            economic_contract=prereg["economic_replication_gates"],
            candidate_state={"status": "ACTIVE"},
        )
        self.assertTrue(result["frequency_passed"])
        self.assertTrue(result["economic_replication_passed"])
        self.assertTrue(result["full_statistical_passed"])
        self.assertEqual(result["parent_control_id"], PARENT_CONTROL_ID)
        self.assertTrue(
            result["gates"]["economic_replication"][
                "positive_net_pnl_without_best_trade_passed"
            ]
        )

    def test_best_trade_concentration_gate_is_enforced(self) -> None:
        rows = [
            *[_record(hour=index, won=True, cost=0.90) for index in range(16)],
            *[
                _record(hour=16 + index, won=False, cost=0.60)
                for index in range(3)
            ],
            _record(hour=23.0, won=True, cost=0.51),
        ]
        prereg = json.loads(PREREG.read_text(encoding="utf-8"))
        result = evaluate_primary(
            candidate_id=PRIMARY_ID,
            candidate_rows=rows,
            parent_rows=[*rows, _record(hour=23.5, won=False, distance=6.0)],
            start_ms=0,
            cutoff_ms=24 * 3_600_000,
            frequency_contract=prereg["candidate_frequency"],
            economic_contract=prereg["economic_replication_gates"],
            candidate_state={"status": "ACTIVE"},
        )
        self.assertTrue(result["frequency_passed"])
        self.assertGreater(result["metrics"]["net_pnl_per_share_sequence"], 0)
        self.assertLess(
            result["metrics"]["net_without_best_trade_per_share"],
            0,
        )
        self.assertFalse(result["economic_replication_passed"])

    def test_run_stays_blocked_without_separate_launch_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            implementation = Path(directory) / "implementation.json"
            build_implementation_manifest(
                prereg_path=PREREG,
                output_path=implementation,
            )
            with self.assertRaisesRegex(V028RunnerError, "NOT_LAUNCHED"):
                load_and_verify_launch_approval(
                    Path(directory) / "missing-launch.json",
                    prereg_path=PREREG,
                    implementation_path=implementation,
                )

    def test_implementation_manifest_fails_closed_if_safety_is_altered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            implementation = Path(directory) / "implementation.json"
            payload = build_implementation_manifest(
                prereg_path=PREREG,
                output_path=implementation,
            )
            payload["safety"]["orders_enabled"] = True
            implementation.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(V028RunnerError, "safety"):
                load_and_verify_implementation(implementation)

    def test_checkpoint_futility_uses_single_frozen_frequency_contract(self) -> None:
        prereg = json.loads(PREREG.read_text(encoding="utf-8"))
        result = _candidate_futility(
            candidate_id=PRIMARY_ID,
            checkpoint_hour=8.0,
            captured_rows=[],
            resolved_rows=[],
            first_half_count=0,
            prereg=prereg,
        )
        self.assertEqual(result["decision"], "FROZEN_FREQUENCY_FUTILITY")
        self.assertEqual(result["minimum_final_trades"], 20)

    def test_terminal_auditor_passes_single_hypothesis_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            implementation = root / "implementation.json"
            build_implementation_manifest(
                prereg_path=PREREG,
                output_path=implementation,
            )
            database = root / "v028.db"
            store = V028Store(database)
            store.open(
                preregistration_sha256=sha256_file(PREREG),
                launch_manifest_sha256="synthetic-launch",
            )
            now = int(time.time())
            observation_seconds = now - now % 300
            start_seconds = observation_seconds - 24 * 3600
            start_iso = time.strftime(
                "%Y-%m-%dT%H:%M:%S+00:00",
                time.gmtime(start_seconds),
            )
            observation_iso = time.strftime(
                "%Y-%m-%dT%H:%M:%S+00:00",
                time.gmtime(observation_seconds),
            )
            store.set_meta("experiment_started_at", start_iso)
            store.set_meta("target_end_at", observation_iso)
            primary_indices = {
                block * 48 + offset
                for block in range(6)
                for offset in (1, 2, 3, 4)
            }
            parent_only_indices = {
                block * 48 + offset
                for block in range(6)
                for offset in (5, 6, 7, 8)
            }
            for index in range(288):
                market_start_ms = (start_seconds + index * 300) * 1000
                market = _market(source=SOURCE_60, start_ms=market_start_ms)
                store.save_market(
                    market,
                    discovery_metadata={"strike_fetch_status": "FOUND"},
                    contract=resolution_twap_contract(SOURCE_60),
                )
                favorite_up = index in primary_indices or index in parent_only_indices
                store.save_feature(
                    condition_id=market.condition_id,
                    feature={
                        "decision_timestamp_ms": market_start_ms + 240_000,
                        "horizon_seconds": 60,
                        "decision_quality_flags": 0,
                        "implied_up_mid_probability": 0.55 if favorite_up else 0.45,
                        "up_best_ask": 0.55,
                        "down_best_ask": 0.55,
                        "volatility_regime_ratio": 0.5,
                        "resolution_twap_window_s": 60,
                        "twap_distance_to_open_bps": (
                            1.0 if index in primary_indices else 6.0
                        ),
                    },
                )
                label = "Up" if index in primary_indices else "Down"
                store.save_resolution(
                    condition_id=market.condition_id,
                    resolution=ResolutionInfo(label=label, verified=True),
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
                "v028-rtds-chainlink": "CONNECTED",
                "v028-binance": "CONNECTED",
                "v028-rtds-twap-60s": "CONNECTED",
            }
            store.set_meta(
                "v028_candidate_states",
                {
                    PRIMARY_ID: {
                        "status": "ACTIVE",
                        "frozen_at": None,
                        "freeze_reason": None,
                        "evaluation_cutoff_market_start_ms": None,
                    }
                },
            )
            store.set_meta("v028_completion_reason", "FULL_24H_REACHED")
            store.set_meta("v028_observation_ended_at", observation_iso)
            store.set_meta(
                "v028_checkpoints",
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
            result = audit_v028(
                database=database,
                prereg_path=PREREG,
                implementation_path=implementation,
                result_path=result_path,
            )
            self.assertEqual(result["verdict"], "PASS_SINGLE_HYPOTHESIS_PAPER_CANDIDATE")
            self.assertEqual(result["selected_strategy"], PRIMARY_ID)
            self.assertTrue(result["technical_passed"])
            self.assertTrue(result["terminal_feed_shutdown_expected"])
            self.assertEqual(result["controls"][PARENT_CONTROL_ID]["selectable"], False)
            self.assertEqual(result["database_sha256"], before)
            self.assertEqual(sha256_file(database), before)
            self.assertEqual(result["orders_created"], 0)
            self.assertEqual(result["paper_orders"], 0)
            self.assertFalse(result["wallet_required"])
            self.assertEqual(result["real_money"], "BLOQUEADO")

            repeated = audit_v028(
                database=database,
                prereg_path=PREREG,
                implementation_path=implementation,
                result_path=result_path,
            )
            self.assertEqual(repeated, result)


if __name__ == "__main__":
    unittest.main()
