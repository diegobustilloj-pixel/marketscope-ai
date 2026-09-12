from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket_bot.config import Settings
from polymarket_bot.v031_capture import V031CaptureState
from polymarket_bot.v039_audit import audit_v039
from polymarket_bot.v039_capture import V039Store
from polymarket_bot.v039_contract import PASS_VERDICT, evaluate_probe, load_and_verify_prereg
from polymarket_bot.v039_runner import (
    V039RunnerError,
    ValidatedPeerCache,
    run_v039,
    supervise_validated_peer_clob,
    v039_status,
)


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "data" / "prereg_v039_validated_peer_dns_fallback_4h.json"
PREVIOUS_PREREG = ROOT / "data" / "prereg_v038_single_layer_recovery_4h.json"
IMPLEMENTATION = ROOT / "data" / "implementation_v039_validated_peer_dns_fallback_4h.json"
DESIGN = ROOT / "data" / "diagnostico_v039_validated_peer_dns_fallback.json"


def _row(offset: int, depth: float = 1000.0) -> dict:
    return {
        "second_offset": offset, "complete_v2": 1,
        "up_book_fresh": 1, "down_book_fresh": 1,
        "up_bid_depth_top5": depth, "up_ask_depth_top5": depth,
        "down_bid_depth_top5": depth, "down_ask_depth_top5": depth,
    }


def _path(end: int = 160) -> dict[int, dict]:
    return {offset: _row(offset) for offset in range(end + 1)}


def _book_payload(now_ms: int) -> str:
    return json.dumps([
        {
            "event_type": "book", "asset_id": outcome.lower(),
            "timestamp": str(now_ms),
            "bids": [{"price": "0.4", "size": "100"}],
            "asks": [{"price": "0.6", "size": "100"}],
        }
        for outcome in ("Up", "Down")
    ])


class _TLSObject:
    server_hostname = "ws-subscriptions-clob.polymarket.com"


class _Transport:
    def __init__(self, *, peer_ip: str = "104.18.34.205", tls: bool = True) -> None:
        self.peer_ip = peer_ip
        self.tls = tls

    def get_extra_info(self, name: str) -> object:
        if name == "peername":
            return (self.peer_ip, 443)
        if name == "ssl_object":
            return _TLSObject() if self.tls else None
        return None


class _FakeWebSocket:
    def __init__(self, *, outer_stop: asyncio.Event, stop_after_fresh: bool) -> None:
        self.outer_stop = outer_stop
        self.stop_after_fresh = stop_after_fresh
        self.transport = _Transport()
        self.receives = 0
        self.sent: list[str] = []

    async def send(self, value: str) -> None:
        self.sent.append(value)

    async def recv(self) -> str:
        self.receives += 1
        if self.receives == 1:
            if self.stop_after_fresh:
                async def stop_soon() -> None:
                    await asyncio.sleep(0.03)
                    self.outer_stop.set()

                asyncio.create_task(stop_soon())
            return _book_payload(int(time.time() * 1000))
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class _FakeConnection:
    def __init__(self, websocket: _FakeWebSocket | None = None, error: Exception | None = None) -> None:
        self.websocket = websocket
        self.error = error

    async def __aenter__(self) -> _FakeWebSocket:
        if self.error is not None:
            raise self.error
        assert self.websocket is not None
        return self.websocket

    async def __aexit__(self, *args: object) -> bool:
        return False


class _EventSink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def save_transport_event(self, **event: object) -> None:
        self.events.append(dict(event))


def _fast_contract() -> dict:
    return {
        "stale_timeout_ms": 100, "poll_ms": 10, "heartbeat_ms": 1000,
        "open_timeout_ms": 100, "close_timeout_ms": 10,
        "reconnect_backoff_schedule_ms": [1, 2, 3],
        "recovery_service_level_ms": 500,
    }


