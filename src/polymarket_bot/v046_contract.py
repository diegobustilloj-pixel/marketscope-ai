from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file


PREREG_SCHEMA = "prereg_v046_rest_websocket_transport_probe_1"
PREREG_STATUS = "FROZEN_BEFORE_PUBLIC_TRANSPORT_PROBE"
VARIANT = "V0.46_REST_WEBSOCKET_NEG_RISK_SYNCHRONIZATION_PROBE"
SOURCE_FILES = {
    "v045_preregistration": "data/prereg_v045_neg_risk_structural_census.json",
    "v045_result": "data/resultado_v045_neg_risk_structural_census.json",
    "v045_contract_code": "src/polymarket_bot/v045_contract.py",
    "v045_census_code": "src/polymarket_bot/v045_census.py",
}


class V046ContractError(RuntimeError):
    pass


def frozen_contract() -> dict[str, Any]:
    return {
        "purpose": "TRANSPORT_SEMANTICS_ONLY_NO_ECONOMIC_RETUNING",
        "event_selection": {
            "reuse_v045_filter_function": True,
            "gamma_active_open_top_volume24hr_limit": 100,
            "maximum_eligible_events": 10,
            "selection_order": "GAMMA_RESPONSE_ORDER_NO_POSTHOC_SELECTION",
        },
        "transport_sequence": [
            "REST_BATCH_BOOKS_BEFORE",
            "ONE_PUBLIC_WEBSOCKET_SUBSCRIPTION_FOR_ALL_YES_TOKENS",
            "REST_BATCH_BOOKS_AFTER",
        ],
        "websocket": {
            "endpoint": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            "subscription_type": "market",
            "full_book_event_type": "book",
            "heartbeat_text": "PING",
            "heartbeat_seconds": 10.0,
            "initial_snapshot_timeout_seconds": 20.0,
            "maximum_local_receive_spread_ms_per_event": 2000,
        },
        "continuity": {
            "require_rest_before_for_every_token": True,
            "require_websocket_book_for_every_token": True,
            "require_rest_after_for_every_token": True,
            "require_nonempty_official_hashes": True,
            "websocket_hash_must_match_rest_before_or_after": True,
        },
        "evidence_gate": {
            "minimum_complete_events": 5,
            "minimum_complete_fraction_of_eligible": 0.5,
            "source_timestamp_spread_is_diagnostic_only": True,
            "local_receive_spread_is_transport_gate": True,
        },
        "interpretation": {
            "pass_action": "ALLOW_SEPARATE_PREREGISTERED_V047_WS_RECEIVE_WINDOW_CENSUS",
            "fail_action": "DO_NOT_REPEAT_ECONOMIC_CENSUS_UNTIL_TRANSPORT_IS_RESOLVED",
            "automatic_followup_launch": False,
            "economic_result_allowed": False,
            "v045_family_rejection_allowed": False,
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


def build_preregistration(
    *, output_path: str | Path, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    evidence: dict[str, Any] = {}
    for key, relative in SOURCE_FILES.items():
        source = root / relative
        if not source.is_file():
            raise V046ContractError(f"Falta evidencia V0.46: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    v045 = json.loads((root / SOURCE_FILES["v045_result"]).read_text(encoding="utf-8"))
    if v045.get("verdict") != "FAIL_CENSUS_INSUFFICIENT_COMPARABLE_BOOKS":
        raise V046ContractError("V0.45 no tiene el fallo de comparabilidad esperado")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "source_evidence": evidence,
        "official_references": [
            "https://docs.polymarket.com/api-reference/wss/market",
            "https://docs.polymarket.com/market-data/realtime-data",
            "https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body",
        ],
        "technical_contract": frozen_contract(),
        "execution": {
            "maximum_probe_minutes": 2.0,
            "scheduled_supervision": False,
            "public_endpoints_only": True,
            "persistent_capture": False,
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
            raise V046ContractError("Ya existe otra preinscripcion V0.46")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_preregistration(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V046ContractError("Preinscripcion V0.46 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    expected = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "variant": VARIANT,
        "technical_contract": frozen_contract(),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V046ContractError(f"Preinscripcion V0.46 incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V046ContractError("Inventario de evidencia V0.46 incompatible")
    for key, relative in SOURCE_FILES.items():
        record = evidence[key]
        if record.get("relative_path") != relative:
            raise V046ContractError(f"Ruta V0.46 incompatible: {key}")
        if record.get("sha256") != sha256_file(root / relative):
            raise V046ContractError(f"Hash V0.46 no coincide: {key}")
    required_safety = {
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "wallet_required": False,
        "authentication_required": False,
        "real_money": "BLOQUEADO",
    }
    for key, value in required_safety.items():
        if payload.get("safety", {}).get(key) != value:
            raise V046ContractError(f"Seguridad V0.46 incompatible: {key}")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "SOURCE_FILES",
    "VARIANT",
    "V046ContractError",
    "build_preregistration",
    "frozen_contract",
    "load_and_verify_preregistration",
]
