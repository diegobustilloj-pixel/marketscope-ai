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
    _load_v031,
    _load_v033,
    evaluate_relative_guard_probe,
)
from polymarket_bot.v035_temporal_cutoff_design import _load_v034


DESIGN_SCHEMA = "diagnostic_v036_absolute_exit_deadline_1"
VARIANT = "V0.36_ABSOLUTE_EXIT_DEADLINE_DESIGN"
INPUTS = {
    "v031_database": "data/capture_v031_path_execution_1h.db",
    "v031r_result": "data/resultado_v031_quality_semantics_reaudit_v2.json",
    "v033_database": "data/capture_v033_fresh_exit_safety_4h.db",
    "v033_result": "data/resultado_v033_fresh_exit_safety_4h.json",
    "v034_database": "data/capture_v034_selected_bid_guard_4h.db",
    "v034_result": "data/resultado_v034_selected_bid_guard_4h.json",
    "v035_database": "data/capture_v035_temporal_cutoff_4h.db",
    "v035_result": "data/resultado_v035_temporal_cutoff_4h.json",
}


class V036AbsoluteExitDesignError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_v035(path: Path) -> list[dict[str, Any]]:
    connection = open_read_only(path)
    try:
        markets = [
            dict(row)
            for row in connection.execute(
                "SELECT condition_id,slug,capture_status FROM v035_markets ORDER BY market_start_ms"
            )
        ]
        result: list[dict[str, Any]] = []
        for market in markets:
            if market["capture_status"] != "COMPLETED":
                continue
            snapshots = {
                int(row["second_offset"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM v035_snapshots WHERE condition_id=? ORDER BY second_offset",
                    (market["condition_id"],),
                )
            }
            result.append({"source": "V035", **market, "snapshots": snapshots})
        return result
    finally:
        connection.close()


def evaluate_absolute_deadline_probe(
    snapshots: Mapping[int, Mapping[str, Any]],
    *,
    decision_offset: int,
    outcome: str,
    absolute_exit_deadline: int,
    maximum_holding_seconds: int = 30,
    drawdown_threshold: float = 0.8,
) -> dict[str, Any]:
    entry_offset = decision_offset + 1
    planned_holding = min(maximum_holding_seconds, absolute_exit_deadline - entry_offset)
    if planned_holding <= 0:
        return {
            "status": "DECISION_REJECTED",
            "reason": "INSUFFICIENT_TIME_BEFORE_ABSOLUTE_EXIT_DEADLINE",
        }
    return {
        **evaluate_relative_guard_probe(
            snapshots,
            decision_offset=decision_offset,
            outcome=outcome,
            drawdown_threshold=drawdown_threshold,
            position_shares=5.0,
            entry_depth_buffer_shares=10.0,
            decision_latency_seconds=1,
            holding_seconds_after_entry=planned_holding,
            exit_grace_seconds=5,
            relative_guard_scope="SELECTED_BID_ONLY",
        ),
        "planned_holding_seconds": planned_holding,
        "absolute_exit_deadline": absolute_exit_deadline,
    }


def _evaluate(
    markets: Sequence[Mapping[str, Any]],
    *,
    decision_max: int,
    absolute_exit_deadline: int,
) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    for market in markets:
        for decision_offset in range(30, decision_max + 1):
            for outcome in ("Up", "Down"):
                probes.append(
                    {
                        **evaluate_absolute_deadline_probe(
                            market["snapshots"],
                            decision_offset=decision_offset,
                            outcome=outcome,
                            absolute_exit_deadline=absolute_exit_deadline,
                        ),
                        "source": market["source"],
                        "slug": market["slug"],
                        "outcome": outcome,
                        "decision_offset": decision_offset,
                    }
                )
    return probes


def run_absolute_exit_design(
    *, output_path: str | Path | None = None, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    hashes = {key: sha256_file(root / relative) for key, relative in INPUTS.items()}
    for version in ("v033", "v034", "v035"):
        result = json.loads((root / INPUTS[f"{version}_result"]).read_text(encoding="utf-8"))
        if result.get("verdict") != "FAIL_FIRST_TRAPPED_POSITION":
            raise V036AbsoluteExitDesignError(f"Resultado {version.upper()} incompatible")

    markets = (
        _load_v031(root / INPUTS["v031_database"])
        + _load_v033(root / INPUTS["v033_database"])
        + _load_v034(root / INPUTS["v034_database"])
        + _load_v035(root / INPUTS["v035_database"])
    )
    earliest_observed_irreducible_exit_offset = 126
    freshness_uncertainty_seconds = 5
    exit_grace_seconds = 5
    required_margin_seconds = freshness_uncertainty_seconds + exit_grace_seconds
    absolute_exit_deadline = (
        earliest_observed_irreducible_exit_offset - required_margin_seconds
    )
    minimum_planned_holding_seconds = 20
    decision_latency_seconds = 1
    decision_max = (
        absolute_exit_deadline
        - decision_latency_seconds
        - minimum_planned_holding_seconds
    )
    probes = _evaluate(
        markets,
        decision_max=decision_max,
        absolute_exit_deadline=absolute_exit_deadline,
    )
    overall = aggregate_probe_results(probes)
    per_source = {
        source: aggregate_probe_results(
            [probe for probe in probes if probe["source"] == source]
        )
        for source in ("V031", "V033", "V034", "V035")
    }
    per_outcome = {
        outcome: aggregate_probe_results(
            [probe for probe in probes if probe["outcome"] == outcome]
        )
        for outcome in ("Up", "Down")
    }
    planned_holds = [
        min(30, absolute_exit_deadline - (decision + decision_latency_seconds))
        for decision in range(30, decision_max + 1)
    ]
    retention = round((decision_max - 29) / 90, 8)
    gates = {
        "zero_trapped_overall_passed": overall["trapped_positions"] == 0,
        "zero_trapped_each_source_passed": all(
            per_source[source]["trapped_positions"] == 0 for source in per_source
        ),
        "zero_trapped_each_outcome_passed": all(
            per_outcome[outcome]["trapped_positions"] == 0 for outcome in per_outcome
        ),
        "exit_success_rate_one_passed": overall["exit_success_within_grace_rate"] == 1.0,
        "minimum_scheduled_exit_fraction_passed": overall["scheduled_exit_fraction"] >= 0.8,
        "minimum_decision_window_retention_passed": retention >= 0.7,
        "minimum_planned_holding_floor_passed": min(planned_holds) >= 20,
        "planned_holding_median_thirty_passed": sorted(planned_holds)[len(planned_holds) // 2] == 30,
        "required_margin_passed": (
            earliest_observed_irreducible_exit_offset - absolute_exit_deadline
        ) >= required_margin_seconds,
    }
    passed = all(gates.values())
    payload = {
        "schema": DESIGN_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "closed_technical_absolute_exit_deadline_design_no_economic_claim",
        "inputs": {
            key: {"relative_path": relative, "sha256": hashes[key]}
            for key, relative in INPUTS.items()
        },
        "markets": len(markets),
        "market_sources": dict(Counter(str(market["source"]) for market in markets)),
        "failure_mechanism": {
            "v035_first_zero_selected_down_bid_offset": 126,
            "v035_previous_offset": 125,
            "v035_previous_down_bid_depth_top5": 887.06,
            "gradual_precursor_observed": False,
            "reactive_guard_can_guarantee_exit": False,
            "fixed_market_time_can_guarantee_future_exit": False,
        },
        "derivation": {
            "earliest_observed_irreducible_exit_offset": earliest_observed_irreducible_exit_offset,
            "freshness_uncertainty_seconds": freshness_uncertainty_seconds,
            "exit_grace_seconds": exit_grace_seconds,
            "required_margin_seconds": required_margin_seconds,
            "absolute_exit_deadline": absolute_exit_deadline,
            "minimum_planned_holding_seconds": minimum_planned_holding_seconds,
            "maximum_planned_holding_seconds": 30,
            "decision_latency_seconds": decision_latency_seconds,
            "derived_decision_max_inclusive": decision_max,
            "derivation_formula": "deadline=earliest_irreducible-(freshness+grace); decision_max=deadline-latency-minimum_hold",
            "parameter_grid_used": False,
        },
        "candidate": {
            "decision_offsets_inclusive": [30, decision_max],
            "decision_offset_count": decision_max - 29,
            "decision_window_retention_vs_30_119": retention,
            "scheduled_exit_rule": "min(entry_offset_plus_30,absolute_exit_deadline_116)",
            "full_30_second_holding_decisions_inclusive": [30, 85],
            "tapered_holding_decisions_inclusive": [86, decision_max],
            "planned_holding_seconds_minimum": min(planned_holds),
            "planned_holding_seconds_median": float(sorted(planned_holds)[len(planned_holds) // 2]),
            "planned_holding_seconds_maximum": max(planned_holds),
            "margin_before_earliest_observed_irreducible_exit_seconds": (
                earliest_observed_irreducible_exit_offset - absolute_exit_deadline
            ),
            "expected_probes_in_48_markets": (decision_max - 29) * 2 * 48,
            "unchanged": {
                "position_shares": 5.0,
                "entry_depth_buffer_shares": 10.0,
                "relative_guard_scope": "SELECTED_BID_ONLY",
                "relative_drawdown_threshold_inclusive": 0.8,
                "absolute_guards_preserved": True,
                "exit_grace_seconds": 5,
            },
        },
        "closed_evidence": {
            "overall": overall,
            "per_source": per_source,
            "per_outcome": per_outcome,
            "gates": gates,
            "all_gates_passed": passed,
        },
        "duration_decision": {
            "fresh_replication_maximum_hours": 4.0,
            "expected_markets": 48,
            "expected_probes": (decision_max - 29) * 2 * 48,
            "stop_after_first_trapped_position": True,
            "why_not_24h_now": "technical_falsification_precedes_intraday_or_economic_validation",
            "future_24h_condition": "only_after_fresh_exit_safety_passes_and_an_economic_candidate_exists",
        },
        "decision": (
            "PREPARE_ONE_FRESH_V036_ABSOLUTE_EXIT_DEADLINE_REPLICATION"
            if passed
            else "DO_NOT_BUILD_V036_CLOSED_EVIDENCE_FAILED"
        ),
        "limitations": {
            "posthoc_development_only": True,
            "fresh_validation_required": True,
            "universal_zero_trap_guarantee": False,
            "future_liquidity_can_disappear_before_deadline": True,
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
            raise V036AbsoluteExitDesignError("Existe otro diagnostico V0.36")
        _write_atomic(output, payload)
    return payload


__all__ = [
    "DESIGN_SCHEMA",
    "V036AbsoluteExitDesignError",
    "evaluate_absolute_deadline_probe",
    "run_absolute_exit_design",
]
