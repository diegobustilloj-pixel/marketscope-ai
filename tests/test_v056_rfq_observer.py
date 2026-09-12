from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v056_audit import audit_v056, inspect_database
from polymarket_bot.v056_contract import (
    build_preregistration,
    frozen_contract,
    load_and_verify_preregistration,
)
from polymarket_bot.v056_rfq_observer import (
    AuthOnlySender,
    V056ObserverError,
    V056Store,
    collect_rfq_activity,
    credential_preflight,
    database_footprint,
    run_v056_async,
    sanitize_inbound,
)


PREREG = ROOT / "data" / "prereg_v056_bounded_rfq_observer.json"
DATABASE = ROOT / "data" / "v056_bounded_rfq_observer.db"
RESULT = ROOT / "data" / "resultado_v056_bounded_rfq_observer.json"


def _valid_environment() -> dict[str, str]:
    return {
        "PM_RFQ_API_KEY": "api-key-test",
        "PM_RFQ_API_SECRET": "api-secret-test",
        "PM_RFQ_API_PASSPHRASE": "api-passphrase-test",
        "PM_RFQ_SIGNER_ADDRESS": "0x" + "12" * 20,
        "PM_RFQ_MAKER_ADDRESS": "0x" + "34" * 20,
        "PM_RFQ_SIGNATURE_TYPE": "2",
    }


def _request(index: int = 1, *, received_at_ms: int = 2_000_000) -> dict[str, object]:
    return {
        "type": "RFQ_REQUEST",
        "rfq_id": f"rfq-{index}",
        "requestor_public_id": f"requestor-{index}",
        "leg_position_ids": [str(index * 2), str(index * 2 + 1)],
        "condition_id": f"0x{index:064x}",
        "yes_position_id": str(index * 10),
        "no_position_id": str(index * 10 + 1),
        "direction": "BUY" if index % 2 else "SELL",
        "side": "YES",
        "requested_size": {"unit": "notional", "value_e6": "1000000"},
        "submission_deadline": received_at_ms + 500,
    }


def _trade(index: int = 1) -> dict[str, object]:
    return {
        "type": "RFQ_TRADE",
        "rfq_id": f"rfq-{index}",
        "requester_id": f"requestor-{index}",
        "condition_id": f"0x{index:064x}",
        "leg_position_ids": [str(index * 2), str(index * 2 + 1)],
        "direction": "BUY",
        "side": "YES",
        "price_e6": "450000",
        "size_e6": "1000000",
        "executed_at": 2_000_400,
    }


def _clock_samples(**_: Any) -> list[dict[str, int]]:
    return [
        {
            "local_send_ms": 1_000 + index,
            "local_receive_ms": 1_005 + index,
            "server_time_s": 1,
            "round_trip_ms": 5,
            "server_minus_local_lower_ms": -325 + index,
            "server_minus_local_upper_ms": -305 + index,
        }
        for index in range(9)
    ]


class _FakeSocket:
    def __init__(self, messages: list[object]) -> None:
        self.messages = list(messages)
        self.sent: list[dict[str, object]] = []

    async def send(self, value: str) -> None:
        self.sent.append(json.loads(value))

    async def recv(self) -> str:
        if self.messages:
            return json.dumps(self.messages.pop(0))
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class _FakeConnection:
    def __init__(self, socket: _FakeSocket) -> None:
        self.socket = socket

    async def __aenter__(self) -> _FakeSocket:
        return self.socket

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _FakeFactory:
    def __init__(self, messages: list[object]) -> None:
        self.socket = _FakeSocket(messages)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, endpoint: str, **kwargs: object) -> _FakeConnection:
        self.calls.append((endpoint, kwargs))
        return _FakeConnection(self.socket)


