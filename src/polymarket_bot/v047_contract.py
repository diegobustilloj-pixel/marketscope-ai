from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file


PREREG_SCHEMA = "prereg_v047_ws_neg_risk_economic_census_1"
PREREG_STATUS = "FROZEN_BEFORE_WS_ECONOMIC_CENSUS"
VARIANT = "V0.47_WS_NEG_RISK_BUY_ALL_YES_FEASIBILITY_CENSUS"
SOURCE_FILES = {
    "v045_preregistration": "data/prereg_v045_neg_risk_structural_census.json",
    "v045_result": "data/resultado_v045_neg_risk_structural_census.json",
    "v045_census_code": "src/polymarket_bot/v045_census.py",
    "v046_preregistration": "data/prereg_v046_rest_websocket_transport_probe.json",
    "v046_result": "data/resultado_v046_rest_websocket_transport_probe.json",
    "v046_probe_code": "src/polymarket_bot/v046_transport_probe.py",
}


class V047ContractError(RuntimeError):
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
        },
        "transport": {
            "source": "OFFICIAL_CLOB_PUBLIC_MARKET_WEBSOCKET_FULL_BOOK",
            "endpoint": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            "one_subscription_for_all_yes_tokens": True,
            "initial_snapshot_timeout_seconds": 20.0,
            "heartbeat_text": "PING",
            "heartbeat_seconds": 10.0,
            "maximum_local_receive_spread_ms_per_event": 2000,
            "source_timestamp_spread_is_diagnostic_only": True,
            "require_nonempty_official_book_hash": True,
            "require_ws_neg_risk_true": True,
            "require_condition_id_match": True,
        },
        "hypothetical_position": {
            "action": "BUY_EQUAL_SHARES_OF_EVERY_YES_TOKEN",
            "shares_per_leg": 5.0,
            "require_full_visible_ask_depth": True,
            "require_minimum_order_size_lte_shares": True,
            "payout_if_exactly_one_yes_per_share": 1.0,
        },
        "cost_model": {
            "fee_source": "CLOB_MARKET_INFO_FD_CROSSCHECKED_WITH_GAMMA_FEE_SCHEDULE",
            "fee_formula": "shares*rate*(price*(1-price))**exponent",
            "fee_applied_per_book_level": True,
            "conservative_price_reserve_ticks_per_leg": 1,
            "minimum_conservative_conditional_edge_per_share": 0.01,
        },
        "evidence_gate": {
            "minimum_evaluated_events": 5,
            "minimum_evaluated_fraction_of_selected": 0.5,
        },
        "semantic_limit": {
            "neg_risk_proves_mutual_exclusivity_not_exhaustiveness": True,
            "conditional_edge_requires_exactly_one_yes": True,
            "manual_exhaustiveness_review_required": True,
            "atomic_multileg_execution_available": False,
        },
        "use": "ONE_SHOT_WS_FEASIBILITY_SCREEN_NOT_PERFORMANCE_VALIDATION",
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
            raise V047ContractError(f"Falta evidencia V0.47: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    v046 = json.loads((root / SOURCE_FILES["v046_result"]).read_text(encoding="utf-8"))
    if v046.get("verdict") != "PASS_WS_RECEIVE_WINDOW_AND_HASH_CONTINUITY":
        raise V047ContractError("V0.46 no valido el transporte WebSocket")
    if not bool(v046.get("transport", {}).get("transport_validated")):
        raise V047ContractError("La puerta de transporte V0.46 no fue superada")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "repeat_v045_economic_screen_with_v046_validated_local_receive_window",
        "source_evidence": evidence,
        "official_references": [
            "https://docs.polymarket.com/api-reference/wss/market",
            "https://docs.polymarket.com/api-reference/markets/get-clob-market-info",
            "https://docs.polymarket.com/trading/fees",
            "https://github.com/Polymarket/neg-risk-ctf-adapter/blob/main/docs/NegRiskAdapter.md",
        ],
        "economic_contract": frozen_contract(),
        "decision": {
            "insufficient_coverage": "FAIL_CENSUS_INSUFFICIENT_WS_BOOKS",
            "zero_cost_candidates": "REJECT_CURRENT_WS_BUY_ALL_YES_FEASIBILITY",
            "any_cost_candidate": "REQUIRE_MANUAL_EXHAUSTIVENESS_REVIEW_AND_FRESH_PERSISTENCE_OBSERVER",
            "automatic_followup_launch": False,
            "posthoc_parameter_selection_allowed": False,
            "paper_or_money_promotion_allowed": False,
        },
        "execution": {
            "maximum_census_minutes": 2.0,
            "new_capture_hours": 0.0,
            "scheduled_supervision": False,
            "public_endpoints_only": True,
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "authentication_required": False,
            "real_money": "BLOQUEADO",
        },
    }
    output = Path(output_path).resolve()
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        comparable = dict(existing)
        comparable["created_at"] = payload["created_at"]
        if comparable != payload:
            raise V047ContractError("Ya existe otra preinscripcion V0.47")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_preregistration(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V047ContractError("Preinscripcion V0.47 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    expected = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "variant": VARIANT,
        "economic_contract": frozen_contract(),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V047ContractError(f"Preinscripcion V0.47 incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V047ContractError("Inventario de evidencia V0.47 incompatible")
    for key, relative in SOURCE_FILES.items():
        record = evidence[key]
        if record.get("relative_path") != relative:
            raise V047ContractError(f"Ruta V0.47 incompatible: {key}")
        if record.get("sha256") != sha256_file(root / relative):
            raise V047ContractError(f"Hash V0.47 no coincide: {key}")
    required_safety = {
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "wallet_required": False,
        "authentication_required": False,
        "real_money": "BLOQUEADO",
    }
    for key, value in required_safety.items():
        if payload.get("safety", {}).get(key) != value:
            raise V047ContractError(f"Seguridad V0.47 incompatible: {key}")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "SOURCE_FILES",
    "VARIANT",
    "V047ContractError",
    "build_preregistration",
    "frozen_contract",
    "load_and_verify_preregistration",
]
