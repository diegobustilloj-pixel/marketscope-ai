from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import median
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v034_guard_design import evaluate_relative_guard_probe


PREREG_SCHEMA = "prereg_v034_selected_bid_guard_4h_1"
PREREG_STATUS = "FROZEN_FRESH_SELECTED_BID_GUARD_AWAITING_IMPLEMENTATION_AND_LAUNCH_APPROVAL"
VARIANT = "V0.34_FRESH_SELECTED_BID_DRAWDOWN_GUARD_4H"
PASS_VERDICT = "PASS_FRESH_SELECTED_BID_EXIT_SAFETY_ONLY"
EXPECTED_EVIDENCE = {
    "v033_result": "data/resultado_v033_fresh_exit_safety_4h.json",
    "v033_database": "data/capture_v033_fresh_exit_safety_4h.db",
    "broad_guard_diagnostic": "data/diagnostico_v034_relative_depth_guard.json",
    "selected_bid_guard_diagnostic": "data/diagnostico_v034_selected_bid_drawdown_guard.json",
}
EXPECTED_DESIGN_CODE = {
    "broad_guard": "src/polymarket_bot/v034_guard_design.py",
    "selected_bid_guard": "src/polymarket_bot/v034_selected_bid_design.py",
}


class V034ContractError(RuntimeError):
    pass


