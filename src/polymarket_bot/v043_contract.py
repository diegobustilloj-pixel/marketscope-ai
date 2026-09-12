from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v043_chainlink_outage_design import DECISION


PREREG_SCHEMA = "prereg_v043_chainlink_outage_fail_closed_audit_1"
PREREG_STATUS = "FROZEN_CLOSED_INCIDENT_AUDIT_AWAITING_IMPLEMENTATION"
VARIANT = "V0.43_CHAINLINK_OUTAGE_FAIL_CLOSED_AUDIT"
PASS_VERDICT = "PASS_BOT_CONTROLLED_CHAINLINK_OUTAGE_SAFETY_ONLY"


class V043ContractError(RuntimeError):
    pass


def _require(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise V043ContractError(f"V0.43 incompatible: {field}")


def load_and_verify_prereg(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V043ContractError("Preinscripcion V0.43 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    _require(payload.get("schema"), PREREG_SCHEMA, "schema")
    _require(payload.get("status"), PREREG_STATUS, "status")
    _require(payload.get("variant"), VARIANT, "variant")
    scope = payload.get("scope", {})
    _require(scope.get("maximum_positive_result"), PASS_VERDICT, "scope.pass")
    _require(scope.get("fresh_forward_capture"), False, "scope.fresh_capture")
    _require(scope.get("closed_incident_audit"), True, "scope.closed_audit")
    _require(
        scope.get("v042_official_verdict_can_be_overwritten"),
        False,
        "scope.v042_immutable",
    )
    _require(
        scope.get("provider_liveness_can_be_claimed"),
        False,
        "scope.provider_liveness",
    )
    audit = payload.get("audit_contract", {})
    _require(
        audit.get("input_variant"),
        "V0.42_PLANNED_CHAINLINK_RECONNECT_4H",
        "audit.input_variant",
    )
    _require(
        audit.get("input_completion_reason"),
        "FULL_4H_REACHED",
        "audit.completion",
    )
    _require(audit.get("input_expected_markets"), 48, "audit.markets")
    _require(
        audit.get("input_official_verdict"),
        "FAIL_CHAINLINK_LIVENESS",
        "audit.v042_verdict",
    )
    _require(
        audit.get("required_exact_failed_v042_gates"),
        ["maximum_chainlink_stale_streak_seconds_passed"],
        "audit.failed_gates",
    )
    _require(
        audit.get("provider_liveness_gate_remains_failed"),
        True,
        "audit.provider_gate",
    )
    _require(
        audit.get("entry_requires_fresh_chainlink_at_decision_and_entry"),
        True,
        "audit.entry_fail_closed",
    )
    _require(
        audit.get("exit_must_not_require_chainlink_or_twap"),
        True,
        "audit.exit_independence",
    )
    gates = payload.get("technical_gates", {})
    expected_gates = {
        "minimum_completed_markets": 48,
        "minimum_longest_chainlink_stale_streak_seconds": 21,
        "required_watchdog_reconnect_accounting_rate": 1.0,
        "maximum_capacity_entries_using_stale_chainlink": 0,
        "minimum_capacity_probes_blocked_by_stale_chainlink": 1,
        "minimum_capacity_positions_open_during_stale_chainlink": 1,
        "required_capacity_exit_success_rate_during_stale_chainlink": 1.0,
        "minimum_exits_completed_while_chainlink_stale": 1,
        "maximum_trapped_positions": 0,
        "maximum_exit_delay_seconds": 10,
        "required_v042_safety_passed": True,
        "required_source_artifacts_unchanged": True,
        "required_design_code_unchanged": True,
    }
    _require(gates, expected_gates, "technical_gates")
    policy = payload.get("classification_policy", {})
    _require(
        policy.get("v042_provider_liveness_result"),
        "FAILED_AND_PRESERVED",
        "policy.provider_result",
    )
    _require(
        policy.get("reference_provider_outage_is_not_solved_by_relaxing_a_threshold"),
        True,
        "policy.no_relaxation",
    )
    _require(
        policy.get("repeat_same_four_hour_capture_if_audit_passes"),
        False,
        "policy.no_repeat",
    )
    _require(
        payload.get("execution", {}).get("fresh_capture_hours"),
        0.0,
        "execution.hours",
    )
    _require(
        payload.get("execution", {}).get("scheduled_supervision"),
        False,
        "execution.supervision",
    )
    data_policy = payload.get("data_policy", {})
    for key, expected in {
        "outcomes_read": 0,
        "labels_read": 0,
        "prices_stored": False,
        "pnl_calculated": False,
        "signals_generated": False,
        "trades_generated": False,
        "orders_created": 0,
    }.items():
        _require(data_policy.get(key), expected, f"data_policy.{key}")
    safety = payload.get("safety", {})
    for key, expected in {
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }.items():
        _require(safety.get(key), expected, f"safety.{key}")
    for key, record in payload.get("evidence", {}).items():
        relative = str(record.get("relative_path") or "")
        _require(
            record.get("sha256"),
            sha256_file(root / relative),
            f"evidence.{key}.sha256",
        )
    design = payload.get("design_code", {})
    _require(
        design.get("sha256"),
        sha256_file(root / str(design.get("relative_path") or "")),
        "design_code.sha256",
    )
    diagnostic_path = root / str(
        payload["evidence"]["v043_diagnostic"]["relative_path"]
    )
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    _require(diagnostic.get("decision"), DECISION, "diagnostic.decision")
    _require(
        diagnostic.get("all_bot_controlled_gates_passed"),
        True,
        "diagnostic.gates",
    )
    _require(
        diagnostic.get("classification", {}).get(
            "provider_liveness_within_twenty_seconds"
        ),
        False,
        "diagnostic.provider_liveness",
    )
    return dict(payload)


__all__ = [
    "PASS_VERDICT",
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "VARIANT",
    "V043ContractError",
    "load_and_verify_prereg",
]
