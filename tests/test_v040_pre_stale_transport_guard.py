from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.config import Settings
from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v040_audit import audit_v040
from polymarket_bot.v040_capture import V040Store
from polymarket_bot.v040_contract import (
    NEW_PROBE_FIELDS,
    PASS_VERDICT,
    evaluate_probe,
    load_and_verify_prereg,
)
from polymarket_bot.v040_runner import V040RunnerError, run_v040, v040_status


DESIGN = ROOT / "data" / "diagnostico_v040_pre_stale_transport_guard.json"
PREREG = ROOT / "data" / "prereg_v040_pre_stale_transport_guard_4h.json"
IMPLEMENTATION = ROOT / "data" / "implementation_v040_pre_stale_transport_guard_4h.json"
PREVIOUS_PREREG = ROOT / "data" / "prereg_v039_validated_peer_dns_fallback_4h.json"
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


class V040PreStaleTransportGuardTests(unittest.TestCase):
    def test_design_replays_eighty_one_markets_without_traps_or_grid(self) -> None:
        design = json.loads(DESIGN.read_text(encoding="utf-8"))
        self.assertEqual(
            design["decision"],
            "PREPARE_ONE_FRESH_V040_PRE_STALE_TRANSPORT_GUARD_REPLICATION",
        )
        self.assertTrue(design["all_design_gates_passed"])
        self.assertEqual(design["closed_replay"]["completed_markets"], 81)
        combined = design["closed_replay"]["combined"]
        self.assertEqual(combined["trapped_positions"], 0)
        self.assertEqual(combined["exit_success_within_grace_rate"], 1.0)
        self.assertEqual(combined["transport_pre_stale_guard_exits"], 85)
        self.assertFalse(design["derivation"]["parameter_grid_used"])

    def test_prereg_preserves_v039_and_adds_only_frozen_guard_fields(self) -> None:
        prereg = load_and_verify_prereg(PREREG, project_root=ROOT)
        previous = json.loads(PREVIOUS_PREREG.read_text(encoding="utf-8"))
        current_probe = dict(prereg["probe_contract"])
        for key, value in NEW_PROBE_FIELDS.items():
            self.assertEqual(current_probe.pop(key), value)
        self.assertEqual(current_probe, previous["probe_contract"])
        self.assertEqual(prereg["capture_contract"], previous["capture_contract"])
        self.assertEqual(prereg["transport_contract"], previous["transport_contract"])
        self.assertEqual(prereg["safety"]["real_money"], "BLOQUEADO")
        self.assertTrue(
            prereg["limitations"][
                "redundant_network_or_remote_executor_required_before_real_money"
            ]
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
            database = Path(temporary) / "v040.db"
            store = V040Store(database)
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
                    for row in connection.execute("PRAGMA table_info(v040_markets)")
                }
                snapshot_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(v040_snapshots)")
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
            store = V040Store(database)
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
                    """INSERT INTO v040_markets(
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
                    "INSERT INTO v040_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            store.close()
            result = audit_v040(
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

    def test_status_and_launch_gate_keep_every_safety_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "missing.db"
            status = v040_status(database)
            self.assertEqual(status["status"], "NOT_STARTED")
            self.assertEqual(status["paper_orders"], 0)
            self.assertFalse(status["wallet_required"])
            self.assertEqual(status["real_money"], "BLOQUEADO")
            if not IMPLEMENTATION.is_file():
                return
            with self.assertRaises(V040RunnerError):
                asyncio.run(
                    run_v040(
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
