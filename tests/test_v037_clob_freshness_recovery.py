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
from polymarket_bot.v037_capture import V037Store
from polymarket_bot.v037_contract import evaluate_probe, load_and_verify_prereg
from polymarket_bot.v037_runner import (
    V037RunnerError,
    clob_book_ages_ms,
    clob_generation_is_fresh,
    run_v037,
    supervise_clob_feed,
    v037_status,
)


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "data" / "prereg_v037_clob_freshness_recovery_4h.json"
IMPLEMENTATION = ROOT / "data" / "implementation_v037_clob_freshness_recovery_4h.json"
DESIGN = ROOT / "data" / "diagnostico_v037_clob_freshness_recovery.json"


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


class _EventSink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def save_watchdog_event(self, **event: object) -> None:
        self.events.append(dict(event))


class V037ClobFreshnessRecoveryTests(unittest.TestCase):
    def test_design_uses_one_fixed_recovery_budget_without_grid(self) -> None:
        design = json.loads(DESIGN.read_text(encoding="utf-8"))
        self.assertEqual(
            design["decision"],
            "PREPARE_ONE_FRESH_V037_CLOB_RECOVERY_REPLICATION",
        )
        self.assertTrue(design["all_design_gates_passed"])
        self.assertFalse(design["derivation"]["parameter_grid_used"])
        self.assertEqual(design["derivation"]["watchdog_timeout_ms"], 3000)
        self.assertEqual(design["derivation"]["absolute_exit_deadline_offset"], 111)
        self.assertFalse(design["closed_replay_without_watchdog"]["validation_credit"])

    def test_prereg_freezes_watchdog_deadline_and_safety(self) -> None:
        prereg = load_and_verify_prereg(PREREG, project_root=ROOT)
        self.assertEqual(prereg["watchdog_contract"]["timeout_ms"], 3000)
        self.assertFalse(
            prereg["watchdog_contract"]["connected_label_alone_is_sufficient"]
        )
        self.assertEqual(prereg["probe_contract"]["absolute_exit_deadline_offset"], 111)
        self.assertEqual(prereg["probe_contract"]["decision_offset_max_inclusive"], 90)
        self.assertFalse(prereg["data_policy"]["prices_stored"])
        self.assertFalse(prereg["data_policy"]["pnl_calculated"])
        self.assertEqual(prereg["safety"]["real_money"], "BLOQUEADO")

    def test_last_decision_holds_twenty_seconds_and_targets_111(self) -> None:
        contract = load_and_verify_prereg(PREREG, project_root=ROOT)["probe_contract"]
        result = evaluate_probe(_path(), decision_offset=90, outcome="Down", contract=contract)
        self.assertEqual(result["status"], "EXITED")
        self.assertEqual(result["target_exit_offset"], 111)
        self.assertEqual(result["planned_holding_seconds"], 20)

    def test_stale_depth_never_counts_as_exit(self) -> None:
        snapshots = _path()
        for offset in range(111, 117):
            snapshots[offset]["up_book_fresh"] = 0
            snapshots[offset]["down_book_fresh"] = 0
        contract = load_and_verify_prereg(PREREG, project_root=ROOT)["probe_contract"]
        result = evaluate_probe(
            snapshots, decision_offset=90, outcome="Up", contract=contract
        )
        self.assertEqual(result["status"], "TRAPPED")
        self.assertEqual(
            result["reason"], "NO_FRESH_FULL_DEPTH_SELECTED_BID_BY_TARGET_PLUS_GRACE"
        )

    def test_book_age_and_generation_require_both_new_books(self) -> None:
        state = V031CaptureState()
        state.condition_id = "c"
        state.token_sides = {"up": "Up", "down": "Down"}
        now_ms = int(time.time() * 1000)
        state.ingest_clob(
            json.dumps(
                [
                    {
                        "event_type": "book",
                        "asset_id": "up",
                        "timestamp": str(now_ms),
                        "bids": [{"price": "0.4", "size": "100"}],
                        "asks": [{"price": "0.6", "size": "100"}],
                    },
                    {
                        "event_type": "book",
                        "asset_id": "down",
                        "timestamp": str(now_ms),
                        "bids": [{"price": "0.4", "size": "100"}],
                        "asks": [{"price": "0.6", "size": "100"}],
                    },
                ]
            ),
            received_timestamp_ms=now_ms,
        )
        self.assertEqual(clob_book_ages_ms(state, now_timestamp_ms=now_ms), (0, 0))
        self.assertTrue(
            clob_generation_is_fresh(
                state,
                generation_started_ms=now_ms,
                now_timestamp_ms=now_ms,
                timeout_ms=3000,
            )
        )
        self.assertFalse(
            clob_generation_is_fresh(
                state,
                generation_started_ms=now_ms + 1,
                now_timestamp_ms=now_ms + 1,
                timeout_ms=3000,
            )
        )

    def test_supervisor_forces_reconnect_and_records_fresh_recovery(self) -> None:
        async def scenario() -> tuple[_EventSink, V031CaptureState]:
            state = V031CaptureState()
            state.condition_id = "c"
            state.token_sides = {"up": "Up", "down": "Down"}
            sink = _EventSink()
            outer_stop = asyncio.Event()
            calls = 0

            async def fake_feed(**kwargs: object) -> None:
                nonlocal calls
                calls += 1
                handler = kwargs["handler"]
                session_stop = kwargs["stop_event"]
                now_ms = int(time.time() * 1000)
                payload = json.dumps(
                    [
                        {
                            "event_type": "book",
                            "asset_id": "up",
                            "timestamp": str(now_ms),
                            "bids": [{"price": "0.4", "size": "100"}],
                            "asks": [{"price": "0.6", "size": "100"}],
                        },
                        {
                            "event_type": "book",
                            "asset_id": "down",
                            "timestamp": str(now_ms),
                            "bids": [{"price": "0.4", "size": "100"}],
                            "asks": [{"price": "0.6", "size": "100"}],
                        },
                    ]
                )
                await handler(payload)  # type: ignore[operator]
                if calls >= 2:
                    await asyncio.sleep(0.03)
                    outer_stop.set()
                await session_stop.wait()  # type: ignore[union-attr]

            contract = {
                "timeout_ms": 100,
                "poll_ms": 10,
                "forced_reconnect_backoff_ms": 1,
                "recovery_service_level_ms": 500,
            }
            with patch("polymarket_bot.v037_runner._websocket_feed", new=fake_feed):
                await supervise_clob_feed(
                    settings=Settings.from_env(),
                    state=state,
                    store=sink,  # type: ignore[arg-type]
                    condition_id="c",
                    slug="s",
                    market_start_ms=int(time.time() * 1000),
                    up_token_id="up",
                    down_token_id="down",
                    stop_event=outer_stop,
                    watchdog_contract=contract,
                )
            self.assertGreaterEqual(calls, 2)
            return sink, state

        sink, state = asyncio.run(scenario())
        event_types = [event["event_type"] for event in sink.events]
        self.assertIn("STALE_TRIGGER", event_types)
        self.assertIn("RECOVERED", event_types)
        self.assertGreaterEqual(event_types.count("GENERATION_FRESH"), 2)
        self.assertEqual(state.counters["clob:watchdog_forced_reconnects"], 1)
        recovery = next(event for event in sink.events if event["event_type"] == "RECOVERED")
        self.assertLessEqual(int(recovery["recovery_ms"]), 500)

    def test_store_has_isolated_watchdog_journal_and_no_price_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "v037.db"
            store = V037Store(database)
            store.open(
                preregistration_sha256="p",
                implementation_sha256="i",
                launch_manifest_sha256="l",
                now_timestamp=1_800_000_000.0,
            )
            self.assertEqual(store.meta()["variant"], "V0.37_FRESH_CLOB_RECOVERY_4H")
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
                    for row in connection.execute("PRAGMA table_info(v037_snapshots)")
                }
            finally:
                connection.close()
        self.assertIn("v037_clob_watchdog_events", tables)
        self.assertNotIn("v036_snapshots", tables)
        self.assertFalse(
            any("price" in column or "pnl" in column for column in snapshot_columns)
        )

    def test_status_before_launch_keeps_all_safety_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status = v037_status(Path(temporary) / "missing.db")
        self.assertEqual(status["status"], "NOT_STARTED")
        self.assertEqual(status["paper_orders"], 0)
        self.assertFalse(status["wallet_required"])
        self.assertEqual(status["real_money"], "BLOQUEADO")

    def test_runner_cannot_create_database_without_launch_approval(self) -> None:
        if not IMPLEMENTATION.is_file():
            self.skipTest("El manifiesto se sella despues de la primera pasada de pruebas")
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "blocked.db"
            with self.assertRaises(V037RunnerError):
                asyncio.run(
                    run_v037(
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
