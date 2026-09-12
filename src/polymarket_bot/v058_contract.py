from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file


PREREG_SCHEMA = "prereg_v058_mapped_rfq_clob_replay_1"
PREREG_STATUS = "FROZEN_BEFORE_CREDENTIAL_MAPPING_OR_RFQ_PREFLIGHT"
VARIANT = "V0.58_MAPPED_RFQ_CLOB_PAPER_REPLAY_NO_QUOTES"

SOURCE_FILES = {
    "v057_preregistration": "data/prereg_v057_rfq_clob_joined_replay.json",
    "v057_early_abort": "data/resultado_v057_early_abort_position_mapping.json",
    "v058_position_seed": "data/v058_position_seed_from_v057.json",
    "v058_contract_code": "src/polymarket_bot/v058_contract.py",
    "v058_replay_code": "src/polymarket_bot/v058_mapped_replay.py",
    "v058_audit_code": "src/polymarket_bot/v058_audit.py",
    "v058_monitor_code": "v058_monitor.py",
    "v058_bootstrap": "v058_phantom_bootstrap.py",
    "v058_tests": "tests/test_v058_mapped_replay.py",
    "v058_bootstrap_tests": "tests/test_v058_phantom_bootstrap.py",
    "v058_design": "docs/V058_MAPPED_RFQ_CLOB_REPLAY.md",
}


