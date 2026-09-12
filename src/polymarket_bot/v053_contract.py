from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v052_contract import frozen_contract as v052_frozen_contract


DISCOVERY_PREREG_SCHEMA = "prereg_v053_cross_event_threshold_semantic_discovery_1"
DISCOVERY_PREREG_STATUS = "FROZEN_BEFORE_KEYSET_GAMMA_DISCOVERY"
ECONOMIC_LOCK_SCHEMA = "prereg_v053_cross_event_threshold_economic_lock_1"
ECONOMIC_LOCK_STATUS = "FROZEN_AFTER_CROSS_EVENT_SEMANTICS_BEFORE_ANY_CLOB_CALL"
SEMANTIC_SCHEMA = "semantic_relationships_v053_cross_event_threshold_1"
VARIANT = "V0.53_CROSS_EVENT_THRESHOLD_TAKER_FLOOR_CENSUS"

SOURCE_FILES = {
    "v052_preregistration": "data/prereg_v052_linked_threshold_semantic_discovery.json",
    "v052_semantic_result": "data/relaciones_v052_linked_threshold_semantic.json",
    "v052_final_result": "data/resultado_v052_linked_threshold_taker_floor_census.json",
    "v052_economic_engine": "src/polymarket_bot/v052_linked_threshold_census.py",
    "v053_contract_code": "src/polymarket_bot/v053_contract.py",
    "v053_census_code": "src/polymarket_bot/v053_cross_event_threshold_census.py",
    "v053_monitor_code": "v053_monitor.py",
    "v053_tests": "tests/test_v053_cross_event_threshold_census.py",
}


class V053ContractError(RuntimeError):
    pass


