from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v029_design import DESIGN_SCHEMA
from polymarket_bot.v029_strategy import (
    CANDIDATE_ID,
    PAIRED_CONTROL_ID,
    V029StrategyError,
    frozen_model_spec,
    frozen_selection_spec,
    validate_model_spec,
    validate_selection_spec,
)


PREREG_SCHEMA = "prereg_v029_high_frequency_temporal_holdout_1"
PREREG_STATUS = "FROZEN_DESIGN_AWAITING_IMPLEMENTATION_AND_LAUNCH_APPROVAL"
PREREG_VARIANT = "V0.29_HIGH_FREQUENCY_TEMPORAL_HOLDOUT_24H"

EXPECTED_EVIDENCE_PATHS = {
    "v028_result": "data/resultado_v028_twap_lt5_replication.json",
    "v028_database": "data/paper_v028_twap_lt5_replication.db",
    "v028_final_postmortem": "data/postmortem_v028_twap_lt5_final.json",
    "v029_design": "data/diagnostico_v029_high_frequency_holdout.json",
}

EXPECTED_CODE_PATHS = {
    "strategy": "src/polymarket_bot/v029_strategy.py",
    "design": "src/polymarket_bot/v029_design.py",
    "postmortem": "src/polymarket_bot/v028_final_postmortem.py",
    "prereg_loader": "src/polymarket_bot/v029_prereg.py",
    "resolution_contract": "src/polymarket_bot/resolution_contract.py",
}

EXPECTED_HOLDOUT = {
    "total_hours": 24.0,
    "training_hours": 12.0,
    "validation_hours": 12.0,
    "split_rule": "market_start_before_experiment_start_plus_12h",
    "model_fit_after_terminal_state_only": True,
    "training_rows_fit_preprocessing_and_coefficients": True,
    "validation_rows_never_fit_preprocessing_or_coefficients": True,
    "training_pnl_is_not_selection_evidence": True,
    "validation_metrics_are_the_only_economic_test": True,
}

EXPECTED_GATES = {
    "minimum_training_rows": 100,
    "minimum_validation_rows": 100,
    "minimum_validation_candidate_trades": 20,
    "positive_validation_net_pnl": True,
    "validation_profit_factor_above_one": True,
    "positive_validation_one_sided_95_lcb": True,
    "positive_validation_without_best_trade": True,
    "candidate_mean_above_paired_control": True,
}


class V029PreregistrationError(ValueError):
    pass


