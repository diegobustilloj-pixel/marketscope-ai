from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v028_design import DESIGN_SCHEMA, ONE_SIDED_95_Z
from polymarket_bot.v028_strategy import (
    PRIMARY_ID,
    V028StrategyError,
    frozen_arm_config,
    validate_arm_config,
)


PREREG_SCHEMA = "prereg_v028_twap_lt5_replication_design_1"
PREREG_STATUS = "FROZEN_DESIGN_AWAITING_IMPLEMENTATION_AND_LAUNCH_APPROVAL"
PREREG_VARIANT = "V0.28_UP_LOW_VOL_TWAP_LT5_REPLICATION_24H"

EXPECTED_EVIDENCE_PATHS = {
    "v027_result": "data/resultado_v027_regime_tournament.json",
    "v027_database": "data/paper_v027_regime_tournament.db",
    "v027_final_postmortem": "data/postmortem_v027_up_low_vol_final.json",
    "v028_candidate_design": "data/diagnostico_v028_twap_lt5_candidate.json",
}

EXPECTED_CODE_PATHS = {
    "strategy": "src/polymarket_bot/v028_strategy.py",
    "candidate_design": "src/polymarket_bot/v028_design.py",
    "postmortem": "src/polymarket_bot/v027_final_postmortem.py",
    "prereg_loader": "src/polymarket_bot/v028_prereg.py",
    "resolution_contract": "src/polymarket_bot/resolution_contract.py",
}

EXPECTED_HYPOTHESIS = {
    "candidate_id": PRIMARY_ID,
    "favorite_side": "Up",
    "signal_source": "market_implied_only_no_transferred_model",
    "decision_horizon_seconds": 60,
    "required_resolution_twap_window_s": 60,
    "maximum_abs_twap_distance_to_open_bps": 5.0,
    "distance_boundary": "STRICT_LESS_THAN",
    "distance_uses_only_official_open_and_decision_twap": True,
}

EXPECTED_FREQUENCY = {
    "minimum_final_trades": 20,
    "minimum_first_half_trades": 8,
    "minimum_second_half_trades": 8,
}

EXPECTED_ECONOMIC_GATES = {
    "positive_total_net_pnl": True,
    "profit_factor_above_one": True,
    "positive_first_half_pnl": True,
    "positive_second_half_pnl": True,
    "positive_net_pnl_without_best_trade": True,
    "minimum_evaluable_block_trades": 2,
    "minimum_evaluable_blocks": 4,
    "minimum_positive_evaluable_block_fraction": 0.6,
    "candidate_mean_pnl_above_parent_control": True,
}


class V028PreregistrationError(ValueError):
    pass


