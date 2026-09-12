from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v031_capture import open_read_only
from polymarket_bot.v031_quality_reaudit import classify_snapshot


PREREG_SCHEMA = "prereg_v032_exit_safety_capacity_1"
PREREG_STATUS = "FROZEN_CLOSED_DATA_TECHNICAL_SCREEN"
IMPLEMENTATION_SCHEMA = "implementation_v032_exit_safety_capacity_1"
RESULT_SCHEMA = "result_v032_exit_safety_capacity_1"
VARIANT = "V0.32_INTENTIONAL_SINGLE_POSITION_EXIT_SAFETY"
PASS_VERDICT = "PASS_EXIT_SAFETY_CAPACITY_ONLY"

INPUT_PATHS = {
    "database": "data/capture_v031_path_execution_1h.db",
    "v031r_result": "data/resultado_v031_quality_semantics_reaudit_v2.json",
    "v031r_preregistration": "data/prereg_v031_quality_semantics_reaudit_v2.json",
    "v031r_implementation": "data/implementation_v031_quality_semantics_reaudit_v2.json",
    "v020_hedge_design": "data/diagnostico_v020_hedge_design.json",
    "v021_safe_pair_design": "data/diagnostico_v021_safe_pair_design.json",
    "v021_result": "data/resultado_v021_safe_pair_observer.json",
    "v022_design": "data/diagnostico_v022_sync_persistence_design.json",
    "v022b_result": "data/resultado_v022b_synced_persistent_observer.json",
}
IMPLEMENTATION_FILES = {
    "screen": "src/polymarket_bot/v032_exit_safety.py",
    "entrypoint": "v032_exit_safety_report.py",
    "tests": "tests/test_v032_exit_safety.py",
}

EXPECTED_SCOPE = {
    "purpose": "verify_that_an_intentional_single_position_can_be_closed_early_without_accidental_residual_inventory",
    "posthoc_technical_calibration": True,
    "independent_validation_claim_allowed": False,
    "economic_strategy_present": False,
    "directional_signal_present": False,
    "closed_database_read_only": True,
    "new_capture_hours": 0,
    "maximum_positive_result": PASS_VERDICT,
}
EXPECTED_FAMILY_DECISION = {
    "non_atomic_two_leg_pair_reopened": False,
    "reason": "V0.20_completed_hedges_at_deterministic_loss_and_V0.21_V0.22_did_not_confirm_persistent_simultaneous_pair_opportunities",
    "intentional_position_is_not_accidental_residual": True,
    "position_must_have_precommitted_exit": True,
}
EXPECTED_PROBE_CONTRACT = {
    "market_family": "btc-updown-5m",
    "probe_both_outcomes_independently": True,
    "decision_offset_min_inclusive": 30,
    "decision_offset_max_inclusive": 149,
    "decision_latency_seconds": 1,
    "holding_seconds_after_entry": 30,
    "exit_retry_seconds": 5,
    "shares": 5.0,
    "maximum_book_levels": 5,
    "decision_requires_v2_complete_data": True,
    "decision_requires_all_four_book_sides_depth": True,
    "entry_requires_v2_complete_data": True,
    "entry_requires_selected_ask_depth": True,
    "scheduled_exit_requires_v2_complete_data": False,
    "exit_requires_selected_book_initialized_fresh_and_bid_depth": True,
    "forced_exit_ignores_signal_and_reference_feed_outages": True,
    "synthetic_complement_allowed": False,
    "partial_entry_allowed": False,
    "partial_exit_allowed": False,
    "missing_exit_action": "RETRY_EACH_SECOND_THEN_CLASSIFY_TRAPPED",
    "overlapping_probes_are_capacity_diagnostics_not_trades": True,
}
EXPECTED_GATES = {
    "minimum_total_capacity_probes": 2500,
    "minimum_entry_eligible_probes": 1000,
    "minimum_entry_eligible_per_outcome": 500,
    "minimum_scheduled_exit_success_rate": 0.99,
    "required_exit_success_within_grace_rate": 1.0,
    "maximum_trapped_positions": 0,
    "maximum_observed_exit_delay_seconds": 5,
    "sqlite_quick_check_required": "ok",
    "database_hash_must_remain_unchanged": True,
    "v031r_result_hash_must_remain_unchanged": True,
}
EXPECTED_DIAGNOSTICS = [
    "decision_rejections_by_reason",
    "entry_rejections_by_reason",
    "scheduled_and_grace_exit_success",
    "trapped_positions",
    "exit_delay_distribution",
    "per_outcome",
    "per_market",
    "per_decision_30_second_bin",
]
EXPECTED_DATA_POLICY = {
    "outcomes_read": 0,
    "labels_read": 0,
    "pnl_calculated": False,
    "fees_calculated": False,
    "slippage_calculated": False,
    "signals_generated": False,
    "trades_generated": False,
    "economic_edge_can_be_claimed": False,
    "parameter_grid": False,
    "cross_date_join_allowed": False,
}
EXPECTED_SAFETY = {
    "orders_enabled": False,
    "paper_orders_enabled": False,
    "wallet_required": False,
    "real_money": "BLOQUEADO",
    "automatic_followup_launch": False,
    "scheduled_supervision": False,
}


