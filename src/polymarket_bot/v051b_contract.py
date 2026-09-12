from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v051_contract import frozen_contract as v051_frozen_contract


PREREG_SCHEMA = "prereg_v051b_prefunded_maker_transport_correction_census_1"
PREREG_STATUS = "FROZEN_BEFORE_CORRECTED_REST_WS_CENSUS"
VARIANT = "V0.51b_PREFUNDED_MAKER_TRANSPORT_CORRECTION_CENSUS"
SOURCE_FILES = {
    "v051_preregistration": "data/prereg_v051_prefunded_maker_single_fill_hedge_census.json",
    "v051_result": "data/resultado_v051_prefunded_maker_single_fill_hedge_census.json",
    "v051_code": "src/polymarket_bot/v051_prefunded_maker_census.py",
    "v051b_contract_code": "src/polymarket_bot/v051b_contract.py",
    "v051b_census_code": "src/polymarket_bot/v051b_prefunded_maker_census.py",
    "v051b_monitor_code": "v051b_monitor.py",
    "v051b_tests": "tests/test_v051b_prefunded_maker_census.py",
}


class V051bContractError(RuntimeError):
    pass


def frozen_contract() -> dict[str, Any]:
    contract = copy.deepcopy(v051_frozen_contract())
    contract["transport_correction"] = {
        "economic_rules_changed": False,
        "observed_v051_clob_standard_market_shape": "NEG_RISK_FIELD_ABSENT",
        "clob_neg_risk_true_rejected": True,
        "clob_neg_risk_absent_allowed_only_after_gamma_false": True,
        "rest_neg_risk_false_required_for_both_tokens_before_evaluation": True,
        "missing_or_true_rest_neg_risk_fails_closed": True,
    }
    return contract


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
            raise V051bContractError(f"Falta evidencia V0.51b: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    v051 = json.loads((root / SOURCE_FILES["v051_result"]).read_text(encoding="utf-8"))
    if v051.get("verdict") != "FAIL_CENSUS_INSUFFICIENT_PREFUNDED_MAKER_BOOKS":
        raise V051bContractError("V0.51 no conserva el fallo de cobertura esperado")
    if v051.get("sample", {}).get("markets_selected") != 30:
        raise V051bContractError("V0.51 no selecciono los 30 mercados esperados")
    if v051.get("sample", {}).get("validation_rejections") != {
        "CLOB_NEG_RISK_NOT_EXPLICITLY_FALSE": 30
    }:
        raise V051bContractError("V0.51 no conserva el rechazo CLOB diagnosticado")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "correct_only_the_v051_clob_standard_market_field_omission_without_changing_economics",
        "source_evidence": evidence,
        "observed_transport_evidence": {
            "gamma_neg_risk": False,
            "clob_market_info_neg_risk": "FIELD_ABSENT",
            "rest_yes_token_neg_risk": False,
            "rest_no_token_neg_risk": False,
            "diagnostic_was_read_only": True,
        },
        "official_references": [
            "https://docs.polymarket.com/concepts/positions-tokens",
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
            raise V051bContractError("Ya existe otra preinscripcion V0.51b")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_preregistration(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V051bContractError("Preinscripcion V0.51b no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    for key, value in {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "variant": VARIANT,
        "economic_contract": frozen_contract(),
    }.items():
        if payload.get(key) != value:
            raise V051bContractError(f"Preinscripcion V0.51b incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V051bContractError("Inventario V0.51b incompatible")
    for key, relative in SOURCE_FILES.items():
        record = evidence[key]
        if record.get("relative_path") != relative:
            raise V051bContractError(f"Ruta V0.51b incompatible: {key}")
        if record.get("sha256") != sha256_file(root / relative):
            raise V051bContractError(f"Hash V0.51b no coincide: {key}")
    for key, value in {
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "wallet_required": False,
        "authentication_required": False,
        "transactions_enabled": False,
        "real_money": "BLOQUEADO",
    }.items():
        if payload.get("safety", {}).get(key) != value:
            raise V051bContractError(f"Seguridad V0.51b incompatible: {key}")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "SOURCE_FILES",
    "VARIANT",
    "V051bContractError",
    "build_preregistration",
    "frozen_contract",
    "load_and_verify_preregistration",
]
