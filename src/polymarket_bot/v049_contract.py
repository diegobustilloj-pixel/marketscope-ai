from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file


PREREG_SCHEMA = "prereg_v049_neg_risk_buy_all_no_conversion_census_1"
PREREG_STATUS = "FROZEN_BEFORE_ONCHAIN_REST_WS_CENSUS"
VARIANT = "V0.49_NEG_RISK_BUY_ALL_NO_CONVERSION_CENSUS"
SOURCE_FILES = {
    "v018_result": "data/resultado_v018_multifill.json",
    "v019_result": "data/resultado_v019_fifo_pair.json",
    "v045_census_code": "src/polymarket_bot/v045_census.py",
    "v046_result": "data/resultado_v046_rest_websocket_transport_probe.json",
    "v048_preregistration": "data/prereg_v048_rest_metadata_ws_economic_census.json",
    "v048_result": "data/resultado_v048_rest_metadata_ws_economic_census.json",
    "v048_census_code": "src/polymarket_bot/v048_corrected_census.py",
    "v049_contract_code": "src/polymarket_bot/v049_contract.py",
    "v049_census_code": "src/polymarket_bot/v049_conversion_census.py",
    "v049_monitor_code": "v049_monitor.py",
    "v049_tests": "tests/test_v049_conversion_census.py",
}


class V049ContractError(RuntimeError):
    pass