class V032ExitSafetyError(RuntimeError):
    pass


def _require(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise V032ExitSafetyError(f"V0.32 incompatible: {field}")


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_and_verify_v032_prereg(
    path: str | Path,
    *,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(path).resolve()
    if not prereg_file.is_file():
        raise V032ExitSafetyError("Preinscripcion V0.32 no encontrada")
    payload = json.loads(prereg_file.read_text(encoding="utf-8"))
    expected_sections = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "variant": VARIANT,
        "scope": EXPECTED_SCOPE,
        "family_decision": EXPECTED_FAMILY_DECISION,
        "probe_contract": EXPECTED_PROBE_CONTRACT,
        "technical_gates": EXPECTED_GATES,
        "reported_diagnostics": EXPECTED_DIAGNOSTICS,
        "data_policy": EXPECTED_DATA_POLICY,
        "safety": EXPECTED_SAFETY,
    }
    for key, value in expected_sections.items():
        _require(payload.get(key), value, key)
    inputs = payload.get("inputs")
    if not isinstance(inputs, Mapping) or set(inputs) != set(INPUT_PATHS):
        raise V032ExitSafetyError("Inventario de entradas V0.32 incompatible")
    for key, relative in INPUT_PATHS.items():
        record = inputs.get(key)
        if not isinstance(record, Mapping):
            raise V032ExitSafetyError(f"Entrada V0.32 invalida: {key}")
        _require(record.get("relative_path"), relative, f"inputs.{key}.relative_path")
        source = root / relative
        if not source.is_file():
            raise V032ExitSafetyError(f"Entrada V0.32 ausente: {relative}")
        _require(record.get("sha256"), sha256_file(source), f"inputs.{key}.sha256")
    return dict(payload)


def build_v032_implementation_manifest(
    *,
    prereg_path: str | Path,
    output_path: str | Path,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    output = Path(output_path).resolve()
    prereg = load_and_verify_v032_prereg(prereg_file, project_root=root)
    hashes: dict[str, str] = {}
    for key, relative in IMPLEMENTATION_FILES.items():
        source = root / relative
        if not source.is_file():
            raise V032ExitSafetyError(f"Implementacion V0.32 incompleta: {relative}")
        hashes[key] = sha256_file(source)
    payload = {
        "schema": IMPLEMENTATION_SCHEMA,
        "status": "BUILT_TESTED_READY_FOR_ONE_CLOSED_TECHNICAL_SCREEN",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "preregistration": str(prereg_file),
        "preregistration_sha256": sha256_file(prereg_file),
        "code_hashes": hashes,
        "collector_changed": False,
        "economic_strategy_built": False,
        "directional_signal_built": False,
        "original_database_immutable": True,
        "probe_contract": prereg["probe_contract"],
        "data_policy": prereg["data_policy"],
        "safety": prereg["safety"],
    }
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == IMPLEMENTATION_SCHEMA
            and existing.get("preregistration_sha256") == payload["preregistration_sha256"]
            and existing.get("code_hashes") == hashes
        ):
            return existing
        raise V032ExitSafetyError("Existe otro manifiesto V0.32")
    _write_atomic(output, payload)
    return payload


def load_and_verify_v032_implementation(
    path: str | Path,
    *,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    implementation_file = Path(path).resolve()
    if not implementation_file.is_file():
        raise V032ExitSafetyError("Implementacion V0.32 no sellada")
    payload = json.loads(implementation_file.read_text(encoding="utf-8"))
    _require(payload.get("schema"), IMPLEMENTATION_SCHEMA, "implementation.schema")
    prereg_file = Path(str(payload.get("preregistration") or "")).resolve()
    prereg = load_and_verify_v032_prereg(prereg_file, project_root=root)
    expected = {
        "status": "BUILT_TESTED_READY_FOR_ONE_CLOSED_TECHNICAL_SCREEN",
        "variant": VARIANT,
        "preregistration_sha256": sha256_file(prereg_file),
        "collector_changed": False,
        "economic_strategy_built": False,
        "directional_signal_built": False,
        "original_database_immutable": True,
        "probe_contract": prereg["probe_contract"],
        "data_policy": prereg["data_policy"],
        "safety": prereg["safety"],
    }
    for key, value in expected.items():
        _require(payload.get(key), value, f"implementation.{key}")
    hashes = payload.get("code_hashes")
    if not isinstance(hashes, Mapping) or set(hashes) != set(IMPLEMENTATION_FILES):
        raise V032ExitSafetyError("Inventario de implementacion V0.32 incompatible")
    for key, relative in IMPLEMENTATION_FILES.items():
        _require(hashes.get(key), sha256_file(root / relative), f"code_hashes.{key}")
    return dict(payload)


def _parse_levels(value: Any, *, side: str) -> tuple[bool, list[list[float]]]:
    try:
        raw = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return False, []
    if not isinstance(raw, list) or len(raw) > 5:
        return False, []
    levels: list[list[float]] = []
    for item in raw:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
            return False, []
        try:
            price = float(item[0])
            size = float(item[1])
        except (TypeError, ValueError):
            return False, []
        if not (math.isfinite(price) and 0.0 < price < 1.0):
            return False, []
        if not (math.isfinite(size) and size > 0.0):
            return False, []
        levels.append([price, size])
    prices = [level[0] for level in levels]
    if side == "bid" and any(left <= right for left, right in zip(prices, prices[1:])):
        return False, []
    if side == "ask" and any(left >= right for left, right in zip(prices, prices[1:])):
        return False, []
    return True, levels


def _depth_ready(row: Mapping[str, Any], outcome: str, side: str, shares: float) -> bool:
    valid, levels = _parse_levels(row.get(f"{outcome}_{side}_levels_json"), side=side)
    return valid and sum(level[1] for level in levels) >= shares


def _book_initialized_fresh(row: Mapping[str, Any], outcome: str) -> bool:
    try:
        source = int(row.get(f"{outcome}_book_source_timestamp_ms"))
        received = int(row.get(f"{outcome}_book_received_timestamp_ms"))
        age = int(row.get(f"{outcome}_book_age_ms"))
    except (TypeError, ValueError):
        return False
    return source > 0 and received > 0 and age >= 0 and bool(row.get(f"{outcome}_book_fresh"))


def _v2_complete(row: Mapping[str, Any], resolution_twap_window_s: int) -> bool:
    return bool(
        classify_snapshot(
            row,
            resolution_twap_window_s=resolution_twap_window_s,
        )["data_complete_v2"]
    )


def evaluate_capacity_probe(
    snapshots_by_offset: Mapping[int, Mapping[str, Any]],
    *,
    decision_offset: int,
    outcome: str,
    resolution_twap_window_s: int,
    shares: float = 5.0,
    decision_latency_seconds: int = 1,
    holding_seconds_after_entry: int = 30,
    exit_retry_seconds: int = 5,
) -> dict[str, Any]:
    selected = outcome.lower()
    if selected not in {"up", "down"}:
        raise ValueError("outcome debe ser Up o Down")
    decision = snapshots_by_offset.get(decision_offset)
    if decision is None:
        return {"status": "DECISION_REJECTED", "reason": "DECISION_SNAPSHOT_MISSING"}
    if not _v2_complete(decision, resolution_twap_window_s):
        return {"status": "DECISION_REJECTED", "reason": "DECISION_DATA_INCOMPLETE"}
    if not all(
        _depth_ready(decision, side_outcome, book_side, shares)
        for side_outcome in ("up", "down")
        for book_side in ("bid", "ask")
    ):
        return {"status": "DECISION_REJECTED", "reason": "DECISION_ALL_FOUR_DEPTH_INSUFFICIENT"}

    entry_offset = decision_offset + decision_latency_seconds
    entry = snapshots_by_offset.get(entry_offset)
    if entry is None:
        return {"status": "ENTRY_REJECTED", "reason": "ENTRY_SNAPSHOT_MISSING"}
    if not _v2_complete(entry, resolution_twap_window_s):
        return {"status": "ENTRY_REJECTED", "reason": "ENTRY_DATA_INCOMPLETE"}
    if not _depth_ready(entry, selected, "ask", shares):
        return {"status": "ENTRY_REJECTED", "reason": "ENTRY_ASK_DEPTH_INSUFFICIENT"}

    target_exit_offset = entry_offset + holding_seconds_after_entry
    exit_rejections: Counter[str] = Counter()
    for exit_offset in range(target_exit_offset, target_exit_offset + exit_retry_seconds + 1):
        exit_snapshot = snapshots_by_offset.get(exit_offset)
        if exit_snapshot is None:
            exit_rejections["EXIT_SNAPSHOT_MISSING"] += 1
            continue
        if not _book_initialized_fresh(exit_snapshot, selected):
            exit_rejections["EXIT_BOOK_NOT_FRESH"] += 1
            continue
        if not _depth_ready(exit_snapshot, selected, "bid", shares):
            exit_rejections["EXIT_BID_DEPTH_INSUFFICIENT"] += 1
            continue
        return {
            "status": "EXITED",
            "reason": None,
            "entry_offset": entry_offset,
            "target_exit_offset": target_exit_offset,
            "exit_offset": exit_offset,
            "exit_delay_seconds": exit_offset - target_exit_offset,
            "exit_rejections": dict(exit_rejections),
        }
    return {
        "status": "TRAPPED",
        "reason": "NO_FRESH_FULL_DEPTH_BID_WITHIN_GRACE",
        "entry_offset": entry_offset,
        "target_exit_offset": target_exit_offset,
        "exit_offset": None,
        "exit_delay_seconds": None,
        "exit_rejections": dict(exit_rejections),
    }


def _rate(count: int, denominator: int) -> float:
    return round(count / denominator, 8) if denominator else 0.0


def _aggregate_probe_results(probes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(probes)
    decision_rejections = Counter(
        str(probe["reason"])
        for probe in probes
        if probe["status"] == "DECISION_REJECTED"
    )
    entry_rejections = Counter(
        str(probe["reason"])
        for probe in probes
        if probe["status"] == "ENTRY_REJECTED"
    )
    entered = [probe for probe in probes if probe["status"] in {"EXITED", "TRAPPED"}]
    exited = [probe for probe in entered if probe["status"] == "EXITED"]
    scheduled = [probe for probe in exited if int(probe["exit_delay_seconds"]) == 0]
    trapped = [probe for probe in entered if probe["status"] == "TRAPPED"]
    delays = Counter(int(probe["exit_delay_seconds"]) for probe in exited)
    maximum_delay = max(delays, default=0)
    return {
        "total_capacity_probes": total,
        "decision_eligible_probes": total - sum(decision_rejections.values()),
        "entry_eligible_probes": len(entered),
        "scheduled_exit_successes": len(scheduled),
        "scheduled_exit_success_rate": _rate(len(scheduled), len(entered)),
        "exit_successes_within_grace": len(exited),
        "exit_success_within_grace_rate": _rate(len(exited), len(entered)),
        "trapped_positions": len(trapped),
        "maximum_observed_exit_delay_seconds": maximum_delay,
        "decision_rejections_by_reason": dict(sorted(decision_rejections.items())),
        "entry_rejections_by_reason": dict(sorted(entry_rejections.items())),
        "exit_delay_distribution": {
            str(delay): count for delay, count in sorted(delays.items())
        },
    }


def run_v032_exit_safety_screen(
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
    prereg = load_and_verify_v032_prereg(prereg_file, project_root=root)
    load_and_verify_v032_implementation(implementation_file, project_root=root)
    inputs = {
        key: root / str(record["relative_path"])
        for key, record in prereg["inputs"].items()
    }
    database = inputs["database"]
    v031r_path = inputs["v031r_result"]
    database_hash_before = sha256_file(database)
    v031r_hash_before = sha256_file(v031r_path)
    prereg_hash = sha256_file(prereg_file)
    implementation_hash = sha256_file(implementation_file)
    if output is not None and output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == RESULT_SCHEMA
            and existing.get("database_sha256") == database_hash_before
            and existing.get("v031r_result_sha256") == v031r_hash_before
            and existing.get("preregistration_sha256") == prereg_hash
            and existing.get("implementation_sha256") == implementation_hash
        ):
            return existing
        raise V032ExitSafetyError("Existe otro resultado V0.32")

    v031r = json.loads(v031r_path.read_text(encoding="utf-8"))
    _require(
        v031r.get("verdict"),
        "PASS_TECHNICAL_CAPTURE_ONLY_REAUDITED_SEMANTICS_V2",
        "v031r.verdict",
    )
    _require(v031r.get("safety_passed"), True, "v031r.safety_passed")
    contract = prereg["probe_contract"]
    decision_min = int(contract["decision_offset_min_inclusive"])
    decision_max = int(contract["decision_offset_max_inclusive"])
    connection = open_read_only(database)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        query_only = bool(int(connection.execute("PRAGMA query_only").fetchone()[0]))
        meta = {
            str(row[0]): json.loads(str(row[1]))
            for row in connection.execute("SELECT key,value FROM v031_meta")
        }
        markets = [
            dict(row)
            for row in connection.execute(
                """
                SELECT condition_id,slug,market_start_ms,resolution_contract_status,
                 resolution_twap_window_s,capture_status
                FROM v031_markets ORDER BY market_start_ms
                """
            )
        ]
        snapshots: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
        for row in connection.execute(
            "SELECT * FROM v031_snapshots ORDER BY condition_id,second_offset"
        ):
            values = dict(row)
            snapshots[str(values["condition_id"])][int(values["second_offset"])] = values
    finally:
        connection.close()

    probes: list[dict[str, Any]] = []
    for market in markets:
        condition_id = str(market["condition_id"])
        market_snapshots = snapshots.get(condition_id, {})
        resolution_window = int(market["resolution_twap_window_s"])
        for decision_offset in range(decision_min, decision_max + 1):
            for outcome in ("Up", "Down"):
                probe = evaluate_capacity_probe(
                    market_snapshots,
                    decision_offset=decision_offset,
                    outcome=outcome,
                    resolution_twap_window_s=resolution_window,
                    shares=float(contract["shares"]),
                    decision_latency_seconds=int(contract["decision_latency_seconds"]),
                    holding_seconds_after_entry=int(contract["holding_seconds_after_entry"]),
                    exit_retry_seconds=int(contract["exit_retry_seconds"]),
                )
                probes.append(
                    {
                        **probe,
                        "condition_id": condition_id,
                        "slug": str(market["slug"]),
                        "outcome": outcome,
                        "decision_offset": decision_offset,
                    }
                )

    overall = _aggregate_probe_results(probes)
    per_outcome = {
        outcome: _aggregate_probe_results(
            [probe for probe in probes if probe["outcome"] == outcome]
        )
        for outcome in ("Up", "Down")
    }
    per_market = {
        str(market["slug"]): _aggregate_probe_results(
            [probe for probe in probes if probe["condition_id"] == market["condition_id"]]
        )
        for market in markets
    }
    per_bin: dict[str, dict[str, Any]] = {}
    for start in range(decision_min, decision_max + 1, 30):
        end = min(decision_max, start + 29)
        per_bin[f"{start}_{end}"] = _aggregate_probe_results(
            [probe for probe in probes if start <= int(probe["decision_offset"]) <= end]
        )

    database_hash_after = sha256_file(database)
    v031r_hash_after = sha256_file(v031r_path)
    gates = prereg["technical_gates"]
    technical_gates = {
        "minimum_total_capacity_probes_passed": overall["total_capacity_probes"]
        >= int(gates["minimum_total_capacity_probes"]),
        "minimum_entry_eligible_probes_passed": overall["entry_eligible_probes"]
        >= int(gates["minimum_entry_eligible_probes"]),
        "minimum_entry_eligible_per_outcome_passed": all(
            per_outcome[outcome]["entry_eligible_probes"]
            >= int(gates["minimum_entry_eligible_per_outcome"])
            for outcome in ("Up", "Down")
        ),
        "minimum_scheduled_exit_success_rate_passed": overall[
            "scheduled_exit_success_rate"
        ]
        >= float(gates["minimum_scheduled_exit_success_rate"]),
        "required_exit_success_within_grace_rate_passed": overall[
            "exit_success_within_grace_rate"
        ]
        >= float(gates["required_exit_success_within_grace_rate"]),
        "maximum_trapped_positions_passed": overall["trapped_positions"]
        <= int(gates["maximum_trapped_positions"]),
        "maximum_observed_exit_delay_seconds_passed": overall[
            "maximum_observed_exit_delay_seconds"
        ]
        <= int(gates["maximum_observed_exit_delay_seconds"]),
        "sqlite_quick_check_passed": quick_check
        == str(gates["sqlite_quick_check_required"]),
        "database_hash_unchanged_passed": database_hash_after == database_hash_before,
        "v031r_result_hash_unchanged_passed": v031r_hash_after == v031r_hash_before,
        "market_contracts_and_capture_status_passed": bool(markets)
        and all(
            market["resolution_contract_status"] == "VERIFIED"
            and market["capture_status"] == "COMPLETED"
            for market in markets
        ),
    }
    safety_passed = all(
        (
            meta.get("orders_enabled") is False,
            meta.get("paper_orders_enabled") is False,
            meta.get("wallet_required") is False,
            meta.get("real_money") == "BLOQUEADO",
            meta.get("outcomes_read") == 0,
            meta.get("pnl_calculated") is False,
            meta.get("signals_generated") is False,
        )
    )
    technical_passed = all(technical_gates.values())
    if not safety_passed:
        verdict = "FAIL_SAFETY"
    elif technical_passed:
        verdict = PASS_VERDICT
    else:
        verdict = "FAIL_EXIT_SAFETY_CAPACITY"
    result = {
        "schema": RESULT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "verdict": verdict,
        "meaning": "posthoc_exit_capacity_only_no_signal_no_pnl",
        "posthoc_technical_calibration": True,
        "independent_validation_claim": False,
        "database": str(database),
        "database_sha256": database_hash_before,
        "database_sha256_after": database_hash_after,
        "database_read_only_verified": query_only,
        "v031r_result": str(v031r_path),
        "v031r_result_sha256": v031r_hash_before,
        "v031r_result_sha256_after": v031r_hash_after,
        "preregistration_sha256": prereg_hash,
        "implementation_sha256": implementation_hash,
        "sqlite_quick_check": quick_check,
        "markets": len(markets),
        "probe_contract": contract,
        "overall": overall,
        "per_outcome": per_outcome,
        "per_market": per_market,
        "per_decision_30_second_bin": per_bin,
        "technical_gates": technical_gates,
        "technical_passed": technical_passed,
        "safety_passed": safety_passed,
        "economic_strategy_present": False,
        "directional_signal_present": False,
        "economic_edge_evaluated": False,
        "outcomes_read": 0,
        "labels_read": 0,
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
            "fresh_economic_preregistration_required": True,
        },
    }
    if output is not None:
        _write_atomic(output, result)
    return result


__all__ = [
    "PASS_VERDICT",
    "RESULT_SCHEMA",
    "V032ExitSafetyError",
    "build_v032_implementation_manifest",
    "evaluate_capacity_probe",
    "load_and_verify_v032_implementation",
    "load_and_verify_v032_prereg",
    "run_v032_exit_safety_screen",
]
