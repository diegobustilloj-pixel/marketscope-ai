from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v034_contract import aggregate_probe_results
from polymarket_bot.v036_absolute_exit_design import evaluate_absolute_deadline_probe


PREREG_SCHEMA = "prereg_v036_absolute_exit_deadline_4h_1"
PREREG_STATUS = "FROZEN_FRESH_ABSOLUTE_EXIT_DEADLINE_AWAITING_IMPLEMENTATION_AND_LAUNCH_APPROVAL"
VARIANT = "V0.36_FRESH_ABSOLUTE_EXIT_DEADLINE_4H"
PASS_VERDICT = "PASS_FRESH_ABSOLUTE_EXIT_SAFETY_ONLY"


class V036ContractError(RuntimeError):
    pass


def _require(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise V036ContractError(f"V0.36 incompatible: {field}")


def load_and_verify_prereg(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V036ContractError("Preinscripcion V0.36 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    _require(payload.get("schema"), PREREG_SCHEMA, "schema")
    _require(payload.get("status"), PREREG_STATUS, "status")
    _require(payload.get("variant"), VARIANT, "variant")
    _require(
        payload.get("scope", {}).get("maximum_positive_result"),
        PASS_VERDICT,
        "scope.maximum_positive_result",
    )
    contract = payload.get("probe_contract", {})
    exact = {
        "probe_both_outcomes_independently": True,
        "decision_offset_min_inclusive": 30,
        "decision_offset_max_inclusive": 95,
        "decision_offset_count": 66,
        "decision_window_retention_vs_30_119": 0.73333333,
        "decision_latency_seconds": 1,
        "maximum_holding_seconds_after_entry": 30,
        "minimum_planned_holding_seconds_after_entry": 20,
        "absolute_exit_deadline_offset": 116,
        "scheduled_exit_rule": "MIN_ENTRY_PLUS_30_OR_ABSOLUTE_OFFSET_116",
        "full_holding_decision_offsets_inclusive": [30, 85],
        "tapered_holding_decision_offsets_inclusive": [86, 95],
        "exit_grace_seconds": 5,
        "earliest_observed_irreducible_exit_offset": 126,
        "required_margin_before_earliest_irreducible_exit_seconds": 10,
        "required_margin_formula": "freshness_uncertainty_plus_exit_grace",
        "position_shares": 5.0,
        "entry_depth_buffer_shares": 10.0,
        "decision_and_entry_require_v2_complete_and_all_four_sides_at_buffer": True,
        "absolute_guards_preserved": True,
        "relative_guard_scope": "SELECTED_BID_ONLY",
        "relative_guard_baseline": "IMMEDIATELY_PREVIOUS_ONE_SECOND_SNAPSHOT",
        "relative_drawdown_threshold_inclusive": 0.8,
        "relative_guard_requires_current_selected_bid_depth_at_position_size": True,
        "forced_exit_at_target_ignores_signal_chainlink_and_twap": True,
        "missing_target_exit_action": "RETRY_EACH_SECOND_UNTIL_TARGET_PLUS_GRACE_THEN_CLASSIFY_TRAPPED",
        "synthetic_complement_allowed": False,
        "partial_entry_allowed": False,
        "partial_exit_allowed": False,
        "overlapping_probes_are_capacity_diagnostics_not_trades": True,
    }
    _require(contract, exact, "probe_contract")
    _require(payload.get("stopping", {}).get("maximum_hours"), 4.0, "stopping.maximum_hours")
    _require(payload.get("stopping", {}).get("scheduled_supervision"), False, "stopping.scheduled_supervision")
    _require(payload.get("stopping", {}).get("automatic_final_audit"), True, "stopping.automatic_final_audit")
    _require(payload.get("duration_rationale", {}).get("expected_capacity_probes"), 6336, "duration.expected_capacity_probes")
    _require(payload.get("data_policy", {}).get("outcomes_read"), 0, "data_policy.outcomes_read")
    _require(payload.get("data_policy", {}).get("prices_stored"), False, "data_policy.prices_stored")
    _require(payload.get("data_policy", {}).get("pnl_calculated"), False, "data_policy.pnl_calculated")
    _require(payload.get("safety", {}).get("orders_enabled"), False, "safety.orders_enabled")
    _require(payload.get("safety", {}).get("paper_orders_enabled"), False, "safety.paper_orders_enabled")
    _require(payload.get("safety", {}).get("wallet_required"), False, "safety.wallet_required")
    _require(payload.get("safety", {}).get("real_money"), "BLOQUEADO", "safety.real_money")
    for key, record in payload.get("evidence", {}).items():
        relative = str(record.get("relative_path") or "")
        _require(record.get("sha256"), sha256_file(root / relative), f"evidence.{key}.sha256")
    design = payload.get("design_code", {})
    _require(
        design.get("sha256"),
        sha256_file(root / str(design.get("relative_path"))),
        "design_code.sha256",
    )
    diagnostic = json.loads(
        (root / payload["evidence"]["absolute_exit_deadline_diagnostic"]["relative_path"]).read_text(
            encoding="utf-8"
        )
    )
    _require(
        diagnostic.get("decision"),
        "PREPARE_ONE_FRESH_V036_ABSOLUTE_EXIT_DEADLINE_REPLICATION",
        "diagnostic.decision",
    )
    _require(diagnostic.get("closed_evidence", {}).get("all_gates_passed"), True, "diagnostic.gates")
    _require(diagnostic.get("derivation", {}).get("absolute_exit_deadline"), 116, "diagnostic.deadline")
    return dict(payload)


def evaluate_probe(
    snapshots: Mapping[int, Mapping[str, Any]],
    *, decision_offset: int, outcome: str, contract: Mapping[str, Any]
) -> dict[str, Any]:
    return evaluate_absolute_deadline_probe(
        snapshots,
        decision_offset=decision_offset,
        outcome=outcome,
        absolute_exit_deadline=int(contract["absolute_exit_deadline_offset"]),
        maximum_holding_seconds=int(contract["maximum_holding_seconds_after_entry"]),
        drawdown_threshold=float(contract["relative_drawdown_threshold_inclusive"]),
    )


def evaluate_market_probes(
    snapshots: Mapping[int, Mapping[str, Any]],
    *, condition_id: str, slug: str, contract: Mapping[str, Any]
) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    for offset in range(
        int(contract["decision_offset_min_inclusive"]),
        int(contract["decision_offset_max_inclusive"]) + 1,
    ):
        for outcome in ("Up", "Down"):
            probes.append(
                {
                    **evaluate_probe(
                        snapshots,
                        decision_offset=offset,
                        outcome=outcome,
                        contract=contract,
                    ),
                    "condition_id": condition_id,
                    "slug": slug,
                    "outcome": outcome,
                    "decision_offset": offset,
                }
            )
    return probes


__all__ = [
    "PASS_VERDICT",
    "VARIANT",
    "V036ContractError",
    "aggregate_probe_results",
    "evaluate_market_probes",
    "evaluate_probe",
    "load_and_verify_prereg",
]