class V056RfqObserverTests(unittest.TestCase):
    def test_missing_credentials_are_reported_without_values(self) -> None:
        credentials, report = credential_preflight(frozen_contract(), environ={})
        self.assertIsNone(credentials)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertFalse(report["credential_values_exposed"])
        self.assertFalse(report["database_created"])

    def test_safe_type_two_credentials_are_redacted(self) -> None:
        credentials, report = credential_preflight(
            frozen_contract(), environ=_valid_environment()
        )
        self.assertIsNotNone(credentials)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(repr(credentials), "CredentialBundle(<redacted>)")
        self.assertNotIn("api-secret-test", repr(credentials))
        self.assertFalse(report["private_key_required"])

    def test_outbound_guard_allows_only_one_auth(self) -> None:
        credentials, _ = credential_preflight(
            frozen_contract(), environ=_valid_environment()
        )
        socket = _FakeSocket([])
        sender = AuthOnlySender()
        asyncio.run(sender.send_auth(socket, credentials))
        self.assertEqual([item["type"] for item in socket.sent], ["auth"])
        with self.assertRaises(V056ObserverError):
            asyncio.run(sender.send_auth(socket, credentials))
        with self.assertRaises(V056ObserverError):
            asyncio.run(sender.send(socket, {"type": "RFQ_QUOTE"}))

    def test_sanitizer_uses_binary_hashes_and_drops_secrets(self) -> None:
        payload = {
            "type": "auth",
            "success": True,
            "address": "0x" + "ab" * 20,
            "secret": "must-not-persist",
            "role": "maker",
        }
        record = sanitize_inbound(payload, 1000)
        serialized = json.dumps(
            {key: value.hex() if isinstance(value, bytes) else value for key, value in record.items()},
            sort_keys=True,
        )
        self.assertNotIn("must-not-persist", serialized)
        self.assertNotIn("0x" + "ab" * 20, serialized)
        self.assertIsInstance(record["payload_sha256"], bytes)
        self.assertEqual(len(record["payload_sha256"]), 32)

    def test_blocked_run_creates_no_database_and_attempts_no_network(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as temporary:
            folder = Path(temporary)
            prereg = folder / "prereg.json"
            database = folder / "observer.db"
            build_preregistration(output_path=prereg, project_root=ROOT)
            result = asyncio.run(
                run_v056_async(
                    prereg_path=prereg,
                    database_path=database,
                    project_root=ROOT,
                    environ={},
                    clock_sampler=_clock_samples,
                )
            )
            self.assertFalse(database.exists())
        self.assertEqual(result["status"], "BLOCKED_CREDENTIAL_PREFLIGHT")
        self.assertFalse(result["network_connection_attempted"])

    def test_storage_limit_is_terminal_and_never_reconnects(self) -> None:
        contract = frozen_contract()
        contract["storage"]["maximum_database_bytes"] = 4096
        contract["storage"]["transaction_batch_events"] = 1
        messages = [{"type": "auth", "success": True}, _request(1)]
        factory = _FakeFactory(messages)
        credentials, _ = credential_preflight(contract, environ=_valid_environment())
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as temporary:
            database = Path(temporary) / "limit.db"
            store = V056Store(database, contract["storage"])
            store.open_new(preregistration_sha256="test", duration_seconds=3600)
            run_id = store.start_run()
            result = asyncio.run(
                collect_rfq_activity(
                    endpoint=contract["transport"]["endpoint"],
                    credentials=credentials,
                    store=store,
                    run_id=run_id,
                    contract=contract,
                    connect_factory=factory,
                    duration_seconds=1,
                )
            )
            store.close()
        self.assertEqual(result["status"], "STORAGE_LIMIT_REACHED")
        self.assertEqual(result["reconnects"], 0)
        self.assertEqual(len(factory.calls), 1)

    def test_high_volume_storage_is_bounded_and_sampled(self) -> None:
        contract = frozen_contract()
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as temporary:
            database = Path(temporary) / "volume.db"
            store = V056Store(database, contract["storage"])
            store.open_new(preregistration_sha256="test", duration_seconds=3600)
            run_id = store.start_run()
            base_ms = store.run_started_at_ms or 2_000_000
            for index in range(1, 140_001):
                store.record(run_id, _request(index, received_at_ms=base_ms), base_ms)
            store.finish_run(
                run_id,
                status="COMPLETED",
                error_class=None,
                error_code=None,
                reconnects=0,
                clock_probe_errors=0,
            )
            stored_samples = store.request_samples_stored
            store.close()
            footprint = database_footprint(database)
        self.assertLess(footprint, 80 * 1024 * 1024)
        self.assertGreater(stored_samples, 7000)
        self.assertLess(stored_samples, 11000)

    def test_terminal_observer_and_audit_preserve_paper_only_contract(self) -> None:
        base_ms = 2_000_000
        messages: list[object] = [
            {"type": "auth", "success": True, "address": "0x" + "12" * 20},
            *[_request(index, received_at_ms=base_ms) for index in range(1, 2401)],
            _trade(1),
        ]
        factory = _FakeFactory(messages)
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as temporary:
            folder = Path(temporary)
            prereg = folder / "prereg.json"
            database = folder / "observer.db"
            result_path = folder / "result.json"
            build_preregistration(output_path=prereg, project_root=ROOT)
            run = asyncio.run(
                run_v056_async(
                    prereg_path=prereg,
                    database_path=database,
                    project_root=ROOT,
                    environ=_valid_environment(),
                    connect_factory=factory,
                    duration_seconds=0.5,
                    clock_sampler=_clock_samples,
                )
            )
            snapshot = inspect_database(
                prereg_path=prereg,
                database_path=database,
                project_root=ROOT,
            )
            audit = audit_v056(
                prereg_path=prereg,
                database_path=database,
                result_path=result_path,
                project_root=ROOT,
            )
        self.assertEqual(run["status"], "COMPLETED")
        self.assertEqual(len(factory.socket.sent), 1)
        self.assertEqual(snapshot["rfq_requests"], 2400)
        self.assertEqual(snapshot["rfq_trades"], 1)
        self.assertGreaterEqual(snapshot["request_sampling"]["stored"], 100)
        self.assertTrue(snapshot["server_clock"]["deadline_clock_conclusion_allowed"])
        self.assertEqual(
            audit["verdict"],
            "PASS_RFQ_ACTIVITY_STORAGE_STABLE_FOR_JOINED_PAPER_REPLAY",
        )
        self.assertEqual(audit["safety"]["quotes_submitted"], 0)
        self.assertFalse(audit["interpretation"]["profitability_measured"])

    def test_official_artifacts_preserve_safety(self) -> None:
        if not PREREG.is_file():
            self.skipTest("Se congela V0.56 antes del preflight oficial")
        prereg = load_and_verify_preregistration(PREREG, project_root=ROOT)
        self.assertFalse(prereg["safety"]["orders_enabled"])
        self.assertFalse(prereg["safety"]["quote_submission_enabled"])
        if DATABASE.is_file():
            snapshot = inspect_database(
                prereg_path=PREREG,
                database_path=DATABASE,
                project_root=ROOT,
            )
            self.assertEqual(snapshot["orders_created"], 0)
            self.assertEqual(snapshot["real_money"], "BLOQUEADO")
        if RESULT.is_file():
            result = json.loads(RESULT.read_text(encoding="utf-8"))
            self.assertEqual(result["safety"]["orders_created"], 0)
            self.assertEqual(result["safety"]["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
