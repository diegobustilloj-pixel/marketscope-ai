from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v031_capture import open_read_only
from polymarket_bot.v043_chainlink_outage_design import analyze_v042_outage
from polymarket_bot.v043_contract import (
    PASS_VERDICT,
    VARIANT,
    load_and_verify_prereg,
)
from polymarket_bot.v043_runner import load_and_verify_implementation


RESULT_SCHEMA = "result_v043_chainlink_outage_fail_closed_audit_1"


class V043AuditError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def audit_v043(
    *,
    prereg_path: str | Path,
    implementation_path: str | Path,
    result_path: str | Path | None = None,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    implementation_file = Path(implementation_path).resolve()
    output = Path(result_path).resolve() if result_path is not None else None
    prereg = load_and_verify_prereg(prereg_file, project_root=root)
    load_and_verify_implementation(implementation_file, project_root=root)
    prereg_hash = sha256_file(prereg_file)
    implementation_hash = sha256_file(implementation_file)
    if output is not None and output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == RESULT_SCHEMA
            and existing.get("preregistration_sha256") == prereg_hash
            and existing.get("implementation_sha256") == implementation_hash
        ):
            return existing
        raise V043AuditError("Existe otro resultado V0.43")

    evidence_paths = {
        key: root / str(record["relative_path"])
        for key, record in prereg["evidence"].items()
    }
    source_hashes = {
        key: sha256_file(path) for key, path in evidence_paths.items()
    }
    sources_unchanged = all(
        source_hashes[key] == prereg["evidence"][key]["sha256"]
        for key in source_hashes
    )
    design_record = prereg["design_code"]
    design_unchanged = sha256_file(
        root / str(design_record["relative_path"])
    ) == design_record["sha256"]
    database = evidence_paths["v042_database"]
    v042_result_path = evidence_paths["v042_result"]
    v042_prereg_path = evidence_paths["v042_preregistration"]
    analysis = analyze_v042_outage(
        database=database,
        result=v042_result_path,
        v042_preregistration=v042_prereg_path,
    )
    v042_result = json.loads(v042_result_path.read_text(encoding="utf-8"))
    connection = open_read_only(database)
    try:
        query_only = bool(int(connection.execute("PRAGMA query_only").fetchone()[0]))
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        connection.close()

    gates = prereg["technical_gates"]
    fail_closed = analysis["fail_closed_entry"]
    exit_safety = analysis["chainlink_independent_exit"]
    outage = analysis["outage"]
    blocked = int(fail_closed["stale_decision_capacity_probes_blocked"]) + int(
        fail_closed["stale_entry_capacity_probes_blocked"]
    )
    failed_v042_gates = sorted(
        key
        for key, passed in v042_result["technical_gates"].items()
        if not passed
    )
    provider_liveness_failed_and_preserved = (
        v042_result.get("verdict") == "FAIL_CHAINLINK_LIVENESS"
        and failed_v042_gates
        == prereg["audit_contract"]["required_exact_failed_v042_gates"]
        and int(outage["longest"]["seconds"])
        > int(
            json.loads(v042_prereg_path.read_text(encoding="utf-8"))[
                "technical_gates"
            ]["maximum_chainlink_stale_streak_seconds"]
        )
    )
    gate_results = {
        "minimum_completed_markets_passed": int(
            v042_result.get("completed_markets", 0)
        )
        >= int(gates["minimum_completed_markets"]),
        "minimum_longest_chainlink_stale_streak_seconds_passed": int(
            outage["longest"]["seconds"]
        )
        >= int(gates["minimum_longest_chainlink_stale_streak_seconds"]),
        "required_watchdog_reconnect_accounting_rate_passed": float(
            outage["watchdog_reconnect_accounting_rate"]
        )
        >= float(gates["required_watchdog_reconnect_accounting_rate"]),
        "maximum_capacity_entries_using_stale_chainlink_passed": len(
            fail_closed["capacity_entry_violations"]
        )
        <= int(gates["maximum_capacity_entries_using_stale_chainlink"]),
        "minimum_capacity_probes_blocked_by_stale_chainlink_passed": blocked
        >= int(gates["minimum_capacity_probes_blocked_by_stale_chainlink"]),
        "minimum_capacity_positions_open_during_stale_chainlink_passed": int(
            exit_safety["capacity_positions_open_during_any_stale_run"]
        )
        >= int(gates["minimum_capacity_positions_open_during_stale_chainlink"]),
        "required_capacity_exit_success_rate_during_stale_chainlink_passed": float(
            exit_safety["exit_success_rate"]
        )
        >= float(gates["required_capacity_exit_success_rate_during_stale_chainlink"]),
        "minimum_exits_completed_while_chainlink_stale_passed": int(
            exit_safety["exits_completed_while_chainlink_stale"]
        )
        >= int(gates["minimum_exits_completed_while_chainlink_stale"]),
        "maximum_trapped_positions_passed": int(
            exit_safety["trapped_positions"]
        )
        <= int(gates["maximum_trapped_positions"]),
        "maximum_exit_delay_seconds_passed": int(
            exit_safety["maximum_exit_delay_seconds"]
        )
        <= int(gates["maximum_exit_delay_seconds"]),
        "required_v042_safety_passed": v042_result.get("safety_passed")
        is bool(gates["required_v042_safety_passed"]),
        "required_source_artifacts_unchanged_passed": sources_unchanged
        is bool(gates["required_source_artifacts_unchanged"]),
        "required_design_code_unchanged_passed": design_unchanged
        is bool(gates["required_design_code_unchanged"]),
        "provider_liveness_failure_preserved_passed": (
            provider_liveness_failed_and_preserved
        ),
        "database_opened_read_only_passed": query_only,
        "sqlite_quick_check_passed": quick_check
        == prereg["audit_contract"]["sqlite_quick_check_required"],
    }
    safety_passed = all(
        (
            prereg["safety"]["orders_enabled"] is False,
            prereg["safety"]["paper_orders_enabled"] is False,
            prereg["safety"]["wallet_required"] is False,
            prereg["safety"]["real_money"] == "BLOQUEADO",
            v042_result.get("orders_created") == 0,
            v042_result.get("paper_orders") == 0,
            v042_result.get("wallet_required") is False,
            v042_result.get("real_money") == "BLOQUEADO",
            v042_result.get("outcomes_read") == 0,
            v042_result.get("prices_stored") is False,
            v042_result.get("pnl_calculated") is False,
        )
    )
    technical_passed = all(gate_results.values())
    if not safety_passed:
        verdict = "FAIL_SAFETY"
    elif not sources_unchanged or not design_unchanged:
        verdict = "FAIL_SOURCE_ARTIFACT_MUTATION"
    elif not provider_liveness_failed_and_preserved:
        verdict = "INCONCLUSIVE_PROVIDER_OUTAGE_NOT_EXERCISED_AS_FROZEN"
    elif technical_passed:
        verdict = PASS_VERDICT
    elif fail_closed["capacity_entry_violations"]:
        verdict = "FAIL_STALE_REFERENCE_ENTRY_NOT_BLOCKED"
    elif float(exit_safety["exit_success_rate"]) < 1.0:
        verdict = "FAIL_REFERENCE_INDEPENDENT_EXIT_SAFETY"
    else:
        verdict = "FAIL_BOT_CONTROLLED_OUTAGE_SAFETY"

    result = {
        "schema": RESULT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "verdict": verdict,
        "meaning": "bot_controlled_fail_closed_outage_safety_only_not_provider_liveness_or_economic_edge",
        "preregistration_sha256": prereg_hash,
        "implementation_sha256": implementation_hash,
        "source_artifacts_unchanged": sources_unchanged,
        "source_hashes": source_hashes,
        "design_code_unchanged": design_unchanged,
        "database_read_only_verified": query_only,
        "sqlite_quick_check": quick_check,
        "v042_official_result": {
            "verdict": v042_result.get("verdict"),
            "verdict_preserved": True,
            "failed_technical_gates": failed_v042_gates,
            "provider_liveness_within_twenty_seconds": False,
            "provider_liveness_claimed_by_v043": False,
        },
        "outage": outage,
        "fail_closed_entry": fail_closed,
        "chainlink_independent_exit": exit_safety,
        "technical_gates": gate_results,
        "technical_passed": technical_passed,
        "safety_passed": safety_passed,
        "economic_strategy_present": False,
        "economic_edge_evaluated": False,
        "outcomes_read": 0,
        "labels_read": 0,
        "prices_stored": False,
        "pnl_calculated": False,
        "signals_generated": False,
        "trades_generated": False,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
        "limitations": prereg["limitations"],
        "promotion": {
            "repeat_v042_unchanged": False,
            "automatic_followup_launch": False,
            "paper_or_money_candidate": False,
            "fresh_economic_preregistration_allowed": verdict == PASS_VERDICT,
            "fresh_economic_preregistration_required": True,
            "provider_redundancy_still_required_before_real_money": True,
        },
    }
    if output is not None:
        _write_atomic(output, result)
    return result


__all__ = ["RESULT_SCHEMA", "V043AuditError", "audit_v043"]
