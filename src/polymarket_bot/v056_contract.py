from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file


PREREG_SCHEMA = "prereg_v056_bounded_rfq_observer_1"
PREREG_STATUS = "FROZEN_BEFORE_CREDENTIAL_PREFLIGHT_OR_RFQ_CONNECTION"
VARIANT = "V0.56_BOUNDED_AUTHENTICATED_RFQ_OBSERVER_PAPER_ONLY"

SOURCE_FILES = {
    "v055_preregistration": "data/prereg_v055_authenticated_rfq_observer.json",
    "v055_result": "data/resultado_v055_authenticated_rfq_observer.json",
    "v056_contract_code": "src/polymarket_bot/v056_contract.py",
    "v056_observer_code": "src/polymarket_bot/v056_rfq_observer.py",
    "v056_audit_code": "src/polymarket_bot/v056_audit.py",
    "v056_monitor_code": "v056_monitor.py",
    "v056_bootstrap": "v056_phantom_bootstrap.py",
    "v056_observer_tests": "tests/test_v056_rfq_observer.py",
    "v056_bootstrap_tests": "tests/test_v056_phantom_bootstrap.py",
    "v056_setup": "docs/V056_RFQ_OBSERVER_SETUP.md",
}


class V056ContractError(RuntimeError):
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
            "storage_failures_are_never_reconnected": True,
        },
        "clock": {
            "endpoint": "https://clob.polymarket.com/time",
            "official_resolution_ms": 1000,
            "sample_phases": ["START", "END"],
            "samples_per_phase": 9,
            "sample_spacing_ms": 125,
            "request_timeout_seconds": 5,
            "minimum_successful_samples_per_phase": 3,
            "maximum_interval_width_ms": 750,
            "midpoint_interval_method": True,
            "credentials_sent": False,
            "deadline_field": "submission_deadline",
            "deadline_unit": "UNIX_EPOCH_MILLISECONDS",
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
            "raw_payloads_persisted": False,
            "identifiers_hashed": ["rfq_id", "requestor_public_id", "requester_id"],
            "addresses_persisted": False,
            "credentials_persisted": False,
        },
        "storage": {
            "format": "SQLITE_WAL_BOUNDED_AGGREGATION",
            "maximum_database_bytes": 268435456,
            "wal_autocheckpoint_pages": 256,
            "transaction_batch_events": 512,
            "transaction_max_seconds": 1.0,
            "exact_all_message_counts": True,
            "exact_all_request_aggregates": True,
            "exact_distinct_conditions": True,
            "headroom_histogram_width_ms": 25,
            "headroom_histogram_minimum_ms": -10000,
            "headroom_histogram_maximum_ms": 10000,
            "request_sample_hash_modulus": 16,
            "request_sample_hash_remainder": 0,
            "maximum_request_samples": 100000,
            "full_non_request_record_types": [
                "auth",
                "RFQ_TRADE",
                "RFQ_EXECUTION_UPDATE",
                "RFQ_ERROR",
            ],
            "maximum_full_non_request_records": 100000,
            "unknown_messages": "COUNT_EXACTLY_AND_STORE_SANITIZED_FINGERPRINT_SAMPLE_ONLY",
            "database_created_only_after_valid_credential_preflight": True,
            "resume_existing_database": False,
            "one_off_capture": True,
            "storage_limit_terminal_status": "STORAGE_LIMIT_REACHED",
            "storage_limit_consumes_reconnect_budget": False,
        },
        "decision": {
            "minimum_rfq_requests": 5,
            "minimum_rfq_trades": 1,
            "minimum_request_samples": 100,
            "maximum_database_utilization": 0.75,
            "auth_failure": "FAIL_RFQ_AUTHENTICATION",
            "storage_limit": "FAIL_RFQ_STORAGE_LIMIT",
            "local_storage_failure": "FAIL_RFQ_LOCAL_STORAGE",
            "storage_inefficient": "FAIL_RFQ_STORAGE_HEADROOM",
            "incomplete_runtime": "FAIL_RFQ_OBSERVER_INCOMPLETE_RUNTIME",
            "insufficient_activity": "FAIL_INSUFFICIENT_RFQ_ACTIVITY_ONE_HOUR",
            "insufficient_sample": "FAIL_INSUFFICIENT_RFQ_REQUEST_SAMPLE",
            "activity_observable": "PASS_RFQ_ACTIVITY_STORAGE_STABLE_FOR_JOINED_PAPER_REPLAY",
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
            raise V056ContractError(f"Falta evidencia V0.56: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    return evidence


def build_preregistration(
    *, output_path: str | Path, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    v055 = json.loads((root / SOURCE_FILES["v055_result"]).read_text(encoding="utf-8"))
    observation = v055.get("observation", {})
    if v055.get("verdict") != "FAIL_RFQ_OBSERVER_INCOMPLETE_RUNTIME":
        raise V056ContractError("V0.55 no conserva el fallo de runtime esperado")
    if observation.get("status") != "TRANSPORT_FAILED":
        raise V056ContractError("V0.55 no conserva TRANSPORT_FAILED")
    if int(observation.get("rfq_requests", 0)) < 100000:
        raise V056ContractError("V0.55 no conserva volumen suficiente para V0.56")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": (
            "complete_one_hour_of_authenticated_rfq_observation_with_bounded_storage_"
            "and_server_clock_intervals_without_quotes_orders_signatures_or_pnl"
        ),
        "source_evidence": _source_evidence(root),
        "official_references": [
            "https://docs.polymarket.com/api-reference/wss/rfq",
            "https://docs.polymarket.com/api-reference/data/get-server-time",
            "https://docs.polymarket.com/trading/combos/market-makers",
        ],
        "contract": frozen_contract(),
        "change_from_v055": {
            "v055_artifacts_modified": False,
            "per_request_full_persistence_replaced": (
                "exact_aggregates_plus_hash_sample_plus_all_non_request_records"
            ),
            "request_sample_expected_fraction": "1/16",
            "transaction_batching_enabled": True,
            "wal_size_bounded_by_autocheckpoint": True,
            "database_limit_doubled_bytes": 268435456,
            "storage_limit_is_terminal_without_reconnect": True,
            "official_server_clock_interval_samples_added": True,
            "quote_order_signature_transaction_paths_enabled": False,
            "profitability_or_pnl_test_enabled": False,
        },
        "v055_failure_evidence": {
            "elapsed_ms": 959161,
            "rfq_requests": int(observation["rfq_requests"]),
            "rfq_trades": int(observation["rfq_trades"]),
            "database_bytes_main_file": int(observation["database_bytes"]),
            "v055_maximum_total_database_bytes": 134217728,
            "diagnosis": "DATABASE_FOOTPRINT_LIMIT_TRIGGERED_RECONNECT_LOOP",
        },
        "stage_order": [
            "FREEZE_BOUNDED_STORAGE_CLOCK_AND_OUTBOUND_ALLOWLIST",
            "VERIFY_CREDENTIAL_PRESENCE_AND_FORMAT_WITHOUT_LOGGING_VALUES",
            "CREATE_NEW_V056_DATABASE_ONLY_AFTER_PREFLIGHT_PASS",
            "MEASURE_PUBLIC_CLOB_SERVER_CLOCK_INTERVAL_WITHOUT_CREDENTIALS",
            "SEND_ONE_AUTH_MESSAGE_PER_CONNECTION",
            "COUNT_ALL_EVENTS_AND_STORE_BOUNDED_EVIDENCE_FOR_ONE_HOUR",
            "STOP_TERMINALLY_WITHOUT_RECONNECT_IF_LOCAL_STORAGE_LIMIT_IS_REACHED",
            "MEASURE_END_CLOCK_INTERVAL_AND_AUDIT_ONCE_WITHOUT_PNL",
        ],
        "safety": frozen_contract()["safety"],
    }
    output = Path(output_path).resolve()
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        comparable = dict(existing)
        comparable["created_at"] = payload["created_at"]
        if comparable != payload:
            raise V056ContractError("Ya existe otra preinscripcion V0.56")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_preregistration(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V056ContractError("Preinscripcion V0.56 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    for key, expected in {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "variant": VARIANT,
        "contract": frozen_contract(),
    }.items():
        if payload.get(key) != expected:
            raise V056ContractError(f"Preinscripcion V0.56 incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V056ContractError("Inventario V0.56 incompatible")
    for key, relative in SOURCE_FILES.items():
        record = evidence[key]
        if record.get("relative_path") != relative:
            raise V056ContractError(f"Ruta V0.56 incompatible: {key}")
        if record.get("sha256") != sha256_file(root / relative):
            raise V056ContractError(f"Hash V0.56 no coincide: {key}")
    if payload.get("safety") != frozen_contract()["safety"]:
        raise V056ContractError("Seguridad V0.56 incompatible")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "SOURCE_FILES",
    "VARIANT",
    "V056ContractError",
    "build_preregistration",
    "frozen_contract",
    "load_and_verify_preregistration",
]
