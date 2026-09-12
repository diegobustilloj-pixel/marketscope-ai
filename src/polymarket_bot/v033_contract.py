from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file


PREREG_SCHEMA = "prereg_v033_fresh_exit_safety_4h_1"
PREREG_STATUS = (
    "FROZEN_FRESH_TECHNICAL_REPLICATION_AWAITING_IMPLEMENTATION_AND_LAUNCH_APPROVAL"
)
VARIANT = "V0.33_FRESH_EXIT_SAFETY_REPLICATION_4H"
PASS_VERDICT = "PASS_FRESH_EXIT_SAFETY_CAPACITY_ONLY"
EXPECTED_EVIDENCE = {
    "v031r_result": "data/resultado_v031_quality_semantics_reaudit_v2.json",
    "v032_result": "data/resultado_v032_exit_safety_capacity.json",
    "v032_preregistration": "data/prereg_v032_exit_safety_capacity.json",
    "v032_implementation": "data/implementation_v032_exit_safety_capacity.json",
}
EXPECTED_REUSE = {
    "v031_capture_state": "src/polymarket_bot/v031_capture.py",
    "v031_network_runner": "src/polymarket_bot/v031_runner.py",
}
EXPECTED_CAPTURE_CONTRACT = {
    "market_family": "btc-updown-5m",
    "fresh_capture_hours": 4.0,
    "expected_five_minute_markets": 48,
    "snapshot_frequency_hz": 1,
    "market_seconds": 300,
    "maximum_book_levels": 5,
    "freshness_max_age_ms": 5000,
    "chainlink_topic": "crypto_prices_chainlink",
    "official_resolution_twap_windows_s": [30, 60],
    "official_twap_window_must_match_market_resolution_contract": True,
    "single_database_contains_all_required_streams": True,
    "store_reference_prices": False,
    "store_order_book_prices": False,
    "store_only_timestamps_freshness_and_top5_depth": True,
}
EXPECTED_PROBE_CONTRACT = {
    "probe_both_outcomes_independently": True,
    "decision_offset_min_inclusive": 30,
    "decision_offset_max_inclusive": 119,
    "decision_latency_seconds": 1,
    "holding_seconds_after_entry": 30,
    "exit_grace_seconds": 5,
    "position_shares": 5.0,
    "entry_depth_buffer_shares": 10.0,
    "decision_requires_v2_complete_data": True,
    "decision_requires_all_four_book_sides_at_buffer": True,
    "entry_requires_v2_complete_data": True,
    "entry_requires_all_four_book_sides_at_buffer": True,
    "exit_checks_each_second_from_entry_plus_one": True,
    "exit_requires_selected_book_fresh_and_selected_bid_depth_at_position_size": True,
    "proactive_guard_before_target": (
        "EXIT_IF_SELECTED_BID_DEPTH_BELOW_BUFFER_OR_ANY_BOOK_SIDE_BELOW_POSITION_SIZE"
    ),
    "proactive_guard_requires_selected_bid_depth_at_position_size": True,
    "forced_exit_at_target_ignores_signal_chainlink_and_twap": True,
    "missing_target_exit_action": (
        "RETRY_EACH_SECOND_UNTIL_TARGET_PLUS_GRACE_THEN_CLASSIFY_TRAPPED"
    ),
    "synthetic_complement_allowed": False,
    "partial_entry_allowed": False,
    "partial_exit_allowed": False,
    "overlapping_probes_are_capacity_diagnostics_not_trades": True,
    "prices_costs_fees_slippage_and_pnl_are_out_of_scope": True,
}
EXPECTED_SAFETY = {
    "orders_enabled": False,
    "paper_orders_enabled": False,
    "wallet_required": False,
    "real_money": "BLOQUEADO",
    "maximum_database_gb": 1.0,
    "minimum_free_disk_gb": 20.0,
}


class V033ContractError(RuntimeError):
    pass


