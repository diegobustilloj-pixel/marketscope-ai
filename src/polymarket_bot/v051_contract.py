from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file


PREREG_SCHEMA = "prereg_v051_prefunded_maker_single_fill_hedge_census_1"
PREREG_STATUS = "FROZEN_BEFORE_REST_WS_CENSUS"
VARIANT = "V0.51_PREFUNDED_MAKER_SINGLE_FILL_HEDGE_CENSUS"
SOURCE_FILES = {
    "v020_preregistration": "data/prereg_v020_mandatory_hedge_paper.json",
    "v020_engine": "src/polymarket_bot/v020_mandatory_hedge.py",
    "v021_result": "data/resultado_v021_safe_pair_observer.json",
    "v022b_result": "data/resultado_v022b_synced_persistent_observer.json",
    "v024_postmortem": "data/postmortem_v024_loss_attribution.json",
    "v050_preregistration": "data/prereg_v050_neg_risk_partial_conversion_cross_market_census.json",
    "v050_result": "data/resultado_v050_neg_risk_partial_conversion_cross_market_census.json",
    "v050_sell_model": "src/polymarket_bot/v050_partial_conversion_census.py",
    "v051_contract_code": "src/polymarket_bot/v051_contract.py",
    "v051_census_code": "src/polymarket_bot/v051_prefunded_maker_census.py",
    "v051_monitor_code": "v051_monitor.py",
    "v051_tests": "tests/test_v051_prefunded_maker_census.py",
}


