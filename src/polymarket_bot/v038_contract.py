from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v034_contract import aggregate_probe_results
from polymarket_bot.v038_recovery_budget_design import evaluate_recovery_budget_probe


PREREG_SCHEMA = "prereg_v038_single_layer_recovery_4h_1"
PREREG_STATUS = "FROZEN_SINGLE_LAYER_RECOVERY_AWAITING_IMPLEMENTATION_AND_LAUNCH_APPROVAL"
VARIANT = "V0.38_FRESH_SINGLE_LAYER_RECOVERY_4H"
PASS_VERDICT = "PASS_FRESH_SINGLE_LAYER_RECOVERY_EXIT_SAFETY_ONLY"


class V038ContractError(RuntimeError):
    pass


def _require(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise V038ContractError(f"V0.38 incompatible: {field}")


def load_and_verify_prereg(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V038ContractError("Preinscripcion V0.38 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    _require(payload.get("schema"), PREREG_SCHEMA, "schema")
    _require(payload.get("status"), PREREG_STATUS, "status")
    _require(payload.get("variant"), VARIANT, "variant")
    _require(
        payload.get("scope", {}).get("maximum_positive_result"),
        PASS_VERDICT,
        "scope.maximum_positive_result",
    )
    _require(
        payload.get("transport_contract"),
        {
            "enabled": True,
            "scope": "BOTH_CLOB_BOOKS",
            "reconnect_owner_count": 1,
            "socket_session_has_internal_reconnect": False,
            "supervisor_owns_connect_receive_staleness_and_reconnect": True,
            "age_source": "OLDEST_UP_OR_DOWN_CLOB_SOURCE_TIMESTAMP",
            "stale_timeout_ms": 3000,
            "poll_ms": 250,
            "open_timeout_ms": 3000,
            "close_timeout_ms": 250,
            "heartbeat_ms": 10000,
            "reconnect_backoff_schedule_ms": [250, 500, 1000],
            "reconnect_backoff_cap_ms": 1000,
            "recovery_service_level_ms": 10000,
            "connected_label_alone_is_sufficient": False,
            "fresh_generation_requires_both_books_updated_after_generation_start": True,
            "record_generation_trigger_and_recovery_events": True,
            "stale_depth_can_count_as_exit": False,
            "unrecovered_or_slow_relevant_incident_action": "FAIL_CLOSED",
        },
        "transport_contract",
    )
    _require(
        payload.get("probe_contract"),
        {
            "probe_both_outcomes_independently": True,
            "decision_offset_min_inclusive": 30,
            "decision_offset_max_inclusive": 90,
            "decision_offset_count": 61,
            "decision_window_retention_vs_30_119": 0.67777778,
            "decision_latency_seconds": 1,
            "maximum_holding_seconds_after_entry": 30,
            "minimum_planned_holding_seconds_after_entry": 20,
            "absolute_exit_target_deadline_offset": 111,
            "scheduled_exit_rule": "MIN_ENTRY_PLUS_30_OR_ABSOLUTE_TARGET_OFFSET_111",
            "full_holding_decision_offsets_inclusive": [30, 80],
            "tapered_holding_decision_offsets_inclusive": [81, 90],
            "operational_retry_budget_seconds": 10,
            "operational_retry_budget_formula": "V037_EXIT_GRACE_5_PLUS_ALREADY_RESERVED_RECOVERY_5",
            "absolute_retry_deadline_offset": 121,
            "retry_deadline_rule": "MIN_TARGET_PLUS_10_OR_ABSOLUTE_RETRY_OFFSET_121",
            "earliest_observed_irreducible_exit_offset": 126,
            "required_freshness_reserve_after_retry_deadline_seconds": 5,
            "position_shares": 5.0,
            "entry_depth_buffer_shares": 10.0,
            "decision_and_entry_require_v2_complete_and_all_four_sides_at_buffer": True,
            "absolute_guards_preserved": True,
            "relative_guard_scope": "SELECTED_BID_ONLY",
            "relative_guard_baseline": "IMMEDIATELY_PREVIOUS_ONE_SECOND_SNAPSHOT",
            "relative_drawdown_threshold_inclusive": 0.8,
            "relative_guard_requires_current_selected_bid_depth_at_position_size": True,
            "forced_exit_at_target_ignores_signal_chainlink_and_twap": True,
            "missing_target_exit_action": "RETRY_EACH_SECOND_UNTIL_OPERATIONAL_RETRY_DEADLINE_THEN_CLASSIFY_TRAPPED",
            "synthetic_complement_allowed": False,
            "partial_entry_allowed": False,
            "partial_exit_allowed": False,
            "overlapping_probes_are_capacity_diagnostics_not_trades": True,
        },
        "probe_contract",
    )
    _require(payload.get("stopping", {}).get("maximum_hours"), 4.0, "stopping.maximum_hours")
    _require(payload.get("stopping", {}).get("scheduled_supervision"), False, "stopping.scheduled_supervision")
    _require(payload.get("stopping", {}).get("automatic_final_audit"), True, "stopping.automatic_final_audit")
    _require(payload.get("duration_rationale", {}).get("expected_capacity_probes"), 5856, "duration.expected_capacity_probes")
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
        (root / payload["evidence"]["single_layer_recovery_diagnostic"]["relative_path"]).read_text(
            encoding="utf-8"
        )
    )
    _require(
        diagnostic.get("decision"),
        "PREPARE_ONE_FRESH_V038_SINGLE_LAYER_RECOVERY_REPLICATION",
        "diagnostic.decision",
    )
    _require(diagnostic.get("all_design_gates_passed"), True, "diagnostic.gates")
    _require(diagnostic.get("derivation", {}).get("operational_retry_budget_seconds"), 10, "diagnostic.retry_budget")
    _require(diagnostic.get("single_layer_transport", {}).get("reconnect_owner_count"), 1, "diagnostic.reconnect_owner")
    return dict(payload)


def evaluate_probe(
    snapshots: Mapping[int, Mapping[str, Any]],
    *,
    decision_offset: int,
    outcome: str,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    return evaluate_recovery_budget_probe(
        snapshots,
        decision_offset=decision_offset,
        outcome=outcome,
        absolute_exit_deadline=int(contract["absolute_exit_target_deadline_offset"]),
        maximum_holding_seconds=int(contract["maximum_holding_seconds_after_entry"]),
        retry_budget_seconds=int(contract["operational_retry_budget_seconds"]),
        absolute_retry_deadline=int(contract["absolute_retry_deadline_offset"]),
        drawdown_threshold=float(contract["relative_drawdown_threshold_inclusive"]),
        position_shares=float(contract["position_shares"]),
        entry_depth_buffer_shares=float(contract["entry_depth_buffer_shares"]),
    )


def evaluate_market_probes(
    snapshots: Mapping[int, Mapping[str, Any]],
    *,
    condition_id: str,
    slug: str,
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    return [
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
        for offset in range(
            int(contract["decision_offset_min_inclusive"]),
            int(contract["decision_offset_max_inclusive"]) + 1,
        )
        for outcome in ("Up", "Down")
    ]


__all__ = [
    "PASS_VERDICT",
    "VARIANT",
    "V038ContractError",
    "aggregate_probe_results",
    "evaluate_market_probes",
    "evaluate_probe",
    "load_and_verify_prereg",
]
