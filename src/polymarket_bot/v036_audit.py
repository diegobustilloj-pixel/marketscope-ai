from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v031_capture import open_read_only
from polymarket_bot.v036_capture import V036_EXPECTED_MARKETS, V036_MARKET_SECONDS
from polymarket_bot.v036_contract import (
    PASS_VERDICT,
    VARIANT,
    aggregate_probe_results,
    evaluate_market_probes,
    load_and_verify_prereg,
)
from polymarket_bot.v036_runner import load_and_verify_implementation


RESULT_SCHEMA = "result_v036_absolute_exit_deadline_4h_1"


class V036AuditError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 8) if denominator else 0.0


def audit_v036(
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
        raise V036AuditError("Base V0.36 no encontrada")
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
        raise V036AuditError("Existe otro resultado V0.36")

    connection = open_read_only(database_file)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        query_only = bool(int(connection.execute("PRAGMA query_only").fetchone()[0]))
        meta = {
            str(row[0]): json.loads(str(row[1]))
            for row in connection.execute("SELECT key,value FROM v036_meta")
        }
        markets = [
            dict(row)
            for row in connection.execute("SELECT * FROM v036_markets ORDER BY market_start_ms")
        ]
        snapshots: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
        complete_v2 = 0
        for row in connection.execute(
            "SELECT * FROM v036_snapshots ORDER BY condition_id,second_offset"
        ):
            values = dict(row)
            snapshots[str(values["condition_id"])][int(values["second_offset"])] = values
            complete_v2 += int(values["complete_v2"])
    finally:
        connection.close()
    completion = meta.get("completion_reason")
    if completion is None:
        raise V036AuditError("V0.36 todavia no termino")

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
        per_market[str(market["slug"])] = aggregate_probe_results(market_probes)
    overall = aggregate_probe_results(probes)
    per_outcome = {
        outcome: aggregate_probe_results(
            [probe for probe in probes if probe["outcome"] == outcome]
        )
        for outcome in ("Up", "Down")
    }
    decision_bins = ((30, 59), (60, 85), (86, 95))
    per_bin = {
        f"{start}_{end}": aggregate_probe_results(
            [probe for probe in probes if start <= int(probe["decision_offset"]) <= end]
        )
        for start, end in decision_bins
    }
    planned_holding_distribution: dict[str, int] = {}
    for probe in probes:
        if "planned_holding_seconds" in probe:
            key = str(int(probe["planned_holding_seconds"]))
            planned_holding_distribution[key] = planned_holding_distribution.get(key, 0) + 1

    rows = sum(len(values) for values in snapshots.values())
    expected_rows = V036_EXPECTED_MARKETS * V036_MARKET_SECONDS
    completed_counts = [len(snapshots.get(str(market["condition_id"]), {})) for market in completed]
    market_coverage = _ratio(len(completed), V036_EXPECTED_MARKETS)
    snapshot_coverage = _ratio(rows, expected_rows)
    complete_coverage = _ratio(complete_v2, rows)
    resolution_coverage = _ratio(
        sum(
            market["resolution_contract_status"] == "VERIFIED"
            and int(market["resolution_twap_window_s"] or 0) in {30, 60}
            for market in markets
        ),
        len(markets),
    )
    gates = prereg["technical_gates"]
    gate_results = {
        "minimum_completed_market_coverage_passed": market_coverage >= float(gates["minimum_completed_market_coverage"]),
        "minimum_snapshot_coverage_passed": snapshot_coverage >= float(gates["minimum_snapshot_coverage"]),
        "minimum_complete_v2_snapshot_coverage_passed": complete_coverage >= float(gates["minimum_complete_v2_snapshot_coverage"]),
        "minimum_snapshots_per_completed_market_passed": bool(completed_counts) and min(completed_counts) >= int(gates["minimum_snapshots_per_completed_market"]),
        "maximum_missing_seconds_per_completed_market_passed": bool(completed_counts) and max(V036_MARKET_SECONDS - count for count in completed_counts) <= int(gates["maximum_missing_seconds_per_completed_market"]),
        "minimum_total_capacity_probes_passed": overall["total_capacity_probes"] >= int(gates["minimum_total_capacity_probes"]),
        "minimum_entry_eligible_probes_passed": overall["entry_eligible_probes"] >= int(gates["minimum_entry_eligible_probes"]),
        "minimum_entry_eligible_per_outcome_passed": all(
            per_outcome[outcome]["entry_eligible_probes"] >= int(gates["minimum_entry_eligible_per_outcome"])
            for outcome in ("Up", "Down")
        ),
        "minimum_relative_guard_exit_observations_passed": overall["relative_guard_exits"] >= int(gates["minimum_relative_guard_exit_observations"]),
        "minimum_scheduled_exit_fraction_passed": overall["scheduled_exit_fraction"] >= float(gates["minimum_scheduled_exit_fraction"]),
        "minimum_median_holding_seconds_passed": overall["median_holding_seconds"] >= float(gates["minimum_median_holding_seconds"]),
        "required_exit_success_within_grace_rate_passed": overall["exit_success_within_grace_rate"] >= float(gates["required_exit_success_within_grace_rate"]),
        "maximum_trapped_positions_passed": overall["trapped_positions"] <= int(gates["maximum_trapped_positions"]),
        "maximum_observed_exit_delay_seconds_passed": overall["maximum_observed_exit_delay_seconds"] <= int(gates["maximum_observed_exit_delay_seconds"]),
        "required_resolution_contract_coverage_passed": resolution_coverage >= float(gates["required_resolution_contract_coverage"]),
        "sqlite_quick_check_passed": quick_check == str(gates["sqlite_quick_check_required"]),
        "full_duration_completion_passed": completion == "FULL_4H_REACHED",
    }
    safety_passed = all((
        meta.get("orders_enabled") is False, meta.get("paper_orders_enabled") is False,
        meta.get("wallet_required") is False, meta.get("money_real_enabled") is False,
        meta.get("real_money") == "BLOQUEADO", meta.get("outcomes_read") == 0,
        meta.get("prices_stored") is False, meta.get("pnl_calculated") is False,
        meta.get("signals_generated") is False, meta.get("trades_generated") is False,
    ))
    source_hashes = {
        key: sha256_file(root / str(record["relative_path"]))
        for key, record in prereg["evidence"].items()
    }
    sources_unchanged = all(
        source_hashes[key] == prereg["evidence"][key]["sha256"] for key in source_hashes
    )
    design_record = prereg["design_code"]
    design_unchanged = sha256_file(root / str(design_record["relative_path"])) == design_record["sha256"]
    technical_passed = all(gate_results.values())
    if not safety_passed:
        verdict = "FAIL_SAFETY"
    elif not sources_unchanged or not design_unchanged:
        verdict = "FAIL_SOURCE_ARTIFACT_MUTATION"
    elif completion == "FREEZE_FIRST_TRAPPED_POSITION" or overall["trapped_positions"] > 0:
        verdict = "FAIL_FIRST_TRAPPED_POSITION"
    elif technical_passed:
        verdict = PASS_VERDICT
    elif not gate_results["minimum_relative_guard_exit_observations_passed"]:
        verdict = "INCONCLUSIVE_RELATIVE_GUARD_NOT_EXERCISED"
    elif not gate_results["minimum_scheduled_exit_fraction_passed"]:
        verdict = "FAIL_VACUOUS_OVERACTIVE_GUARD"
    else:
        verdict = "FAIL_FRESH_ABSOLUTE_EXIT_SAFETY"
    result = {
        "schema": RESULT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "verdict": verdict,
        "meaning": "fresh_absolute_exit_deadline_capacity_only_no_prices_no_signal_no_pnl",
        "database": str(database_file),
        "database_sha256": database_hash,
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
        "expected_markets": V036_EXPECTED_MARKETS,
        "discovered_markets": len(markets),
        "completed_markets": len(completed),
        "market_coverage": market_coverage,
        "snapshots": rows,
        "expected_snapshots": expected_rows,
        "snapshot_coverage": snapshot_coverage,
        "complete_v2_snapshots": complete_v2,
        "complete_v2_snapshot_coverage": complete_coverage,
        "resolution_contract_coverage": resolution_coverage,
        "probe_contract": prereg["probe_contract"],
        "overall": overall,
        "per_outcome": per_outcome,
        "per_market": per_market,
        "per_decision_bin": per_bin,
        "planned_holding_distribution": dict(sorted(planned_holding_distribution.items(), key=lambda item: int(item[0]))),
        "technical_gates": gate_results,
        "technical_passed": technical_passed,
        "safety_passed": safety_passed,
        "economic_strategy_present": False,
        "economic_edge_evaluated": False,
        "outcomes_read": 0,
        "labels_read": 0,
        "prices_stored": False,
        "pnl_calculated": False,
        "fees_calculated": False,
        "slippage_calculated": False,
        "signals_generated": False,
        "trades_generated": False,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
        "promotion": {
            "automatic_followup_launch": False,
            "paper_or_money_candidate": False,
            "economic_strategy_can_be_tested_next": verdict == PASS_VERDICT,
            "fresh_economic_preregistration_required": True,
        },
    }
    if output is not None:
        _write_atomic(output, result)
    return result


__all__ = ["RESULT_SCHEMA", "V036AuditError", "audit_v036"]
