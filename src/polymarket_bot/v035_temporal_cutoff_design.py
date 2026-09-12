from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v031_capture import open_read_only
from polymarket_bot.v034_contract import aggregate_probe_results, evaluate_probe
from polymarket_bot.v034_guard_design import _load_v031, _load_v033


DESIGN_SCHEMA = "diagnostic_v035_temporal_exposure_cutoff_1"
VARIANT = "V0.35_TEMPORAL_EXPOSURE_CUTOFF_DESIGN"
DECISION_CAPS = (89, 99, 104, 105, 109, 114, 119)
INPUTS = {
    "v031_database": "data/capture_v031_path_execution_1h.db",
    "v031r_result": "data/resultado_v031_quality_semantics_reaudit_v2.json",
    "v033_database": "data/capture_v033_fresh_exit_safety_4h.db",
    "v033_result": "data/resultado_v033_fresh_exit_safety_4h.json",
    "v034_database": "data/capture_v034_selected_bid_guard_4h.db",
    "v034_result": "data/resultado_v034_selected_bid_guard_4h.json",
    "v034_preregistration": "data/prereg_v034_selected_bid_guard_4h.json",
}


class V035TemporalDesignError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_v034(path: Path) -> list[dict[str, Any]]:
    connection = open_read_only(path)
    try:
        markets = [
            dict(row)
            for row in connection.execute(
                "SELECT condition_id,slug,capture_status FROM v034_markets ORDER BY market_start_ms"
            )
        ]
        result: list[dict[str, Any]] = []
        for market in markets:
            if market["capture_status"] != "COMPLETED":
                continue
            rows = {
                int(row["second_offset"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM v034_snapshots WHERE condition_id=? ORDER BY second_offset",
                    (market["condition_id"],),
                )
            }
            result.append({"source": "V034", **market, "snapshots": rows})
        return result
    finally:
        connection.close()


def _evaluate(
    markets: Sequence[Mapping[str, Any]],
    *,
    contract: Mapping[str, Any],
    decision_cap: int,
) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    for market in markets:
        for decision_offset in range(30, decision_cap + 1):
            for outcome in ("Up", "Down"):
                probes.append(
                    {
                        **evaluate_probe(
                            market["snapshots"],
                            decision_offset=decision_offset,
                            outcome=outcome,
                            contract=contract,
                        ),
                        "source": market["source"],
                        "slug": market["slug"],
                        "outcome": outcome,
                        "decision_offset": decision_offset,
                    }
                )
    return probes


def run_temporal_cutoff_design(
    *,
    output_path: str | Path | None = None,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    input_hashes = {key: sha256_file(root / relative) for key, relative in INPUTS.items()}
    v034_result = json.loads((root / INPUTS["v034_result"]).read_text(encoding="utf-8"))
    if v034_result.get("verdict") != "FAIL_FIRST_TRAPPED_POSITION":
        raise V035TemporalDesignError("Resultado V0.34 incompatible")
    prereg = json.loads((root / INPUTS["v034_preregistration"]).read_text(encoding="utf-8"))
    contract = prereg["probe_contract"]
    markets = (
        _load_v031(root / INPUTS["v031_database"])
        + _load_v033(root / INPUTS["v033_database"])
        + _load_v034(root / INPUTS["v034_database"])
    )
    baseline_probes = _evaluate(markets, contract=contract, decision_cap=119)
    baseline_traps = [probe for probe in baseline_probes if probe["status"] == "TRAPPED"]
    if not baseline_traps:
        raise V035TemporalDesignError("La falla V0.34 no se reproduce")
    earliest_irreducible_exit_offset = min(
        int(probe["target_exit_offset"]) for probe in baseline_traps
    )
    freshness_uncertainty_seconds = int(
        prereg["capture_contract"]["freshness_max_age_ms"] / 1000
    )
    exit_grace_seconds = int(contract["exit_grace_seconds"])
    required_margin_seconds = freshness_uncertainty_seconds + exit_grace_seconds
    candidates: dict[str, Any] = {}
    for cap in DECISION_CAPS:
        probes = _evaluate(markets, contract=contract, decision_cap=cap)
        overall = aggregate_probe_results(probes)
        per_outcome = {
            outcome: aggregate_probe_results(
                [probe for probe in probes if probe["outcome"] == outcome]
            )
            for outcome in ("Up", "Down")
        }
        latest_target = cap + int(contract["decision_latency_seconds"]) + int(
            contract["holding_seconds_after_entry"]
        )
        margin = earliest_irreducible_exit_offset - latest_target
        candidates[str(cap)] = {
            "decision_offsets": [30, cap],
            "decision_offset_count": cap - 29,
            "probe_retention_vs_30_119": round((cap - 29) / 90, 8),
            "latest_scheduled_exit_offset": latest_target,
            "margin_before_earliest_irreducible_exit_seconds": margin,
            "overall": overall,
            "per_outcome": per_outcome,
            "traps": [
                {
                    key: probe.get(key)
                    for key in (
                        "source", "slug", "outcome", "decision_offset",
                        "entry_offset", "target_exit_offset", "reason",
                    )
                }
                for probe in probes
                if probe["status"] == "TRAPPED"
            ],
        }
    eligible = [
        cap
        for cap in DECISION_CAPS
        if candidates[str(cap)]["overall"]["trapped_positions"] == 0
        and candidates[str(cap)]["probe_retention_vs_30_119"] >= 0.80
        and candidates[str(cap)]["overall"]["scheduled_exit_fraction"] >= 0.80
        and candidates[str(cap)]["overall"]["median_holding_seconds"] >= 25.0
        and candidates[str(cap)]["margin_before_earliest_irreducible_exit_seconds"]
        >= required_margin_seconds
        and all(
            candidates[str(cap)]["per_outcome"][outcome]["trapped_positions"] == 0
            for outcome in ("Up", "Down")
        )
    ]
    selected = max(eligible) if eligible else None
    payload = {
        "schema": DESIGN_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "closed_technical_temporal_cutoff_calibration_no_economic_claim",
        "inputs": {
            key: {"relative_path": relative, "sha256": input_hashes[key]}
            for key, relative in INPUTS.items()
        },
        "markets": len(markets),
        "market_sources": dict(Counter(str(market["source"]) for market in markets)),
        "baseline_30_119": {
            "summary": aggregate_probe_results(baseline_probes),
            "traps": [
                {
                    key: probe.get(key)
                    for key in (
                        "source", "slug", "outcome", "decision_offset",
                        "entry_offset", "target_exit_offset", "reason",
                    )
                }
                for probe in baseline_traps
            ],
            "earliest_irreducible_exit_offset": earliest_irreducible_exit_offset,
        },
        "unchanged_contract": {
            "decision_offset_min_inclusive": 30,
            "decision_latency_seconds": 1,
            "holding_seconds_after_entry": 30,
            "exit_grace_seconds": 5,
            "position_shares": 5.0,
            "entry_depth_buffer_shares": 10.0,
            "relative_guard_scope": "SELECTED_BID_ONLY",
            "relative_drawdown_threshold_inclusive": 0.8,
        },
        "candidate_decision_caps": list(DECISION_CAPS),
        "selection_rule": {
            "zero_trapped_overall_and_each_outcome": True,
            "minimum_probe_retention_vs_30_119": 0.80,
            "minimum_scheduled_exit_fraction": 0.80,
            "minimum_median_holding_seconds": 25.0,
            "freshness_uncertainty_seconds": freshness_uncertainty_seconds,
            "exit_grace_seconds": exit_grace_seconds,
            "required_margin_seconds": required_margin_seconds,
            "required_margin_formula": "freshness_uncertainty_plus_exit_grace",
            "choose_largest_eligible_decision_cap": True,
        },
        "candidates": candidates,
        "selected_decision_cap": selected,
        "selected_expected_probes_in_48_markets": (
            (selected - 29) * 2 * 48 if selected is not None else 0
        ),
        "decision": (
            "PREPARE_ONE_FRESH_V035_TEMPORAL_CUTOFF_REPLICATION"
            if selected is not None
            else "DO_NOT_BUILD_TEMPORAL_CUTOFF_NO_CANDIDATE_SOLVES_CLOSED_EVIDENCE"
        ),
        "limitations": {
            "posthoc_development_only": True,
            "fresh_validation_required": True,
            "universal_zero_trap_guarantee": False,
            "future_liquidity_can_disappear_before_observed_cutoff": True,
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
                and existing.get("candidate_decision_caps") == payload["candidate_decision_caps"]
            ):
                return existing
            raise V035TemporalDesignError("Existe otro diagnostico V0.35")
        _write_atomic(output, payload)
    return payload


__all__ = ["DESIGN_SCHEMA", "V035TemporalDesignError", "run_temporal_cutoff_design"]
