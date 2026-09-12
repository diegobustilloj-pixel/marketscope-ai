from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file


PREREG_SCHEMA = "prereg_v045_neg_risk_structural_census_1"
PREREG_STATUS = "FROZEN_BEFORE_OFFICIAL_LIVE_CENSUS"
VARIANT = "V0.45_NEG_RISK_BUY_ALL_YES_FEASIBILITY_CENSUS"
PREVIOUS_RESULT = "data/resultado_v044_short_horizon_repricing_rejection.json"


class V045ContractError(RuntimeError):
    pass


def frozen_contract() -> dict[str, Any]:
    return {
        "source": "OFFICIAL_GAMMA_AND_CLOB_PUBLIC_APIS",
        "event_sample": {
            "active": True,
            "closed": False,
            "limit": 100,
            "order": "volume24hr",
            "ascending": False,
            "pagination": False,
        },
        "event_filters": {
            "neg_risk": True,
            "neg_risk_augmented": False,
            "minimum_markets": 3,
            "maximum_markets": 10,
            "require_every_market_active": True,
            "require_every_market_open": True,
            "require_every_market_accepting_orders": True,
            "require_every_market_order_book_enabled": True,
            "require_every_market_neg_risk": True,
            "allow_neg_risk_other": False,
            "required_binary_outcomes": ["Yes", "No"],
        },
        "hypothetical_position": {
            "action": "BUY_EQUAL_SHARES_OF_EVERY_YES_TOKEN",
            "shares_per_leg": 5.0,
            "require_full_visible_ask_depth": True,
            "require_minimum_order_size_lte_shares": True,
            "payout_if_exactly_one_yes_per_share": 1.0,
        },
        "cost_model": {
            "book_source": "CLOB_BATCH_BOOK_ENDPOINT",
            "fee_source": "CLOB_MARKET_INFO_FD_CROSSCHECKED_WITH_GAMMA_FEE_SCHEDULE",
            "fee_formula": "shares*rate*(price*(1-price))**exponent",
            "fee_applied_per_book_level": True,
            "conservative_price_reserve_ticks_per_leg": 1,
            "maximum_book_timestamp_spread_ms": 2000,
            "minimum_conservative_conditional_edge_per_share": 0.01,
        },
        "semantic_limit": {
            "neg_risk_proves_mutual_exclusivity_not_exhaustiveness": True,
            "conditional_edge_requires_exactly_one_yes": True,
            "manual_exhaustiveness_review_required": True,
            "automatic_execution_claim_allowed": False,
        },
        "use": "ONE_SHOT_FEASIBILITY_SCREEN_NOT_PERFORMANCE_VALIDATION",
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
    previous = root / PREVIOUS_RESULT
    if not previous.is_file():
        raise V045ContractError(f"Falta el cierre V0.44: {PREVIOUS_RESULT}")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "independent_market_universe_and_execution_mechanism_feasibility_screen",
        "prior_evidence": {
            "relative_path": PREVIOUS_RESULT,
            "sha256": sha256_file(previous),
            "v044_verdict": "CLOSE_SHORT_HORIZON_TAKER_REPRICING_FAMILY",
        },
        "official_references": [
            "https://docs.polymarket.com/market-data/overview",
            "https://docs.polymarket.com/api-reference/events/list-events",
            "https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body",
            "https://docs.polymarket.com/api-reference/markets/get-clob-market-info",
            "https://docs.polymarket.com/trading/fees",
            "https://github.com/Polymarket/neg-risk-ctf-adapter/blob/main/docs/NegRiskAdapter.md",
            "https://github.com/Polymarket/py-clob-client-v2/blob/main/tests/test_fee_calculations.py",
        ],
        "economic_contract": frozen_contract(),
        "decision": {
            "zero_cost_candidates": "REJECT_SNAPSHOT_HYPOTHESIS_NO_OBSERVER",
            "any_cost_candidate": "REQUIRE_MANUAL_SEMANTIC_REVIEW_AND_FRESH_PERSISTENCE_OBSERVER",
            "automatic_followup_launch": False,
            "posthoc_parameter_selection_allowed": False,
            "paper_or_money_promotion_allowed": False,
        },
        "execution": {
            "new_capture_hours": 0.0,
            "maximum_http_census_minutes": 10.0,
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
            raise V045ContractError("Ya existe otra preinscripcion V0.45")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_preregistration(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V045ContractError("Preinscripcion V0.45 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    expected = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "variant": VARIANT,
        "economic_contract": frozen_contract(),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V045ContractError(f"Preinscripcion V0.45 incompatible: {key}")
    previous = root / PREVIOUS_RESULT
    evidence = payload.get("prior_evidence", {})
    if evidence.get("relative_path") != PREVIOUS_RESULT:
        raise V045ContractError("Ruta de evidencia V0.44 incompatible")
    if evidence.get("sha256") != sha256_file(previous):
        raise V045ContractError("Hash de V0.44 no coincide")
    safety = payload.get("safety", {})
    required_safety = {
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "wallet_required": False,
        "authentication_required": False,
        "real_money": "BLOQUEADO",
    }
    for key, value in required_safety.items():
        if safety.get(key) != value:
            raise V045ContractError(f"Seguridad V0.45 incompatible: {key}")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "VARIANT",
    "V045ContractError",
    "build_preregistration",
    "frozen_contract",
    "load_and_verify_preregistration",
]
