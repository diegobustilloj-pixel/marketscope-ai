from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file


PREREG_SCHEMA = "prereg_v044_short_horizon_repricing_rejection_1"
PREREG_STATUS = "FROZEN_CLOSED_DEVELOPMENT_AUDIT"
VARIANT = "V0.44_SHORT_HORIZON_REPRICING_REJECTION"
SOURCE_FILES = {
    "v031_database": "data/capture_v031_path_execution_1h.db",
    "v031_quality_result": "data/resultado_v031_quality_semantics_reaudit_v2.json",
    "v042_preregistration": "data/prereg_v042_planned_chainlink_reconnect_4h.json",
    "v043_result": "data/resultado_v043_chainlink_outage_fail_closed_audit.json",
}


class V044ContractError(RuntimeError):
    pass


def frozen_contract() -> dict[str, Any]:
    return {
        "market_family": "btc-updown-5m",
        "source_window_hours": 1.0,
        "decision_offsets_inclusive": [30, 90],
        "decision_latency_seconds": 1,
        "holding_seconds_after_entry": 30,
        "maximum_trades_per_market_per_variant": 1,
        "position_shares": 5.0,
        "chainlink_lookbacks_seconds": [1, 3, 5],
        "minimum_absolute_chainlink_moves_bps": [1.0, 2.0, 3.0, 5.0],
        "direction": "UP_IF_CHAINLINK_MOVE_POSITIVE_ELSE_DOWN",
        "lag_rule": "SELECTED_OUTCOME_MID_CHANGE_OVER_LOOKBACK_LE_ZERO",
        "entry": "TOP5_ASK_VWAP_AT_DECISION_PLUS_ONE_SECOND",
        "exit": "V042_PROACTIVE_TRANSPORT_AND_SCHEDULED_EXIT_CONTRACT",
        "trapped_position_valuation": "ZERO_EXIT_PROCEEDS",
        "taker_fee_rate": 0.07,
        "slippage_per_share_each_leg": 0.005,
        "parameter_variants": 12,
        "variant_use": "FAMILY_REJECTION_ONLY_NEVER_POSTHOC_SELECTION",
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
    output = Path(output_path).resolve()
    evidence: dict[str, Any] = {}
    for key, relative in SOURCE_FILES.items():
        source = root / relative
        if not source.is_file():
            raise V044ContractError(f"Falta evidencia V0.44: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "closed_multi_sensitivity_family_rejection_before_fresh_collection",
        "source_evidence": evidence,
        "economic_contract": frozen_contract(),
        "safety_inheritance": {
            "entry_requires_fresh_chainlink_official_twap_and_both_books": True,
            "entry_requires_all_four_top5_depths_at_least_ten_shares": True,
            "exit_does_not_require_chainlink_or_official_twap": True,
            "synthetic_complement_allowed": False,
            "partial_entry_allowed": False,
            "partial_exit_allowed": False,
            "v043_provider_liveness_claimed": False,
        },
        "interpretation": {
            "fresh_validation": False,
            "all_variants_nonpositive_action": "CLOSE_SHORT_HORIZON_TAKER_REPRICING_FAMILY",
            "any_positive_variant_action": "NO_SELECTION_REQUIRE_ONE_FRESH_PREREGISTERED_REPLICATION",
            "posthoc_winner_selection_allowed": False,
            "paper_or_money_promotion_allowed": False,
            "automatic_followup_launch": False,
        },
        "execution": {
            "new_capture_hours": 0.0,
            "new_backtest_hours": 0.0,
            "database_read_only": True,
            "scheduled_supervision": False,
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
    }
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        comparable = dict(existing)
        comparable["created_at"] = payload["created_at"]
        if comparable != payload:
            raise V044ContractError("Ya existe otra preinscripcion V0.44")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_preregistration(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V044ContractError("Preinscripcion V0.44 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    expected = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "variant": VARIANT,
        "economic_contract": frozen_contract(),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V044ContractError(f"Preinscripcion V0.44 incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V044ContractError("Inventario de evidencia V0.44 incompatible")
    for key, relative in SOURCE_FILES.items():
        record = evidence[key]
        source_file = root / relative
        if record.get("relative_path") != relative:
            raise V044ContractError(f"Ruta V0.44 incompatible: {key}")
        if record.get("sha256") != sha256_file(source_file):
            raise V044ContractError(f"Hash V0.44 no coincide: {key}")
    safety = payload.get("safety", {})
    required_safety = {
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }
    for key, value in required_safety.items():
        if safety.get(key) != value:
            raise V044ContractError(f"Seguridad V0.44 incompatible: {key}")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "SOURCE_FILES",
    "VARIANT",
    "V044ContractError",
    "build_preregistration",
    "frozen_contract",
    "load_and_verify_preregistration",
]
