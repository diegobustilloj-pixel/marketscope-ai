from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v034_contract import aggregate_probe_results, evaluate_probe


PREREG_SCHEMA = "prereg_v035_temporal_cutoff_4h_1"
PREREG_STATUS = "FROZEN_FRESH_TEMPORAL_CUTOFF_AWAITING_IMPLEMENTATION_AND_LAUNCH_APPROVAL"
VARIANT = "V0.35_FRESH_TEMPORAL_EXPOSURE_CUTOFF_4H"
PASS_VERDICT = "PASS_FRESH_TEMPORAL_EXIT_SAFETY_ONLY"


class V035ContractError(RuntimeError):
    pass


def _require(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise V035ContractError(f"V0.35 incompatible: {field}")


def load_and_verify_prereg(path: str | Path, *, project_root: str | Path = ROOT) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    _require(payload.get("schema"), PREREG_SCHEMA, "schema")
    _require(payload.get("status"), PREREG_STATUS, "status")
    _require(payload.get("variant"), VARIANT, "variant")
    _require(payload.get("scope", {}).get("maximum_positive_result"), PASS_VERDICT, "scope.maximum_positive_result")
    contract = payload.get("probe_contract", {})
    exact = {
        "decision_offset_min_inclusive": 30,
        "decision_offset_max_inclusive": 105,
        "decision_offset_count": 76,
        "decision_window_retention_vs_30_119": 0.84444444,
        "decision_latency_seconds": 1,
        "holding_seconds_after_entry": 30,
        "latest_scheduled_exit_offset": 136,
        "exit_grace_seconds": 5,
        "required_margin_before_earliest_irreducible_exit_seconds": 10,
        "required_margin_formula": "freshness_uncertainty_plus_exit_grace",
        "position_shares": 5.0,
        "entry_depth_buffer_shares": 10.0,
        "relative_guard_scope": "SELECTED_BID_ONLY",
        "relative_guard_baseline": "IMMEDIATELY_PREVIOUS_ONE_SECOND_SNAPSHOT",
        "relative_drawdown_threshold_inclusive": 0.8,
    }
    for key, value in exact.items():
        _require(contract.get(key), value, f"probe_contract.{key}")
    _require(contract.get("probe_both_outcomes_independently"), True, "probe_contract.both_outcomes")
    _require(contract.get("decision_and_entry_require_v2_complete_and_all_four_sides_at_buffer"), True, "probe_contract.entry_quality")
    _require(contract.get("absolute_guards_preserved"), True, "probe_contract.absolute_guards")
    _require(payload.get("stopping", {}).get("maximum_hours"), 4.0, "stopping.maximum_hours")
    _require(payload.get("stopping", {}).get("scheduled_supervision"), False, "stopping.scheduled_supervision")
    _require(payload.get("data_policy", {}).get("outcomes_read"), 0, "data_policy.outcomes_read")
    _require(payload.get("data_policy", {}).get("prices_stored"), False, "data_policy.prices_stored")
    _require(payload.get("data_policy", {}).get("pnl_calculated"), False, "data_policy.pnl_calculated")
    _require(payload.get("safety", {}).get("real_money"), "BLOQUEADO", "safety.real_money")
    for key, record in payload.get("evidence", {}).items():
        relative = str(record.get("relative_path") or "")
        _require(record.get("sha256"), sha256_file(root / relative), f"evidence.{key}.sha256")
    design = payload.get("design_code", {})
    _require(design.get("sha256"), sha256_file(root / str(design.get("relative_path"))), "design_code.sha256")
    diagnostic = json.loads((root / payload["evidence"]["temporal_cutoff_diagnostic"]["relative_path"]).read_text(encoding="utf-8"))
    _require(diagnostic.get("decision"), "PREPARE_ONE_FRESH_V035_TEMPORAL_CUTOFF_REPLICATION", "diagnostic.decision")
    _require(diagnostic.get("selected_decision_cap"), 105, "diagnostic.selected_decision_cap")
    return dict(payload)


def evaluate_market_probes(
    snapshots: Mapping[int, Mapping[str, Any]], *, condition_id: str,
    slug: str, contract: Mapping[str, Any]
) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    for offset in range(int(contract["decision_offset_min_inclusive"]), int(contract["decision_offset_max_inclusive"]) + 1):
        for outcome in ("Up", "Down"):
            probes.append({
                **evaluate_probe(snapshots, decision_offset=offset, outcome=outcome, contract=contract),
                "condition_id": condition_id, "slug": slug,
                "outcome": outcome, "decision_offset": offset,
            })
    return probes


__all__ = [
    "PASS_VERDICT", "VARIANT", "V035ContractError", "aggregate_probe_results",
    "evaluate_market_probes", "load_and_verify_prereg",
]
