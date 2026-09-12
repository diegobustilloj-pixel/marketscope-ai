from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from websockets.exceptions import ConnectionClosed

from polymarket_bot.config import Settings
from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v042_audit import audit_v042
from polymarket_bot.v042_capture import V042Store
from polymarket_bot.v042_contract import (
    PASS_VERDICT,
    evaluate_probe,
    load_and_verify_prereg,
)
from polymarket_bot.v041_chainlink_watchdog_design import (
    CHAINLINK_WATCHDOG_SECONDS,
    is_fresh_chainlink_message,
)
from polymarket_bot.v042_planned_reconnect_design import (
    PLANNED_STALE_RECONNECT_BACKOFF_MS,
)
from polymarket_bot.v042_runner import (
    V042RunnerError,
    load_and_verify_launch_approval,
    run_v042,
    run_v042_chainlink_preflight,
    supervise_chainlink_rtds,
    v042_status,
)


DESIGN = ROOT / "data" / "diagnostico_v042_planned_chainlink_reconnect.json"
PREREG = ROOT / "data" / "prereg_v042_planned_chainlink_reconnect_4h.json"
IMPLEMENTATION = ROOT / "data" / "implementation_v042_planned_chainlink_reconnect_4h.json"
NETWORK_PREFLIGHT = ROOT / "data" / "evidencia_v042_planned_chainlink_reconnect_network_preflight.json"
PREVIOUS_PREREG = ROOT / "data" / "prereg_v041_chainlink_silence_watchdog_4h.json"
V039_DATABASE = ROOT / "data" / "capture_v039_validated_peer_dns_fallback_restart_4h.db"


def _row(offset: int) -> dict:
    timestamp = 1_800_000_000_000 + offset * 1000
    return {
        "second_offset": offset,
        "snapshot_timestamp_ms": timestamp,
        "recorded_timestamp_ms": timestamp,
        "chainlink_age_ms": 0,
        "chainlink_fresh": 1,
        "official_twap_age_ms": 0,
        "official_twap_fresh": 1,
        "up_book_age_ms": 1000,
        "up_book_fresh": 1,
        "down_book_age_ms": 1000,
        "down_book_fresh": 1,
        "up_bid_depth_top5": 1000.0,
        "up_ask_depth_top5": 1000.0,
        "down_bid_depth_top5": 1000.0,
        "down_ask_depth_top5": 1000.0,
        "complete_v2": 1,
    }


def _path() -> dict[int, dict]:
    return {offset: _row(offset) for offset in range(300)}


