from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v027_strategy import V027StrategyError, validate_arm_config


PREREG_SCHEMA = "prereg_v027_regime_replication_tournament_design_1"
PREREG_STATUS = "FROZEN_DESIGN_AWAITING_IMPLEMENTATION_AND_LAUNCH_APPROVAL"
PREREG_VARIANT = "V0.27_REGIME_REPLICATION_TOURNAMENT_24H"

EXPECTED_EVIDENCE_PATHS = {
    "v026b_result": "data/resultado_v026b_adaptive_checkpoints.json",
    "v026b_database": "data/paper_v026b_adaptive_checkpoints.db",
    "terminal_reconciliation": "data/reconciliacion_v026b_auditoria_terminal.json",
    "closed_postmortem": "data/postmortem_v027_from_v026b_closed.json",
    "candidate_design": "data/diagnostico_v027_candidates.json",
}

EXPECTED_CODE_PATHS = {
    "strategy": "src/polymarket_bot/v027_strategy.py",
    "candidate_design": "src/polymarket_bot/v027_design.py",
    "postmortem": "src/polymarket_bot/v027_postmortem.py",
    "terminal_reconciliation": "src/polymarket_bot/v026b_reconciliation.py",
    "prereg_loader": "src/polymarket_bot/v027_prereg.py",
}

EXPECTED_FREQUENCY = {
    "favorite_up_low_vol_lt_075": {
        "minimum_final_trades": 15,
        "minimum_first_half_trades": 5,
        "minimum_second_half_trades": 5,
    },
    "favorite_down_cost_070_080": {
        "minimum_final_trades": 10,
        "minimum_first_half_trades": 3,
        "minimum_second_half_trades": 3,
    },
}

EXPECTED_ECONOMIC_GATES = {
    "positive_total_net_pnl": True,
    "profit_factor_above_one": True,
    "positive_first_half_pnl": True,
    "positive_second_half_pnl": True,
    "minimum_evaluable_block_trades": 2,
    "minimum_evaluable_blocks": 4,
    "minimum_positive_evaluable_block_fraction": 0.6,
    "candidate_mean_pnl_above_parent_control": True,
}


class V027PreregistrationError(ValueError):
    pass