def frozen_contract() -> dict[str, Any]:
    contract = copy.deepcopy(v052_frozen_contract())
    contract["semantic_discovery"] = {
        "endpoint": "https://gamma-api.polymarket.com/events/keyset",
        "query": {
            "closed": False,
            "limit": 500,
            "order": "volume24hr",
            "ascending": False,
        },
        "pagination": "OFFICIAL_OPAQUE_AFTER_CURSOR",
        "maximum_pages": 2,
        "maximum_events": 1000,
        "cursor_repeat_fails_closed": True,
        "maximum_adjacent_relationships": 30,
        "selection_order": "KEYSET_PAGE_THEN_EVENT_RESPONSE_THEN_ASCENDING_THRESHOLD_ADJACENCY",
        "allowed_exact_comparator_phrases": [
            "be above",
            "be over",
            "be greater than",
            "reach",
            "hit",
            "exceed",
        ],
        "market_group_not_required": True,
        "group_item_threshold_not_required": True,
        "threshold_parsed_from_question": True,
        "required_question_threshold": True,
        "required_description_threshold_and_exact_skeleton_match": True,
        "required_different_event_ids": True,
        "required_same_question_skeleton": True,
        "required_same_resolution_source": True,
        "required_same_end_date": True,
        "required_same_market_and_format_type": True,
        "standard_neg_risk_false_only": True,
        "binary_outcomes": ["Yes", "No"],
        "duplicate_threshold_within_semantic_group": "REJECT_ENTIRE_GROUP",
        "ignored_price_fields": copy.deepcopy(
            v052_frozen_contract()["semantic_discovery"]["ignored_price_fields"]
        ),
    }
    contract["logical_contract"]["relationship"] = (
        "CROSS_EVENT_LOWER_THRESHOLD_EVENT_IS_A_SUPERSET_OF_UPPER_THRESHOLD_EVENT"
    )
    contract["use"] = (
        "TWO_STAGE_KEYSET_CROSS_EVENT_SEMANTIC_THEN_ECONOMIC_CENSUS_NOT_PERFORMANCE_VALIDATION"
    )
    return contract


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
            raise V053ContractError(f"Falta evidencia V0.53: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    return evidence


def build_discovery_preregistration(
    *, output_path: str | Path, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    v052_semantic = json.loads(
        (root / SOURCE_FILES["v052_semantic_result"]).read_text(encoding="utf-8")
    )
    v052_result = json.loads(
        (root / SOURCE_FILES["v052_final_result"]).read_text(encoding="utf-8")
    )
    if v052_result.get("verdict") != "FAIL_SEMANTIC_RELATION_INSUFFICIENT":
        raise V053ContractError("V0.52 no conserva el fallo semantico esperado")
    if v052_semantic.get("discovery", {}).get("adjacent_relationships_selected") != 0:
        raise V053ContractError("V0.52 no conserva cero relaciones")
    if v052_semantic.get("discovery", {}).get("filter_rejections", {}).get(
        "MARKET_GROUP_MISSING"
    ) != 323:
        raise V053ContractError("V0.52 no conserva la evidencia de marketGroup ausente")
    payload = {
        "schema": DISCOVERY_PREREG_SCHEMA,
        "status": DISCOVERY_PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "test_distinct_standard_events_after_v052_group_field_coverage_failure_without_posthoc_price_selection",
        "source_evidence": _source_evidence(root),
        "official_references": [
            "https://docs.polymarket.com/api-reference/events/list-events-keyset-pagination",
            "https://docs.polymarket.com/api-reference/markets/list-markets",
            "https://docs.polymarket.com/market-data/overview",
            "https://docs.polymarket.com/api-reference/wss/market",
            "https://docs.polymarket.com/trading/fees",
        ],
        "contract": frozen_contract(),
        "change_from_v052": {
            "same_event_and_market_group_required": False,
            "different_event_ids_required": True,
            "market_group_required": False,
            "group_item_threshold_required": False,
            "threshold_must_still_be_in_question_and_description": True,
            "same_question_description_source_and_end_date_required": True,
            "official_keyset_pagination_added": True,
            "economic_model_changed": False,
            "v052_artifacts_modified": False,
        },
        "stage_order": [
            "FREEZE_CROSS_EVENT_DISCOVERY_AND_ECONOMICS",
            "DISCOVER_AND_FREEZE_SANITIZED_RELATIONSHIPS_WITH_KEYSET_PAGINATION",
            "HASH_RELATIONSHIPS_IN_ECONOMIC_LOCK",
            "ONLY_THEN_ACCESS_CLOB_BOOKS",
        ],
        "decision": {
            "zero_semantic_relationships": "CLOSE_STANDARD_THRESHOLD_RELATION_FAMILY",
            "insufficient_book_coverage": "FAIL_CENSUS_INSUFFICIENT_CROSS_EVENT_THRESHOLD_BOOKS",
            "zero_cost_candidates": "REJECT_CROSS_EVENT_THRESHOLD_TAKER_FLOOR_ARBITRAGE_SNAPSHOT",
            "any_cost_candidate": "REQUIRE_MANUAL_RULE_REVIEW_AND_FRESH_PERSISTENCE_OBSERVER",
            "automatic_followup_launch": False,
            "posthoc_relationship_selection_allowed": False,
            "paper_or_money_promotion_allowed": False,
        },
        "execution": {
            "maximum_total_minutes": 8.0,
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
            raise V053ContractError("Ya existe otra preinscripcion V0.53")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_discovery_preregistration(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V053ContractError("Preinscripcion V0.53 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    for key, value in {
        "schema": DISCOVERY_PREREG_SCHEMA,
        "status": DISCOVERY_PREREG_STATUS,
        "variant": VARIANT,
        "contract": frozen_contract(),
    }.items():
        if payload.get(key) != value:
            raise V053ContractError(f"Preinscripcion V0.53 incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V053ContractError("Inventario V0.53 incompatible")
    for key, relative in SOURCE_FILES.items():
        record = evidence[key]
        if record.get("relative_path") != relative:
            raise V053ContractError(f"Ruta V0.53 incompatible: {key}")
        if record.get("sha256") != sha256_file(root / relative):
            raise V053ContractError(f"Hash V0.53 no coincide: {key}")
    for key, value in {
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "wallet_required": False,
        "authentication_required": False,
        "transactions_enabled": False,
        "real_money": "BLOQUEADO",
    }.items():
        if payload.get("safety", {}).get(key) != value:
            raise V053ContractError(f"Seguridad V0.53 incompatible: {key}")
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
        raise V053ContractError("Relaciones V0.53 no encontradas")
    relationships = json.loads(relationships_file.read_text(encoding="utf-8"))
    if relationships.get("schema") != SEMANTIC_SCHEMA:
        raise V053ContractError("Schema de relaciones V0.53 incompatible")
    if relationships.get("preregistration", {}).get("sha256") != sha256_file(discovery_file):
        raise V053ContractError("Relaciones V0.53 no pertenecen a esta preinscripcion")
    try:
        discovery_ref = str(discovery_file.relative_to(root)).replace("\\", "/")
        relationships_ref = str(relationships_file.relative_to(root)).replace("\\", "/")
    except ValueError as exc:
        raise V053ContractError("Los artefactos V0.53 deben estar dentro del proyecto") from exc
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
            raise V053ContractError("Ya existe otro bloqueo economico V0.53")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_economic_lock(
    path: str | Path, *, project_root: str | Path = ROOT
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V053ContractError("Bloqueo economico V0.53 no encontrado")
    payload = json.loads(source.read_text(encoding="utf-8"))
    for key, value in {
        "schema": ECONOMIC_LOCK_SCHEMA,
        "status": ECONOMIC_LOCK_STATUS,
        "variant": VARIANT,
        "contract": frozen_contract(),
        "clob_access_before_this_lock": False,
    }.items():
        if payload.get(key) != value:
            raise V053ContractError(f"Bloqueo V0.53 incompatible: {key}")
    discovery = payload.get("discovery_preregistration", {})
    relationships_record = payload.get("semantic_relationships", {})
    discovery_path = root / str(discovery.get("relative_path") or "")
    relationships_path = root / str(relationships_record.get("relative_path") or "")
    if discovery.get("sha256") != sha256_file(discovery_path):
        raise V053ContractError("Hash de preinscripcion V0.53 alterado")
    load_and_verify_discovery_preregistration(discovery_path, project_root=root)
    if relationships_record.get("sha256") != sha256_file(relationships_path):
        raise V053ContractError("Hash de relaciones V0.53 alterado")
    relationships = json.loads(relationships_path.read_text(encoding="utf-8"))
    if relationships_record.get("relationship_count") != len(relationships.get("relationships", [])):
        raise V053ContractError("Conteo de relaciones V0.53 alterado")
    return dict(payload), relationships


__all__ = [
    "DISCOVERY_PREREG_SCHEMA",
    "DISCOVERY_PREREG_STATUS",
    "ECONOMIC_LOCK_SCHEMA",
    "ECONOMIC_LOCK_STATUS",
    "SEMANTIC_SCHEMA",
    "SOURCE_FILES",
    "VARIANT",
    "V053ContractError",
    "build_discovery_preregistration",
    "build_economic_lock",
    "frozen_contract",
    "load_and_verify_discovery_preregistration",
    "load_and_verify_economic_lock",
]
