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
from polymarket_bot.v041_chainlink_watchdog_design import (
    CHAINLINK_SYMBOL,
    CHAINLINK_TOPIC,
    CHAINLINK_WATCHDOG_SECONDS,
)


PREREG_SCHEMA = "prereg_v041_chainlink_silence_watchdog_4h_1"
PREREG_STATUS = (
    "FROZEN_CHAINLINK_SILENCE_WATCHDOG_AWAITING_IMPLEMENTATION_AND_LAUNCH_APPROVAL"
)
VARIANT = "V0.41_CHAINLINK_SILENCE_WATCHDOG_4H"
PASS_VERDICT = "PASS_CHAINLINK_LIVENESS_AND_V040_EXIT_SAFETY_ONLY"


class V041ContractError(RuntimeError):
    pass


def _require(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise V041ContractError(f"V0.41 incompatible: {field}")


def load_and_verify_prereg(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V041ContractError("Preinscripcion V0.41 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    _require(payload.get("schema"), PREREG_SCHEMA, "schema")
    _require(payload.get("status"), PREREG_STATUS, "status")
    _require(payload.get("variant"), VARIANT, "variant")
    _require(
        payload.get("scope", {}).get("maximum_positive_result"),
        PASS_VERDICT,
        "scope.maximum_positive_result",
    )
    previous_path = root / payload["evidence"]["v040_preregistration"]["relative_path"]
    previous = json.loads(previous_path.read_text(encoding="utf-8"))
    _require(payload.get("capture_contract"), previous.get("capture_contract"), "capture_contract")
    _require(payload.get("transport_contract"), previous.get("transport_contract"), "transport_contract")
    _require(payload.get("probe_contract"), previous.get("probe_contract"), "probe_contract")
    chainlink = payload.get("chainlink_feed_contract", {})
    _require(chainlink.get("topic"), CHAINLINK_TOPIC, "chainlink.topic")
    _require(chainlink.get("symbol"), CHAINLINK_SYMBOL, "chainlink.symbol")
    _require(
        chainlink.get("silence_watchdog_seconds"),
        CHAINLINK_WATCHDOG_SECONDS,
        "chainlink.silence_watchdog_seconds",
    )
    _require(
        chainlink.get("silent_socket_action"),
        "CLOSE_AND_RESUBSCRIBE",
        "chainlink.silent_socket_action",
    )
    _require(
        chainlink.get("valid_update_resets_watchdog_only_after_strict_predicate"),
        True,
        "chainlink.strict_predicate",
    )
    _require(
        chainlink.get("stale_chainlink_can_enter"),
        False,
        "chainlink.stale_entry",
    )
    gates = payload.get("technical_gates", {})
    _require(gates.get("minimum_transport_pre_stale_guard_exit_observations"), 1, "gates.guard_exercise")
    _require(gates.get("maximum_transport_guard_book_age_ms"), 2999, "gates.guard_age")
    _require(gates.get("maximum_transport_guard_seconds_before_target"), 5, "gates.guard_horizon")
    _require(gates.get("maximum_trapped_positions"), 0, "gates.trapped")
    _require(gates.get("required_exit_success_within_retry_rate"), 1.0, "gates.exit_rate")
    _require(gates.get("maximum_relevant_transport_recovery_ms"), 15000, "gates.recovery_envelope")
    _require(gates.get("minimum_chainlink_snapshot_coverage"), 0.95, "gates.chainlink_coverage")
    _require(gates.get("maximum_chainlink_stale_streak_seconds"), 20, "gates.chainlink_stale_streak")
    _require(gates.get("minimum_chainlink_valid_updates"), 1, "gates.chainlink_updates")
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
    _require(payload.get("duration_rationale", {}).get("v040_fresh_markets"), 48, "duration.v040_markets")
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
            root / payload["evidence"]["chainlink_silence_diagnostic"]["relative_path"]
        ).read_text(encoding="utf-8")
    )
    _require(
        diagnostic.get("decision"),
        "PREPARE_ONE_FRESH_V041_CHAINLINK_SILENCE_WATCHDOG_REPLICATION",
        "diagnostic.decision",
    )
    _require(diagnostic.get("all_design_gates_passed"), True, "diagnostic.gates")
    _require(
        diagnostic.get("v040_evidence", {}).get("chainlink_not_fresh_snapshots"),
        4397,
        "diagnostic.chainlink_not_fresh",
    )
    _require(
        diagnostic.get("v041_design", {}).get("watchdog_seconds"),
        CHAINLINK_WATCHDOG_SECONDS,
        "diagnostic.watchdog_seconds",
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
    "V041ContractError",
    "aggregate_probe_results",
    "evaluate_market_probes",
    "evaluate_probe",
    "load_and_verify_prereg",
    "summarize_transport_guard",
]