def _require_exact(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise V028PreregistrationError(f"Preinscripcion V0.28 incompatible: {field}")


def validate_frozen_prereg_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise V028PreregistrationError("Preinscripcion V0.28 invalida")
    _require_exact(payload.get("schema"), PREREG_SCHEMA, "schema")
    _require_exact(payload.get("status"), PREREG_STATUS, "status")
    _require_exact(payload.get("variant"), PREREG_VARIANT, "variant")
    _require_exact(payload.get("maximum_hours"), 24.0, "maximum_hours")
    try:
        validate_arm_config(payload.get("arms"))
    except V028StrategyError as error:
        raise V028PreregistrationError(str(error)) from error
    _require_exact(payload.get("hypothesis"), EXPECTED_HYPOTHESIS, "hypothesis")
    _require_exact(
        payload.get("candidate_frequency"), EXPECTED_FREQUENCY, "candidate_frequency"
    )
    _require_exact(
        payload.get("economic_replication_gates"),
        EXPECTED_ECONOMIC_GATES,
        "economic_replication_gates",
    )

    data_contract = payload.get("data_contract")
    _require_exact(
        data_contract,
        {
            "resolution_source_selection": "FAIL_CLOSED",
            "required_resolution_twap_window_s": 60,
            "required_resolution_contract_status": "VERIFIED",
            "official_twap_required_at_open": True,
            "official_twap_required_at_decision": True,
            "official_twap_max_age_ms": 5000,
            "missing_or_ambiguous_source_is_ineligible": True,
            "transferred_30s_model_enabled": False,
            "fallback_to_other_twap_window_allowed": False,
            "outcomes_read_only_after_terminal_state": True,
        },
        "data_contract",
    )
    execution = payload.get("execution_model")
    _require_exact(
        execution,
        {
            "paper_shares": 5.0,
            "slippage_per_share": 0.005,
            "fee_rate": 0.07,
            "entry": "favorite_best_ask_plus_slippage_and_fee",
            "binary_payout_per_winning_share": 1.0,
            "orders_created": False,
        },
        "execution_model",
    )
    statistical = payload.get("statistical_gate")
    _require_exact(
        statistical,
        {
            "primary_hypotheses": 1,
            "one_sided_alpha": 0.05,
            "one_sided_confidence": 0.95,
            "z_value": ONE_SIDED_95_Z,
            "positive_one_sided_lcb_required": True,
            "parent_control_selectable": False,
        },
        "statistical_gate",
    )
    stopping = payload.get("stopping")
    if not isinstance(stopping, Mapping):
        raise V028PreregistrationError("Preinscripcion V0.28 sin stopping")
    expected_stopping = {
        "maximum_hours": 24.0,
        "checkpoint_hours": [4.0, 8.0, 12.0, 16.0, 20.0],
        "technical_retry_minutes": 10.0,
        "early_success_allowed": False,
        "experiment_early_stop_when_primary_futile": True,
        "final_result_only": True,
        "intermediate_outcome_metrics_exposed": False,
        "intermediate_scheduled_reports": False,
    }
    _require_exact(stopping, expected_stopping, "stopping")
    _require_exact(
        payload.get("checkpoint_futility"),
        {
            "frequency_method": "one_sided_95_poisson_upper_projection_to_24h",
            "profitability_minimum_resolved_trades": 8,
            "profitability_method": "one_sided_95_mean_pnl_upper_bound_nonpositive",
            "first_half_frequency_checked_at_12h": True,
            "no_early_success": True,
        },
        "checkpoint_futility",
    )
    _require_exact(
        payload.get("terminal_audit"),
        {
            "completed_run_terminal_cancelled_feeds_are_expected": True,
            "requires_last_checkpoint_technical_passed": True,
            "requires_last_checkpoint_feeds_connected": True,
            "requires_all_non_feed_terminal_gates": True,
            "unexpected_disconnect_fails_closed": True,
            "database_query_only": True,
            "audit_once": True,
        },
        "terminal_audit",
    )
    _require_exact(
        payload.get("verdicts"),
        [
            "FAIL_SAFETY",
            "FAIL_TECHNICAL_QUALITY",
            "FAIL_INSUFFICIENT_FREQUENCY",
            "FAIL_ECONOMIC_REPLICATION",
            "CONTINUE_PAPER_ACCUMULATION",
            "PASS_SINGLE_HYPOTHESIS_PAPER_CANDIDATE",
        ],
        "verdicts",
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
            "threshold_selected_after_examining_13_segments": True,
            "fresh_independent_replication_required": True,
            "old_v027_rows_are_design_only": True,
            "single_primary_hypothesis": True,
            "no_midrun_threshold_changes": True,
            "no_reuse_of_v027_for_validation": True,
            "no_automatic_launch": True,
            "new_backtest_hours": 0,
            "new_forward_maximum_hours": 24.0,
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
        raise V028PreregistrationError("Evidencia de diseno V0.28 incompatible")
    evidence = {
        key: {
            "relative_path": relative,
            "sha256": sha256_file(root / relative),
        }
        for key, relative in EXPECTED_EVIDENCE_PATHS.items()
    }
    code_hashes = {
        key: sha256_file(root / relative)
        for key, relative in EXPECTED_CODE_PATHS.items()
    }
    development = design["development_evidence_only"]
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": PREREG_VARIANT,
        "maximum_hours": 24.0,
        "arms": frozen_arm_config(),
        "hypothesis": EXPECTED_HYPOTHESIS,
        "development_evidence_only": {
            "candidate_id": PRIMARY_ID,
            "trades": development["metrics"]["trades"],
            "net_pnl_at_5_shares": development["metrics"][
                "net_pnl_at_5_shares"
            ],
            "profit_factor": development["metrics"]["profit_factor"],
            "roi_on_cost": development["metrics"]["roi_on_cost"],
            "one_sided_95_lcb": development["metrics"]["one_sided_95_lcb"],
            "first_half_trades": development["first_half_metrics"]["trades"],
            "first_half_pnl_at_5_shares": development["first_half_metrics"][
                "net_pnl_at_5_shares"
            ],
            "second_half_trades": development["second_half_metrics"]["trades"],
            "second_half_pnl_at_5_shares": development["second_half_metrics"][
                "net_pnl_at_5_shares"
            ],
            "positive_evaluable_blocks": len(
                development["positive_evaluable_blocks"]
            ),
            "evaluable_blocks": len(development["evaluable_blocks"]),
            "net_without_best_trade_at_5_shares": development[
                "net_without_best_trade_at_5_shares"
            ],
            "validated": False,
        },
        "sample_size_planning": design["sample_size_planning"],
        "candidate_frequency": EXPECTED_FREQUENCY,
        "economic_replication_gates": EXPECTED_ECONOMIC_GATES,
        "statistical_gate": {
            "primary_hypotheses": 1,
            "one_sided_alpha": 0.05,
            "one_sided_confidence": 0.95,
            "z_value": ONE_SIDED_95_Z,
            "positive_one_sided_lcb_required": True,
            "parent_control_selectable": False,
        },
        "data_contract": {
            "resolution_source_selection": "FAIL_CLOSED",
            "required_resolution_twap_window_s": 60,
            "required_resolution_contract_status": "VERIFIED",
            "official_twap_required_at_open": True,
            "official_twap_required_at_decision": True,
            "official_twap_max_age_ms": 5000,
            "missing_or_ambiguous_source_is_ineligible": True,
            "transferred_30s_model_enabled": False,
            "fallback_to_other_twap_window_allowed": False,
            "outcomes_read_only_after_terminal_state": True,
        },
        "execution_model": {
            "paper_shares": 5.0,
            "slippage_per_share": 0.005,
            "fee_rate": 0.07,
            "entry": "favorite_best_ask_plus_slippage_and_fee",
            "binary_payout_per_winning_share": 1.0,
            "orders_created": False,
        },
        "stopping": {
            "maximum_hours": 24.0,
            "checkpoint_hours": [4.0, 8.0, 12.0, 16.0, 20.0],
            "technical_retry_minutes": 10.0,
            "early_success_allowed": False,
            "experiment_early_stop_when_primary_futile": True,
            "final_result_only": True,
            "intermediate_outcome_metrics_exposed": False,
            "intermediate_scheduled_reports": False,
        },
        "checkpoint_futility": {
            "frequency_method": "one_sided_95_poisson_upper_projection_to_24h",
            "profitability_minimum_resolved_trades": 8,
            "profitability_method": "one_sided_95_mean_pnl_upper_bound_nonpositive",
            "first_half_frequency_checked_at_12h": True,
            "no_early_success": True,
        },
        "terminal_audit": {
            "completed_run_terminal_cancelled_feeds_are_expected": True,
            "requires_last_checkpoint_technical_passed": True,
            "requires_last_checkpoint_feeds_connected": True,
            "requires_all_non_feed_terminal_gates": True,
            "unexpected_disconnect_fails_closed": True,
            "database_query_only": True,
            "audit_once": True,
        },
        "verdicts": [
            "FAIL_SAFETY",
            "FAIL_TECHNICAL_QUALITY",
            "FAIL_INSUFFICIENT_FREQUENCY",
            "FAIL_ECONOMIC_REPLICATION",
            "CONTINUE_PAPER_ACCUMULATION",
            "PASS_SINGLE_HYPOTHESIS_PAPER_CANDIDATE",
        ],
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
            "threshold_selected_after_examining_13_segments": True,
            "fresh_independent_replication_required": True,
            "old_v027_rows_are_design_only": True,
            "single_primary_hypothesis": True,
            "no_midrun_threshold_changes": True,
            "no_reuse_of_v027_for_validation": True,
            "no_automatic_launch": True,
            "new_backtest_hours": 0,
            "new_forward_maximum_hours": 24.0,
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
        raise V028PreregistrationError("Existe otra preinscripcion V0.28")
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
        raise V028PreregistrationError("Inventario de evidencia V0.28 incompatible")
    for key, relative in EXPECTED_EVIDENCE_PATHS.items():
        record = evidence[key]
        if not isinstance(record, Mapping):
            raise V028PreregistrationError(f"Evidencia V0.28 invalida: {key}")
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
        raise V028PreregistrationError("Inventario de codigo V0.28 incompatible")
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
    "V028PreregistrationError",
    "build_frozen_prereg",
    "load_and_verify_frozen_prereg",
    "validate_frozen_prereg_payload",
]
