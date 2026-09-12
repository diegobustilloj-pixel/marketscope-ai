from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file


PREREG_SCHEMA = "prereg_v057_rfq_clob_joined_replay_1"
PREREG_STATUS = "FROZEN_BEFORE_CREDENTIAL_PREFLIGHT_OR_NETWORK_CAPTURE"
VARIANT = "V0.57_RFQ_CLOB_JOINED_PAPER_REPLAY_NO_QUOTES"

SOURCE_FILES = {
    "v056_preregistration": "data/prereg_v056_bounded_rfq_observer.json",
    "v056_result": "data/resultado_v056_bounded_rfq_observer.json",
    "v057_contract_code": "src/polymarket_bot/v057_contract.py",
    "v057_replay_code": "src/polymarket_bot/v057_joined_replay.py",
    "v057_audit_code": "src/polymarket_bot/v057_audit.py",
    "v057_monitor_code": "v057_monitor.py",
    "v057_bootstrap": "v057_phantom_bootstrap.py",
    "v057_tests": "tests/test_v057_joined_replay.py",
    "v057_bootstrap_tests": "tests/test_v057_phantom_bootstrap.py",
    "v057_design": "docs/V057_RFQ_CLOB_JOINED_REPLAY.md",
}


class V057ContractError(RuntimeError):
    pass


