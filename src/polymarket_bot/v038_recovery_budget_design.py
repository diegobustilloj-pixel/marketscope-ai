from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v031_capture import open_read_only
from polymarket_bot.v034_contract import aggregate_probe_results
from polymarket_bot.v034_guard_design import (
    DEPTH_KEYS,
    _all_depth,
    _exit_ready,
    _relative_guard_reasons,
)


DESIGN_SCHEMA = "diagnostic_v038_single_layer_recovery_budget_1"
VARIANT = "V0.38_SINGLE_LAYER_CLOB_RECOVERY_BUDGET_DESIGN"
INPUTS = {
    "v037_database": "data/capture_v037_clob_freshness_recovery_4h.db",
    "v037_result": "data/resultado_v037_clob_freshness_recovery_4h.json",
    "v037_preregistration": "data/prereg_v037_clob_freshness_recovery_4h.json",
    "v037_implementation": "data/implementation_v037_clob_freshness_recovery_4h.json",
}


class V038DesignError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def evaluate_recovery_budget_probe(
    snapshots: Mapping[int, Mapping[str, Any]],
    *,
    decision_offset: int,
    outcome: str,
    absolute_exit_deadline: int = 111,
    maximum_holding_seconds: int = 30,
    retry_budget_seconds: int = 10,
    absolute_retry_deadline: int = 121,
    drawdown_threshold: float = 0.8,
    position_shares: float = 5.0,
    entry_depth_buffer_shares: float = 10.0,
) -> dict[str, Any]:
    selected = outcome.lower()
    if selected not in {"up", "down"}:
        raise ValueError("outcome debe ser Up o Down")
    entry_offset = decision_offset + 1
    planned_holding = min(maximum_holding_seconds, absolute_exit_deadline - entry_offset)
    if planned_holding <= 0:
        return {
            "status": "DECISION_REJECTED",
            "reason": "INSUFFICIENT_TIME_BEFORE_ABSOLUTE_EXIT_DEADLINE",
        }
    decision = snapshots.get(decision_offset)
    if decision is None:
        return {"status": "DECISION_REJECTED", "reason": "DECISION_SNAPSHOT_MISSING"}
    if not bool(decision.get("complete_v2")):
        return {"status": "DECISION_REJECTED", "reason": "DECISION_DATA_INCOMPLETE"}
    if not _all_depth(decision, entry_depth_buffer_shares):
        return {
            "status": "DECISION_REJECTED",
            "reason": "DECISION_BUFFER_DEPTH_INSUFFICIENT",
        }
    entry = snapshots.get(entry_offset)
    if entry is None:
        return {"status": "ENTRY_REJECTED", "reason": "ENTRY_SNAPSHOT_MISSING"}
    if not bool(entry.get("complete_v2")):
        return {"status": "ENTRY_REJECTED", "reason": "ENTRY_DATA_INCOMPLETE"}
    if not _all_depth(entry, entry_depth_buffer_shares):
        return {
            "status": "ENTRY_REJECTED",
            "reason": "ENTRY_BUFFER_DEPTH_INSUFFICIENT",
        }

    target = entry_offset + planned_holding
    retry_deadline = min(target + retry_budget_seconds, absolute_retry_deadline)
    exit_rejections: Counter[str] = Counter()
    for offset in range(entry_offset + 1, retry_deadline + 1):
        current = snapshots.get(offset)
        if current is None:
            exit_rejections["EXIT_SNAPSHOT_MISSING"] += 1
            continue
        if offset < target:
            reasons: list[str] = []
            selected_depth = float(current.get(f"{selected}_bid_depth_top5") or 0.0)
            if selected_depth < entry_depth_buffer_shares:
                reasons.append("SELECTED_BID_BELOW_ABSOLUTE_BUFFER")
            if not _all_depth(current, position_shares):
                reasons.append("ANY_BOOK_SIDE_BELOW_POSITION_SIZE")
            reasons.extend(
                _relative_guard_reasons(
                    snapshots.get(offset - 1),
                    current,
                    drawdown_threshold=drawdown_threshold,
                    baseline_minimum_depth=entry_depth_buffer_shares,
                    depth_keys=(f"{selected}_bid_depth_top5",),
                )
            )
            if reasons and _exit_ready(current, selected, position_shares):
                return {
                    "status": "EXITED",
                    "exit_kind": "PROACTIVE_GUARD",
                    "guard_reasons": sorted(set(reasons)),
                    "entry_offset": entry_offset,
                    "target_exit_offset": target,
                    "retry_deadline_offset": retry_deadline,
                    "exit_offset": offset,
                    "holding_seconds": offset - entry_offset,
                    "exit_delay_seconds": offset - target,
                    "exit_rejections": dict(exit_rejections),
                    "planned_holding_seconds": planned_holding,
                    "absolute_exit_deadline": absolute_exit_deadline,
                    "absolute_retry_deadline": absolute_retry_deadline,
                }
            if reasons:
                exit_rejections["GUARD_TRIGGERED_BUT_SELECTED_EXIT_UNAVAILABLE"] += 1
            continue
        if not _exit_ready(current, selected, position_shares):
            exit_rejections["TARGET_OR_RETRY_SELECTED_EXIT_UNAVAILABLE"] += 1
            continue
        return {
            "status": "EXITED",
            "exit_kind": "SCHEDULED" if offset == target else "GRACE_RETRY",
            "guard_reasons": [],
            "entry_offset": entry_offset,
            "target_exit_offset": target,
            "retry_deadline_offset": retry_deadline,
            "exit_offset": offset,
            "holding_seconds": offset - entry_offset,
            "exit_delay_seconds": offset - target,
            "exit_rejections": dict(exit_rejections),
            "planned_holding_seconds": planned_holding,
            "absolute_exit_deadline": absolute_exit_deadline,
            "absolute_retry_deadline": absolute_retry_deadline,
        }
    return {
        "status": "TRAPPED",
        "reason": "NO_FRESH_FULL_DEPTH_SELECTED_BID_BY_OPERATIONAL_RETRY_DEADLINE",
        "exit_kind": None,
        "guard_reasons": [],
        "entry_offset": entry_offset,
        "target_exit_offset": target,
        "retry_deadline_offset": retry_deadline,
        "exit_offset": None,
        "holding_seconds": None,
        "exit_delay_seconds": None,
        "exit_rejections": dict(exit_rejections),
        "planned_holding_seconds": planned_holding,
        "absolute_exit_deadline": absolute_exit_deadline,
        "absolute_retry_deadline": absolute_retry_deadline,
    }