def _require_exact(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise V027PreregistrationError(f"Preinscripcion V0.27 incompatible: {field}")


def validate_frozen_prereg_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise V027PreregistrationError("Preinscripcion V0.27 invalida")
    _require_exact(payload.get("schema"), PREREG_SCHEMA, "schema")
    _require_exact(payload.get("status"), PREREG_STATUS, "status")
    _require_exact(payload.get("variant"), PREREG_VARIANT, "variant")
    _require_exact(payload.get("maximum_hours"), 24.0, "maximum_hours")
    try:
        validate_arm_config(payload.get("arms"))
    except V027StrategyError as error:
        raise V027PreregistrationError(str(error)) from error

    stopping = payload.get("stopping")
    if not isinstance(stopping, Mapping):
        raise V027PreregistrationError("Preinscripcion V0.27 sin stopping")
    _require_exact(stopping.get("maximum_hours"), 24.0, "stopping.maximum_hours")
    _require_exact(
        stopping.get("checkpoint_hours"),
        [4.0, 8.0, 12.0, 16.0, 20.0],
        "stopping.checkpoint_hours",
    )
    for field, expected in {
        "early_success_allowed": False,
        "candidate_futility_is_individual": True,
        "experiment_early_stop_requires_all_candidates_futile": True,
        "final_result_only": True,
        "intermediate_scheduled_reports": False,
    }.items():
        _require_exact(stopping.get(field), expected, f"stopping.{field}")

    _require_exact(
        payload.get("candidate_frequency"), EXPECTED_FREQUENCY, "candidate_frequency"
    )
    _require_exact(
        payload.get("economic_replication_gates_per_candidate"),
        EXPECTED_ECONOMIC_GATES,
        "economic_replication_gates_per_candidate",
    )

    multiplicity = payload.get("multiplicity_and_selection")
    if not isinstance(multiplicity, Mapping):
        raise V027PreregistrationError("Preinscripcion V0.27 sin multiplicidad")
    for field, expected in {
        "co_primary_candidates": 2,
        "family_one_sided_alpha": 0.05,
        "method": "bonferroni",
        "one_sided_alpha_per_candidate": 0.025,
        "one_sided_confidence_per_candidate": 0.975,
        "z_value": 1.959963984540054,
        "full_statistical_pass_requires_positive_adjusted_lcb": True,
        "select_at_most_one": True,
        "controls_selectable": False,
    }.items():
        _require_exact(multiplicity.get(field), expected, f"multiplicity.{field}")
    _require_exact(
        multiplicity.get("tie_break_if_both_pass"),
        [
            "higher_bonferroni_lcb",
            "lower_maximum_drawdown",
            "higher_trade_count",
        ],
        "multiplicity.tie_break_if_both_pass",
    )

    terminal = payload.get("terminal_audit")
    if not isinstance(terminal, Mapping) or not all(
        terminal.get(field) is True
        for field in (
            "completed_run_terminal_cancelled_feeds_are_expected",
            "requires_last_checkpoint_technical_passed",
            "requires_last_checkpoint_feeds_connected",
            "requires_all_non_feed_terminal_gates",
            "unexpected_disconnect_fails_closed",
        )
    ):
        raise V027PreregistrationError("Contrato terminal V0.27 incompatible")

    implementation = payload.get("implementation")
    _require_exact(
        implementation,
        {
            "runner_built": False,
            "auditor_built": False,
            "entrypoint_built": False,
            "launch_approved": False,
            "launch_status": "NOT_LAUNCHED",
        },
        "implementation",
    )
    safety = payload.get("safety")
    _require_exact(
        safety,
        {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
            "maximum_hours": 24,
        },
        "safety",
    )
    anti_overfit = payload.get("anti_overfit")
    if not isinstance(anti_overfit, Mapping):
        raise V027PreregistrationError("Preinscripcion V0.27 sin anti_overfit")
    for field, expected in {
        "candidate_thresholds_selected_after_v026b": True,
        "fresh_independent_replication_required": True,
        "two_candidates_use_familywise_correction": True,
        "no_candidate_promoted_from_closed_data": True,
        "parent_controls_non_selectable": True,
        "shortening_to_12h_forbidden": True,
        "new_backtest_hours": 0,
        "new_forward_maximum_hours": 24.0,
        "no_midrun_threshold_changes": True,
    }.items():
        _require_exact(anti_overfit.get(field), expected, f"anti_overfit.{field}")
    return dict(payload)


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
    if not isinstance(evidence, Mapping) or set(evidence) != set(EXPECTED_EVIDENCE_PATHS):
        raise V027PreregistrationError("Inventario de evidencia V0.27 incompatible")
    for key, relative_path in EXPECTED_EVIDENCE_PATHS.items():
        record = evidence[key]
        if not isinstance(record, Mapping):
            raise V027PreregistrationError(f"Evidencia V0.27 invalida: {key}")
        _require_exact(record.get("relative_path"), relative_path, f"evidence.{key}.path")
        _require_exact(
            sha256_file(root / relative_path),
            record.get("sha256"),
            f"evidence.{key}.sha256",
        )

    code_hashes = payload.get("design_code_hashes")
    if not isinstance(code_hashes, Mapping) or set(code_hashes) != set(EXPECTED_CODE_PATHS):
        raise V027PreregistrationError("Inventario de codigo V0.27 incompatible")
    for key, relative_path in EXPECTED_CODE_PATHS.items():
        _require_exact(
            sha256_file(root / relative_path),
            code_hashes.get(key),
            f"design_code_hashes.{key}",
        )
    return payload


__all__ = [
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "PREREG_VARIANT",
    "V027PreregistrationError",
    "load_and_verify_frozen_prereg",
    "validate_frozen_prereg_payload",
]
