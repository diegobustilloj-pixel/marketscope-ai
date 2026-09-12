from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file


DISCOVERY_PREREG_SCHEMA = "prereg_v052_linked_threshold_semantic_discovery_1"
DISCOVERY_PREREG_STATUS = "FROZEN_BEFORE_GAMMA_SEMANTIC_DISCOVERY"
ECONOMIC_LOCK_SCHEMA = "prereg_v052_linked_threshold_economic_lock_1"
ECONOMIC_LOCK_STATUS = "FROZEN_AFTER_SEMANTICS_BEFORE_ANY_CLOB_CALL"
VARIANT = "V0.52_LINKED_THRESHOLD_TAKER_FLOOR_CENSUS"

SOURCE_FILES = {
    "v050_result": "data/resultado_v050_neg_risk_partial_conversion_cross_market_census.json",
    "v051b_result": "data/resultado_v051b_prefunded_maker_transport_correction_census.json",
    "v045_buy_cost_model": "src/polymarket_bot/v045_census.py",
    "v051b_standard_validator": "src/polymarket_bot/v051b_prefunded_maker_census.py",
    "v052_contract_code": "src/polymarket_bot/v052_contract.py",
    "v052_census_code": "src/polymarket_bot/v052_linked_threshold_census.py",
    "v052_monitor_code": "v052_monitor.py",
    "v052_tests": "tests/test_v052_linked_threshold_census.py",
}


class V052ContractError(RuntimeError):
    pass