def frozen_contract() -> dict[str, Any]:
    return {
        "transport": {
            "rfq_endpoint": "wss://combos-rfq-gateway-quoter.polymarket.com/ws/rfq",
            "clob_books_endpoint": "https://clob.polymarket.com/books",
            "clob_market_endpoint_template": "https://clob.polymarket.com/clob-markets/{condition_id}",
            "duration_seconds": 3600,
            "auth_timeout_seconds": 15,
            "receive_poll_seconds": 15,
            "open_timeout_seconds": 12,
            "close_timeout_seconds": 5,
            "maximum_reconnects": 10,
            "reconnect_backoff_seconds": [1, 2, 4, 8, 16, 30],
            "book_http_timeout_seconds": 2.0,
            "book_workers": 16,
            "book_requests_per_second": 25,
            "book_queue_maximum": 2048,
            "queue_drain_seconds": 15,
            "protocol_ping_pong_managed_by_library": True,
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
        "scope": {
            "direction": "BUY",
            "side": "YES",
            "requested_size_unit": "notional",
            "minimum_leg_count": 2,
            "maximum_leg_count": 8,
            "request_hash_modulus": 4,
            "request_hash_remainder": 0,
            "economic_model": "SHORT_COMBO_YES_HEDGED_BY_CHEAPEST_SINGLE_DESIRED_LEG",
            "superhedge_reason": "FOR_BINARY_LEG_PAYOFFS_PRODUCT_OF_LEGS_LESS_THAN_OR_EQUAL_TO_EACH_LEG",
            "quote_improvement_e6": 1,
            "minimum_locked_edge_per_share": 0.002,
            "rfq_submission_budget_ms": 400,
            "trade_size_field": "RFQ_TRADE.size_e6_MATCHED_COMBO_SHARES",
            "actual_maker_allocation_observable": False,
        },
        "public_clob": {
            "method": "POST",
            "body": "ARRAY_OF_TOKEN_ID_OBJECTS",
            "credentials_sent": False,
            "requested_sides_stored": ["asks"],
            "bids_stored": False,
            "raw_responses_stored": False,
            "exact_depth_required": True,
            "source_timestamps_required": True,
            "no_retry_for_decision_validity": True,
        },
        "outbound": {
            "rfq_allowed_json_message_types": ["auth"],
            "maximum_auth_messages_per_connection": 1,
            "public_clob_allowed_requests": ["POST /books", "GET /clob-markets/{condition_id} AFTER_CAPTURE_AUDIT_ONLY"],
            "rfq_quote_allowed": False,
            "rfq_quote_cancel_allowed": False,
            "rfq_confirmation_response_allowed": False,
            "orders_allowed": False,
            "signatures_allowed": False,
            "transactions_allowed": False,
        },
        "storage": {
            "format": "SQLITE_WAL_COMPRESSED_SANITIZED_ASKS",
            "maximum_database_bytes": 536870912,
            "wal_autocheckpoint_pages": 256,
            "transaction_batch_events": 256,
            "transaction_max_seconds": 1.0,
            "maximum_joined_requests": 100000,
            "maximum_trade_records": 100000,
            "raw_rfq_payloads_persisted": False,
            "raw_clob_responses_persisted": False,
            "requestor_identifiers_persisted": False,
            "rfq_ids_hashed": True,
            "credentials_or_addresses_persisted": False,
            "database_created_only_after_valid_credential_preflight": True,
            "resume_existing_database": False,
            "one_off_capture": True,
        },
        "gates": {
            "minimum_all_rfq_requests": 5,
            "minimum_eligible_buy_yes_requests": 10000,
            "minimum_completed_book_joins": 2000,
            "minimum_join_success_rate": 0.90,
            "maximum_queue_drop_rate": 0.05,
            "minimum_joined_observed_trades": 5,
            "minimum_economically_evaluable_trades": 5,
            "minimum_locked_superhedge_candidates": 1,
        },
        "interpretation": {
            "realized_pnl_conclusion_allowed": False,
            "fill_probability_conclusion_allowed": False,
            "profitability_conclusion_allowed": False,
            "candidate_is_counterfactual_viability_only": True,
            "actual_trade_price_used_only_after_capture": True,
            "actual_maker_allocation_absent": True,
            "posthoc_threshold_change_allowed": False,
            "automatic_followup_launch": False,
            "positive_result_next_step": "PREREGISTER_SEPARATE_FOUR_HOUR_CONFIRMATION_WITHOUT_QUOTES",
            "negative_result_next_step": "CLOSE_OR_REDESIGN_RFQ_SUPERHEDGE_MECHANISM",
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
            raise V057ContractError(f"Falta evidencia V0.57: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    return evidence


def build_preregistration(
    *, output_path: str | Path, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    v056 = json.loads((root / SOURCE_FILES["v056_result"]).read_text(encoding="utf-8"))
    if v056.get("verdict") != "PASS_RFQ_ACTIVITY_STORAGE_STABLE_FOR_JOINED_PAPER_REPLAY":
        raise V057ContractError("V0.56 no autoriza el replay conjunto")
    observation = v056.get("observation", {})
    if int(observation.get("rfq_requests", 0)) < 100000 or int(observation.get("rfq_trades", 0)) < 1:
        raise V057ContractError("V0.56 no conserva actividad suficiente")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "test_counterfactual_short_combo_yes_single_leg_superhedge_at_observed_trade_prices_using_contemporaneous_exact_clob_asks_without_quotes_or_orders",
        "source_evidence": _source_evidence(root),
        "official_references": [
            "https://docs.polymarket.com/api-reference/wss/rfq",
            "https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body",
            "https://docs.polymarket.com/trading/combos/market-makers",
            "https://docs.polymarket.com/cn/api-reference/endpoints/clob/get-clob-markets-condition-id",
        ],
        "contract": frozen_contract(),
        "change_from_v056": {
            "v056_artifacts_modified": False,
            "request_scope": "BUY_YES_NOTIONAL_2_TO_8_LEGS",
            "deterministic_request_sample_fraction": "1/4",
            "contemporaneous_public_clob_ask_depth_joined": True,
            "observed_rfq_trades_joined_by_hashed_rfq_id": True,
            "single_leg_superhedge_evaluated_after_capture": True,
            "quotes_orders_signatures_transactions_enabled": False,
            "realized_pnl_or_fill_probability_claim_enabled": False,
        },
        "stage_order": [
            "FREEZE_SCOPE_GATES_STORAGE_AND_OUTBOUND_ALLOWLIST",
            "VERIFY_CREDENTIAL_FORMAT_WITHOUT_LOGGING_VALUES",
            "CREATE_NEW_DATABASE_ONLY_AFTER_PREFLIGHT_PASS",
            "AUTHENTICATE_RFQ_READ_STREAM",
            "DETERMINISTICALLY_SAMPLE_ELIGIBLE_BUY_YES_REQUESTS",
            "FETCH_PUBLIC_BATCH_BOOKS_ONCE_WITHOUT_RETRY_OR_CREDENTIALS",
            "STORE_COMPRESSED_SANITIZED_ASK_DEPTH_AND_ALL_RFQ_TRADES",
            "STOP_AFTER_ONE_HOUR_AND_AUDIT_ONCE",
            "DO_NOT_CLAIM_REALIZED_PNL_OR_FILL_PROBABILITY",
        ],
        "safety": frozen_contract()["safety"],
    }
    output = Path(output_path).resolve()
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        comparable = dict(existing)
        comparable["created_at"] = payload["created_at"]
        if comparable != payload:
            raise V057ContractError("Ya existe otra preinscripcion V0.57")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_preregistration(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    for key, expected in {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "variant": VARIANT,
        "contract": frozen_contract(),
        "safety": frozen_contract()["safety"],
    }.items():
        if payload.get(key) != expected:
            raise V057ContractError(f"Preinscripcion V0.57 incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V057ContractError("Inventario V0.57 incompatible")
    for key, relative in SOURCE_FILES.items():
        record = evidence[key]
        if record.get("relative_path") != relative or record.get("sha256") != sha256_file(root / relative):
            raise V057ContractError(f"Evidencia V0.57 cambio: {key}")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "SOURCE_FILES",
    "VARIANT",
    "V057ContractError",
    "build_preregistration",
    "frozen_contract",
    "load_and_verify_preregistration",
]