def _require_exact(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise V029PreregistrationError(f"Preinscripcion V0.29 incompatible: {field}")


def validate_frozen_prereg_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise V029PreregistrationError("Preinscripcion V0.29 invalida")
    _require_exact(payload.get("schema"), PREREG_SCHEMA, "schema")
    _require_exact(payload.get("status"), PREREG_STATUS, "status")
    _require_exact(payload.get("variant"), PREREG_VARIANT, "variant")
    _require_exact(payload.get("maximum_hours"), 24.0, "maximum_hours")
    candidate = payload.get("candidate")
    if not isinstance(candidate, Mapping):
        raise V029PreregistrationError("Preinscripcion V0.29 sin candidato")
    _require_exact(candidate.get("id"), CANDIDATE_ID, "candidate.id")
    _require_exact(
        candidate.get("paired_control_id"),
        PAIRED_CONTROL_ID,
        "candidate.paired_control_id",
    )
    try:
        validate_model_spec(candidate.get("model"))
        validate_selection_spec(candidate.get("selection"))
    except V029StrategyError as error:
        raise V029PreregistrationError(str(error)) from error
    _require_exact(payload.get("temporal_holdout"), EXPECTED_HOLDOUT, "holdout")
    _require_exact(payload.get("validation_gates"), EXPECTED_GATES, "gates")
    _require_exact(
        payload.get("data_contract"),
        {
            "resolution_source_selection": "FAIL_CLOSED",
            "required_resolution_twap_window_s": 60,
            "required_resolution_contract_status": "VERIFIED",
            "official_twap_required_at_open": True,
            "official_twap_required_at_decision": True,
            "official_twap_max_age_ms": 5000,
            "fallback_to_other_twap_window_allowed": False,
            "outcomes_read_only_after_terminal_state": True,
            "training_and_validation_labels_separated_by_time": True,
        },
        "data_contract",
    )
    _require_exact(
        payload.get("execution_model"),
        {
            "paper_shares": 5.0,
            "slippage_per_share": 0.005,
            "fee_rate": 0.07,
            "entry": "selected_side_best_ask_plus_slippage_and_fee",
            "binary_payout_per_winning_share": 1.0,
            "orders_created": False,
        },
        "execution_model",
    )
    _require_exact(
        payload.get("stopping"),
        {
            "maximum_hours": 24.0,
            "technical_checkpoint_hours": [4.0, 8.0, 12.0, 16.0, 20.0],
            "technical_retry_minutes": 10.0,
            "early_success_allowed": False,
            "economic_futility_before_terminal_allowed": False,
            "final_result_only": True,
            "intermediate_outcome_metrics_exposed": False,
            "intermediate_scheduled_reports": False,
        },
        "stopping",
    )
    _require_exact(
        payload.get("promotion_cap"),
        {
            "maximum_positive_result": "HYPOTHESIS_REQUIRES_FRESH_V030_REPLICATION",
            "direct_paper_promotion_allowed": False,
            "money_real_promotion_allowed": False,
        },
        "promotion_cap",
    )
    _require_exact(
        payload.get("implementation"),
        {
            "runner_built": False,
            "auditor_built": False,
            "entrypoint_built": False,
            "launch_approved": False,
            "launch_status": "NOT_LAUNCHED",
        },
        "implementation",
    )
    _require_exact(
        payload.get("anti_overfit"),
        {
            "v028_outcomes_used_to_choose_features_or_thresholds": False,
            "v028_outcome_blind_funnel_used_for_capacity_only": True,
            "model_and_hyperparameters_frozen_before_collection": True,
            "hyperparameter_search_allowed": False,
            "second_half_is_sealed_holdout": True,
            "no_midrun_model_fit": True,
            "fresh_v030_replication_required_after_any_pass": True,
            "no_automatic_launch": True,
            "new_backtest_hours": 0,
        },
        "anti_overfit",
    )
    _require_exact(
        payload.get("safety"),
        {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
            "maximum_hours": 24,
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
    design_path: str | Path,
    output_path: str | Path,
    project_root: str | Path,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    design_file = Path(design_path).resolve()
    output = Path(output_path).resolve()
    design = json.loads(design_file.read_text(encoding="utf-8"))
    if design.get("schema") != DESIGN_SCHEMA:
        raise V029PreregistrationError("Evidencia de diseno V0.29 incompatible")
    evidence = {
        key: {"relative_path": relative, "sha256": sha256_file(root / relative)}
        for key, relative in EXPECTED_EVIDENCE_PATHS.items()
    }
    code_hashes = {
        key: sha256_file(root / relative)
        for key, relative in EXPECTED_CODE_PATHS.items()
    }
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": PREREG_VARIANT,
        "maximum_hours": 24.0,
        "candidate": {
            "id": CANDIDATE_ID,
            "role": "single_development_candidate",
            "model": frozen_model_spec(),
            "selection": frozen_selection_spec(),
            "paired_control_id": PAIRED_CONTROL_ID,
        },
        "temporal_holdout": EXPECTED_HOLDOUT,
        "validation_gates": EXPECTED_GATES,
        "data_contract": {
            "resolution_source_selection": "FAIL_CLOSED",
            "required_resolution_twap_window_s": 60,
            "required_resolution_contract_status": "VERIFIED",
            "official_twap_required_at_open": True,
            "official_twap_required_at_decision": True,
            "official_twap_max_age_ms": 5000,
            "fallback_to_other_twap_window_allowed": False,
            "outcomes_read_only_after_terminal_state": True,
            "training_and_validation_labels_separated_by_time": True,
        },
        "execution_model": {
            "paper_shares": 5.0,
            "slippage_per_share": 0.005,
            "fee_rate": 0.07,
            "entry": "selected_side_best_ask_plus_slippage_and_fee",
            "binary_payout_per_winning_share": 1.0,
            "orders_created": False,
        },
        "stopping": {
            "maximum_hours": 24.0,
            "technical_checkpoint_hours": [4.0, 8.0, 12.0, 16.0, 20.0],
            "technical_retry_minutes": 10.0,
            "early_success_allowed": False,
            "economic_futility_before_terminal_allowed": False,
            "final_result_only": True,
            "intermediate_outcome_metrics_exposed": False,
            "intermediate_scheduled_reports": False,
        },
        "promotion_cap": {
            "maximum_positive_result": "HYPOTHESIS_REQUIRES_FRESH_V030_REPLICATION",
            "direct_paper_promotion_allowed": False,
            "money_real_promotion_allowed": False,
        },
        "verdicts": [
            "FAIL_SAFETY",
            "FAIL_TECHNICAL_QUALITY",
            "FAIL_INSUFFICIENT_TRAINING_DATA",
            "FAIL_MODEL_TRAINING",
            "FAIL_INSUFFICIENT_VALIDATION_FREQUENCY",
            "FAIL_VALIDATION_ECONOMICS",
            "HYPOTHESIS_REQUIRES_FRESH_V030_REPLICATION",
        ],
        "capacity_planning": design["outcome_blind_capacity_planning"],
        "evidence": evidence,
        "design_code_hashes": code_hashes,
        "implementation": {
            "runner_built": False,
            "auditor_built": False,
            "entrypoint_built": False,
            "launch_approved": False,
            "launch_status": "NOT_LAUNCHED",
        },
        "anti_overfit": {
            "v028_outcomes_used_to_choose_features_or_thresholds": False,
            "v028_outcome_blind_funnel_used_for_capacity_only": True,
            "model_and_hyperparameters_frozen_before_collection": True,
            "hyperparameter_search_allowed": False,
            "second_half_is_sealed_holdout": True,
            "no_midrun_model_fit": True,
            "fresh_v030_replication_required_after_any_pass": True,
            "no_automatic_launch": True,
            "new_backtest_hours": 0,
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
            "maximum_hours": 24,
        },
    }
    validate_frozen_prereg_payload(payload)
    if output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == PREREG_SCHEMA
            and existing.get("evidence") == evidence
            and existing.get("design_code_hashes") == code_hashes
        ):
            return existing
        raise V029PreregistrationError("Existe otra preinscripcion V0.29")
    _write_atomic(output, payload)
    return payload


def load_and_verify_frozen_prereg(
    prereg_path: str | Path, *, project_root: str | Path | None = None
) -> dict[str, Any]:
    prereg_file = Path(prereg_path).resolve()
    root = (
        Path(project_root).resolve()
        if project_root is not None
        else prereg_file.parent.parent
    )
    payload = validate_frozen_prereg_payload(
        json.loads(prereg_file.read_text(encoding="utf-8"))
    )
    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(
        EXPECTED_EVIDENCE_PATHS
    ):
        raise V029PreregistrationError("Inventario de evidencia V0.29 incompatible")
    for key, relative in EXPECTED_EVIDENCE_PATHS.items():
        record = evidence[key]
        if not isinstance(record, Mapping):
            raise V029PreregistrationError(f"Evidencia V0.29 invalida: {key}")
        _require_exact(record.get("relative_path"), relative, f"evidence.{key}.path")
        _require_exact(
            sha256_file(root / relative),
            record.get("sha256"),
            f"evidence.{key}.sha256",
        )
    code_hashes = payload.get("design_code_hashes")
    if not isinstance(code_hashes, Mapping) or set(code_hashes) != set(
        EXPECTED_CODE_PATHS
    ):
        raise V029PreregistrationError("Inventario de codigo V0.29 incompatible")
    for key, relative in EXPECTED_CODE_PATHS.items():
        _require_exact(
            sha256_file(root / relative),
            code_hashes.get(key),
            f"design_code_hashes.{key}",
        )
    return payload


__all__ = [
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "PREREG_VARIANT",
    "V029PreregistrationError",
    "build_frozen_prereg",
    "load_and_verify_frozen_prereg",
    "validate_frozen_prereg_payload",
]