def frozen_contract() -> dict[str, Any]:
    return {
        "semantic_discovery": {
            "endpoint": "https://gamma-api.polymarket.com/events",
            "query": {
                "active": True,
                "closed": False,
                "limit": 500,
                "order": "volume24hr",
                "ascending": False,
            },
            "pagination": False,
            "maximum_adjacent_relationships": 60,
            "selection_order": "GAMMA_EVENT_RESPONSE_THEN_ASCENDING_THRESHOLD_ADJACENCY",
            "allowed_exact_comparator_phrases": [
                "be above",
                "be over",
                "be greater than",
                "reach",
                "hit",
                "exceed",
            ],
            "required_market_group": True,
            "required_group_item_threshold": True,
            "required_question_threshold_match": True,
            "required_description_threshold_and_exact_skeleton_match": True,
            "required_same_event": True,
            "required_same_market_group": True,
            "required_same_question_skeleton": True,
            "required_same_resolution_source": True,
            "required_same_end_date": True,
            "standard_neg_risk_false_only": True,
            "binary_outcomes": ["Yes", "No"],
            "ignored_price_fields": [
                "outcomePrices",
                "bestBid",
                "bestAsk",
                "lastTradePrice",
                "spread",
                "oneHourPriceChange",
                "oneDayPriceChange",
                "oneWeekPriceChange",
                "oneMonthPriceChange",
                "oneYearPriceChange",
            ],
        },
        "logical_contract": {
            "relationship": "LOWER_THRESHOLD_EVENT_IS_A_SUPERSET_OF_UPPER_THRESHOLD_EVENT",
            "lower_threshold_less_than_upper_threshold": True,
            "portfolio": ["BUY_LOWER_THRESHOLD_YES", "BUY_UPPER_THRESHOLD_NO"],
            "payout_floor_per_bundle_share": 1.0,
            "truth_table": {
                "x_at_or_below_lower": [0, 1, 1],
                "x_between_thresholds": [1, 1, 2],
                "x_above_upper": [1, 0, 1],
            },
            "truth_table_columns": ["LOWER_YES", "UPPER_NO", "TOTAL_PAYOUT"],
            "manual_rule_review_required_for_any_candidate": True,
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
            "initial_snapshot_timeout_seconds": 20.0,
            "heartbeat_text": "PING",
            "heartbeat_seconds": 10.0,
            "maximum_local_receive_spread_ms_per_relationship": 500,
            "source_timestamp_spread_is_diagnostic_only": True,
        },
        "hypothetical_execution": {
            "shares_per_leg": 5.0,
            "both_legs_are_taker_buys": True,
            "require_full_visible_ask_depth_for_both_legs": True,
            "atomic_multileg_execution_available": False,
            "snapshot_is_feasibility_not_fill_proof": True,
        },
        "cost_model": {
            "taker_fee_source": "CLOB_MARKET_INFO_FD_CROSSCHECKED_WITH_GAMMA_FEE_SCHEDULE",
            "taker_fee_formula": "shares*rate*(price*(1-price))**exponent",
            "taker_fee_applied_per_book_level_and_leg": True,
            "observed_uses_visible_ask_prices": True,
            "conservative_buy_price_reserve_ticks_per_leg": 2,
            "minimum_conservative_edge_per_bundle_share": 0.01,
            "gas_included": False,
            "gas_is_nonnegative_and_cannot_rescue_a_nonpositive_candidate": True,
        },
        "evidence_gate": {
            "minimum_evaluated_relationships": 1,
            "minimum_evaluated_fraction_of_selected": 0.5,
            "insufficient_depth_is_currently_non_executable": True,
        },
        "use": "TWO_STAGE_ONE_SHOT_SEMANTIC_THEN_ECONOMIC_CENSUS_NOT_PERFORMANCE_VALIDATION",
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
            raise V052ContractError(f"Falta evidencia V0.52: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    return evidence


def build_discovery_preregistration(
    *, output_path: str | Path, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    v050 = json.loads((root / SOURCE_FILES["v050_result"]).read_text(encoding="utf-8"))
    v051b = json.loads((root / SOURCE_FILES["v051b_result"]).read_text(encoding="utf-8"))
    if v050.get("verdict") != "REJECT_PARTIAL_CONVERSION_CROSS_MARKET_FEASIBILITY":
        raise V052ContractError("V0.50 no conserva el cierre esperado")
    if v051b.get("verdict") != "REJECT_PREFUNDED_MAKER_SINGLE_FILL_HEDGE_FEASIBILITY":
        raise V052ContractError("V0.51b no conserva el cierre esperado")
    payload = {
        "schema": DISCOVERY_PREREG_SCHEMA,
        "status": DISCOVERY_PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "freeze_fail_closed_logical_relationship_rules_before_metadata_and_prices",
        "source_evidence": _source_evidence(root),
        "official_references": [
            "https://docs.polymarket.com/api-reference/events/list-events",
            "https://docs.polymarket.com/api-reference/markets/list-markets",
            "https://docs.polymarket.com/market-data/overview",
            "https://docs.polymarket.com/api-reference/wss/market",
            "https://docs.polymarket.com/trading/fees",
        ],
        "contract": frozen_contract(),
        "stage_order": [
            "FREEZE_DISCOVERY_AND_ECONOMICS",
            "DISCOVER_AND_FREEZE_SANITIZED_SEMANTIC_RELATIONSHIPS",
            "HASH_RELATIONSHIPS_IN_ECONOMIC_LOCK",
            "ONLY_THEN_ACCESS_CLOB_BOOKS",
        ],
        "decision": {
            "zero_semantic_relationships": "FAIL_SEMANTIC_RELATION_INSUFFICIENT",
            "insufficient_book_coverage": "FAIL_CENSUS_INSUFFICIENT_LINKED_THRESHOLD_BOOKS",
            "zero_cost_candidates": "REJECT_LINKED_THRESHOLD_TAKER_FLOOR_ARBITRAGE_SNAPSHOT",
            "any_cost_candidate": "REQUIRE_MANUAL_RULE_REVIEW_AND_FRESH_PERSISTENCE_OBSERVER",
            "automatic_followup_launch": False,
            "posthoc_relationship_selection_allowed": False,
            "paper_or_money_promotion_allowed": False,
        },
        "execution": {
            "maximum_total_minutes": 5.0,
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
            raise V052ContractError("Ya existe otra preinscripcion semantica V0.52")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_discovery_preregistration(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V052ContractError("Preinscripcion semantica V0.52 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    for key, value in {
        "schema": DISCOVERY_PREREG_SCHEMA,
        "status": DISCOVERY_PREREG_STATUS,
        "variant": VARIANT,
        "contract": frozen_contract(),
    }.items():
        if payload.get(key) != value:
            raise V052ContractError(f"Preinscripcion V0.52 incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V052ContractError("Inventario V0.52 incompatible")
    for key, relative in SOURCE_FILES.items():
        record = evidence[key]
        if record.get("relative_path") != relative:
            raise V052ContractError(f"Ruta V0.52 incompatible: {key}")
        if record.get("sha256") != sha256_file(root / relative):
            raise V052ContractError(f"Hash V0.52 no coincide: {key}")
    for key, value in {
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "wallet_required": False,
        "authentication_required": False,
        "transactions_enabled": False,
        "real_money": "BLOQUEADO",
    }.items():
        if payload.get("safety", {}).get(key) != value:
            raise V052ContractError(f"Seguridad V0.52 incompatible: {key}")
    return dict(payload)


def build_economic_lock(
    *,
    discovery_prereg_path: str | Path,
    relationships_path: str | Path,
    output_path: str | Path,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    discovery_file = Path(discovery_prereg_path).resolve()
    relationships_file = Path(relationships_path).resolve()
    discovery = load_and_verify_discovery_preregistration(discovery_file, project_root=root)
    if not relationships_file.is_file():
        raise V052ContractError("Relaciones semanticas V0.52 no encontradas")
    relationships = json.loads(relationships_file.read_text(encoding="utf-8"))
    if relationships.get("schema") != "semantic_relationships_v052_linked_threshold_1":
        raise V052ContractError("Schema de relaciones V0.52 incompatible")
    if relationships.get("preregistration", {}).get("sha256") != sha256_file(discovery_file):
        raise V052ContractError("Relaciones V0.52 no pertenecen a esta preinscripcion")
    try:
        discovery_ref = str(discovery_file.relative_to(root)).replace("\\", "/")
        relationships_ref = str(relationships_file.relative_to(root)).replace("\\", "/")
    except ValueError as exc:
        raise V052ContractError("Los artefactos V0.52 deben estar dentro del proyecto") from exc
    payload = {
        "schema": ECONOMIC_LOCK_SCHEMA,
        "status": ECONOMIC_LOCK_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "discovery_preregistration": {
            "relative_path": discovery_ref,
            "sha256": sha256_file(discovery_file),
        },
        "semantic_relationships": {
            "relative_path": relationships_ref,
            "sha256": sha256_file(relationships_file),
            "relationship_count": len(relationships.get("relationships", [])),
        },
        "contract": discovery["contract"],
        "clob_access_before_this_lock": False,
        "safety": discovery["safety"],
    }
    output = Path(output_path).resolve()
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        comparable = dict(existing)
        comparable["created_at"] = payload["created_at"]
        if comparable != payload:
            raise V052ContractError("Ya existe otro bloqueo economico V0.52")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_economic_lock(
    path: str | Path, *, project_root: str | Path = ROOT
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V052ContractError("Bloqueo economico V0.52 no encontrado")
    payload = json.loads(source.read_text(encoding="utf-8"))
    for key, value in {
        "schema": ECONOMIC_LOCK_SCHEMA,
        "status": ECONOMIC_LOCK_STATUS,
        "variant": VARIANT,
        "contract": frozen_contract(),
        "clob_access_before_this_lock": False,
    }.items():
        if payload.get(key) != value:
            raise V052ContractError(f"Bloqueo economico V0.52 incompatible: {key}")
    discovery = payload.get("discovery_preregistration", {})
    relationships_record = payload.get("semantic_relationships", {})
    discovery_path = root / str(discovery.get("relative_path") or "")
    relationships_path = root / str(relationships_record.get("relative_path") or "")
    if discovery.get("sha256") != sha256_file(discovery_path):
        raise V052ContractError("Hash de preinscripcion V0.52 alterado")
    load_and_verify_discovery_preregistration(discovery_path, project_root=root)
    if relationships_record.get("sha256") != sha256_file(relationships_path):
        raise V052ContractError("Hash de relaciones V0.52 alterado")
    relationships = json.loads(relationships_path.read_text(encoding="utf-8"))
    if relationships_record.get("relationship_count") != len(relationships.get("relationships", [])):
        raise V052ContractError("Conteo de relaciones V0.52 alterado")
    return dict(payload), relationships


__all__ = [
    "DISCOVERY_PREREG_SCHEMA",
    "DISCOVERY_PREREG_STATUS",
    "ECONOMIC_LOCK_SCHEMA",
    "ECONOMIC_LOCK_STATUS",
    "SOURCE_FILES",
    "VARIANT",
    "V052ContractError",
    "build_discovery_preregistration",
    "build_economic_lock",
    "frozen_contract",
    "load_and_verify_discovery_preregistration",
    "load_and_verify_economic_lock",
]