class V051ContractError(RuntimeError):
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
            "maximum_eligible_markets": 30,
            "selection_order": "GAMMA_EVENT_THEN_MARKET_RESPONSE_ORDER_NO_POSTHOC_SELECTION",
        },
        "market_filters": {
            "standard_ctf_only": True,
            "neg_risk": False,
            "active": True,
            "closed": False,
            "accepting_orders": True,
            "order_book_enabled": True,
            "required_binary_outcomes": ["Yes", "No"],
            "require_unique_condition_and_token_ids": True,
        },
        "metadata": {
            "clob_market_info_per_condition_before_snapshot": True,
            "single_rest_batch_immediately_before_websocket": True,
            "rest_endpoint": "https://clob.polymarket.com/books",
            "require_neg_risk_false_per_yes_and_no_token": True,
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
            "maximum_local_receive_spread_ms_per_market": 500,
            "source_timestamp_spread_is_diagnostic_only": True,
        },
        "prefunded_inventory": {
            "source": "STANDARD_CTF_SPLIT_COMPLETE_SET",
            "collateral_per_complete_set": 1.0,
            "yes_and_no_shares_received_per_complete_set": 1.0,
            "complete_sets": 5.0,
            "inventory_is_acquired_before_any_hypothetical_quote": True,
            "unused_equal_inventory_can_be_merged": True,
            "split_and_merge_gas_included": False,
        },
        "hypothetical_execution": {
            "directions": [
                "MAKER_SELL_YES_THEN_TAKER_SELL_NO",
                "MAKER_SELL_NO_THEN_TAKER_SELL_YES",
            ],
            "maker_quote_price": "CURRENT_VISIBLE_BEST_ASK",
            "maker_quote_must_be_non_marketable": True,
            "maker_fill_is_conditional_not_assumed": True,
            "full_five_share_maker_fill_is_the_screened_case": True,
            "maker_queue_ahead_is_diagnostic_only": True,
            "after_single_maker_fill": "IMMEDIATE_TAKER_SELL_OF_PREFUNDED_COMPLEMENT",
            "require_full_visible_bid_depth_for_hedge": True,
            "simultaneous_two_sided_quotes": False,
            "directional_inventory_after_successful_hedge": 0.0,
        },
        "cost_model": {
            "maker_fee": 0.0,
            "maker_rebate": 0.0,
            "taker_fee_source": "CLOB_MARKET_INFO_FD_CROSSCHECKED_WITH_GAMMA_FEE_SCHEDULE",
            "taker_fee_formula": "shares*rate*(price*(1-price))**exponent",
            "taker_fee_applied_per_hedge_book_level": True,
            "observed_hedge_uses_visible_bid_prices": True,
            "conservative_hedge_price_reserve_ticks": 2,
            "minimum_conservative_edge_per_complete_set": 0.01,
            "gas_included": False,
            "gas_is_nonnegative_and_cannot_rescue_a_nonpositive_candidate": True,
        },
        "evidence_gate": {
            "minimum_synchronized_markets": 15,
            "minimum_synchronized_fraction_of_selected": 0.5,
            "both_single_fill_directions_screened_per_synchronized_market": True,
            "insufficient_depth_classifies_direction_as_currently_non_executable": True,
        },
        "execution_limits": {
            "this_is_conditional_fill_feasibility_not_fill_probability": True,
            "queue_priority_not_proven": True,
            "partial_maker_fill_below_minimum_hedge_size_not_solved": True,
            "adverse_selection_beyond_two_ticks_not_proven": True,
            "clob_fill_and_hedge_not_atomic": True,
            "fresh_persistence_queue_latency_gas_study_required_for_any_candidate": True,
        },
        "use": "ONE_SHOT_PREFUNDED_SINGLE_FILL_WORST_CASE_HEDGE_SCREEN_NOT_PERFORMANCE_VALIDATION",
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
            raise V051ContractError(f"Falta evidencia V0.51: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    v020 = json.loads(
        (root / SOURCE_FILES["v020_preregistration"]).read_text(encoding="utf-8")
    )
    v021 = json.loads((root / SOURCE_FILES["v021_result"]).read_text(encoding="utf-8"))
    v022b = json.loads((root / SOURCE_FILES["v022b_result"]).read_text(encoding="utf-8"))
    postmortem = json.loads(
        (root / SOURCE_FILES["v024_postmortem"]).read_text(encoding="utf-8")
    )
    v050 = json.loads((root / SOURCE_FILES["v050_result"]).read_text(encoding="utf-8"))
    if v020.get("status") != "FROZEN_PAPER_ONLY" or not v020.get("strategy", {}).get(
        "mandatory_hedge"
    ):
        raise V051ContractError("V0.20 no contiene el contrato de cobertura obligatoria")
    if v021.get("verdict") != "PASS_OBSERVER_ONLY":
        raise V051ContractError("V0.21 no contiene el observer esperado")
    if v022b.get("opportunities", {}).get("confirmed_opportunity_episodes") != 0:
        raise V051ContractError("V0.22b no conserva el fallo de persistencia esperado")
    timeline = {
        str(item.get("version")): item
        for item in postmortem.get("historical_timeline", [])
        if isinstance(item, Mapping)
    }
    if timeline.get("V0.20", {}).get("verdict") != "FAIL_NEGATIVE_DETERMINISTIC_PNL":
        raise V051ContractError("El postmortem no confirma el fallo económico V0.20")
    if v050.get("verdict") != "REJECT_PARTIAL_CONVERSION_CROSS_MARKET_FEASIBILITY":
        raise V051ContractError("V0.50 no contiene el cierre estructural esperado")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "test_the_untried_prefunded_sell_side_mirror_of_v020_without_directional_residual",
        "source_evidence": evidence,
        "official_references": [
            "https://docs.polymarket.com/concepts/positions-tokens",
            "https://github.com/Polymarket/py-merge-split-positions/blob/main/merge-split.py",
            "https://docs.polymarket.com/trading/fees",
            "https://docs.polymarket.com/api-reference/wss/market",
            "https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body",
            "https://docs.polymarket.com/api-reference/markets/get-clob-market-info",
        ],
        "economic_contract": frozen_contract(),
        "decision": {
            "insufficient_coverage": "FAIL_CENSUS_INSUFFICIENT_PREFUNDED_MAKER_BOOKS",
            "zero_cost_candidates": "REJECT_PREFUNDED_MAKER_SINGLE_FILL_HEDGE_FEASIBILITY",
            "any_cost_candidate": "REQUIRE_FRESH_PERSISTENCE_QUEUE_LATENCY_GAS_STUDY",
            "automatic_followup_launch": False,
            "posthoc_market_or_direction_selection_allowed": False,
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
            raise V051ContractError("Ya existe otra preinscripcion V0.51")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_preregistration(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V051ContractError("Preinscripcion V0.51 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    for key, value in {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "variant": VARIANT,
        "economic_contract": frozen_contract(),
    }.items():
        if payload.get(key) != value:
            raise V051ContractError(f"Preinscripcion V0.51 incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V051ContractError("Inventario V0.51 incompatible")
    for key, relative in SOURCE_FILES.items():
        record = evidence[key]
        if record.get("relative_path") != relative:
            raise V051ContractError(f"Ruta V0.51 incompatible: {key}")
        if record.get("sha256") != sha256_file(root / relative):
            raise V051ContractError(f"Hash V0.51 no coincide: {key}")
    for key, value in {
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "wallet_required": False,
        "authentication_required": False,
        "transactions_enabled": False,
        "real_money": "BLOQUEADO",
    }.items():
        if payload.get("safety", {}).get(key) != value:
            raise V051ContractError(f"Seguridad V0.51 incompatible: {key}")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "SOURCE_FILES",
    "VARIANT",
    "V051ContractError",
    "build_preregistration",
    "frozen_contract",
    "load_and_verify_preregistration",
]