def frozen_contract() -> dict[str, Any]:
    return {
        "event_sample": {
            "active": True,
            "closed": False,
            "limit": 100,
            "order": "volume24hr",
            "ascending": False,
            "pagination": False,
            "maximum_eligible_events": 10,
            "selection_order": "GAMMA_RESPONSE_ORDER_NO_POSTHOC_SELECTION",
        },
        "event_filters": {
            "inherit_v045_filter_function_exactly": True,
            "neg_risk": True,
            "neg_risk_augmented": False,
            "minimum_markets": 3,
            "maximum_markets": 10,
            "allow_neg_risk_other": False,
            "required_binary_outcomes": ["Yes", "No"],
            "require_all_markets_open_and_accepting_orders": True,
            "require_single_shared_neg_risk_market_id": True,
            "require_contiguous_question_indices_from_zero": True,
        },
        "onchain_conversion": {
            "chain_id": 137,
            "adapter_address": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
            "adapter_address_source": "https://raw.githubusercontent.com/Polymarket/neg-risk-ctf-adapter/main/addresses.json",
            "rpc_endpoints": [
                "https://polygon-bor-rpc.publicnode.com",
                "https://1rpc.io/matic",
            ],
            "minimum_agreeing_endpoints": 2,
            "get_fee_bips_selector": "0x2582cb5e",
            "get_question_count_selector": "0xb7f75d2c",
            "fee_denominator": 10000,
            "token_unit_scale": 1000000,
            "require_question_count_equal_gamma_market_count": True,
            "require_identical_contract_code": True,
            "reference_block": "MINIMUM_HEAD_REPORTED_BY_AGREEING_ENDPOINTS",
        },
        "metadata": {
            "single_rest_batch_immediately_before_websocket": True,
            "rest_endpoint": "https://clob.polymarket.com/books",
            "require_neg_risk_true_per_no_token": True,
            "require_minimum_order_size_match_clob_market_info": True,
            "require_tick_size_match_clob_market_info": True,
            "require_condition_id_match": True,
            "require_nonempty_official_hash": True,
            "maximum_rest_response_to_ws_connect_ms": 5000,
        },
        "transport": {
            "source": "OFFICIAL_CLOB_PUBLIC_MARKET_WEBSOCKET_FULL_BOOK",
            "endpoint": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            "one_subscription_for_all_no_tokens": True,
            "initial_snapshot_timeout_seconds": 20.0,
            "heartbeat_text": "PING",
            "heartbeat_seconds": 10.0,
            "maximum_local_receive_spread_ms_per_event": 2000,
            "source_timestamp_spread_is_diagnostic_only": True,
        },
        "hypothetical_position": {
            "action": "BUY_EQUAL_SHARES_OF_EVERY_NO_TOKEN_THEN_CONVERT_ALL",
            "shares_per_leg": 5.0,
            "require_full_visible_ask_depth": True,
            "require_minimum_order_size_lte_shares": True,
            "gross_collateral_multiplier": "MARKET_COUNT_MINUS_ONE",
            "conversion_fee_applied_with_onchain_integer_floor": True,
        },
        "cost_model": {
            "taker_fee_source": "CLOB_MARKET_INFO_FD_CROSSCHECKED_WITH_GAMMA_FEE_SCHEDULE",
            "taker_fee_formula": "shares*rate*(price*(1-price))**exponent",
            "taker_fee_applied_per_book_level": True,
            "conversion_fee_source": "NEG_RISK_ADAPTER_GET_FEE_BIPS_TWO_RPC_AGREEMENT",
            "conservative_price_reserve_ticks_per_leg": 1,
            "minimum_conservative_edge_per_bundle_share": 0.01,
            "gas_included": False,
            "gas_is_nonnegative_and_cannot_rescue_a_nonpositive_candidate": True,
        },
        "evidence_gate": {
            "minimum_evaluated_events": 5,
            "minimum_evaluated_fraction_of_selected": 0.5,
        },
        "execution_limits": {
            "conversion_property_is_structural": True,
            "semantic_exhaustiveness_required": False,
            "atomic_multileg_order_execution_available": False,
            "gas_and_sequential_execution_review_required_for_any_candidate": True,
        },
        "use": "ONE_SHOT_LINKED_MARKET_CONVERSION_FEASIBILITY_SCREEN_NOT_PERFORMANCE_VALIDATION",
    }


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_preregistration(
    *, output_path: str | Path, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    evidence: dict[str, Any] = {}
    for key, relative in SOURCE_FILES.items():
        source = root / relative
        if not source.is_file():
            raise V049ContractError(f"Falta evidencia V0.49: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    v018 = json.loads((root / SOURCE_FILES["v018_result"]).read_text(encoding="utf-8"))
    v019 = json.loads((root / SOURCE_FILES["v019_result"]).read_text(encoding="utf-8"))
    v048 = json.loads((root / SOURCE_FILES["v048_result"]).read_text(encoding="utf-8"))
    if v018.get("status") != "FAIL_BEHAVIOR_REPLICATION":
        raise V049ContractError("V0.18 no contiene el fallo maker esperado")
    if v019.get("status") != "FAIL_BEHAVIOR_REPLICATION":
        raise V049ContractError("V0.19 no contiene el fallo de inventario esperado")
    if v048.get("verdict") != "REJECT_CURRENT_CORRECTED_WS_BUY_ALL_YES_FEASIBILITY":
        raise V049ContractError("V0.48 no contiene el cierre buy-all-YES esperado")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "test_a_distinct_linked_market_no_token_conversion_mechanism",
        "source_evidence": evidence,
        "official_references": [
            "https://github.com/Polymarket/neg-risk-ctf-adapter/blob/main/docs/NegRiskAdapter.md",
            "https://github.com/Polymarket/neg-risk-ctf-adapter/blob/main/src/NegRiskAdapter.sol",
            "https://raw.githubusercontent.com/Polymarket/neg-risk-ctf-adapter/main/addresses.json",
            "https://docs.polymarket.com/api-reference/wss/market",
            "https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body",
            "https://docs.polymarket.com/api-reference/markets/get-clob-market-info",
            "https://docs.polymarket.com/trading/fees",
        ],
        "economic_contract": frozen_contract(),
        "decision": {
            "insufficient_coverage": "FAIL_CENSUS_INSUFFICIENT_BUY_ALL_NO_BOOKS",
            "zero_cost_candidates": "REJECT_BUY_ALL_NO_CONVERSION_FEASIBILITY",
            "any_cost_candidate": "REQUIRE_FRESH_PERSISTENCE_GAS_AND_SEQUENTIAL_EXECUTION_STUDY",
            "automatic_followup_launch": False,
            "posthoc_parameter_selection_allowed": False,
            "paper_or_money_promotion_allowed": False,
        },
        "execution": {
            "maximum_census_minutes": 3.0,
            "new_capture_hours": 0.0,
            "scheduled_supervision": False,
            "public_endpoints_only": True,
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "authentication_required": False,
            "transactions_enabled": False,
            "real_money": "BLOQUEADO",
        },
    }
    output = Path(output_path).resolve()
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        comparable = dict(existing)
        comparable["created_at"] = payload["created_at"]
        if comparable != payload:
            raise V049ContractError("Ya existe otra preinscripcion V0.49")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_preregistration(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V049ContractError("Preinscripcion V0.49 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    for key, value in {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "variant": VARIANT,
        "economic_contract": frozen_contract(),
    }.items():
        if payload.get(key) != value:
            raise V049ContractError(f"Preinscripcion V0.49 incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V049ContractError("Inventario V0.49 incompatible")
    for key, relative in SOURCE_FILES.items():
        record = evidence[key]
        if record.get("relative_path") != relative:
            raise V049ContractError(f"Ruta V0.49 incompatible: {key}")
        if record.get("sha256") != sha256_file(root / relative):
            raise V049ContractError(f"Hash V0.49 no coincide: {key}")
    for key, value in {
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "wallet_required": False,
        "authentication_required": False,
        "transactions_enabled": False,
        "real_money": "BLOQUEADO",
    }.items():
        if payload.get("safety", {}).get(key) != value:
            raise V049ContractError(f"Seguridad V0.49 incompatible: {key}")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "SOURCE_FILES",
    "VARIANT",
    "V049ContractError",
    "build_preregistration",
    "frozen_contract",
    "load_and_verify_preregistration",
]
