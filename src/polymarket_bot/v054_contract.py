from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file


PREREG_SCHEMA = "prereg_v054_public_combo_observability_census_1"
PREREG_STATUS = "FROZEN_BEFORE_PUBLIC_COMBO_CATALOG_ACCESS"
VARIANT = "V0.54_PUBLIC_COMBO_RFQ_OBSERVABILITY_CENSUS"

SOURCE_FILES = {
    "v053_result": "data/resultado_v053_cross_event_threshold_taker_floor_census.json",
    "v054_contract_code": "src/polymarket_bot/v054_contract.py",
    "v054_census_code": "src/polymarket_bot/v054_combo_public_census.py",
    "v054_monitor_code": "v054_monitor.py",
    "v054_tests": "tests/test_v054_combo_public_census.py",
}


class V054ContractError(RuntimeError):
    pass


def frozen_contract() -> dict[str, Any]:
    return {
        "catalog": {
            "endpoint": "https://combos-rfq-api.polymarket.com/v1/rfq/combo-markets",
            "method": "GET",
            "limit": 100,
            "maximum_pages": 3,
            "maximum_markets": 300,
            "pagination": "OFFICIAL_OPAQUE_CURSOR_PASSED_UNCHANGED",
            "cursor_repeat_fails_closed": True,
            "selection_order": "PAGE_THEN_RESPONSE_ORDER_ALL_RECORDS_UNTIL_FROZEN_CAP",
            "price_based_selection_allowed": False,
            "minimum_valid_markets": 10,
            "minimum_valid_fraction": 0.90,
        },
        "market_schema": {
            "required_fields": [
                "id",
                "condition_id",
                "position_ids",
                "slug",
                "title",
                "outcomes",
                "outcome_prices",
                "volume",
                "tags",
            ],
            "binary_outcomes": ["Yes", "No"],
            "position_outcome_price_mapping": "SAME_ARRAY_INDEX",
            "outcome_prices_are": "INDICATIVE_LEG_PRICES_NOT_EXECUTABLE_COMBO_QUOTES",
        },
        "economic_observability": {
            "public_catalog_contains_active_combo_leg_markets": True,
            "public_catalog_documents_executable_combo_quotes": False,
            "public_catalog_documents_active_rfq_requests": False,
            "rfq_requests_documented_channel": "AUTHENTICATED_QUOTER_WEBSOCKET",
            "maker_quote_requires": ["CLOB_L2_AUTHENTICATION", "SIGNED_EXCHANGE_V3_ORDER"],
            "forbidden_catalog_quote_fields": [
                "rfq_id",
                "quote_id",
                "price_e6",
                "size_e6",
                "signed_order",
                "submission_deadline",
                "blended_price_e6",
                "allocations",
            ],
            "profitability_allowed_without_executable_combo_quote": False,
            "pnl_allowed_without_executable_combo_quote": False,
        },
        "decision": {
            "insufficient_catalog": "FAIL_PUBLIC_COMBO_CATALOG_INSUFFICIENT_VALID_MARKETS",
            "unexpected_quote_schema": "STOP_PUBLIC_COMBO_SCHEMA_CHANGED_MANUAL_REVIEW",
            "catalog_only": "STOP_COMBO_RFQ_PUBLIC_READ_ONLY_NOT_ECONOMICALLY_OBSERVABLE",
            "next_step_catalog_only": "DO_NOT_INFER_COMBO_EDGE_FROM_LEG_PRICES_REQUIRE_EXPLICIT_AUTHORITY_FOR_AUTHENTICATED_PAPER_OBSERVER",
            "automatic_followup_launch": False,
            "posthoc_selection_allowed": False,
        },
        "execution": {
            "maximum_total_minutes": 5.0,
            "new_capture_hours": 0.0,
            "scheduled_supervision": False,
            "public_get_only": True,
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "authentication_required": False,
            "websocket_enabled": False,
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
            raise V054ContractError(f"Falta evidencia V0.54: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    return evidence


def build_preregistration(
    *, output_path: str | Path, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    v053 = json.loads(
        (root / SOURCE_FILES["v053_result"]).read_text(encoding="utf-8")
    )
    if v053.get("verdict") != "CLOSE_STANDARD_THRESHOLD_RELATION_FAMILY_NO_SEMANTIC_PAIRS":
        raise V054ContractError("V0.53 no conserva el cierre semantico esperado")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "verify_native_combo_catalog_and_public_economic_observability_without_authentication_orders_or_posthoc_edge_inference",
        "source_evidence": _source_evidence(root),
        "official_references": [
            "https://docs.polymarket.com/api-reference/combo-markets/get-combo-markets",
            "https://docs.polymarket.com/api-reference/wss/rfq",
            "https://docs.polymarket.com/api-reference/maker/submit-a-quote",
        ],
        "contract": frozen_contract(),
        "change_from_v053": {
            "mechanism": "NATIVE_COMBO_RFQ_INSTEAD_OF_INFERRED_THRESHOLD_RELATION",
            "semantic_threshold_parser_used": False,
            "clob_books_used": False,
            "authenticated_rfq_stream_used": False,
            "profitability_test_authorized": False,
            "v053_artifacts_modified": False,
        },
        "stage_order": [
            "FREEZE_PUBLIC_COMBO_CATALOG_AND_OBSERVABILITY_RULES",
            "GET_PUBLIC_CATALOG_WITH_FROZEN_PAGINATION",
            "VALIDATE_ALL_RECORDS_WITHOUT_PRICE_BASED_SELECTION",
            "STOP_IF_EXECUTABLE_RFQ_PRICE_IS_NOT_PUBLICLY_OBSERVED",
        ],
        "safety": copy.deepcopy(frozen_contract()["safety"]),
    }
    output = Path(output_path).resolve()
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        comparable = dict(existing)
        comparable["created_at"] = payload["created_at"]
        if comparable != payload:
            raise V054ContractError("Ya existe otra preinscripcion V0.54")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_preregistration(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V054ContractError("Preinscripcion V0.54 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    for key, expected in {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "variant": VARIANT,
        "contract": frozen_contract(),
    }.items():
        if payload.get(key) != expected:
            raise V054ContractError(f"Preinscripcion V0.54 incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V054ContractError("Inventario V0.54 incompatible")
    for key, relative in SOURCE_FILES.items():
        record = evidence[key]
        if record.get("relative_path") != relative:
            raise V054ContractError(f"Ruta V0.54 incompatible: {key}")
        if record.get("sha256") != sha256_file(root / relative):
            raise V054ContractError(f"Hash V0.54 no coincide: {key}")
    if payload.get("safety") != frozen_contract()["safety"]:
        raise V054ContractError("Seguridad V0.54 incompatible")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "SOURCE_FILES",
    "VARIANT",
    "V054ContractError",
    "build_preregistration",
    "frozen_contract",
    "load_and_verify_preregistration",
]
