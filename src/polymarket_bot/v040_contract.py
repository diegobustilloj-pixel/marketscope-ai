from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v034_contract import aggregate_probe_results
from polymarket_bot.v040_transport_guard_design import (
    TRANSPORT_GUARD_AGE_MS,
    TRANSPORT_GUARD_HORIZON_SECONDS,
    TRANSPORT_STALE_TIMEOUT_MS,
    evaluate_market_probes,
    evaluate_transport_guard_probe,
    summarize_transport_guard,
)


PREREG_SCHEMA = "prereg_v040_pre_stale_transport_guard_4h_1"
PREREG_STATUS = (
    "FROZEN_PRE_STALE_TRANSPORT_GUARD_AWAITING_IMPLEMENTATION_AND_LAUNCH_APPROVAL"
)
VARIANT = "V0.40_FRESH_PRE_STALE_TRANSPORT_GUARD_4H"
PASS_VERDICT = "PASS_FRESH_PRE_STALE_TRANSPORT_GUARD_EXIT_SAFETY_ONLY"
NEW_PROBE_FIELDS = {
    "transport_guard_rule": (
        "EXIT_WHEN_WITHIN_FIVE_SECONDS_OF_TARGET_AND_OLDEST_CLOB_BOOK_AGE_IS_"
        "AT_LEAST_2000_MS_BUT_BELOW_3000_MS"
    ),
    "transport_guard_age_ms": TRANSPORT_GUARD_AGE_MS,
    "transport_stale_timeout_ms": TRANSPORT_STALE_TIMEOUT_MS,
    "transport_guard_horizon_seconds": TRANSPORT_GUARD_HORIZON_SECONDS,
    "transport_guard_requires_both_books_fresh": True,
    "transport_guard_requires_selected_bid_depth_at_position_size": True,
    "transport_guard_stale_depth_allowed": False,
}


class V040ContractError(RuntimeError):
    pass


def _require(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise V040ContractError(f"V0.40 incompatible: {field}")


def load_and_verify_prereg(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V040ContractError("Preinscripcion V0.40 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    _require(payload.get("schema"), PREREG_SCHEMA, "schema")
    _require(payload.get("status"), PREREG_STATUS, "status")
    _require(payload.get("variant"), VARIANT, "variant")
    _require(
        payload.get("scope", {}).get("maximum_positive_result"),
        PASS_VERDICT,
        "scope.maximum_positive_result",
    )
    previous_path = root / payload["evidence"]["v039_preregistration"]["relative_path"]
    previous = json.loads(previous_path.read_text(encoding="utf-8"))
    _require(payload.get("capture_contract"), previous.get("capture_contract"), "capture_contract")
    _require(payload.get("transport_contract"), previous.get("transport_contract"), "transport_contract")
    current_probe = dict(payload.get("probe_contract", {}))
    for key, value in NEW_PROBE_FIELDS.items():
        _require(current_probe.pop(key, None), value, f"probe_contract.{key}")
    _require(current_probe, previous.get("probe_contract"), "probe_contract.v039_base")
    gates = payload.get("technical_gates", {})
    _require(gates.get("minimum_transport_pre_stale_guard_exit_observations"), 1, "gates.guard_exercise")
    _require(gates.get("maximum_transport_guard_book_age_ms"), 2999, "gates.guard_age")
    _require(gates.get("maximum_transport_guard_seconds_before_target"), 5, "gates.guard_horizon")
    _require(gates.get("maximum_trapped_positions"), 0, "gates.trapped")
    _require(gates.get("required_exit_success_within_retry_rate"), 1.0, "gates.exit_rate")
    _require(gates.get("maximum_relevant_transport_recovery_ms"), 15000, "gates.recovery_envelope")
    _require(
        gates.get("transport_resilience_envelope_formula"),
        "OPERATIONAL_RETRY_BUDGET_10S_PLUS_TRANSPORT_GUARD_HORIZON_5S",
        "gates.recovery_formula",
    )
    _require(payload.get("stopping", {}).get("maximum_hours"), 4.0, "stopping.maximum_hours")
    _require(payload.get("stopping", {}).get("scheduled_supervision"), False, "stopping.scheduled_supervision")
    _require(payload.get("stopping", {}).get("automatic_final_audit"), True, "stopping.automatic_final_audit")
    _require(payload.get("duration_rationale", {}).get("expected_capacity_probes"), 5856, "duration.expected_capacity_probes")
    _require(payload.get("duration_rationale", {}).get("closed_replay_markets"), 81, "duration.closed_replay_markets")
    _require(payload.get("data_policy", {}).get("outcomes_read"), 0, "data_policy.outcomes_read")
    _require(payload.get("data_policy", {}).get("prices_stored"), False, "data_policy.prices_stored")
    _require(payload.get("data_policy", {}).get("pnl_calculated"), False, "data_policy.pnl_calculated")
    _require(payload.get("data_policy", {}).get("runtime_peer_ip_stored"), False, "data_policy.runtime_peer_ip_stored")
    _require(payload.get("safety", {}).get("orders_enabled"), False, "safety.orders_enabled")
    _require(payload.get("safety", {}).get("paper_orders_enabled"), False, "safety.paper_orders_enabled")
    _require(payload.get("safety", {}).get("wallet_required"), False, "safety.wallet_required")
    _require(payload.get("safety", {}).get("real_money"), "BLOQUEADO", "safety.real_money")
    _require(
        payload.get("limitations", {}).get(
            "actual_order_submission_during_total_route_loss_guaranteed"
        ),
        False,
        "limitations.order_submission_guarantee",
    )
    _require(
        payload.get("limitations", {}).get(
            "redundant_network_or_remote_executor_required_before_real_money"
        ),
        True,
        "limitations.redundancy",
    )
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
        (
            root
            / payload["evidence"]["pre_stale_transport_guard_diagnostic"][
                "relative_path"
            ]
        ).read_text(encoding="utf-8")
    )
    _require(
        diagnostic.get("decision"),
        "PREPARE_ONE_FRESH_V040_PRE_STALE_TRANSPORT_GUARD_REPLICATION",
        "diagnostic.decision",
    )
    _require(diagnostic.get("all_design_gates_passed"), True, "diagnostic.gates")
    _require(
        diagnostic.get("closed_replay", {}).get("combined", {}).get(
            "trapped_positions"
        ),
        0,
        "diagnostic.trapped",
    )
    _require(
        diagnostic.get("closed_replay", {}).get("combined", {}).get(
            "exit_success_within_grace_rate"
        ),
        1.0,
        "diagnostic.exit_rate",
    )
    return dict(payload)


def evaluate_probe(
    snapshots: dict[int, dict[str, Any]],
    *,
    decision_offset: int,
    outcome: str,
    contract: dict[str, Any],
) -> dict[str, Any]:
    return evaluate_transport_guard_probe(
        snapshots,
        decision_offset=decision_offset,
        outcome=outcome,
        contract=contract,
    )


__all__ = [
    "PASS_VERDICT",
    "VARIANT",
    "V040ContractError",
    "aggregate_probe_results",
    "evaluate_market_probes",
    "evaluate_probe",
    "load_and_verify_prereg",
    "summarize_transport_guard",
]
