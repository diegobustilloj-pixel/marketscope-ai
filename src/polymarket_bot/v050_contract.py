from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file


PREREG_SCHEMA = "prereg_v050_neg_risk_partial_conversion_cross_market_census_1"
PREREG_STATUS = "FROZEN_BEFORE_ONCHAIN_REST_WS_CENSUS"
VARIANT = "V0.50_NEG_RISK_PARTIAL_CONVERSION_CROSS_MARKET_CENSUS"
SOURCE_FILES = {
    "v018_result": "data/resultado_v018_multifill.json",
    "v019_result": "data/resultado_v019_fifo_pair.json",
    "v048_result": "data/resultado_v048_rest_metadata_ws_economic_census.json",
    "v049_preregistration": "data/prereg_v049_neg_risk_buy_all_no_conversion_census.json",
    "v049_result": "data/resultado_v049_neg_risk_buy_all_no_conversion_census.json",
    "v049_census_code": "src/polymarket_bot/v049_conversion_census.py",
    "v050_contract_code": "src/polymarket_bot/v050_contract.py",
    "v050_census_code": "src/polymarket_bot/v050_partial_conversion_census.py",
    "v050_monitor_code": "v050_monitor.py",
    "v050_tests": "tests/test_v050_partial_conversion_census.py",
}


