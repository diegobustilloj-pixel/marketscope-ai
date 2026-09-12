from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket_bot.config import Settings
from polymarket_bot.v031_capture import V031CaptureState
from polymarket_bot.v038_capture import V038Store
from polymarket_bot.v038_contract import evaluate_probe, load_and_verify_prereg
from polymarket_bot.v038_runner import (
    V038RunnerError,
    clob_book_ages_ms,
    clob_generation_is_fresh,
    reconnect_backoff_ms,
    run_v038,
    supervise_single_layer_clob,
    v038_status,
)


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "data" / "prereg_v038_single_layer_recovery_4h.json"
IMPLEMENTATION = ROOT / "data" / "implementation_v038_single_layer_recovery_4h.json"
DESIGN = ROOT / "data" / "diagnostico_v038_single_layer_recovery_budget.json"


def _row(offset: int, depth: float = 1000.0) -> dict:
    return {
        "second_offset": offset,
        "complete_v2": 1,
        "up_book_fresh": 1,
        "down_book_fresh": 1,
        "up_bid_depth_top5": depth,
        "up_ask_depth_top5": depth,
        "down_bid_depth_top5": depth,
        "down_ask_depth_top5": depth,
    }


def _path(end: int = 160) -> dict[int, dict]:
    return {offset: _row(offset) for offset in range(end + 1)}


def _book_payload(now_ms: int) -> str:
    return json.dumps(
        [
            {
                "event_type": "book",
                "asset_id": outcome.lower(),
                "timestamp": str(now_ms),
                "bids": [{"price": "0.4", "size": "100"}],
                "asks": [{"price": "0.6", "size": "100"}],
            }
            for outcome in ("Up", "Down")
        ]
    )


class _EventSink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def save_transport_event(self, **event: object) -> None:
        self.events.append(dict(event))