class V042PlannedChainlinkReconnectTests(unittest.TestCase):
    def test_design_is_scoped_to_v041_planned_close_branch(self) -> None:
        design = json.loads(DESIGN.read_text(encoding="utf-8"))
        self.assertEqual(
            design["decision"],
            "PREPARE_ONE_FRESH_V042_PLANNED_CHAINLINK_RECONNECT_REPLICATION",
        )
        self.assertTrue(design["all_design_gates_passed"])
        evidence = design["v041_evidence"]
        self.assertEqual(evidence["chainlink_longest_stale_streak_seconds"], 21)
        self.assertEqual(evidence["chainlink_watchdog_stale_events"], 3)
        self.assertEqual(evidence["chainlink_watchdog_reconnects"], 0)
        self.assertEqual(evidence["observed_general_backoff_seconds"], [1.8, 2.3, 2.4])
        self.assertEqual(
            design["v042_design"]["counterfactual_worst_streak_seconds"], 20
        )
        self.assertFalse(design["v042_design"]["threshold_relaxed_after_result"])

    def test_prereg_preserves_every_v041_market_contract_and_liveness_gate(self) -> None:
        prereg = load_and_verify_prereg(PREREG, project_root=ROOT)
        previous = json.loads(PREVIOUS_PREREG.read_text(encoding="utf-8"))
        self.assertEqual(prereg["probe_contract"], previous["probe_contract"])
        self.assertEqual(prereg["capture_contract"], previous["capture_contract"])
        self.assertEqual(prereg["transport_contract"], previous["transport_contract"])
        current_chainlink = dict(prereg["chainlink_feed_contract"])
        current_chainlink.pop("planned_stale_reconnect_backoff_ms")
        current_chainlink.pop("planned_close_connection_closed_action")
        self.assertEqual(current_chainlink, previous["chainlink_feed_contract"])
        self.assertEqual(
            prereg["chainlink_feed_contract"]["silence_watchdog_seconds"],
            CHAINLINK_WATCHDOG_SECONDS,
        )
        self.assertEqual(
            prereg["chainlink_feed_contract"][
                "planned_stale_reconnect_backoff_ms"
            ],
            PLANNED_STALE_RECONNECT_BACKOFF_MS,
        )
        self.assertEqual(
            prereg["technical_gates"]["maximum_chainlink_stale_streak_seconds"],
            previous["technical_gates"]["maximum_chainlink_stale_streak_seconds"],
        )
        self.assertEqual(
            prereg["technical_gates"][
                "required_chainlink_watchdog_reconnect_accounting_rate"
            ],
            1.0,
        )
        self.assertEqual(prereg["safety"]["real_money"], "BLOQUEADO")
        self.assertTrue(
            prereg["limitations"][
                "redundant_network_or_remote_executor_required_before_real_money"
            ]
        )

    def test_chainlink_predicate_is_strict(self) -> None:
        valid = json.dumps({
            "topic": "crypto_prices_chainlink",
            "payload": {"symbol": "btc/usd", "timestamp": 1_800_000_000_000,
                        "value": 65000.0},
        })
        self.assertTrue(is_fresh_chainlink_message(valid))
        self.assertFalse(is_fresh_chainlink_message("PING"))
        self.assertFalse(is_fresh_chainlink_message(valid.replace("btc/usd", "eth/usd")))
        self.assertFalse(is_fresh_chainlink_message(valid.replace("65000.0", "0.0")))

    def test_planned_close_uses_dedicated_counter_and_not_random_backoff(self) -> None:
        class SilentSocket:
            def __init__(self) -> None:
                self.closed = asyncio.Event()

            async def send(self, _: str) -> None:
                return None

            async def close(self) -> None:
                self.closed.set()

            def __aiter__(self):
                return self

            async def __anext__(self):
                await self.closed.wait()
                raise ConnectionClosed(None, None)

        class Context:
            def __init__(self, socket: SilentSocket) -> None:
                self.socket = socket

            async def __aenter__(self) -> SilentSocket:
                return self.socket

            async def __aexit__(self, *_: object) -> None:
                return None

        async def scenario() -> tuple[SimpleNamespace, SilentSocket, int]:
            state = SimpleNamespace(counters=Counter(), connections={})
            stop = asyncio.Event()
            socket = SilentSocket()
            with (
                patch(
                    "polymarket_bot.v042_runner.connect",
                    return_value=Context(socket),
                ),
                patch(
                    "polymarket_bot.v042_runner.random.uniform",
                    return_value=1.0,
                ) as random_uniform,
            ):
                task = asyncio.create_task(supervise_chainlink_rtds(
                    name="test-chainlink", endpoint="wss://example.invalid",
                    subscription={"action": "subscribe", "subscriptions": []},
                    heartbeat_text="PING", heartbeat_seconds=1.0,
                    use_proxy=False, reconnect_max_seconds=1.0, stop_event=stop,
                    state=state, handler=lambda _: asyncio.sleep(0),
                    freshness_timeout_seconds=0.04,
                ))
                for _ in range(100):
                    if state.counters["rtds:chainlink:stale_reconnects"] >= 1:
                        break
                    await asyncio.sleep(0.005)
                stop.set()
                await asyncio.wait_for(task, timeout=1.0)
            return state, socket, random_uniform.call_count

        state, socket, random_backoff_calls = asyncio.run(scenario())
        self.assertTrue(socket.closed.is_set())
        self.assertEqual(state.counters["rtds:chainlink:stale_watchdog"], 1)
        self.assertEqual(state.counters["rtds:chainlink:stale_reconnects"], 1)
        self.assertEqual(random_backoff_calls, 0)

    def test_preflight_reports_counts_without_prices_orders_or_wallet(self) -> None:
        async def fake_supervisor(**kwargs: object) -> None:
            state = kwargs["state"]
            stop = kwargs["stop_event"]
            state.counters["rtds:chainlink:watchdog_fresh"] = 3
            state.counters["rtds:chainlink_updates"] = 3
            await stop.wait()

        with patch(
            "polymarket_bot.v042_runner.supervise_chainlink_rtds",
            new=fake_supervisor,
        ):
            result = asyncio.run(run_v042_chainlink_preflight(
                settings=Settings.from_env(), seconds=0.01
            ))
        self.assertTrue(result["passed"])
        self.assertEqual(result["strict_valid_chainlink_messages"], 3)
        self.assertFalse(result["prices_stored"])
        self.assertEqual(result["orders_created"], 0)
        self.assertFalse(result["wallet_required"])
        self.assertEqual(result["real_money"], "BLOQUEADO")

    def test_launch_requires_hash_bound_passing_network_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            launch = Path(temporary) / "launch.json"
            payload = {
                "schema": "launch_approval_v042_planned_chainlink_reconnect_4h_1",
                "status": "APPROVED_FOR_ONE_FRESH_PLANNED_CHAINLINK_RECONNECT_REPLICATION",
                "variant": "V0.42_PLANNED_CHAINLINK_RECONNECT_4H",
                "technical_capture_hours": 4.0,
                "preregistration_sha256": sha256_file(PREREG),
                "implementation_sha256": sha256_file(IMPLEMENTATION),
                "network_preflight": {
                    "relative_path": "data/evidencia_v042_planned_chainlink_reconnect_network_preflight.json",
                    "sha256": sha256_file(NETWORK_PREFLIGHT),
                    "passed": True,
                    "strict_valid_chainlink_messages": 40,
                },
                "automatic_final_audit": True,
                "scheduled_supervision": False,
                "orders_enabled": False,
                "paper_orders_enabled": False,
                "wallet_required": False,
                "real_money": "BLOQUEADO",
                "outcomes_read": 0,
                "pnl_calculated": False,
            }
            launch.write_text(json.dumps(payload), encoding="utf-8")
            verified = load_and_verify_launch_approval(
                launch,
                prereg_path=PREREG,
                implementation_path=IMPLEMENTATION,
                project_root=ROOT,
            )
            self.assertEqual(
                verified["network_preflight"]["strict_valid_chainlink_messages"],
                40,
            )
            payload["network_preflight"]["strict_valid_chainlink_messages"] = 41
            launch.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(V042RunnerError):
                load_and_verify_launch_approval(
                    launch,
                    prereg_path=PREREG,
                    implementation_path=IMPLEMENTATION,
                    project_root=ROOT,
                )

    def test_exact_v039_traps_exit_at_last_non_stale_snapshot(self) -> None:
        prereg = load_and_verify_prereg(PREREG, project_root=ROOT)
        connection = sqlite3.connect(
            f"file:{V039_DATABASE.as_posix()}?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            market = connection.execute(
                "SELECT condition_id,slug FROM v039_markets WHERE probe_trapped>0"
            ).fetchone()
            snapshots = {
                int(row["second_offset"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM v039_snapshots WHERE condition_id=? ORDER BY second_offset",
                    (market["condition_id"],),
                )
            }
        finally:
            connection.close()
        for outcome in ("Up", "Down"):
            result = evaluate_probe(
                snapshots,
                decision_offset=60,
                outcome=outcome,
                contract=prereg["probe_contract"],
            )
            self.assertEqual(result["status"], "EXITED")
            self.assertEqual(result["exit_kind"], "TRANSPORT_PRE_STALE_GUARD")
            self.assertEqual(result["exit_offset"], 88)
            self.assertEqual(result["holding_seconds"], 27)
            self.assertEqual(result["transport_guard_oldest_book_age_ms"], 2321)

    def test_guard_is_armed_only_inside_five_second_horizon(self) -> None:
        prereg = load_and_verify_prereg(PREREG, project_root=ROOT)
        snapshots = _path()
        snapshots[40]["up_book_age_ms"] = 2500
        snapshots[40]["down_book_age_ms"] = 2500
        result = evaluate_probe(
            snapshots,
            decision_offset=30,
            outcome="Up",
            contract=prereg["probe_contract"],
        )
        self.assertEqual(result["status"], "EXITED")
        self.assertEqual(result["exit_kind"], "SCHEDULED")
        self.assertEqual(result["exit_offset"], 61)

    def test_stale_depth_never_counts_as_transport_guard_exit(self) -> None:
        prereg = load_and_verify_prereg(PREREG, project_root=ROOT)
        snapshots = _path()
        for offset in range(56, 72):
            snapshots[offset]["up_book_age_ms"] = 3000 + (offset - 56) * 1000
            snapshots[offset]["down_book_age_ms"] = 3000 + (offset - 56) * 1000
            snapshots[offset]["up_book_fresh"] = 0
            snapshots[offset]["down_book_fresh"] = 0
            snapshots[offset]["complete_v2"] = 0
        result = evaluate_probe(
            snapshots,
            decision_offset=30,
            outcome="Up",
            contract=prereg["probe_contract"],
        )
        self.assertEqual(result["status"], "TRAPPED")
        self.assertNotEqual(result.get("exit_kind"), "TRANSPORT_PRE_STALE_GUARD")

    def test_store_has_guard_counter_and_no_price_or_pnl_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "v042.db"
            store = V042Store(database)
            store.open(
                preregistration_sha256="p",
                implementation_sha256="i",
                launch_manifest_sha256="l",
                now_timestamp=1_800_000_000.0,
            )
            store.close()
            connection = sqlite3.connect(database)
            try:
                market_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(v042_markets)")
                }
                snapshot_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(v042_snapshots)")
                }
            finally:
                connection.close()
        self.assertIn("probe_transport_guard_exits", market_columns)
        self.assertFalse(
            any("price" in name or "pnl" in name for name in snapshot_columns)
        )

    def test_terminal_auditor_passes_full_guard_fixture(self) -> None:
        if not IMPLEMENTATION.is_file():
            self.skipTest("El manifiesto se sella despues de la primera pasada")
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "fixture.db"
            result_path = Path(temporary) / "result.json"
            store = V042Store(database)
            store.open(
                preregistration_sha256="p",
                implementation_sha256="i",
                launch_manifest_sha256="l",
                now_timestamp=1_800_000_000.0,
            )
            base_ms = 1_800_000_000_000
            for market_index in range(48):
                condition_id = f"c{market_index:02d}"
                slug = f"btc-updown-5m-{1_800_000_000 + market_index * 300}"
                start_ms = base_ms + market_index * 300_000
                store.db.execute(
                    """INSERT INTO v042_markets(
                    condition_id,slug,event_id,market_start_ms,market_end_ms,
                    up_token_id,down_token_id,discovered_at,resolution_source,
                    resolution_contract_status,resolution_twap_window_s,
                    resolution_twap_topic,capture_status,capture_error,probe_total,
                    probe_entered,probe_relative_guard_exits,probe_exit_successes,
                    probe_trapped,probe_transport_guard_exits)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        condition_id,
                        slug,
                        f"e{market_index}",
                        start_ms,
                        start_ms + 300_000,
                        f"up{market_index}",
                        f"down{market_index}",
                        "2030-01-01T00:00:00+00:00",
                        "twap 60 seconds",
                        "VERIFIED",
                        60,
                        "crypto_prices_twap_sixty",
                        "COMPLETED",
                        None,
                        122,
                        122,
                        0,
                        122,
                        0,
                        0,
                    ),
                )
                rows = []
                for offset in range(300):
                    timestamp_ms = start_ms + offset * 1000
                    up_bid = 100.0 if market_index == 0 and offset == 41 else 1000.0
                    book_age = 2321 if market_index == 1 and offset == 60 else 0
                    rows.append(
                        (
                            condition_id,
                            offset,
                            timestamp_ms,
                            timestamp_ms,
                            timestamp_ms,
                            timestamp_ms,
                            0,
                            1,
                            60,
                            timestamp_ms,
                            timestamp_ms,
                            0,
                            1,
                            timestamp_ms,
                            timestamp_ms,
                            book_age,
                            1,
                            timestamp_ms,
                            timestamp_ms,
                            book_age,
                            1,
                            up_bid,
                            1000.0,
                            1000.0,
                            1000.0,
                            1,
                        )
                    )
                store.db.executemany(
                    "INSERT INTO v042_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                store.save_transport_event(
                    condition_id=condition_id,
                    slug=slug,
                    incident_id=None,
                    generation=1,
                    event_type="GENERATION_FRESH",
                    trigger_reason=None,
                    recorded_timestamp_ms=start_ms + 2000,
                    second_offset=2,
                    up_book_age_ms=0,
                    down_book_age_ms=0,
                    recovery_ms=None,
                    transport_error=None,
                    dial_mode="DNS_HOSTNAME",
                    validated_peer_cache_available=True,
                    validated_peer_cache_age_ms=0,
                )
            store.save_transport_event(
                condition_id="c00",
                slug="btc-updown-5m-1800000000",
                incident_id=1,
                generation=1,
                event_type="RECONNECT_TRIGGER",
                trigger_reason="STALE_CLOB",
                recorded_timestamp_ms=base_ms + 50_000,
                second_offset=50,
                up_book_age_ms=3001,
                down_book_age_ms=3001,
                recovery_ms=None,
                transport_error=None,
                dial_mode="DNS_HOSTNAME",
                validated_peer_cache_available=True,
                validated_peer_cache_age_ms=50_000,
            )
            for event_type in ("GENERATION_FRESH", "RECOVERED"):
                store.save_transport_event(
                    condition_id="c00",
                    slug="btc-updown-5m-1800000000",
                    incident_id=1,
                    generation=2,
                    event_type=event_type,
                    trigger_reason=None,
                    recorded_timestamp_ms=base_ms + 64_000,
                    second_offset=64,
                    up_book_age_ms=0,
                    down_book_age_ms=0,
                    recovery_ms=14000 if event_type == "RECOVERED" else None,
                    transport_error=None,
                    dial_mode="VALIDATED_PEER_IP",
                    validated_peer_cache_available=True,
                    validated_peer_cache_age_ms=0,
                )
            store.set_meta("completion_reason", "FULL_4H_REACHED")
            store.set_meta("observation_ended_at", "2030-01-01T04:00:00+00:00")
            store.save_health(
                counters={
                    "rtds:chainlink:watchdog_fresh": 14_400,
                    "rtds:chainlink:stale_watchdog": 1,
                    "rtds:chainlink:stale_reconnects": 1,
                },
                connections={"v042-rtds-chainlink": "CONNECTED"},
            )
            store.close()
            result = audit_v042(
                database=database,
                prereg_path=PREREG,
                implementation_path=IMPLEMENTATION,
                result_path=result_path,
                project_root=ROOT,
            )
        self.assertEqual(result["verdict"], PASS_VERDICT)
        self.assertTrue(result["technical_passed"])
        self.assertTrue(result["safety_passed"])
        self.assertGreaterEqual(
            result["overall"]["transport_pre_stale_guard_exits"], 1
        )
        self.assertEqual(result["overall"]["trapped_positions"], 0)
        self.assertEqual(result["chainlink"]["snapshot_coverage"], 1.0)
        self.assertEqual(
            result["chainlink"]["watchdog_reconnect_accounting_rate"], 1.0
        )
        self.assertTrue(
            result["technical_gates"][
                "required_chainlink_watchdog_reconnect_accounting_rate_passed"
            ]
        )
        self.assertTrue(result["chainlink"]["watchdog_configuration_persisted"])

    def test_status_and_launch_gate_keep_every_safety_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "missing.db"
            status = v042_status(database)
            self.assertEqual(status["status"], "NOT_STARTED")
            self.assertEqual(status["paper_orders"], 0)
            self.assertFalse(status["wallet_required"])
            self.assertEqual(status["real_money"], "BLOQUEADO")
            if not IMPLEMENTATION.is_file():
                return
            with self.assertRaises(V042RunnerError):
                asyncio.run(
                    run_v042(
                        settings=Settings.from_env(),
                        prereg_path=PREREG,
                        implementation_path=IMPLEMENTATION,
                        launch_path=Path(temporary) / "missing.json",
                        output_db=database,
                    )
                )
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