def run_v038_design(
    *, output_path: str | Path | None = None, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    hashes = {key: sha256_file(root / relative) for key, relative in INPUTS.items()}
    result = json.loads((root / INPUTS["v037_result"]).read_text(encoding="utf-8"))
    prereg = json.loads(
        (root / INPUTS["v037_preregistration"]).read_text(encoding="utf-8")
    )
    if result.get("verdict") != "FAIL_FIRST_TRAPPED_POSITION":
        raise V038DesignError("Resultado V0.37 incompatible")
    if not result.get("safety_passed"):
        raise V038DesignError("V0.37 no supero seguridad")

    previous_grace = int(prereg["probe_contract"]["exit_grace_seconds"])
    reserved_recovery = int(
        prereg["probe_contract"]["required_margin_before_earliest_irreducible_exit_seconds"]
    ) - int(prereg["capture_contract"]["freshness_max_age_ms"]) // 1000 - previous_grace
    retry_budget = previous_grace + reserved_recovery
    absolute_exit_deadline = int(prereg["probe_contract"]["absolute_exit_deadline_offset"])
    earliest_irreducible = int(
        prereg["probe_contract"]["earliest_observed_irreducible_exit_offset"]
    )
    freshness_reserve = int(prereg["capture_contract"]["freshness_max_age_ms"]) // 1000
    absolute_retry_deadline = earliest_irreducible - freshness_reserve

    connection = open_read_only(root / INPUTS["v037_database"])
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        markets = [
            dict(row)
            for row in connection.execute(
                "SELECT condition_id,slug,capture_status FROM v037_markets ORDER BY market_start_ms"
            )
            if row["capture_status"] == "COMPLETED"
        ]
        probes: list[dict[str, Any]] = []
        for market in markets:
            snapshots = {
                int(row["second_offset"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM v037_snapshots WHERE condition_id=? ORDER BY second_offset",
                    (market["condition_id"],),
                )
            }
            for decision_offset in range(30, 91):
                for outcome in ("Up", "Down"):
                    probes.append(
                        {
                            **evaluate_recovery_budget_probe(
                                snapshots,
                                decision_offset=decision_offset,
                                outcome=outcome,
                                absolute_exit_deadline=absolute_exit_deadline,
                                retry_budget_seconds=retry_budget,
                                absolute_retry_deadline=absolute_retry_deadline,
                            ),
                            "slug": str(market["slug"]),
                            "outcome": outcome,
                            "decision_offset": decision_offset,
                        }
                    )
    finally:
        connection.close()
    replay = aggregate_probe_results(probes)
    old_traps_reclassified = [
        probe
        for probe in probes
        if probe["slug"] == "btc-updown-5m-1787770500"
        and int(probe["decision_offset"]) == 30
    ]
    gates = {
        "v037_failed_exactly_two_symmetric_positions": (
            int(result["overall"]["trapped_positions"]) == 2
            and int(result["per_outcome"]["Up"]["trapped_positions"]) == 1
            and int(result["per_outcome"]["Down"]["trapped_positions"]) == 1
        ),
        "reserved_recovery_budget_was_five_seconds": reserved_recovery == 5,
        "operational_retry_budget_equals_grace_plus_reserved_recovery": retry_budget == 10,
        "absolute_retry_deadline_leaves_freshness_reserve": (
            earliest_irreducible - absolute_retry_deadline == freshness_reserve == 5
        ),
        "closed_replay_zero_trapped": replay["trapped_positions"] == 0,
        "closed_replay_exit_rate_one": replay["exit_success_within_grace_rate"] == 1.0,
        "old_traps_recover_at_offset_67": all(
            probe.get("status") == "EXITED"
            and int(probe.get("exit_offset", -1)) == 67
            and int(probe.get("exit_delay_seconds", -1)) == 6
            for probe in old_traps_reclassified
        ),
        "sqlite_quick_check_passed": quick_check == "ok",
    }
    payload = {
        "schema": DESIGN_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "remove_nested_reconnect_and_use_the_already_reserved_recovery_budget",
        "inputs": {
            key: {"relative_path": relative, "sha256": hashes[key]}
            for key, relative in INPUTS.items()
        },
        "observed_failure": {
            "v037_verdict": result["verdict"],
            "trapped_positions": int(result["overall"]["trapped_positions"]),
            "trap_market": "btc-updown-5m-1787770500",
            "trap_decision_offset": 30,
            "trap_entry_offset": 31,
            "trap_target_offset": 61,
            "old_retry_deadline_offset": 66,
            "fresh_books_returned_offset": 67,
            "missed_by_seconds": 1,
            "watchdog_trigger_offset": 56,
            "watchdog_recovery_ms": 11560,
            "nested_reconnect_owners": 2,
        },
        "derivation": {
            "parameter_grid_used": False,
            "absolute_exit_deadline_offset": absolute_exit_deadline,
            "previous_exit_grace_seconds": previous_grace,
            "previous_reserved_recovery_seconds": reserved_recovery,
            "operational_retry_budget_seconds": retry_budget,
            "absolute_retry_deadline_offset": absolute_retry_deadline,
            "earliest_observed_irreducible_exit_offset": earliest_irreducible,
            "freshness_reserve_seconds": freshness_reserve,
            "decision_offsets_inclusive": [30, 90],
            "expected_capacity_probes_48_markets": 5856,
            "formula": "retry_budget=old_grace+already_reserved_recovery; retry_deadline=min(target+retry_budget,earliest_irreducible-freshness_reserve)",
        },
        "single_layer_transport": {
            "reconnect_owner_count": 1,
            "socket_session_has_internal_exponential_retry": False,
            "supervisor_owns_connect_receive_staleness_and_reconnect": True,
            "stale_timeout_ms": 3000,
            "poll_ms": 250,
            "open_timeout_ms": 3000,
            "close_timeout_ms": 250,
            "reconnect_backoff_schedule_ms": [250, 500, 1000],
            "reconnect_backoff_cap_ms": 1000,
            "both_books_required_after_generation_start": True,
            "stale_depth_can_count_as_exit": False,
        },
        "closed_replay": {
            **replay,
            "old_traps_reclassified": old_traps_reclassified,
            "posthoc_design_only": True,
            "fresh_validation_credit": False,
        },
        "gates": gates,
        "all_design_gates_passed": all(gates.values()),
        "decision": (
            "PREPARE_ONE_FRESH_V038_SINGLE_LAYER_RECOVERY_REPLICATION"
            if all(gates.values())
            else "DO_NOT_BUILD_V038_DESIGN_GATES_FAILED"
        ),
        "limitations": {
            "network_recovery_can_be_guaranteed": False,
            "fresh_forward_validation_required": True,
            "fail_closed_if_no_fresh_exit_by_deadline": True,
            "economic_edge_evaluated": False,
            "outcomes_read": 0,
            "prices_read_or_stored": False,
            "pnl_calculated": False,
            "orders_created": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
    }
    if output is not None:
        if output.exists():
            existing = json.loads(output.read_text(encoding="utf-8"))
            if existing.get("schema") == DESIGN_SCHEMA and existing.get("inputs") == payload["inputs"]:
                return existing
            raise V038DesignError("Existe otro diagnostico V0.38")
        _write_atomic(output, payload)
    return payload


__all__ = [
    "DESIGN_SCHEMA",
    "V038DesignError",
    "evaluate_recovery_budget_probe",
    "run_v038_design",
]