class _FakeWebSocket:
    def __init__(self, generation: int, outer_stop: asyncio.Event) -> None:
        self.generation = generation
        self.outer_stop = outer_stop
        self.receives = 0
        self.sent: list[str] = []

    async def send(self, value: str) -> None:
        self.sent.append(value)

    async def recv(self) -> str:
        self.receives += 1
        if self.receives == 1:
            now_ms = int(time.time() * 1000)
            if self.generation == 2:
                async def stop_soon() -> None:
                    await asyncio.sleep(0.03)
                    self.outer_stop.set()

                asyncio.create_task(stop_soon())
            return _book_payload(now_ms)
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class _FakeConnection:
    def __init__(self, websocket: _FakeWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> _FakeWebSocket:
        return self.websocket

    async def __aexit__(self, *args: object) -> bool:
        if self.websocket.generation == 1:
            await asyncio.sleep(0.02)
        return False


class V038SingleLayerRecoveryTests(unittest.TestCase):
    def test_design_uses_fixed_derived_budget_without_validation_credit(self) -> None:
        design = json.loads(DESIGN.read_text(encoding="utf-8"))
        self.assertEqual(
            design["decision"],
            "PREPARE_ONE_FRESH_V038_SINGLE_LAYER_RECOVERY_REPLICATION",
        )
        self.assertTrue(design["all_design_gates_passed"])
        self.assertFalse(design["derivation"]["parameter_grid_used"])
        self.assertEqual(design["derivation"]["operational_retry_budget_seconds"], 10)
        self.assertEqual(design["derivation"]["absolute_retry_deadline_offset"], 121)
        self.assertFalse(design["closed_replay"]["fresh_validation_credit"])
        self.assertEqual(design["closed_replay"]["trapped_positions"], 0)

    def test_prereg_freezes_single_owner_retry_and_safety(self) -> None:
        prereg = load_and_verify_prereg(PREREG, project_root=ROOT)
        transport = prereg["transport_contract"]
        self.assertEqual(transport["reconnect_owner_count"], 1)
        self.assertFalse(transport["socket_session_has_internal_reconnect"])
        self.assertEqual(transport["reconnect_backoff_schedule_ms"], [250, 500, 1000])
        self.assertEqual(prereg["probe_contract"]["operational_retry_budget_seconds"], 10)
        self.assertEqual(prereg["probe_contract"]["absolute_retry_deadline_offset"], 121)
        self.assertFalse(prereg["data_policy"]["prices_stored"])
        self.assertFalse(prereg["data_policy"]["pnl_calculated"])
        self.assertEqual(prereg["safety"]["real_money"], "BLOQUEADO")

    def test_previous_one_second_miss_exits_at_offset_67(self) -> None:
        snapshots = _path()
        for offset in range(59, 67):
            snapshots[offset]["up_book_fresh"] = 0
            snapshots[offset]["down_book_fresh"] = 0
        contract = load_and_verify_prereg(PREREG, project_root=ROOT)["probe_contract"]
        for outcome in ("Up", "Down"):
            result = evaluate_probe(
                snapshots, decision_offset=30, outcome=outcome, contract=contract
            )
            self.assertEqual(result["status"], "EXITED")
            self.assertEqual(result["exit_offset"], 67)
            self.assertEqual(result["exit_delay_seconds"], 6)
            self.assertEqual(result["retry_deadline_offset"], 71)

    def test_missing_fresh_exit_through_retry_deadline_is_trapped(self) -> None:
        snapshots = _path()
        for offset in range(61, 72):
            snapshots[offset]["up_book_fresh"] = 0
        contract = load_and_verify_prereg(PREREG, project_root=ROOT)["probe_contract"]
        result = evaluate_probe(
            snapshots, decision_offset=30, outcome="Up", contract=contract
        )
        self.assertEqual(result["status"], "TRAPPED")
        self.assertEqual(result["retry_deadline_offset"], 71)
        self.assertEqual(
            result["reason"],
            "NO_FRESH_FULL_DEPTH_SELECTED_BID_BY_OPERATIONAL_RETRY_DEADLINE",
        )

    def test_last_decision_targets_111_and_caps_retry_at_121(self) -> None:
        snapshots = _path()
        for offset in range(111, 121):
            snapshots[offset]["down_book_fresh"] = 0
        contract = load_and_verify_prereg(PREREG, project_root=ROOT)["probe_contract"]
        result = evaluate_probe(
            snapshots, decision_offset=90, outcome="Down", contract=contract
        )
        self.assertEqual(result["status"], "EXITED")
        self.assertEqual(result["target_exit_offset"], 111)
        self.assertEqual(result["retry_deadline_offset"], 121)
        self.assertEqual(result["exit_offset"], 121)
        self.assertEqual(result["exit_delay_seconds"], 10)
        self.assertEqual(result["planned_holding_seconds"], 20)

    def test_book_age_generation_and_backoff_are_deterministic(self) -> None:
        state = V031CaptureState()
        state.condition_id = "c"
        state.token_sides = {"up": "Up", "down": "Down"}
        now_ms = int(time.time() * 1000)
        state.ingest_clob(_book_payload(now_ms), received_timestamp_ms=now_ms)
        self.assertEqual(clob_book_ages_ms(state, now_timestamp_ms=now_ms), (0, 0))
        self.assertTrue(
            clob_generation_is_fresh(
                state,
                generation_started_ms=now_ms,
                now_timestamp_ms=now_ms,
                stale_timeout_ms=3000,
            )
        )
        self.assertFalse(
            clob_generation_is_fresh(
                state,
                generation_started_ms=now_ms + 1,
                now_timestamp_ms=now_ms + 1,
                stale_timeout_ms=3000,
            )
        )
        self.assertEqual(
            [reconnect_backoff_ms(i, [250, 500, 1000]) for i in range(1, 6)],
            [250, 500, 1000, 1000, 1000],
        )

    def test_single_layer_supervisor_reconnects_and_recovers(self) -> None:
        async def scenario() -> tuple[_EventSink, V031CaptureState, int]:
            state = V031CaptureState()
            state.condition_id = "c"
            state.token_sides = {"up": "Up", "down": "Down"}
            sink = _EventSink()
            outer_stop = asyncio.Event()
            calls = 0

            def fake_connect(*args: object, **kwargs: object) -> _FakeConnection:
                nonlocal calls
                calls += 1
                return _FakeConnection(_FakeWebSocket(calls, outer_stop))

            contract = {
                "stale_timeout_ms": 100,
                "poll_ms": 10,
                "heartbeat_ms": 1000,
                "open_timeout_ms": 100,
                "close_timeout_ms": 10,
                "reconnect_backoff_schedule_ms": [1, 2, 3],
                "recovery_service_level_ms": 500,
            }
            with patch("polymarket_bot.v038_runner.connect", new=fake_connect):
                await supervise_single_layer_clob(
                    settings=Settings.from_env(),
                    state=state,
                    store=sink,  # type: ignore[arg-type]
                    condition_id="c",
                    slug="s",
                    market_start_ms=int(time.time() * 1000),
                    up_token_id="up",
                    down_token_id="down",
                    stop_event=outer_stop,
                    transport_contract=contract,
                )
            return sink, state, calls

        sink, state, calls = asyncio.run(scenario())
        event_types = [event["event_type"] for event in sink.events]
        self.assertEqual(calls, 2)
        self.assertEqual(event_types.count("GENERATION_FRESH"), 2)
        self.assertEqual(event_types.count("RECONNECT_TRIGGER"), 1)
        self.assertEqual(event_types.count("RECOVERED"), 1)
        self.assertEqual(state.counters["clob:single_layer_reconnect_triggers"], 1)
        recovery = next(event for event in sink.events if event["event_type"] == "RECOVERED")
        self.assertGreaterEqual(int(recovery["recovery_ms"]), 20)
        self.assertLessEqual(int(recovery["recovery_ms"]), 500)

    def test_store_has_isolated_transport_journal_and_no_price_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "v038.db"
            store = V038Store(database)
            store.open(
                preregistration_sha256="p",
                implementation_sha256="i",
                launch_manifest_sha256="l",
                now_timestamp=1_800_000_000.0,
            )
            self.assertEqual(store.meta()["variant"], "V0.38_FRESH_SINGLE_LAYER_RECOVERY_4H")
            store.close()
            connection = sqlite3.connect(database)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                snapshot_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(v038_snapshots)")
                }
            finally:
                connection.close()
        self.assertIn("v038_clob_transport_events", tables)
        self.assertNotIn("v037_snapshots", tables)
        self.assertFalse(
            any("price" in column or "pnl" in column for column in snapshot_columns)
        )

    def test_status_and_launch_gate_keep_all_safety_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "missing.db"
            status = v038_status(database)
            self.assertEqual(status["status"], "NOT_STARTED")
            self.assertEqual(status["paper_orders"], 0)
            self.assertFalse(status["wallet_required"])
            self.assertEqual(status["real_money"], "BLOQUEADO")
            if not IMPLEMENTATION.is_file():
                return
            with self.assertRaises(V038RunnerError):
                asyncio.run(
                    run_v038(
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
