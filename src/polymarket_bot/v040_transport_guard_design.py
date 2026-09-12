from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v031_capture import open_read_only
from polymarket_bot.v034_contract import aggregate_probe_results
from polymarket_bot.v034_guard_design import (
    _all_depth,
    _exit_ready,
    _relative_guard_reasons,
)


DESIGN_SCHEMA = "diagnostic_v040_pre_stale_transport_guard_1"
VARIANT = "V0.40_PRE_STALE_TRANSPORT_GUARD_DESIGN"
TRANSPORT_GUARD_AGE_MS = 2000
TRANSPORT_STALE_TIMEOUT_MS = 3000
TRANSPORT_GUARD_HORIZON_SECONDS = 5
INPUTS = {
    "v037_database": "data/capture_v037_clob_freshness_recovery_4h.db",
    "v037_result": "data/resultado_v037_clob_freshness_recovery_4h.json",
    "v038_database": "data/capture_v038_single_layer_recovery_4h.db",
    "v038_result": "data/resultado_v038_single_layer_recovery_4h.json",
    "v039_database": "data/capture_v039_validated_peer_dns_fallback_restart_4h.db",
    "v039_result": "data/resultado_v039_validated_peer_dns_fallback_restart_4h.json",
    "v039_preregistration": "data/prereg_v039_validated_peer_dns_fallback_4h.json",
    "v039_implementation": "data/implementation_v039_validated_peer_dns_fallback_4h.json",
}
SOURCES = {
    "V037": (INPUTS["v037_database"], "v037"),
    "V038": (INPUTS["v038_database"], "v038"),
    "V039_RESTART": (INPUTS["v039_database"], "v039"),
}


class V040DesignError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _base_result(
    *,
    status: str,
    entry_offset: int,
    target: int,
    retry_deadline: int,
    exit_offset: int | None,
    exit_kind: str | None,
    guard_reasons: Sequence[str],
    exit_rejections: Mapping[str, int],
    planned_holding: int,
    absolute_exit_deadline: int,
    absolute_retry_deadline: int,
) -> dict[str, Any]:
    return {
        "status": status,
        "exit_kind": exit_kind,
        "guard_reasons": list(guard_reasons),
        "entry_offset": entry_offset,
        "target_exit_offset": target,
        "retry_deadline_offset": retry_deadline,
        "exit_offset": exit_offset,
        "holding_seconds": None if exit_offset is None else exit_offset - entry_offset,
        "exit_delay_seconds": None if exit_offset is None else exit_offset - target,
        "exit_rejections": dict(exit_rejections),
        "planned_holding_seconds": planned_holding,
        "absolute_exit_deadline": absolute_exit_deadline,
        "absolute_retry_deadline": absolute_retry_deadline,
    }