class V039ValidatedPeerDnsFallbackTests(unittest.TestCase):
    def test_design_identifies_only_dns_slo_failure_without_grid(self) -> None:
        design = json.loads(DESIGN.read_text(encoding="utf-8"))
        self.assertEqual(
            design["decision"],
            "PREPARE_ONE_FRESH_V039_VALIDATED_PEER_DNS_FALLBACK_REPLICATION",
        )
        self.assertTrue(design["all_design_gates_passed"])
        self.assertEqual(design["observed_failure"]["gaierror_getaddrinfo_events"], 55)
        self.assertEqual(len(design["observed_failure"]["slow_relevant_incidents"]), 2)
        self.assertFalse(design["selected_transport_remedy"]["parameter_grid_used"])
        self.assertFalse(
            design["selected_transport_remedy"]["certificate_verification_disabled"]
        )

    def test_prereg_preserves_probe_contract_and_freezes_fallback_exercise(self) -> None:
        prereg = load_and_verify_prereg(PREREG, project_root=ROOT)
        previous = json.loads(PREVIOUS_PREREG.read_text(encoding="utf-8"))
        self.assertEqual(prereg["probe_contract"], previous["probe_contract"])
        self.assertEqual(prereg["capture_contract"], previous["capture_contract"])
        transport = prereg["transport_contract"]
        self.assertEqual(transport["reconnect_owner_count"], 1)
        self.assertEqual(transport["validated_peer_cache_scope"], "PROCESS_MEMORY_ONLY")
        self.assertTrue(transport["original_tls_server_hostname_explicit"])
        self.assertFalse(transport["runtime_peer_ip_persisted"])
        self.assertEqual(
            prereg["technical_gates"]["minimum_validated_peer_fallback_fresh_recoveries"],
            1,
        )
        self.assertEqual(prereg["safety"]["real_money"], "BLOQUEADO")

    def test_previous_failure_path_still_exits_at_67(self) -> None:
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

    def test_cache_accepts_only_direct_global_tls_peer_with_expected_sni(self) -> None:
        cache = ValidatedPeerCache()
        websocket = type("WS", (), {"transport": _Transport()})()
        self.assertTrue(
            cache.remember(
                websocket,
                expected_hostname="ws-subscriptions-clob.polymarket.com",
                proxy_enabled=False,
            )
        )
        self.assertEqual(cache.peer_ip, "104.18.34.205")
        override = cache.connect_override(
            expected_hostname="ws-subscriptions-clob.polymarket.com",
            port=443,
            proxy_enabled=False,
        )
        self.assertEqual(
            override,
            {
                "host": "104.18.34.205", "port": 443,
                "server_hostname": "ws-subscriptions-clob.polymarket.com",
            },
        )
        self.assertIsNone(
            cache.connect_override(
                expected_hostname="ws-subscriptions-clob.polymarket.com",
                port=443,
                proxy_enabled=True,
            )
        )

    def test_cache_rejects_private_non_tls_wrong_sni_and_proxy(self) -> None:
        for transport, hostname, proxy in (
            (_Transport(peer_ip="127.0.0.1"), "ws-subscriptions-clob.polymarket.com", False),
            (_Transport(tls=False), "ws-subscriptions-clob.polymarket.com", False),
            (_Transport(), "wrong.example", False),
            (_Transport(), "ws-subscriptions-clob.polymarket.com", True),
        ):
            cache = ValidatedPeerCache()
            websocket = type("WS", (), {"transport": transport})()
            self.assertFalse(
                cache.remember(
                    websocket, expected_hostname=hostname, proxy_enabled=proxy
                )
            )
            self.assertFalse(cache.available)

    def test_stale_session_recovers_through_validated_peer_without_dns(self) -> None:
        async def scenario() -> tuple[_EventSink, list[dict], ValidatedPeerCache]:
            state = V031CaptureState()
            state.condition_id = "c"
            state.token_sides = {"up": "Up", "down": "Down"}
            sink = _EventSink()
            cache = ValidatedPeerCache()
            outer_stop = asyncio.Event()
            calls: list[dict] = []

            def fake_connect(uri: str, **kwargs: object) -> _FakeConnection:
                calls.append({"uri": uri, **kwargs})
                return _FakeConnection(
                    _FakeWebSocket(
                        outer_stop=outer_stop,
                        stop_after_fresh=len(calls) == 2,
                    )
                )

            with patch("polymarket_bot.v039_runner.connect", new=fake_connect):
                await supervise_validated_peer_clob(
                    settings=Settings.from_env(), state=state,
                    store=sink, peer_cache=cache,  # type: ignore[arg-type]
                    condition_id="c", slug="s",
                    market_start_ms=int(time.time() * 1000),
                    up_token_id="up", down_token_id="down",
                    stop_event=outer_stop, transport_contract=_fast_contract(),
                )
            return sink, calls, cache

        sink, calls, cache = asyncio.run(scenario())
        self.assertTrue(cache.available)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("host", calls[0])
        self.assertEqual(calls[1]["host"], "104.18.34.205")
        self.assertEqual(
            calls[1]["server_hostname"],
            "ws-subscriptions-clob.polymarket.com",
        )
        event_types = [event["event_type"] for event in sink.events]
        self.assertEqual(event_types.count("RECONNECT_TRIGGER"), 1)
        recovery = next(event for event in sink.events if event["event_type"] == "RECOVERED")
        self.assertEqual(recovery["dial_mode"], "VALIDATED_PEER_IP")
        self.assertLessEqual(int(recovery["recovery_ms"]), 500)

    def test_dns_failure_uses_cached_peer_and_preserves_original_uri_sni(self) -> None:
        async def scenario() -> tuple[_EventSink, list[dict]]:
            state = V031CaptureState()
            state.condition_id = "c"
            state.token_sides = {"up": "Up", "down": "Down"}
            sink = _EventSink()
            cache = ValidatedPeerCache(
                peer_ip="104.18.34.205",
                validated_at_ms=int(time.time() * 1000),
            )
            outer_stop = asyncio.Event()
            calls: list[dict] = []

            def fake_connect(uri: str, **kwargs: object) -> _FakeConnection:
                calls.append({"uri": uri, **kwargs})
                if len(calls) == 1:
                    return _FakeConnection(error=socket.gaierror(11001, "getaddrinfo failed"))
                return _FakeConnection(
                    _FakeWebSocket(outer_stop=outer_stop, stop_after_fresh=True)
                )

            with patch("polymarket_bot.v039_runner.connect", new=fake_connect):
                await supervise_validated_peer_clob(
                    settings=Settings.from_env(), state=state,
                    store=sink, peer_cache=cache,  # type: ignore[arg-type]
                    condition_id="c", slug="s",
                    market_start_ms=int(time.time() * 1000),
                    up_token_id="up", down_token_id="down",
                    stop_event=outer_stop, transport_contract=_fast_contract(),
                )
            return sink, calls

        sink, calls = asyncio.run(scenario())
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["uri"], calls[1]["uri"])
        self.assertNotIn("host", calls[0])
        self.assertEqual(calls[1]["host"], "104.18.34.205")
        self.assertEqual(calls[1]["port"], 443)
        self.assertEqual(
            calls[1]["server_hostname"], "ws-subscriptions-clob.polymarket.com"
        )
        self.assertIsNone(calls[1]["proxy"])
        trigger = next(event for event in sink.events if event["event_type"] == "RECONNECT_TRIGGER")
        recovery = next(event for event in sink.events if event["event_type"] == "RECOVERED")
        self.assertEqual(trigger["trigger_reason"], "TRANSPORT_gaierror")
        self.assertEqual(trigger["dial_mode"], "DNS_HOSTNAME")
        self.assertEqual(recovery["dial_mode"], "VALIDATED_PEER_IP")

    def test_failed_cached_peer_attempt_returns_to_hostname(self) -> None:
        async def scenario() -> list[dict]:
            state = V031CaptureState()
            state.condition_id = "c"
            state.token_sides = {"up": "Up", "down": "Down"}
            cache = ValidatedPeerCache(
                peer_ip="104.18.34.205",
                validated_at_ms=int(time.time() * 1000),
            )
            outer_stop = asyncio.Event()
            calls: list[dict] = []

            def fake_connect(uri: str, **kwargs: object) -> _FakeConnection:
                calls.append({"uri": uri, **kwargs})
                if len(calls) == 1:
                    return _FakeConnection(
                        error=socket.gaierror(11001, "getaddrinfo failed")
                    )
                if len(calls) == 2:
                    return _FakeConnection(error=OSError("cached peer unavailable"))
                return _FakeConnection(
                    _FakeWebSocket(outer_stop=outer_stop, stop_after_fresh=True)
                )

            with patch("polymarket_bot.v039_runner.connect", new=fake_connect):
                await supervise_validated_peer_clob(
                    settings=Settings.from_env(), state=state,
                    store=_EventSink(), peer_cache=cache,  # type: ignore[arg-type]
                    condition_id="c", slug="s",
                    market_start_ms=int(time.time() * 1000),
                    up_token_id="up", down_token_id="down",
                    stop_event=outer_stop, transport_contract=_fast_contract(),
                )
            return calls

        calls = asyncio.run(scenario())
        self.assertEqual(len(calls), 3)
        self.assertNotIn("host", calls[0])
        self.assertEqual(calls[1]["host"], "104.18.34.205")
        self.assertNotIn("host", calls[2])

    def test_store_records_dial_metadata_but_never_peer_ip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "v039.db"
            store = V039Store(database)
            store.open(
                preregistration_sha256="p", implementation_sha256="i",
                launch_manifest_sha256="l", now_timestamp=1_800_000_000.0,
            )
            self.assertFalse(store.meta()["runtime_peer_ip_stored"])
            store.close()
            connection = sqlite3.connect(database)
            try:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                event_columns = {row[1] for row in connection.execute(
                    "PRAGMA table_info(v039_clob_transport_events)"
                )}
                snapshot_columns = {row[1] for row in connection.execute(
                    "PRAGMA table_info(v039_snapshots)"
                )}
            finally:
                connection.close()
        self.assertIn("v039_clob_transport_events", tables)
        self.assertIn("dial_mode", event_columns)
        self.assertIn("validated_peer_cache_available", event_columns)
        self.assertNotIn("peer_ip", event_columns)
        self.assertFalse(any("price" in name or "pnl" in name for name in snapshot_columns))

    def test_terminal_auditor_passes_full_cached_recovery_fixture(self) -> None:
        if not IMPLEMENTATION.is_file():
            self.skipTest("El manifiesto se sella despues de la primera pasada de pruebas")
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "fixture.db"
            result_path = Path(temporary) / "result.json"
            store = V039Store(database)
            store.open(
                preregistration_sha256="p", implementation_sha256="i",
                launch_manifest_sha256="l", now_timestamp=1_800_000_000.0,
            )
            market_rows = []
            snapshot_rows = []
            base_ms = 1_800_000_000_000
            for market_index in range(48):
                condition_id = f"c{market_index:02d}"
                slug = f"btc-updown-5m-{1_800_000_000 + market_index * 300}"
                start_ms = base_ms + market_index * 300_000
                market_rows.append(
                    (
                        condition_id, slug, f"e{market_index}", start_ms,
                        start_ms + 300_000, f"up{market_index}", f"down{market_index}",
                        "2030-01-01T00:00:00+00:00", "twap 60 seconds", "VERIFIED",
                        60, "crypto_prices_chainlink_60s", "COMPLETED", None,
                        122, 122, 0, 122, 0,
                    )
                )
                for offset in range(300):
                    depth = 199.0 if market_index == 0 and offset == 41 else 1000.0
                    timestamp_ms = start_ms + offset * 1000
                    snapshot_rows.append(
                        (
                            condition_id, offset, timestamp_ms, timestamp_ms,
                            timestamp_ms, timestamp_ms, 0, 1, 60,
                            timestamp_ms, timestamp_ms, 0, 1,
                            timestamp_ms, timestamp_ms, 0, 1,
                            timestamp_ms, timestamp_ms, 0, 1,
                            depth, 1000.0, 1000.0, 1000.0, 1,
                        )
                    )
            store.db.executemany(
                "INSERT INTO v039_markets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                market_rows,
            )
            store.db.executemany(
                "INSERT INTO v039_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                snapshot_rows,
            )
            for market_index in range(48):
                condition_id = f"c{market_index:02d}"
                slug = f"btc-updown-5m-{1_800_000_000 + market_index * 300}"
                store.save_transport_event(
                    condition_id=condition_id, slug=slug, incident_id=None,
                    generation=1, event_type="GENERATION_FRESH", trigger_reason=None,
                    recorded_timestamp_ms=base_ms + market_index * 300_000 + 2000,
                    second_offset=2, up_book_age_ms=0, down_book_age_ms=0,
                    recovery_ms=None, transport_error=None, dial_mode="DNS_HOSTNAME",
                    validated_peer_cache_available=True,
                    validated_peer_cache_age_ms=0,
                )
            store.save_transport_event(
                condition_id="c00", slug="btc-updown-5m-1800000000",
                incident_id=1, generation=1, event_type="RECONNECT_TRIGGER",
                trigger_reason="STALE_CLOB", recorded_timestamp_ms=base_ms + 50_000,
                second_offset=50, up_book_age_ms=3001, down_book_age_ms=3001,
                recovery_ms=None, transport_error=None, dial_mode="DNS_HOSTNAME",
                validated_peer_cache_available=True,
                validated_peer_cache_age_ms=50_000,
            )
            store.save_transport_event(
                condition_id="c00", slug="btc-updown-5m-1800000000",
                incident_id=1, generation=2, event_type="GENERATION_FRESH",
                trigger_reason=None, recorded_timestamp_ms=base_ms + 52_000,
                second_offset=52, up_book_age_ms=0, down_book_age_ms=0,
                recovery_ms=None, transport_error=None,
                dial_mode="VALIDATED_PEER_IP",
                validated_peer_cache_available=True,
                validated_peer_cache_age_ms=0,
            )
            store.save_transport_event(
                condition_id="c00", slug="btc-updown-5m-1800000000",
                incident_id=1, generation=2, event_type="RECOVERED",
                trigger_reason=None, recorded_timestamp_ms=base_ms + 52_000,
                second_offset=52, up_book_age_ms=0, down_book_age_ms=0,
                recovery_ms=2000, transport_error=None,
                dial_mode="VALIDATED_PEER_IP",
                validated_peer_cache_available=True,
                validated_peer_cache_age_ms=0,
            )
            store.set_meta("completion_reason", "FULL_4H_REACHED")
            store.set_meta("observation_ended_at", "2030-01-01T04:00:00+00:00")
            store.close()
            result = audit_v039(
                database=database, prereg_path=PREREG,
                implementation_path=IMPLEMENTATION, result_path=result_path,
                project_root=ROOT,
            )
        self.assertEqual(result["verdict"], PASS_VERDICT)
        self.assertTrue(result["technical_passed"])
        self.assertTrue(result["safety_passed"])
        self.assertEqual(result["overall"]["trapped_positions"], 0)
        self.assertGreaterEqual(result["transport"]["validated_peer_fallback_recoveries"], 1)

    def test_status_and_launch_gate_keep_safety_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "missing.db"
            status = v039_status(database)
            self.assertEqual(status["status"], "NOT_STARTED")
            self.assertEqual(status["paper_orders"], 0)
            self.assertFalse(status["wallet_required"])
            self.assertEqual(status["real_money"], "BLOQUEADO")
            if not IMPLEMENTATION.is_file():
                return
            with self.assertRaises(V039RunnerError):
                asyncio.run(
                    run_v039(
                        settings=Settings.from_env(), prereg_path=PREREG,
                        implementation_path=IMPLEMENTATION,
                        launch_path=Path(temporary) / "missing.json",
                        output_db=database,
                    )
                )
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