class V058ContractError(RuntimeError):
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
            "book_requests_per_second": 20,
            "book_queue_maximum": 2048,
            "queue_drain_seconds": 15,
            "protocol_ping_pong_managed_by_library": True,
        },
        "mapping": {
            "endpoint": "https://gamma-api.polymarket.com/markets/keyset",
            "query": {"closed": "false", "combo_status": "enabled", "limit": 100},
            "position_seed_path": "data/v058_position_seed_from_v057.json",
            "position_ids_parameter": "position_ids",
            "position_ids_per_batch": 50,
            "maximum_batches": 150,
            "request_timeout_seconds": 10,
            "maximum_attempts_per_batch": 3,
            "retry_backoff_seconds": [0.5, 2.0],
            "minimum_markets": 100,
            "minimum_seed_positions": 1000,
            "minimum_seed_resolution_rate": 0.90,
            "position_field": "positionIds",
            "clob_token_field": "clobTokenIds",
            "aligned_by_outcome_index": True,
            "built_before_rfq_connection": True,
            "raw_payloads_persisted": False,
            "credentials_sent": False,
            "duplicate_position_conflict_is_terminal": True,
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
            "all_legs_must_resolve_to_clob_tokens": True,
            "request_hash_modulus": 8,
            "request_hash_remainder": 0,
            "economic_model": "SHORT_COMBO_YES_HEDGED_BY_CHEAPEST_SINGLE_DESIRED_LEG",
            "quote_improvement_e6": 1,
            "minimum_locked_edge_per_share": 0.002,
            "rfq_submission_budget_ms": 400,
            "actual_maker_allocation_observable": False,
        },
        "public_clob": {
            "method": "POST",
            "body_uses_resolved_clob_token_ids": True,
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
            "public_requests": [
                "GET GAMMA /markets/keyset FILTERED_BY_V057_POSITION_SEED BEFORE_RFQ_CONNECTION WITH_BOUNDED_RETRY",
                "POST CLOB /books DURING_CAPTURE",
                "GET CLOB /clob-markets/{condition_id} AFTER_CAPTURE_AUDIT_ONLY",
            ],
            "rfq_quote_allowed": False,
            "rfq_quote_cancel_allowed": False,
            "rfq_confirmation_response_allowed": False,
            "orders_allowed": False,
            "signatures_allowed": False,
            "transactions_allowed": False,
        },
        "storage": {
            "format": "SQLITE_WAL_COMPRESSED_SANITIZED_ASKS_WITH_POSITION_MAP",
            "maximum_database_bytes": 536870912,
            "wal_autocheckpoint_pages": 256,
            "transaction_batch_events": 256,
            "transaction_max_seconds": 1.0,
            "maximum_joined_requests": 100000,
            "maximum_trade_records": 100000,
            "raw_rfq_payloads_persisted": False,
            "raw_clob_responses_persisted": False,
            "raw_gamma_payloads_persisted": False,
            "requestor_identifiers_persisted": False,
            "rfq_ids_hashed": True,
            "credentials_or_addresses_persisted": False,
            "database_created_only_after_valid_credential_and_mapping_preflight": True,
            "resume_existing_database": False,
            "one_off_capture": True,
        },
        "gates": {
            "minimum_all_rfq_requests": 5,
            "minimum_mapped_buy_yes_requests": 10000,
            "minimum_mapping_coverage": 0.90,
            "minimum_completed_book_joins": 1000,
            "minimum_join_success_rate": 0.90,
            "maximum_queue_drop_rate": 0.05,
            "minimum_joined_observed_trades": 3,
            "minimum_economically_evaluable_trades": 3,
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
            "negative_result_next_step": "CLOSE_OR_REDESIGN_MAPPED_RFQ_SUPERHEDGE_MECHANISM",
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
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _source_evidence(root: Path) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for key, relative in SOURCE_FILES.items():
        source = root / relative
        if not source.is_file():
            raise V058ContractError(f"Falta evidencia V0.58: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    return evidence


def build_preregistration(*, output_path: str | Path, project_root: str | Path = ROOT) -> dict[str, Any]:
    root = Path(project_root).resolve()
    failure = json.loads((root / SOURCE_FILES["v057_early_abort"]).read_text(encoding="utf-8"))
    if failure.get("verdict") != "FAIL_POSITION_ID_WAS_USED_AS_CLOB_TOKEN_ID":
        raise V058ContractError("V0.57 no conserva el diagnostico de mapping esperado")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "repeat_the_v057_joined_replay_only_after_preloading_the_official_position_id_to_clob_token_id_mapping_outside_the_400ms_path",
        "source_evidence": _source_evidence(root),
        "official_references": [
            "https://docs.polymarket.com/trading/combos/market-makers#map-legs-to-markets",
            "https://docs.polymarket.com/api-reference/wss/rfq",
            "https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body",
        ],
        "contract": frozen_contract(),
        "change_from_v057": {
            "v057_artifacts_modified": False,
            "only_semantic_fix": "POSITION_IDS_RESOLVED_TO_ALIGNED_CLOB_TOKEN_IDS",
            "mapping_preloaded_before_rfq_connection": True,
            "sampling_reduced_from_one_quarter_to_one_eighth": True,
            "book_rate_reduced_from_25_to_20_per_second": True,
            "economic_mechanism_changed": False,
            "quotes_orders_signatures_transactions_enabled": False,
        },
        "safety": frozen_contract()["safety"],
    }
    output = Path(output_path).resolve()
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        comparable = dict(existing)
        comparable["created_at"] = payload["created_at"]
        if comparable != payload:
            raise V058ContractError("Ya existe otra preinscripcion V0.58")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_preregistration(path: str | Path, *, project_root: str | Path = ROOT) -> dict[str, Any]:
    root = Path(project_root).resolve()
    payload = json.loads(Path(path).resolve().read_text(encoding="utf-8"))
    for key, expected in {
        "schema": PREREG_SCHEMA, "status": PREREG_STATUS,
        "variant": VARIANT, "contract": frozen_contract(), "safety": frozen_contract()["safety"],
    }.items():
        if payload.get(key) != expected:
            raise V058ContractError(f"Preinscripcion V0.58 incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V058ContractError("Inventario V0.58 incompatible")
    for key, relative in SOURCE_FILES.items():
        if evidence[key].get("relative_path") != relative or evidence[key].get("sha256") != sha256_file(root / relative):
            raise V058ContractError(f"Hash V0.58 no coincide: {key}")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA", "PREREG_STATUS", "SOURCE_FILES", "VARIANT", "V058ContractError",
    "build_preregistration", "frozen_contract", "load_and_verify_preregistration",
]