def evaluate_transport_guard_probe(
    snapshots: Mapping[int, Mapping[str, Any]],
    *,
    decision_offset: int,
    outcome: str,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply the V0.39 rules plus one pre-stale CLOB exit guard.

    The guard is intentionally armed only during the already reserved five-second
    freshness horizon. It may use a snapshot only while both books are explicitly
    fresh and their oldest age is at least one snapshot below the 3 s watchdog.
    """

    selected = outcome.lower()
    if selected not in {"up", "down"}:
        raise ValueError("outcome debe ser Up o Down")
    entry_offset = decision_offset + int(contract["decision_latency_seconds"])
    absolute_exit_deadline = int(contract["absolute_exit_target_deadline_offset"])
    absolute_retry_deadline = int(contract["absolute_retry_deadline_offset"])
    maximum_holding = int(contract["maximum_holding_seconds_after_entry"])
    retry_budget = int(contract["operational_retry_budget_seconds"])
    position_shares = float(contract["position_shares"])
    entry_buffer = float(contract["entry_depth_buffer_shares"])
    drawdown_threshold = float(contract["relative_drawdown_threshold_inclusive"])
    guard_age_ms = int(contract["transport_guard_age_ms"])
    stale_timeout_ms = int(contract["transport_stale_timeout_ms"])
    guard_horizon = int(contract["transport_guard_horizon_seconds"])
    planned_holding = min(maximum_holding, absolute_exit_deadline - entry_offset)
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
    if not _all_depth(decision, entry_buffer):
        return {
            "status": "DECISION_REJECTED",
            "reason": "DECISION_BUFFER_DEPTH_INSUFFICIENT",
        }
    entry = snapshots.get(entry_offset)
    if entry is None:
        return {"status": "ENTRY_REJECTED", "reason": "ENTRY_SNAPSHOT_MISSING"}
    if not bool(entry.get("complete_v2")):
        return {"status": "ENTRY_REJECTED", "reason": "ENTRY_DATA_INCOMPLETE"}
    if not _all_depth(entry, entry_buffer):
        return {
            "status": "ENTRY_REJECTED",
            "reason": "ENTRY_BUFFER_DEPTH_INSUFFICIENT",
        }

    target = entry_offset + planned_holding
    retry_deadline = min(target + retry_budget, absolute_retry_deadline)
    exit_rejections: Counter[str] = Counter()
    for offset in range(entry_offset + 1, retry_deadline + 1):
        current = snapshots.get(offset)
        if current is None:
            exit_rejections["EXIT_SNAPSHOT_MISSING"] += 1
            continue
        if offset < target:
            ages = (current.get("up_book_age_ms"), current.get("down_book_age_ms"))
            ages_known = all(value is not None for value in ages)
            oldest_age = max(int(value) for value in ages) if ages_known else -1
            guard_armed = (
                target - offset <= guard_horizon
                and guard_age_ms <= oldest_age < stale_timeout_ms
                and bool(current.get("up_book_fresh"))
                and bool(current.get("down_book_fresh"))
            )
            if guard_armed and _exit_ready(current, selected, position_shares):
                result = _base_result(
                    status="EXITED",
                    entry_offset=entry_offset,
                    target=target,
                    retry_deadline=retry_deadline,
                    exit_offset=offset,
                    exit_kind="TRANSPORT_PRE_STALE_GUARD",
                    guard_reasons=["TRANSPORT_PRE_STALE_WITHIN_FRESHNESS_RESERVE"],
                    exit_rejections=exit_rejections,
                    planned_holding=planned_holding,
                    absolute_exit_deadline=absolute_exit_deadline,
                    absolute_retry_deadline=absolute_retry_deadline,
                )
                result["transport_guard_oldest_book_age_ms"] = oldest_age
                result["transport_guard_seconds_before_target"] = target - offset
                return result

            reasons: list[str] = []
            if float(current.get(f"{selected}_bid_depth_top5") or 0.0) < entry_buffer:
                reasons.append("SELECTED_BID_BELOW_ABSOLUTE_BUFFER")
            if not _all_depth(current, position_shares):
                reasons.append("ANY_BOOK_SIDE_BELOW_POSITION_SIZE")
            reasons.extend(
                _relative_guard_reasons(
                    snapshots.get(offset - 1),
                    current,
                    drawdown_threshold=drawdown_threshold,
                    baseline_minimum_depth=entry_buffer,
                    depth_keys=(f"{selected}_bid_depth_top5",),
                )
            )
            if reasons and _exit_ready(current, selected, position_shares):
                return _base_result(
                    status="EXITED",
                    entry_offset=entry_offset,
                    target=target,
                    retry_deadline=retry_deadline,
                    exit_offset=offset,
                    exit_kind="PROACTIVE_GUARD",
                    guard_reasons=sorted(set(reasons)),
                    exit_rejections=exit_rejections,
                    planned_holding=planned_holding,
                    absolute_exit_deadline=absolute_exit_deadline,
                    absolute_retry_deadline=absolute_retry_deadline,
                )
            if reasons:
                exit_rejections["GUARD_TRIGGERED_BUT_SELECTED_EXIT_UNAVAILABLE"] += 1
            continue
        if not _exit_ready(current, selected, position_shares):
            exit_rejections["TARGET_OR_RETRY_SELECTED_EXIT_UNAVAILABLE"] += 1
            continue
        return _base_result(
            status="EXITED",
            entry_offset=entry_offset,
            target=target,
            retry_deadline=retry_deadline,
            exit_offset=offset,
            exit_kind="SCHEDULED" if offset == target else "GRACE_RETRY",
            guard_reasons=[],
            exit_rejections=exit_rejections,
            planned_holding=planned_holding,
            absolute_exit_deadline=absolute_exit_deadline,
            absolute_retry_deadline=absolute_retry_deadline,
        )
    result = _base_result(
        status="TRAPPED",
        entry_offset=entry_offset,
        target=target,
        retry_deadline=retry_deadline,
        exit_offset=None,
        exit_kind=None,
        guard_reasons=[],
        exit_rejections=exit_rejections,
        planned_holding=planned_holding,
        absolute_exit_deadline=absolute_exit_deadline,
        absolute_retry_deadline=absolute_retry_deadline,
    )
    result["reason"] = "NO_FRESH_FULL_DEPTH_SELECTED_BID_BY_OPERATIONAL_RETRY_DEADLINE"
    return result


def evaluate_market_probes(
    snapshots: Mapping[int, Mapping[str, Any]],
    *,
    condition_id: str,
    slug: str,
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            **evaluate_transport_guard_probe(
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


def summarize_transport_guard(probes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    summary = aggregate_probe_results(probes)
    guarded = [
        probe
        for probe in probes
        if probe.get("exit_kind") == "TRANSPORT_PRE_STALE_GUARD"
    ]
    summary["transport_pre_stale_guard_exits"] = len(guarded)
    summary["maximum_transport_guard_book_age_ms"] = max(
        (int(probe["transport_guard_oldest_book_age_ms"]) for probe in guarded),
        default=0,
    )
    summary["maximum_transport_guard_seconds_before_target"] = max(
        (int(probe["transport_guard_seconds_before_target"]) for probe in guarded),
        default=0,
    )
    return summary


def _load_source(
    root: Path,
    *,
    relative_path: str,
    prefix: str,
    contract: Mapping[str, Any],
) -> tuple[str, list[dict[str, Any]], int]:
    connection = open_read_only(root / relative_path)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        markets = [
            dict(row)
            for row in connection.execute(
                f"SELECT condition_id,slug,capture_status FROM {prefix}_markets "
                "ORDER BY market_start_ms"
            )
            if row["capture_status"] == "COMPLETED"
        ]
        probes: list[dict[str, Any]] = []
        for market in markets:
            snapshots = {
                int(row["second_offset"]): dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {prefix}_snapshots WHERE condition_id=? "
                    "ORDER BY second_offset",
                    (market["condition_id"],),
                )
            }
            probes.extend(
                {
                    **probe,
                    "source": prefix.upper(),
                }
                for probe in evaluate_market_probes(
                    snapshots,
                    condition_id=str(market["condition_id"]),
                    slug=str(market["slug"]),
                    contract=contract,
                )
            )
    finally:
        connection.close()
    return quick_check, probes, len(markets)


def run_v040_design(
    *, output_path: str | Path | None = None, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    hashes = {key: sha256_file(root / relative) for key, relative in INPUTS.items()}
    v039_result = json.loads((root / INPUTS["v039_result"]).read_text(encoding="utf-8"))
    v039_prereg = json.loads(
        (root / INPUTS["v039_preregistration"]).read_text(encoding="utf-8")
    )
    if v039_result.get("verdict") != "FAIL_FIRST_TRAPPED_POSITION":
        raise V040DesignError("Resultado V0.39 incompatible")
    if not v039_result.get("safety_passed"):
        raise V040DesignError("V0.39 no supero seguridad")
    contract = {
        **v039_prereg["probe_contract"],
        "transport_guard_age_ms": TRANSPORT_GUARD_AGE_MS,
        "transport_stale_timeout_ms": TRANSPORT_STALE_TIMEOUT_MS,
        "transport_guard_horizon_seconds": TRANSPORT_GUARD_HORIZON_SECONDS,
        "transport_guard_requires_both_books_fresh": True,
        "transport_guard_requires_selected_bid_depth_at_position_size": True,
        "transport_guard_stale_depth_allowed": False,
    }

    all_probes: list[dict[str, Any]] = []
    source_summaries: dict[str, Any] = {}
    quick_checks: dict[str, str] = {}
    total_markets = 0
    for source_name, (relative, prefix) in SOURCES.items():
        quick, probes, market_count = _load_source(
            root,
            relative_path=relative,
            prefix=prefix,
            contract=contract,
        )
        quick_checks[source_name] = quick
        total_markets += market_count
        all_probes.extend(probes)
        source_summaries[source_name] = {
            "completed_markets": market_count,
            **summarize_transport_guard(probes),
        }
    combined = summarize_transport_guard(all_probes)
    former_traps = [
        probe
        for probe in all_probes
        if probe["slug"] == "btc-updown-5m-1787848200"
        and int(probe["decision_offset"]) == 60
    ]
    guarded = [
        probe
        for probe in all_probes
        if probe.get("exit_kind") == "TRANSPORT_PRE_STALE_GUARD"
    ]
    gates = {
        "v039_failed_exactly_two_symmetric_positions": (
            int(v039_result["overall"]["trapped_positions"]) == 2
            and int(v039_result["per_outcome"]["Up"]["trapped_positions"]) == 1
            and int(v039_result["per_outcome"]["Down"]["trapped_positions"]) == 1
        ),
        "guard_threshold_is_one_snapshot_before_stale": (
            TRANSPORT_GUARD_AGE_MS + 1000 == TRANSPORT_STALE_TIMEOUT_MS
        ),
        "guard_horizon_equals_existing_freshness_reserve": (
            TRANSPORT_GUARD_HORIZON_SECONDS
            == int(v039_prereg["capture_contract"]["freshness_max_age_ms"]) // 1000
        ),
        "parameter_grid_not_used": True,
        "closed_replay_covers_at_least_eighty_markets": total_markets >= 80,
        "closed_replay_zero_trapped": combined["trapped_positions"] == 0,
        "closed_replay_exit_rate_one": combined["exit_success_within_grace_rate"] == 1.0,
        "closed_replay_scheduled_fraction_preserved": (
            combined["scheduled_exit_fraction"] >= 0.8
        ),
        "closed_replay_median_holding_preserved": (
            combined["median_holding_seconds"] >= 25.0
        ),
        "former_v039_traps_exit_at_last_fresh_offset_88": (
            len(former_traps) == 2
            and {str(probe["outcome"]) for probe in former_traps} == {"Up", "Down"}
            and all(
                probe.get("status") == "EXITED"
                and probe.get("exit_kind") == "TRANSPORT_PRE_STALE_GUARD"
                and int(probe.get("exit_offset", -1)) == 88
                for probe in former_traps
            )
        ),
        "every_transport_guard_used_non_stale_books": bool(guarded)
        and all(
            int(probe["transport_guard_oldest_book_age_ms"])
            < TRANSPORT_STALE_TIMEOUT_MS
            for probe in guarded
        ),
        "every_source_sqlite_quick_check_passed": all(
            value == "ok" for value in quick_checks.values()
        ),
    }
    payload = {
        "schema": DESIGN_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "exit_before_clob_staleness_when_a_position_is_within_the_existing_freshness_reserve",
        "inputs": {
            key: {"relative_path": relative, "sha256": hashes[key]}
            for key, relative in INPUTS.items()
        },
        "observed_failure": {
            "v039_verdict": v039_result["verdict"],
            "trap_market": "btc-updown-5m-1787848200",
            "trap_decision_offset": 60,
            "trap_entry_offset": 61,
            "trap_target_offset": 91,
            "trap_retry_deadline_offset": 101,
            "books_recovered_offset": 102,
            "recovered_one_second_after_deadline": True,
            "maximum_relevant_recovery_ms": int(
                v039_result["transport"]["maximum_relevant_recovery_ms"]
            ),
            "failure_scope": "WINDOWS_ROUTE_DNS_AND_NUMERIC_PEER",
            "trapped_positions": 2,
        },
        "derivation": {
            "parameter_grid_used": False,
            "snapshot_period_ms": 1000,
            "transport_stale_timeout_ms": TRANSPORT_STALE_TIMEOUT_MS,
            "transport_guard_age_ms": TRANSPORT_GUARD_AGE_MS,
            "formula": "guard_age=stale_timeout-one_snapshot_period",
            "transport_guard_horizon_seconds": TRANSPORT_GUARD_HORIZON_SECONDS,
            "horizon_formula": "existing_freshness_reserve=freshness_max_age_ms/1000",
            "stale_depth_can_count_as_exit": False,
            "both_books_must_still_be_fresh": True,
            "selected_bid_must_have_position_depth": True,
        },
        "closed_replay": {
            "completed_markets": total_markets,
            "sources": source_summaries,
            "combined": combined,
            "former_v039_traps_reclassified": former_traps,
            "posthoc_design_only": True,
            "fresh_validation_credit": False,
        },
        "gates": gates,
        "all_design_gates_passed": all(gates.values()),
        "decision": (
            "PREPARE_ONE_FRESH_V040_PRE_STALE_TRANSPORT_GUARD_REPLICATION"
            if all(gates.values())
            else "DO_NOT_BUILD_V040_DESIGN_GATES_FAILED"
        ),
        "limitations": {
            "actual_order_submission_during_total_route_loss_guaranteed": False,
            "redundant_network_or_remote_executor_required_before_real_money": True,
            "capacity_probe_only": True,
            "fresh_forward_validation_required": True,
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
            if (
                existing.get("schema") == DESIGN_SCHEMA
                and existing.get("inputs") == payload["inputs"]
            ):
                return existing
            raise V040DesignError("Existe otro diagnostico V0.40")
        _write_atomic(output, payload)
    return payload


__all__ = [
    "DESIGN_SCHEMA",
    "TRANSPORT_GUARD_AGE_MS",
    "TRANSPORT_GUARD_HORIZON_SECONDS",
    "TRANSPORT_STALE_TIMEOUT_MS",
    "V040DesignError",
    "evaluate_market_probes",
    "evaluate_transport_guard_probe",
    "run_v040_design",
    "summarize_transport_guard",
]
