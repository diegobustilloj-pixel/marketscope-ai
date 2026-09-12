from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v031_capture import open_read_only
from polymarket_bot.v038_audit import _ratio, _retry_named_metrics, _write_atomic
from polymarket_bot.v040_capture import V040_EXPECTED_MARKETS, V040_MARKET_SECONDS
from polymarket_bot.v040_contract import (
    PASS_VERDICT,
    VARIANT,
    evaluate_market_probes,
    load_and_verify_prereg,
    summarize_transport_guard,
)
from polymarket_bot.v040_runner import load_and_verify_implementation


RESULT_SCHEMA = "result_v040_pre_stale_transport_guard_4h_1"


class V040AuditError(RuntimeError):
    pass


def audit_v040(
    *, database: str | Path, prereg_path: str | Path,
    implementation_path: str | Path, result_path: str | Path | None = None,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    database_file = Path(database).resolve()
    prereg_file = Path(prereg_path).resolve()
    implementation_file = Path(implementation_path).resolve()
    output = Path(result_path).resolve() if result_path is not None else None
    prereg = load_and_verify_prereg(prereg_file, project_root=root)
    load_and_verify_implementation(implementation_file, project_root=root)
    if not database_file.is_file():
        raise V040AuditError("Base V0.40 no encontrada")
    database_hash = sha256_file(database_file)
    prereg_hash = sha256_file(prereg_file)
    implementation_hash = sha256_file(implementation_file)
    if output is not None and output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == RESULT_SCHEMA
            and existing.get("database_sha256") == database_hash
            and existing.get("preregistration_sha256") == prereg_hash
            and existing.get("implementation_sha256") == implementation_hash
        ):
            return existing
        raise V040AuditError("Existe otro resultado V0.40")

    connection = open_read_only(database_file)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        query_only = bool(int(connection.execute("PRAGMA query_only").fetchone()[0]))
        meta = {str(row[0]): json.loads(str(row[1]))
                for row in connection.execute("SELECT key,value FROM v040_meta")}
        markets = [dict(row) for row in connection.execute(
            "SELECT * FROM v040_markets ORDER BY market_start_ms"
        )]
        snapshots: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
        complete_v2 = 0
        for row in connection.execute(
            "SELECT * FROM v040_snapshots ORDER BY condition_id,second_offset"
        ):
            values = dict(row)
            snapshots[str(values["condition_id"])][int(values["second_offset"])] = values
            complete_v2 += int(values["complete_v2"])
        transport_events = [dict(row) for row in connection.execute(
            "SELECT * FROM v040_clob_transport_events ORDER BY event_id"
        )]
    finally:
        connection.close()
    completion = meta.get("completion_reason")
    if completion is None:
        raise V040AuditError("V0.40 todavia no termino")

    completed = [market for market in markets if market["capture_status"] == "COMPLETED"]
    probes: list[dict[str, Any]] = []
    per_market: dict[str, Any] = {}
    for market in completed:
        market_probes = evaluate_market_probes(
            snapshots.get(str(market["condition_id"]), {}),
            condition_id=str(market["condition_id"]), slug=str(market["slug"]),
            contract=prereg["probe_contract"],
        )
        probes.extend(market_probes)
        per_market[str(market["slug"])] = _retry_named_metrics(
            summarize_transport_guard(market_probes)
        )
    overall = _retry_named_metrics(summarize_transport_guard(probes))
    per_outcome = {
        outcome: _retry_named_metrics(summarize_transport_guard(
            [probe for probe in probes if probe["outcome"] == outcome]
        ))
        for outcome in ("Up", "Down")
    }
    per_bin = {
        f"{start}_{end}": _retry_named_metrics(summarize_transport_guard([
            probe for probe in probes
            if start <= int(probe["decision_offset"]) <= end
        ]))
        for start, end in ((30, 59), (60, 80), (81, 90))
    }
    planned_holding_distribution: dict[str, int] = {}
    for probe in probes:
        if "planned_holding_seconds" in probe:
            key = str(int(probe["planned_holding_seconds"]))
            planned_holding_distribution[key] = planned_holding_distribution.get(key, 0) + 1

    fresh_events = [event for event in transport_events
                    if event["event_type"] == "GENERATION_FRESH"]
    trigger_events = [event for event in transport_events
                      if event["event_type"] == "RECONNECT_TRIGGER"]
    recovery_events = [event for event in transport_events
                       if event["event_type"] == "RECOVERED"]
    relevant_end = int(prereg["probe_contract"]["absolute_retry_deadline_offset"])
    relevant_trigger_events = [event for event in trigger_events
                               if int(event["second_offset"]) <= relevant_end]
    relevant_incidents = {
        (str(event["condition_id"]), int(event["incident_id"]))
        for event in relevant_trigger_events if event["incident_id"] is not None
    }
    recovered = {
        (str(event["condition_id"]), int(event["incident_id"])): event
        for event in recovery_events if event["incident_id"] is not None
    }
    unresolved_relevant = sorted(relevant_incidents - set(recovered))
    relevant_recovery_ms = [
        int(recovered[key]["recovery_ms"])
        for key in relevant_incidents & set(recovered)
        if recovered[key]["recovery_ms"] is not None
    ]
    fresh_by_market: dict[str, int] = defaultdict(int)
    cache_ready_by_market: dict[str, int] = defaultdict(int)
    for event in fresh_events:
        condition_id = str(event["condition_id"])
        fresh_by_market[condition_id] += 1
        if bool(event["validated_peer_cache_available"]):
            cache_ready_by_market[condition_id] += 1
    completed_ids = {str(market["condition_id"]) for market in completed}
    minimum_fresh_generations = min(
        (fresh_by_market[key] for key in completed_ids), default=0
    )
    minimum_cache_ready_generations = min(
        (cache_ready_by_market[key] for key in completed_ids), default=0
    )
    cached_fresh_events = [event for event in fresh_events
                           if event["dial_mode"] == "VALIDATED_PEER_IP"]
    cached_recoveries = [event for event in recovery_events
                         if event["dial_mode"] == "VALIDATED_PEER_IP"]
    cached_failures = [event for event in trigger_events
                       if event["dial_mode"] == "VALIDATED_PEER_IP"]
    dns_failures = [event for event in trigger_events
                    if event["trigger_reason"] == "TRANSPORT_gaierror"]

    rows = sum(len(values) for values in snapshots.values())
    expected_rows = V040_EXPECTED_MARKETS * V040_MARKET_SECONDS
    completed_counts = [len(snapshots.get(str(market["condition_id"]), {}))
                        for market in completed]
    market_coverage = _ratio(len(completed), V040_EXPECTED_MARKETS)
    snapshot_coverage = _ratio(rows, expected_rows)
    complete_coverage = _ratio(complete_v2, rows)
    resolution_coverage = _ratio(
        sum(
            market["resolution_contract_status"] == "VERIFIED"
            and int(market["resolution_twap_window_s"] or 0) in {30, 60}
            for market in markets
        ), len(markets),
    )
    gates = prereg["technical_gates"]
    gate_results = {
        "minimum_completed_market_coverage_passed": market_coverage
        >= float(gates["minimum_completed_market_coverage"]),
        "minimum_snapshot_coverage_passed": snapshot_coverage
        >= float(gates["minimum_snapshot_coverage"]),
        "minimum_complete_v2_snapshot_coverage_passed": complete_coverage
        >= float(gates["minimum_complete_v2_snapshot_coverage"]),
        "minimum_snapshots_per_completed_market_passed": bool(completed_counts)
        and min(completed_counts) >= int(gates["minimum_snapshots_per_completed_market"]),
        "maximum_missing_seconds_per_completed_market_passed": bool(completed_counts)
        and max(V040_MARKET_SECONDS - count for count in completed_counts)
        <= int(gates["maximum_missing_seconds_per_completed_market"]),
        "minimum_total_capacity_probes_passed": overall["total_capacity_probes"]
        >= int(gates["minimum_total_capacity_probes"]),
        "minimum_entry_eligible_probes_passed": overall["entry_eligible_probes"]
        >= int(gates["minimum_entry_eligible_probes"]),
        "minimum_entry_eligible_per_outcome_passed": all(
            per_outcome[outcome]["entry_eligible_probes"]
            >= int(gates["minimum_entry_eligible_per_outcome"])
            for outcome in ("Up", "Down")
        ),
        "minimum_relative_guard_exit_observations_passed": overall["relative_guard_exits"]
        >= int(gates["minimum_relative_guard_exit_observations"]),
        "minimum_transport_pre_stale_guard_exit_observations_passed": (
            overall["transport_pre_stale_guard_exits"]
            >= int(gates["minimum_transport_pre_stale_guard_exit_observations"])
        ),
        "maximum_transport_guard_book_age_ms_passed": (
            overall["maximum_transport_guard_book_age_ms"]
            <= int(gates["maximum_transport_guard_book_age_ms"])
        ),
        "maximum_transport_guard_seconds_before_target_passed": (
            overall["maximum_transport_guard_seconds_before_target"]
            <= int(gates["maximum_transport_guard_seconds_before_target"])
        ),
        "minimum_scheduled_exit_fraction_passed": overall["scheduled_exit_fraction"]
        >= float(gates["minimum_scheduled_exit_fraction"]),
        "minimum_median_holding_seconds_passed": overall["median_holding_seconds"]
        >= float(gates["minimum_median_holding_seconds"]),
        "required_exit_success_within_retry_rate_passed": overall["exit_success_within_retry_rate"]
        >= float(gates["required_exit_success_within_retry_rate"]),
        "maximum_trapped_positions_passed": overall["trapped_positions"]
        <= int(gates["maximum_trapped_positions"]),
        "maximum_observed_exit_delay_seconds_passed": overall["maximum_observed_exit_delay_seconds"]
        <= int(gates["maximum_observed_exit_delay_seconds"]),
        "minimum_fresh_transport_generations_per_completed_market_passed": minimum_fresh_generations
        >= float(gates["minimum_fresh_transport_generations_per_completed_market"]),
        "minimum_validated_peer_cache_ready_generations_per_completed_market_passed": minimum_cache_ready_generations
        >= float(gates["minimum_validated_peer_cache_ready_generations_per_completed_market"]),
        "minimum_validated_peer_fallback_fresh_recoveries_passed": len(cached_recoveries)
        >= int(gates["minimum_validated_peer_fallback_fresh_recoveries"]),
        "maximum_unrecovered_relevant_transport_incidents_passed": len(unresolved_relevant)
        <= int(gates["maximum_unrecovered_relevant_transport_incidents"]),
        "maximum_relevant_transport_recovery_ms_passed": max(relevant_recovery_ms, default=0)
        <= int(gates["maximum_relevant_transport_recovery_ms"]),
        "required_resolution_contract_coverage_passed": resolution_coverage
        >= float(gates["required_resolution_contract_coverage"]),
        "sqlite_quick_check_passed": quick_check == str(gates["sqlite_quick_check_required"]),
        "full_duration_completion_passed": completion == "FULL_4H_REACHED",
    }
    safety_passed = all((
        meta.get("orders_enabled") is False,
        meta.get("paper_orders_enabled") is False,
        meta.get("wallet_required") is False,
        meta.get("money_real_enabled") is False,
        meta.get("real_money") == "BLOQUEADO",
        meta.get("outcomes_read") == 0,
        meta.get("prices_stored") is False,
        meta.get("pnl_calculated") is False,
        meta.get("signals_generated") is False,
        meta.get("trades_generated") is False,
        meta.get("runtime_peer_ip_stored") is False,
    ))
    source_hashes = {
        key: sha256_file(root / str(record["relative_path"]))
        for key, record in prereg["evidence"].items()
    }
    sources_unchanged = all(
        source_hashes[key] == prereg["evidence"][key]["sha256"]
        for key in source_hashes
    )
    design_record = prereg["design_code"]
    design_unchanged = sha256_file(
        root / str(design_record["relative_path"])
    ) == design_record["sha256"]
    technical_passed = all(gate_results.values())
    if not safety_passed:
        verdict = "FAIL_SAFETY"
    elif not sources_unchanged or not design_unchanged:
        verdict = "FAIL_SOURCE_ARTIFACT_MUTATION"
    elif completion == "FREEZE_FIRST_TRAPPED_POSITION" or overall["trapped_positions"] > 0:
        verdict = "FAIL_FIRST_TRAPPED_POSITION"
    elif unresolved_relevant or not gate_results["maximum_relevant_transport_recovery_ms_passed"]:
        verdict = "FAIL_TRANSPORT_RESILIENCE_ENVELOPE"
    elif not gate_results["minimum_validated_peer_fallback_fresh_recoveries_passed"]:
        verdict = "INCONCLUSIVE_VALIDATED_PEER_FALLBACK_NOT_EXERCISED"
    elif not gate_results["minimum_transport_pre_stale_guard_exit_observations_passed"]:
        verdict = "INCONCLUSIVE_PRE_STALE_TRANSPORT_GUARD_NOT_EXERCISED"
    elif technical_passed:
        verdict = PASS_VERDICT
    elif not gate_results["minimum_relative_guard_exit_observations_passed"]:
        verdict = "INCONCLUSIVE_RELATIVE_GUARD_NOT_EXERCISED"
    else:
        verdict = "FAIL_FRESH_PRE_STALE_TRANSPORT_GUARD_EXIT_SAFETY"

    result = {
        "schema": RESULT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT, "verdict": verdict,
        "meaning": "fresh_pre_stale_transport_guard_capacity_only_no_prices_no_signal_no_pnl",
        "database": str(database_file), "database_sha256": database_hash,
        "database_read_only_verified": query_only,
        "preregistration_sha256": prereg_hash,
        "implementation_sha256": implementation_hash,
        "source_artifacts_unchanged": sources_unchanged,
        "design_code_unchanged": design_unchanged,
        "sqlite_quick_check": quick_check,
        "capture_start_at": meta.get("capture_start_at"),
        "target_end_at": meta.get("target_end_at"),
        "observation_ended_at": meta.get("observation_ended_at"),
        "completion_reason": completion,
        "expected_markets": V040_EXPECTED_MARKETS,
        "discovered_markets": len(markets), "completed_markets": len(completed),
        "market_coverage": market_coverage, "snapshots": rows,
        "expected_snapshots": expected_rows, "snapshot_coverage": snapshot_coverage,
        "complete_v2_snapshots": complete_v2,
        "complete_v2_snapshot_coverage": complete_coverage,
        "resolution_contract_coverage": resolution_coverage,
        "probe_contract": prereg["probe_contract"],
        "transport_contract": prereg["transport_contract"],
        "transport": {
            "reconnect_owner_count": 1, "socket_internal_reconnect": False,
            "fresh_generation_events": len(fresh_events),
            "reconnect_trigger_events": len(trigger_events),
            "recovery_events": len(recovery_events),
            "relevant_incidents": len(relevant_incidents),
            "unrecovered_relevant_incidents": [
                {"condition_id": key[0], "incident_id": key[1]}
                for key in unresolved_relevant
            ],
            "maximum_relevant_recovery_ms": max(relevant_recovery_ms, default=0),
            "minimum_fresh_generations_per_completed_market": minimum_fresh_generations,
            "minimum_cache_ready_generations_per_completed_market": minimum_cache_ready_generations,
            "validated_peer_fresh_dials": len(cached_fresh_events),
            "validated_peer_fallback_recoveries": len(cached_recoveries),
            "validated_peer_failed_dials": len(cached_failures),
            "hostname_dns_gaierrors": len(dns_failures),
            "runtime_peer_ip_persisted": False,
            "events": transport_events,
        },
        "overall": overall, "per_outcome": per_outcome,
        "per_market": per_market, "per_decision_bin": per_bin,
        "planned_holding_distribution": dict(sorted(
            planned_holding_distribution.items(), key=lambda item: int(item[0])
        )),
        "technical_gates": gate_results, "technical_passed": technical_passed,
        "safety_passed": safety_passed,
        "economic_strategy_present": False, "economic_edge_evaluated": False,
        "outcomes_read": 0, "labels_read": 0, "prices_stored": False,
        "pnl_calculated": False, "fees_calculated": False,
        "slippage_calculated": False, "signals_generated": False,
        "trades_generated": False, "orders_created": 0, "paper_orders": 0,
        "wallet_required": False, "real_money": "BLOQUEADO",
        "promotion": {
            "automatic_followup_launch": False, "paper_or_money_candidate": False,
            "economic_strategy_can_be_tested_next": verdict == PASS_VERDICT,
            "fresh_economic_preregistration_required": True,
        },
    }
    if output is not None:
        _write_atomic(output, result)
    return result


__all__ = ["RESULT_SCHEMA", "V040AuditError", "audit_v040"]