class V050ContractError(RuntimeError):
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
            "inherit_v049_filter_function_exactly": True,
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
            "require_neg_risk_true_per_yes_and_no_token": True,
            "require_minimum_order_size_match_clob_market_info": True,
            "require_tick_size_match_clob_market_info": True,
            "require_condition_id_match": True,
            "require_nonempty_official_hash": True,
            "maximum_rest_response_to_ws_connect_ms": 5000,
        },
        "transport": {
            "source": "OFFICIAL_CLOB_PUBLIC_MARKET_WEBSOCKET_FULL_BOOK",
            "endpoint": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            "one_subscription_for_all_yes_and_no_tokens": True,
            "initial_snapshot_timeout_seconds": 20.0,
            "heartbeat_text": "PING",
            "heartbeat_seconds": 10.0,
            "maximum_local_receive_spread_ms_per_event": 2000,
            "source_timestamp_spread_is_diagnostic_only": True,
        },
        "hypothetical_position": {
            "action": "BUY_NO_SUBSET_CONVERT_AND_SELL_COMPLEMENTARY_YES",
            "input_shares_per_no_leg": 5.0,
            "subset_minimum_size": 1,
            "subset_maximum_size": "MARKET_COUNT_MINUS_ONE",
            "enumeration": "ALL_NONEMPTY_PROPER_NO_SUBSETS_LEXICOGRAPHIC_NO_POSTHOC_FILTER",
            "require_full_visible_depth_for_each_executed_leg": True,
            "require_minimum_order_size_lte_input_shares": True,
            "conversion_collateral_multiplier": "NO_SUBSET_SIZE_MINUS_ONE",
            "complementary_yes_shares": "ONCHAIN_FEE_ADJUSTED_AMOUNT_OUT",
            "conversion_fee_applied_with_onchain_integer_floor": True,
        },
        "cost_model": {
            "taker_fee_source": "CLOB_MARKET_INFO_FD_CROSSCHECKED_WITH_GAMMA_FEE_SCHEDULE",
            "taker_fee_formula": "shares*rate*(price*(1-price))**exponent",
            "taker_fee_applied_per_book_level_to_buys_and_sales": True,
            "sell_revenue_formula": "shares*price_minus_taker_fee",
            "conversion_fee_source": "NEG_RISK_ADAPTER_GET_FEE_BIPS_TWO_RPC_AGREEMENT",
            "observed_uses_visible_prices": True,
            "conservative_buy_price_reserve_ticks_per_leg": 1,
            "conservative_sell_price_reserve_ticks_per_leg": 1,
            "minimum_conservative_edge_per_input_share": 0.01,
            "gas_included": False,
            "gas_is_nonnegative_and_cannot_rescue_a_nonpositive_candidate": True,
        },
        "evidence_gate": {
            "minimum_synchronized_events": 5,
            "minimum_synchronized_fraction_of_selected": 0.5,
            "all_enumerated_subsets_are_screened": True,
            "insufficient_depth_classifies_subset_as_currently_non_executable": True,
        },
        "execution_limits": {
            "conversion_property_is_structural": True,
            "semantic_parser_required": False,
            "atomic_clob_and_onchain_execution_available": False,
            "gas_persistence_and_sequential_execution_review_required_for_any_candidate": True,
        },
        "use": "ONE_SHOT_EXHAUSTIVE_PARTIAL_CONVERSION_FEASIBILITY_SCREEN_NOT_PERFORMANCE_VALIDATION",
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
            raise V050ContractError(f"Falta evidencia V0.50: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    v018 = json.loads((root / SOURCE_FILES["v018_result"]).read_text(encoding="utf-8"))
    v019 = json.loads((root / SOURCE_FILES["v019_result"]).read_text(encoding="utf-8"))
    v048 = json.loads((root / SOURCE_FILES["v048_result"]).read_text(encoding="utf-8"))
    v049 = json.loads((root / SOURCE_FILES["v049_result"]).read_text(encoding="utf-8"))
    if v018.get("status") != "FAIL_BEHAVIOR_REPLICATION":
        raise V050ContractError("V0.18 no contiene el fallo maker esperado")
    if v019.get("status") != "FAIL_BEHAVIOR_REPLICATION":
        raise V050ContractError("V0.19 no contiene el fallo de inventario esperado")
    if v048.get("verdict") != "REJECT_CURRENT_CORRECTED_WS_BUY_ALL_YES_FEASIBILITY":
        raise V050ContractError("V0.48 no contiene el cierre buy-all-YES esperado")
    if v049.get("verdict") != "REJECT_BUY_ALL_NO_CONVERSION_FEASIBILITY":
        raise V050ContractError("V0.49 no contiene el cierre buy-all-NO esperado")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "test_exact_partial_negrisk_conversion_inequalities_across_linked_markets",
        "source_evidence": evidence,
        "official_references": [
            "https://github.com/Polymarket/neg-risk-ctf-adapter/blob/main/docs/NegRiskAdapter.md",
            "https://github.com/Polymarket/neg-risk-ctf-adapter/blob/main/src/NegRiskAdapter.sol",
            "https://raw.githubusercontent.com/Polymarket/neg-risk-ctf-adapter/main/addresses.json",
            "https://docs.polymarket.com/api-reference/wss/market",
            "https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body",
            "https://docs.polymarket.com/api-reference/markets/get-clob-market-info",
            "https://docs.polymarket.com/trading/fees",
            "https://github.com/Polymarket/py-clob-client-v2/blob/main/tests/test_fee_calculations.py",
        ],
        "economic_contract": frozen_contract(),
        "decision": {
            "insufficient_coverage": "FAIL_CENSUS_INSUFFICIENT_PARTIAL_CONVERSION_BOOKS",
            "zero_cost_candidates": "REJECT_PARTIAL_CONVERSION_CROSS_MARKET_FEASIBILITY",
            "any_cost_candidate": "REQUIRE_FRESH_PERSISTENCE_GAS_AND_SEQUENTIAL_EXECUTION_STUDY",
            "automatic_followup_launch": False,
            "posthoc_subset_selection_allowed": False,
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
            raise V050ContractError("Ya existe otra preinscripcion V0.50")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_preregistration(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V050ContractError("Preinscripcion V0.50 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    for key, value in {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "variant": VARIANT,
        "economic_contract": frozen_contract(),
    }.items():
        if payload.get(key) != value:
            raise V050ContractError(f"Preinscripcion V0.50 incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V050ContractError("Inventario V0.50 incompatible")
    for key, relative in SOURCE_FILES.items():
        record = evidence[key]
        if record.get("relative_path") != relative:
            raise V050ContractError(f"Ruta V0.50 incompatible: {key}")
        if record.get("sha256") != sha256_file(root / relative):
            raise V050ContractError(f"Hash V0.50 no coincide: {key}")
    for key, value in {
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "wallet_required": False,
        "authentication_required": False,
        "transactions_enabled": False,
        "real_money": "BLOQUEADO",
    }.items():
        if payload.get("safety", {}).get(key) != value:
            raise V050ContractError(f"Seguridad V0.50 incompatible: {key}")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "SOURCE_FILES",
    "VARIANT",
    "V050ContractError",
    "build_preregistration",
    "frozen_contract",
    "load_and_verify_preregistration",
]