def _require(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise V033ContractError(f"V0.33 incompatible: {field}")


def load_and_verify_prereg(
    path: str | Path,
    *,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(path).resolve()
    if not prereg_file.is_file():
        raise V033ContractError("Preinscripcion V0.33 no encontrada")
    payload = json.loads(prereg_file.read_text(encoding="utf-8"))
    _require(payload.get("schema"), PREREG_SCHEMA, "schema")
    _require(payload.get("status"), PREREG_STATUS, "status")
    _require(payload.get("variant"), VARIANT, "variant")
    _require(payload.get("capture_contract"), EXPECTED_CAPTURE_CONTRACT, "capture_contract")
    _require(payload.get("probe_contract"), EXPECTED_PROBE_CONTRACT, "probe_contract")
    _require(payload.get("safety"), EXPECTED_SAFETY, "safety")
    _require(payload.get("scope", {}).get("maximum_positive_result"), PASS_VERDICT, "scope.maximum_positive_result")
    _require(payload.get("scope", {}).get("economic_strategy_present"), False, "scope.economic_strategy_present")
    _require(payload.get("scope", {}).get("directional_signal_present"), False, "scope.directional_signal_present")
    _require(payload.get("stopping", {}).get("maximum_hours"), 4.0, "stopping.maximum_hours")
    _require(payload.get("stopping", {}).get("scheduled_supervision"), False, "stopping.scheduled_supervision")
    _require(payload.get("stopping", {}).get("automatic_final_audit"), True, "stopping.automatic_final_audit")
    _require(payload.get("data_policy", {}).get("outcomes_read"), 0, "data_policy.outcomes_read")
    _require(payload.get("data_policy", {}).get("pnl_calculated"), False, "data_policy.pnl_calculated")
    _require(payload.get("data_policy", {}).get("prices_stored"), False, "data_policy.prices_stored")

    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(EXPECTED_EVIDENCE):
        raise V033ContractError("Inventario de evidencia V0.33 incompatible")
    for key, relative in EXPECTED_EVIDENCE.items():
        record = evidence.get(key)
        if not isinstance(record, Mapping):
            raise V033ContractError(f"Evidencia V0.33 invalida: {key}")
        _require(record.get("relative_path"), relative, f"evidence.{key}.relative_path")
        source = root / relative
        if not source.is_file():
            raise V033ContractError(f"Evidencia V0.33 ausente: {relative}")
        _require(record.get("sha256"), sha256_file(source), f"evidence.{key}.sha256")

    reuse = payload.get("compatible_reuse")
    if not isinstance(reuse, Mapping):
        raise V033ContractError("Reutilizacion V0.33 invalida")
    for key, relative in EXPECTED_REUSE.items():
        record = reuse.get(key)
        if not isinstance(record, Mapping):
            raise V033ContractError(f"Reutilizacion V0.33 invalida: {key}")
        _require(record.get("relative_path"), relative, f"compatible_reuse.{key}.relative_path")
        _require(record.get("sha256"), sha256_file(root / relative), f"compatible_reuse.{key}.sha256")
    _require(reuse.get("reuse_is_read_only"), True, "compatible_reuse.reuse_is_read_only")
    _require(
        reuse.get("v031_v031r_v032_artifacts_must_remain_unchanged"),
        True,
        "compatible_reuse.v031_v031r_v032_artifacts_must_remain_unchanged",
    )
    return dict(payload)


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _depth(row: Mapping[str, Any], outcome: str, side: str) -> float:
    value = _finite_number(row.get(f"{outcome}_{side}_depth_top5"))
    return max(0.0, value) if value is not None else 0.0


def _all_four_depth_ready(row: Mapping[str, Any], required: float) -> bool:
    return all(
        _depth(row, outcome, side) >= required
        for outcome in ("up", "down")
        for side in ("bid", "ask")
    )


def _selected_exit_ready(row: Mapping[str, Any], outcome: str, shares: float) -> bool:
    return bool(row.get(f"{outcome}_book_fresh")) and _depth(row, outcome, "bid") >= shares


def evaluate_fresh_capacity_probe(
    snapshots_by_offset: Mapping[int, Mapping[str, Any]],
    *,
    decision_offset: int,
    outcome: str,
    position_shares: float = 5.0,
    entry_depth_buffer_shares: float = 10.0,
    decision_latency_seconds: int = 1,
    holding_seconds_after_entry: int = 30,
    exit_grace_seconds: int = 5,
) -> dict[str, Any]:
    selected = outcome.lower()
    if selected not in {"up", "down"}:
        raise ValueError("outcome debe ser Up o Down")
    if not (0.0 < position_shares <= entry_depth_buffer_shares):
        raise ValueError("El buffer debe cubrir la posicion")

    decision = snapshots_by_offset.get(decision_offset)
    if decision is None:
        return {"status": "DECISION_REJECTED", "reason": "DECISION_SNAPSHOT_MISSING"}
    if not bool(decision.get("complete_v2")):
        return {"status": "DECISION_REJECTED", "reason": "DECISION_DATA_INCOMPLETE"}
    if not _all_four_depth_ready(decision, entry_depth_buffer_shares):
        return {"status": "DECISION_REJECTED", "reason": "DECISION_BUFFER_DEPTH_INSUFFICIENT"}

    entry_offset = decision_offset + decision_latency_seconds
    entry = snapshots_by_offset.get(entry_offset)
    if entry is None:
        return {"status": "ENTRY_REJECTED", "reason": "ENTRY_SNAPSHOT_MISSING"}
    if not bool(entry.get("complete_v2")):
        return {"status": "ENTRY_REJECTED", "reason": "ENTRY_DATA_INCOMPLETE"}
    if not _all_four_depth_ready(entry, entry_depth_buffer_shares):
        return {"status": "ENTRY_REJECTED", "reason": "ENTRY_BUFFER_DEPTH_INSUFFICIENT"}

    target_exit_offset = entry_offset + holding_seconds_after_entry
    exit_rejections: Counter[str] = Counter()
    for exit_offset in range(entry_offset + 1, target_exit_offset + exit_grace_seconds + 1):
        snapshot = snapshots_by_offset.get(exit_offset)
        if snapshot is None:
            exit_rejections["EXIT_SNAPSHOT_MISSING"] += 1
            continue
        exit_ready = _selected_exit_ready(snapshot, selected, position_shares)
        if exit_offset < target_exit_offset:
            guard_reasons: list[str] = []
            if _depth(snapshot, selected, "bid") < entry_depth_buffer_shares:
                guard_reasons.append("SELECTED_BID_BELOW_BUFFER")
            if not _all_four_depth_ready(snapshot, position_shares):
                guard_reasons.append("ANY_BOOK_SIDE_BELOW_POSITION_SIZE")
            if guard_reasons and exit_ready:
                return {
                    "status": "EXITED",
                    "reason": None,
                    "exit_kind": "PROACTIVE_GUARD",
                    "guard_reasons": guard_reasons,
                    "entry_offset": entry_offset,
                    "target_exit_offset": target_exit_offset,
                    "exit_offset": exit_offset,
                    "exit_delay_seconds": exit_offset - target_exit_offset,
                    "exit_rejections": dict(exit_rejections),
                }
            if guard_reasons and not exit_ready:
                exit_rejections["GUARD_TRIGGERED_BUT_SELECTED_EXIT_UNAVAILABLE"] += 1
            continue

        if not bool(snapshot.get(f"{selected}_book_fresh")):
            exit_rejections["EXIT_BOOK_NOT_FRESH"] += 1
            continue
        if _depth(snapshot, selected, "bid") < position_shares:
            exit_rejections["EXIT_BID_DEPTH_INSUFFICIENT"] += 1
            continue
        return {
            "status": "EXITED",
            "reason": None,
            "exit_kind": "SCHEDULED" if exit_offset == target_exit_offset else "GRACE_RETRY",
            "guard_reasons": [],
            "entry_offset": entry_offset,
            "target_exit_offset": target_exit_offset,
            "exit_offset": exit_offset,
            "exit_delay_seconds": exit_offset - target_exit_offset,
            "exit_rejections": dict(exit_rejections),
        }

    return {
        "status": "TRAPPED",
        "reason": "NO_FRESH_FULL_DEPTH_SELECTED_BID_BY_TARGET_PLUS_GRACE",
        "exit_kind": None,
        "guard_reasons": [],
        "entry_offset": entry_offset,
        "target_exit_offset": target_exit_offset,
        "exit_offset": None,
        "exit_delay_seconds": None,
        "exit_rejections": dict(exit_rejections),
    }


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 8) if denominator else 0.0


def aggregate_probe_results(probes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    decision_rejections = Counter(
        str(probe.get("reason"))
        for probe in probes
        if probe.get("status") == "DECISION_REJECTED"
    )
    entry_rejections = Counter(
        str(probe.get("reason"))
        for probe in probes
        if probe.get("status") == "ENTRY_REJECTED"
    )
    entered = [probe for probe in probes if probe.get("status") in {"EXITED", "TRAPPED"}]
    exited = [probe for probe in entered if probe.get("status") == "EXITED"]
    trapped = [probe for probe in entered if probe.get("status") == "TRAPPED"]
    exit_kinds = Counter(str(probe.get("exit_kind")) for probe in exited)
    guard_reasons = Counter(
        reason
        for probe in exited
        for reason in probe.get("guard_reasons", [])
    )
    delays = Counter(int(probe["exit_delay_seconds"]) for probe in exited)
    nonnegative_delays = [delay for delay in delays if delay >= 0]
    return {
        "total_capacity_probes": len(probes),
        "decision_eligible_probes": len(probes) - sum(decision_rejections.values()),
        "entry_eligible_probes": len(entered),
        "exit_successes_within_grace": len(exited),
        "exit_success_within_grace_rate": _rate(len(exited), len(entered)),
        "proactive_guard_exits": exit_kinds.get("PROACTIVE_GUARD", 0),
        "scheduled_exits": exit_kinds.get("SCHEDULED", 0),
        "grace_retry_exits": exit_kinds.get("GRACE_RETRY", 0),
        "trapped_positions": len(trapped),
        "maximum_observed_exit_delay_seconds": max(nonnegative_delays, default=0),
        "decision_rejections_by_reason": dict(sorted(decision_rejections.items())),
        "entry_rejections_by_reason": dict(sorted(entry_rejections.items())),
        "exit_kind_distribution": dict(sorted(exit_kinds.items())),
        "guard_reason_distribution": dict(sorted(guard_reasons.items())),
        "exit_delay_distribution": {str(key): value for key, value in sorted(delays.items())},
    }


def evaluate_market_probes(
    snapshots_by_offset: Mapping[int, Mapping[str, Any]],
    *,
    condition_id: str,
    slug: str,
    contract: Mapping[str, Any] = EXPECTED_PROBE_CONTRACT,
) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    for decision_offset in range(
        int(contract["decision_offset_min_inclusive"]),
        int(contract["decision_offset_max_inclusive"]) + 1,
    ):
        for outcome in ("Up", "Down"):
            probes.append(
                {
                    **evaluate_fresh_capacity_probe(
                        snapshots_by_offset,
                        decision_offset=decision_offset,
                        outcome=outcome,
                        position_shares=float(contract["position_shares"]),
                        entry_depth_buffer_shares=float(contract["entry_depth_buffer_shares"]),
                        decision_latency_seconds=int(contract["decision_latency_seconds"]),
                        holding_seconds_after_entry=int(contract["holding_seconds_after_entry"]),
                        exit_grace_seconds=int(contract["exit_grace_seconds"]),
                    ),
                    "condition_id": condition_id,
                    "slug": slug,
                    "outcome": outcome,
                    "decision_offset": decision_offset,
                }
            )
    return probes


__all__ = [
    "EXPECTED_CAPTURE_CONTRACT",
    "EXPECTED_PROBE_CONTRACT",
    "PASS_VERDICT",
    "PREREG_SCHEMA",
    "VARIANT",
    "V033ContractError",
    "aggregate_probe_results",
    "evaluate_fresh_capacity_probe",
    "evaluate_market_probes",
    "load_and_verify_prereg",
]
