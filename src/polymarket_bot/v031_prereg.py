from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v031_data_readiness import (
    READINESS_SCHEMA,
    frozen_capture_contract,
    validate_capture_contract,
)


PREREG_SCHEMA = "prereg_v031_path_execution_capture_1"
PREREG_STATUS = "FROZEN_CAPTURE_DESIGN_AWAITING_IMPLEMENTATION_AND_LAUNCH_APPROVAL"
VARIANT = "V0.31_PATH_EXECUTION_CAPTURE_ONLY_TECHNICAL_1H"
EXPECTED_EVIDENCE = {
    "readiness": "data/diagnostico_v031_path_execution_readiness.json",
}
EXPECTED_DESIGN_CODE = {
    "readiness": "src/polymarket_bot/v031_data_readiness.py",
    "resolution_contract": "src/polymarket_bot/resolution_contract.py",
}
TECHNICAL_GATES: dict[str, Any] = {
    "minimum_market_coverage": 0.90,
    "minimum_snapshot_coverage": 0.95,
    "minimum_complete_snapshot_coverage": 0.90,
    "minimum_chainlink_fresh_coverage": 0.90,
    "minimum_official_twap_fresh_coverage": 0.90,
    "minimum_both_books_fresh_coverage": 0.90,
    "required_resolution_contract_coverage": 1.0,
    "minimum_snapshots_per_captured_market": 285,
    "maximum_missing_seconds_per_captured_market": 15,
    "sqlite_quick_check_required": "ok",
}


class V031PreregistrationError(ValueError):
    pass


def _require(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise V031PreregistrationError(f"Preinscripcion V0.31 incompatible: {field}")


def validate_frozen_prereg_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise V031PreregistrationError("Preinscripcion V0.31 invalida")
    _require(payload.get("schema"), PREREG_SCHEMA, "schema")
    _require(payload.get("status"), PREREG_STATUS, "status")
    _require(payload.get("variant"), VARIANT, "variant")
    try:
        validate_capture_contract(payload.get("capture_contract"))
    except Exception as error:
        raise V031PreregistrationError(str(error)) from error
    _require(payload.get("technical_gates"), TECHNICAL_GATES, "technical_gates")
    _require(
        payload.get("stopping"),
        {
            "technical_pilot_hours": 1.0,
            "expected_five_minute_markets": 12,
            "early_success_allowed": False,
            "economic_futility_used": False,
            "scheduled_supervision": False,
            "final_result_only": True,
            "automatic_restart": False,
            "automatic_followup_launch": False,
        },
        "stopping",
    )
    _require(
        payload.get("data_policy"),
        {
            "new_backtest_hours": 0,
            "outcomes_read": 0,
            "labels_stored": False,
            "signals_generated": False,
            "pnl_calculated": False,
            "economic_strategy_present": False,
            "single_database_must_contain_all_required_streams": True,
            "cross_date_dataset_join_allowed": False,
        },
        "data_policy",
    )
    _require(
        payload.get("implementation"),
        {
            "collector_built": False,
            "technical_auditor_built": False,
            "entrypoint_built": False,
            "launch_approved": False,
            "launch_status": "NOT_LAUNCHED",
        },
        "implementation",
    )
    _require(
        payload.get("safety"),
        {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
            "maximum_hours": 1.0,
        },
        "safety",
    )
    return dict(payload)


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_frozen_prereg(
    *,
    output_path: str | Path,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    output = Path(output_path).resolve()
    readiness_path = root / EXPECTED_EVIDENCE["readiness"]
    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    if readiness.get("schema") != READINESS_SCHEMA or readiness.get("decision") != (
        "BUILD_CAPTURE_ONLY_V031_BEFORE_ANY_NEW_STRATEGY_TEST"
    ):
        raise V031PreregistrationError("Diagnostico V0.31 incompatible")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "capture_contract": frozen_capture_contract(),
        "technical_gates": dict(TECHNICAL_GATES),
        "stopping": {
            "technical_pilot_hours": 1.0,
            "expected_five_minute_markets": 12,
            "early_success_allowed": False,
            "economic_futility_used": False,
            "scheduled_supervision": False,
            "final_result_only": True,
            "automatic_restart": False,
            "automatic_followup_launch": False,
        },
        "data_policy": {
            "new_backtest_hours": 0,
            "outcomes_read": 0,
            "labels_stored": False,
            "signals_generated": False,
            "pnl_calculated": False,
            "economic_strategy_present": False,
            "single_database_must_contain_all_required_streams": True,
            "cross_date_dataset_join_allowed": False,
        },
        "evidence": {
            key: {
                "relative_path": relative,
                "sha256": sha256_file(root / relative),
            }
            for key, relative in EXPECTED_EVIDENCE.items()
        },
        "design_code_hashes": {
            key: sha256_file(root / relative)
            for key, relative in EXPECTED_DESIGN_CODE.items()
        },
        "implementation": {
            "collector_built": False,
            "technical_auditor_built": False,
            "entrypoint_built": False,
            "launch_approved": False,
            "launch_status": "NOT_LAUNCHED",
        },
        "promotion_cap": {
            "maximum_positive_result": "PASS_TECHNICAL_CAPTURE_ONLY",
            "economic_edge_can_be_approved": False,
            "paper_or_real_money_can_be_approved": False,
            "separate_future_strategy_preregistration_required": True,
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
            "maximum_hours": 1.0,
        },
    }
    validate_frozen_prereg_payload(payload)
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if existing.get("schema") == PREREG_SCHEMA:
            load_and_verify_frozen_prereg(output, project_root=root)
            return existing
        raise V031PreregistrationError("Ya existe otra preinscripcion V0.31")
    _write_atomic(output, payload)
    return payload


def load_and_verify_frozen_prereg(
    path: str | Path,
    *,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(path).resolve()
    payload = validate_frozen_prereg_payload(
        json.loads(prereg_file.read_text(encoding="utf-8"))
    )
    for key, relative in EXPECTED_EVIDENCE.items():
        record = payload.get("evidence", {}).get(key, {})
        _require(record.get("relative_path"), relative, f"evidence.{key}.path")
        _require(
            record.get("sha256"),
            sha256_file(root / relative),
            f"evidence.{key}.sha256",
        )
    for key, relative in EXPECTED_DESIGN_CODE.items():
        _require(
            payload.get("design_code_hashes", {}).get(key),
            sha256_file(root / relative),
            f"design_code_hashes.{key}",
        )
    return payload


__all__ = [
    "PREREG_SCHEMA",
    "TECHNICAL_GATES",
    "VARIANT",
    "V031PreregistrationError",
    "build_frozen_prereg",
    "load_and_verify_frozen_prereg",
    "validate_frozen_prereg_payload",
]