def _require(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise V034ContractError(f"V0.34 incompatible: {field}")


def load_and_verify_prereg(
    path: str | Path,
    *,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(path).resolve()
    if not prereg_file.is_file():
        raise V034ContractError("Preinscripcion V0.34 no encontrada")
    payload = json.loads(prereg_file.read_text(encoding="utf-8"))
    _require(payload.get("schema"), PREREG_SCHEMA, "schema")
    _require(payload.get("status"), PREREG_STATUS, "status")
    _require(payload.get("variant"), VARIANT, "variant")
    _require(payload.get("scope", {}).get("maximum_positive_result"), PASS_VERDICT, "scope.maximum_positive_result")
    _require(payload.get("scope", {}).get("economic_strategy_present"), False, "scope.economic_strategy_present")
    contract = payload.get("probe_contract", {})
    exact_probe = {
        "probe_both_outcomes_independently": True,
        "decision_offset_min_inclusive": 30,
        "decision_offset_max_inclusive": 119,
        "decision_latency_seconds": 1,
        "holding_seconds_after_entry": 30,
        "exit_grace_seconds": 5,
        "position_shares": 5.0,
        "entry_depth_buffer_shares": 10.0,
        "decision_requires_v2_complete_data": True,
        "decision_requires_all_four_book_sides_at_buffer": True,
        "entry_requires_v2_complete_data": True,
        "entry_requires_all_four_book_sides_at_buffer": True,
        "absolute_selected_bid_buffer_guard_preserved": True,
        "absolute_any_book_side_position_floor_guard_preserved": True,
        "relative_guard_scope": "SELECTED_BID_ONLY",
        "relative_guard_baseline": "IMMEDIATELY_PREVIOUS_ONE_SECOND_SNAPSHOT",
        "relative_baseline_minimum_depth_shares": 10.0,
        "relative_drawdown_threshold_inclusive": 0.8,
        "relative_guard_requires_current_selected_bid_depth_at_position_size": True,
        "forced_exit_at_target_ignores_signal_chainlink_and_twap": True,
        "missing_target_exit_action": "RETRY_EACH_SECOND_UNTIL_TARGET_PLUS_GRACE_THEN_CLASSIFY_TRAPPED",
        "synthetic_complement_allowed": False,
        "partial_entry_allowed": False,
        "partial_exit_allowed": False,
        "overlapping_probes_are_capacity_diagnostics_not_trades": True,
        "prices_costs_fees_slippage_and_pnl_are_out_of_scope": True,
    }
    _require(contract, exact_probe, "probe_contract")
    _require(payload.get("stopping", {}).get("maximum_hours"), 4.0, "stopping.maximum_hours")
    _require(payload.get("stopping", {}).get("scheduled_supervision"), False, "stopping.scheduled_supervision")
    _require(payload.get("stopping", {}).get("automatic_final_audit"), True, "stopping.automatic_final_audit")
    _require(payload.get("data_policy", {}).get("outcomes_read"), 0, "data_policy.outcomes_read")
    _require(payload.get("data_policy", {}).get("prices_stored"), False, "data_policy.prices_stored")
    _require(payload.get("data_policy", {}).get("pnl_calculated"), False, "data_policy.pnl_calculated")
    _require(payload.get("safety", {}).get("orders_enabled"), False, "safety.orders_enabled")
    _require(payload.get("safety", {}).get("paper_orders_enabled"), False, "safety.paper_orders_enabled")
    _require(payload.get("safety", {}).get("wallet_required"), False, "safety.wallet_required")
    _require(payload.get("safety", {}).get("real_money"), "BLOQUEADO", "safety.real_money")

    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(EXPECTED_EVIDENCE):
        raise V034ContractError("Inventario de evidencia V0.34 incompatible")
    for key, relative in EXPECTED_EVIDENCE.items():
        record = evidence.get(key)
        if not isinstance(record, Mapping):
            raise V034ContractError(f"Evidencia V0.34 invalida: {key}")
        _require(record.get("relative_path"), relative, f"evidence.{key}.relative_path")
        _require(record.get("sha256"), sha256_file(root / relative), f"evidence.{key}.sha256")
    design = payload.get("design_code_hashes")
    if not isinstance(design, Mapping) or set(design) != set(EXPECTED_DESIGN_CODE):
        raise V034ContractError("Inventario de codigo de diseno V0.34 incompatible")
    for key, relative in EXPECTED_DESIGN_CODE.items():
        record = design.get(key)
        if not isinstance(record, Mapping):
            raise V034ContractError(f"Codigo de diseno V0.34 invalido: {key}")
        _require(record.get("relative_path"), relative, f"design_code_hashes.{key}.relative_path")
        _require(record.get("sha256"), sha256_file(root / relative), f"design_code_hashes.{key}.sha256")
    selected = json.loads((root / EXPECTED_EVIDENCE["selected_bid_guard_diagnostic"]).read_text(encoding="utf-8"))
    _require(selected.get("decision"), "PREPARE_ONE_FRESH_V034_SELECTED_BID_REPLICATION", "selected_design.decision")
    _require(selected.get("selected_threshold"), 0.8, "selected_design.selected_threshold")
    return dict(payload)


def evaluate_probe(
    snapshots_by_offset: Mapping[int, Mapping[str, Any]],
    *,
    decision_offset: int,
    outcome: str,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    return evaluate_relative_guard_probe(
        snapshots_by_offset,
        decision_offset=decision_offset,
        outcome=outcome,
        drawdown_threshold=float(contract["relative_drawdown_threshold_inclusive"]),
        position_shares=float(contract["position_shares"]),
        entry_depth_buffer_shares=float(contract["entry_depth_buffer_shares"]),
        decision_latency_seconds=int(contract["decision_latency_seconds"]),
        holding_seconds_after_entry=int(contract["holding_seconds_after_entry"]),
        exit_grace_seconds=int(contract["exit_grace_seconds"]),
        relative_guard_scope=str(contract["relative_guard_scope"]),
    )


def evaluate_market_probes(
    snapshots_by_offset: Mapping[int, Mapping[str, Any]],
    *,
    condition_id: str,
    slug: str,
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    for decision_offset in range(
        int(contract["decision_offset_min_inclusive"]),
        int(contract["decision_offset_max_inclusive"]) + 1,
    ):
        for outcome in ("Up", "Down"):
            probes.append(
                {
                    **evaluate_probe(
                        snapshots_by_offset,
                        decision_offset=decision_offset,
                        outcome=outcome,
                        contract=contract,
                    ),
                    "condition_id": condition_id,
                    "slug": slug,
                    "outcome": outcome,
                    "decision_offset": decision_offset,
                }
            )
    return probes


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 8) if denominator else 0.0


def aggregate_probe_results(probes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    decision_rejections = Counter(
        str(probe.get("reason")) for probe in probes if probe.get("status") == "DECISION_REJECTED"
    )
    entry_rejections = Counter(
        str(probe.get("reason")) for probe in probes if probe.get("status") == "ENTRY_REJECTED"
    )
    entered = [probe for probe in probes if probe.get("status") in {"EXITED", "TRAPPED"}]
    exited = [probe for probe in entered if probe.get("status") == "EXITED"]
    trapped = [probe for probe in entered if probe.get("status") == "TRAPPED"]
    kinds = Counter(str(probe.get("exit_kind")) for probe in exited)
    holding = sorted(int(probe["holding_seconds"]) for probe in exited)
    delays = Counter(int(probe["exit_delay_seconds"]) for probe in exited)
    reasons = Counter(reason for probe in exited for reason in probe.get("guard_reasons", []))
    relative_guards = sum(
        1
        for probe in exited
        if any(str(reason).startswith("RELATIVE_DRAWDOWN:") for reason in probe.get("guard_reasons", []))
    )
    nonnegative_delays = [delay for delay in delays if delay >= 0]
    return {
        "total_capacity_probes": len(probes),
        "decision_eligible_probes": len(probes) - sum(decision_rejections.values()),
        "entry_eligible_probes": len(entered),
        "exit_successes_within_grace": len(exited),
        "exit_success_within_grace_rate": _rate(len(exited), len(entered)),
        "scheduled_exits": kinds.get("SCHEDULED", 0),
        "scheduled_exit_fraction": _rate(kinds.get("SCHEDULED", 0), len(entered)),
        "proactive_guard_exits": kinds.get("PROACTIVE_GUARD", 0),
        "relative_guard_exits": relative_guards,
        "grace_retry_exits": kinds.get("GRACE_RETRY", 0),
        "trapped_positions": len(trapped),
        "median_holding_seconds": float(median(holding)) if holding else 0.0,
        "minimum_holding_seconds": min(holding, default=0),
        "maximum_observed_exit_delay_seconds": max(nonnegative_delays, default=0),
        "decision_rejections_by_reason": dict(sorted(decision_rejections.items())),
        "entry_rejections_by_reason": dict(sorted(entry_rejections.items())),
        "exit_kind_distribution": dict(sorted(kinds.items())),
        "guard_reason_distribution": dict(sorted(reasons.items())),
        "exit_delay_distribution": {str(key): value for key, value in sorted(delays.items())},
    }


__all__ = [
    "PASS_VERDICT",
    "VARIANT",
    "V034ContractError",
    "aggregate_probe_results",
    "evaluate_market_probes",
    "evaluate_probe",
    "load_and_verify_prereg",
]
