from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file


PREREG_SCHEMA = "prereg_v055_authenticated_rfq_observer_1"
PREREG_STATUS = "FROZEN_BEFORE_CREDENTIAL_PREFLIGHT_OR_RFQ_CONNECTION"
VARIANT = "V0.55_AUTHENTICATED_RFQ_ACTIVITY_OBSERVER_PAPER_ONLY"

SOURCE_FILES = {
    "v054b_preregistration": "data/prereg_v054b_public_combo_binary_labels_census.json",
    "v054b_result": "data/resultado_v054b_public_combo_binary_labels_census.json",
    "v055_contract_code": "src/polymarket_bot/v055_contract.py",
    "v055_observer_code": "src/polymarket_bot/v055_rfq_observer.py",
    "v055_audit_code": "src/polymarket_bot/v055_audit.py",
    "v055_monitor_code": "v055_monitor.py",
    "v055_tests": "tests/test_v055_rfq_observer.py",
    "v055_setup": "docs/V055_RFQ_OBSERVER_SETUP.md",
}


class V055ContractError(RuntimeError):
    pass


def frozen_contract() -> dict[str, Any]:
    return {
        "transport": {
            "endpoint": "wss://combos-rfq-gateway-quoter.polymarket.com/ws/rfq",
            "protocol": "OFFICIAL_AUTHENTICATED_QUOTER_WEBSOCKET",
            "duration_seconds": 3600,
            "auth_timeout_seconds": 15,
            "receive_poll_seconds": 30,
            "open_timeout_seconds": 12,
            "close_timeout_seconds": 5,
            "maximum_reconnects": 10,
            "reconnect_backoff_seconds": [1, 2, 4, 8, 16, 30],
            "protocol_ping_pong_managed_by_library": True,
            "json_heartbeat_sent": False,
        },
        "credentials": {
            "source": "PROCESS_ENVIRONMENT_ONLY",
            "load_dotenv": False,
            "variables": {
                "api_key": "PM_RFQ_API_KEY",
                "api_secret": "PM_RFQ_API_SECRET",
                "api_passphrase": "PM_RFQ_API_PASSPHRASE",
                "signer_address": "PM_RFQ_SIGNER_ADDRESS",
                "maker_address": "PM_RFQ_MAKER_ADDRESS",
                "signature_type": "PM_RFQ_SIGNATURE_TYPE",
            },
            "allowed_signature_types": [0, 1, 2, 3],
            "credentials_persisted": False,
            "credential_values_logged": False,
            "private_key_required": False,
        },
        "outbound": {
            "allowed_json_message_types": ["auth"],
            "maximum_auth_messages_per_connection": 1,
            "rfq_quote_allowed": False,
            "rfq_quote_cancel_allowed": False,
            "rfq_confirmation_response_allowed": False,
            "orders_allowed": False,
            "signatures_allowed": False,
            "transactions_allowed": False,
        },
        "inbound": {
            "recognized_types": [
                "auth",
                "RFQ_REQUEST",
                "RFQ_TRADE",
                "RFQ_EXECUTION_UPDATE",
                "RFQ_ERROR",
            ],
            "unknown_messages": "STORE_HASH_AND_TYPE_ONLY",
            "raw_payloads_persisted": False,
            "identifiers_hashed": ["rfq_id", "requestor_public_id", "requester_id"],
            "addresses_persisted": False,
            "credentials_persisted": False,
        },
        "storage": {
            "format": "SQLITE_WAL",
            "maximum_database_bytes": 134217728,
            "database_created_only_after_valid_credential_preflight": True,
            "resume_existing_database": False,
            "one_off_capture": True,
        },
        "decision": {
            "minimum_rfq_requests": 5,
            "minimum_rfq_trades": 1,
            "auth_failure": "FAIL_RFQ_AUTHENTICATION",
            "incomplete_runtime": "FAIL_RFQ_OBSERVER_INCOMPLETE_RUNTIME",
            "insufficient_activity": "FAIL_INSUFFICIENT_RFQ_ACTIVITY_ONE_HOUR",
            "activity_observable": "PASS_RFQ_ACTIVITY_OBSERVABLE_FOR_FUTURE_JOINED_PAPER_REPLAY",
            "profitability_conclusion_allowed": False,
            "pnl_conclusion_allowed": False,
            "automatic_followup_launch": False,
        },
        "execution": {
            "new_capture_hours": 1.0,
            "scheduled_supervision": False,
            "automatic_start": False,
            "single_terminal_audit": True,
        },
        "safety": {
            "authentication_required": True,
            "wallet_identity_required": True,
            "wallet_private_key_required": False,
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "quote_submission_enabled": False,
            "signatures_enabled": False,
            "transactions_enabled": False,
            "real_money": "BLOQUEADO",
        },
    }


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _source_evidence(root: Path) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for key, relative in SOURCE_FILES.items():
        source = root / relative
        if not source.is_file():
            raise V055ContractError(f"Falta evidencia V0.55: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    return evidence


def build_preregistration(
    *, output_path: str | Path, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    v054b = json.loads(
        (root / SOURCE_FILES["v054b_result"]).read_text(encoding="utf-8")
    )
    if v054b.get("verdict") != "STOP_COMBO_RFQ_PUBLIC_READ_ONLY_NOT_ECONOMICALLY_OBSERVABLE":
        raise V055ContractError("V0.54b no conserva el cierre de observabilidad esperado")
    if v054b.get("catalog", {}).get("valid_markets") != 300:
        raise V055ContractError("V0.54b no conserva 300 mercados combo validos")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "observe_authenticated_rfq_activity_without_quoting_signing_orders_transactions_or_profitability_claims",
        "source_evidence": _source_evidence(root),
        "official_references": [
            "https://docs.polymarket.com/trading/combos/market-makers",
            "https://docs.polymarket.com/api-reference/wss/rfq",
            "https://docs.polymarket.com/api-reference/maker/submit-a-quote",
        ],
        "contract": frozen_contract(),
        "change_from_v054b": {
            "authenticated_rfq_receive_channel_enabled": True,
            "credential_values_or_private_key_stored": False,
            "quote_order_signature_transaction_paths_enabled": False,
            "profitability_or_pnl_test_enabled": False,
            "v054b_artifacts_modified": False,
        },
        "stage_order": [
            "FREEZE_OBSERVER_IMPLEMENTATION_AND_OUTBOUND_ALLOWLIST",
            "VERIFY_CREDENTIAL_PRESENCE_AND_FORMAT_WITHOUT_LOGGING_VALUES",
            "CREATE_DATABASE_ONLY_AFTER_PREFLIGHT_PASS",
            "SEND_ONE_AUTH_MESSAGE_PER_CONNECTION",
            "RECEIVE_AND_SANITIZE_RFQ_EVENTS_FOR_ONE_HOUR",
            "AUDIT_ACTIVITY_ONCE_WITHOUT_PNL",
        ],
        "safety": frozen_contract()["safety"],
    }
    output = Path(output_path).resolve()
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        comparable = dict(existing)
        comparable["created_at"] = payload["created_at"]
        if comparable != payload:
            raise V055ContractError("Ya existe otra preinscripcion V0.55")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_preregistration(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V055ContractError("Preinscripcion V0.55 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    for key, expected in {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "variant": VARIANT,
        "contract": frozen_contract(),
    }.items():
        if payload.get(key) != expected:
            raise V055ContractError(f"Preinscripcion V0.55 incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V055ContractError("Inventario V0.55 incompatible")
    for key, relative in SOURCE_FILES.items():
        record = evidence[key]
        if record.get("relative_path") != relative:
            raise V055ContractError(f"Ruta V0.55 incompatible: {key}")
        if record.get("sha256") != sha256_file(root / relative):
            raise V055ContractError(f"Hash V0.55 no coincide: {key}")
    if payload.get("safety") != frozen_contract()["safety"]:
        raise V055ContractError("Seguridad V0.55 incompatible")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "SOURCE_FILES",
    "VARIANT",
    "V055ContractError",
    "build_preregistration",
    "frozen_contract",
    "load_and_verify_preregistration",
]
