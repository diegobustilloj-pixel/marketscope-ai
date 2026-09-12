from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v055_audit import audit_v055, inspect_database
from polymarket_bot.v055_contract import (
    build_preregistration,
    frozen_contract,
    load_and_verify_preregistration,
)
from polymarket_bot.v055_rfq_observer import (
    AuthOnlySender,
    CredentialBundle,
    V055ObserverError,
    credential_preflight,
    run_v055_async,
    sanitize_inbound,
)


PREREG = ROOT / "data" / "prereg_v055_authenticated_rfq_observer.json"
DATABASE = ROOT / "data" / "v055_authenticated_rfq_observer.db"
RESULT = ROOT / "data" / "resultado_v055_authenticated_rfq_observer.json"


def _valid_environment() -> dict[str, str]:
    address = "0x" + "12" * 20
    return {
        "PM_RFQ_API_KEY": "api-key-test",
        "PM_RFQ_API_SECRET": "api-secret-test",
        "PM_RFQ_API_PASSPHRASE": "api-passphrase-test",
        "PM_RFQ_SIGNER_ADDRESS": address,
        "PM_RFQ_MAKER_ADDRESS": address,
        "PM_RFQ_SIGNATURE_TYPE": "0",
    }


def _request(index: int = 1) -> dict[str, object]:
    return {
        "type": "RFQ_REQUEST",
        "rfq_id": f"rfq-{index}",
        "requestor_public_id": f"requestor-{index}",
        "leg_position_ids": [str(index * 2), str(index * 2 + 1)],
        "condition_id": f"0x{index:064x}",
        "yes_position_id": str(index * 10),
        "no_position_id": str(index * 10 + 1),
        "direction": "BUY",
        "side": "YES",
        "requested_size": {"unit": "notional", "value_e6": "1000000"},
        "submission_deadline": 9999999999999,
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
        "executed_at": 9999999999000,
    }


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


class V055RfqObserverTests(unittest.TestCase):
    def test_missing_credentials_are_reported_without_values(self) -> None:
        credentials, report = credential_preflight(frozen_contract(), environ={})
        self.assertIsNone(credentials)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["present_count"], 0)
        self.assertFalse(report["credential_values_exposed"])
        self.assertFalse(report["database_created"])

    def test_valid_credentials_are_redacted_and_private_key_is_not_required(self) -> None:
        credentials, report = credential_preflight(
            frozen_contract(), environ=_valid_environment()
        )
        self.assertIsNotNone(credentials)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(repr(credentials), "CredentialBundle(<redacted>)")
        self.assertNotIn("api-secret-test", repr(credentials))
        self.assertFalse(report["private_key_required"])

    def test_eoa_identity_mismatch_fails_preflight(self) -> None:
        environment = _valid_environment()
        environment["PM_RFQ_MAKER_ADDRESS"] = "0x" + "34" * 20
        credentials, report = credential_preflight(
            frozen_contract(), environ=environment
        )
        self.assertIsNone(credentials)
        self.assertIn(
            "SIGNER_MAKER_IDENTITY_MISMATCH_FOR_SIGNATURE_TYPE",
            report["invalid_fields"],
        )

    def test_outbound_guard_allows_one_auth_and_blocks_every_other_message(self) -> None:
        credentials, _ = credential_preflight(
            frozen_contract(), environ=_valid_environment()
        )
        socket = _FakeSocket([])
        sender = AuthOnlySender()
        asyncio.run(sender.send_auth(socket, credentials))
        self.assertEqual([item["type"] for item in socket.sent], ["auth"])
        with self.assertRaises(V055ObserverError):
            asyncio.run(sender.send_auth(socket, credentials))
        for message_type in ("RFQ_QUOTE", "RFQ_QUOTE_CANCEL", "RFQ_CONFIRMATION_RESPONSE"):
            with self.assertRaises(V055ObserverError):
                asyncio.run(sender.send(socket, {"type": message_type}))

    def test_auth_response_is_sanitized_without_address_or_credentials(self) -> None:
        payload = {
            "type": "auth",
            "success": True,
            "address": "0x" + "ab" * 20,
            "role": "maker",
            "secret": "must-not-persist",
        }
        record = sanitize_inbound(payload, 1000)
        changed_sensitive_values = dict(payload)
        changed_sensitive_values["address"] = "0x" + "cd" * 20
        changed_sensitive_values["secret"] = "another-secret"
        changed = sanitize_inbound(changed_sensitive_values, 1000)
        serialized = json.dumps(record, sort_keys=True)
        self.assertNotIn("must-not-persist", serialized)
        self.assertNotIn("0x" + "ab" * 20, serialized)
        self.assertEqual(record["auth_success"], 1)
        self.assertEqual(record["role"], "maker")
        self.assertEqual(record["payload_sha256"], changed["payload_sha256"])

    def test_request_identifiers_are_hashed_and_public_structure_is_preserved(self) -> None:
        payload = _request(1)
        record = sanitize_inbound(payload, 1000)
        serialized = json.dumps(record, sort_keys=True)
        self.assertNotIn("rfq-1", serialized)
        self.assertNotIn("requestor-1", serialized)
        self.assertEqual(json.loads(record["leg_position_ids_json"]), ["2", "3"])
        self.assertEqual(record["direction"], "BUY")
        self.assertEqual(record["size_value"], "1000000")

    def test_blocked_run_creates_no_database_and_attempts_no_network(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as temporary:
            folder = Path(temporary)
            prereg = folder / "prereg.json"
            database = folder / "observer.db"
            build_preregistration(output_path=prereg, project_root=ROOT)
            result = asyncio.run(
                run_v055_async(
                    prereg_path=prereg,
                    database_path=database,
                    project_root=ROOT,
                    environ={},
                )
            )
            self.assertFalse(database.exists())
        self.assertEqual(result["status"], "BLOCKED_CREDENTIAL_PREFLIGHT")
        self.assertFalse(result["network_connection_attempted"])

    def test_fake_terminal_observer_and_audit_preserve_paper_only_contract(self) -> None:
        messages: list[object] = [
            {"type": "auth", "success": True, "address": "0x" + "12" * 20, "role": "maker"},
            *[_request(index) for index in range(1, 6)],
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
                run_v055_async(
                    prereg_path=prereg,
                    database_path=database,
                    project_root=ROOT,
                    environ=_valid_environment(),
                    connect_factory=factory,
                    duration_seconds=0.02,
                )
            )
            snapshot = inspect_database(
                prereg_path=prereg,
                database_path=database,
                project_root=ROOT,
            )
            audit = audit_v055(
                prereg_path=prereg,
                database_path=database,
                result_path=result_path,
                project_root=ROOT,
            )
        self.assertEqual(run["status"], "COMPLETED")
        self.assertEqual(len(factory.socket.sent), 1)
        self.assertEqual(snapshot["rfq_requests"], 5)
        self.assertEqual(snapshot["rfq_trades"], 1)
        self.assertEqual(
            audit["verdict"],
            "PASS_RFQ_ACTIVITY_OBSERVABLE_FOR_FUTURE_JOINED_PAPER_REPLAY",
        )
        self.assertEqual(audit["safety"]["quotes_submitted"], 0)
        self.assertFalse(audit["interpretation"]["profitability_measured"])

    def test_official_artifacts_preserve_safety(self) -> None:
        if not PREREG.is_file():
            self.skipTest("Se congela V0.55 antes del preflight oficial")
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
