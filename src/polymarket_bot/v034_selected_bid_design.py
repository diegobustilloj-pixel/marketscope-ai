from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v034_guard_design import (
    INPUTS,
    THRESHOLDS,
    V034GuardDesignError,
    _load_v031,
    _load_v033,
    evaluate_relative_guard_probe,
    summarize,
)


DESIGN_SCHEMA = "diagnostic_v034_selected_bid_drawdown_guard_1"
VARIANT = "V0.34_SELECTED_BID_RELATIVE_DRAWDOWN_GUARD_DESIGN"


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run_selected_bid_design(
    *,
    output_path: str | Path | None = None,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    input_hashes = {key: sha256_file(root / relative) for key, relative in INPUTS.items()}
    broad_design_path = root / "data" / "diagnostico_v034_relative_depth_guard.json"
    broad_design = json.loads(broad_design_path.read_text(encoding="utf-8"))
    if broad_design.get("decision") != (
        "DO_NOT_BUILD_RELATIVE_GUARD_NO_CANDIDATE_SOLVES_CLOSED_EVIDENCE"
    ):
        raise V034GuardDesignError("Diagnostico amplio V0.34 incompatible")
    markets = _load_v031(root / INPUTS["v031_database"]) + _load_v033(
        root / INPUTS["v033_database"]
    )
    candidates: dict[str, Any] = {}
    for threshold in THRESHOLDS:
        probes: list[dict[str, Any]] = []
        for market in markets:
            for decision_offset in range(30, 120):
                for outcome in ("Up", "Down"):
                    probes.append(
                        {
                            **evaluate_relative_guard_probe(
                                market["snapshots"],
                                decision_offset=decision_offset,
                                outcome=outcome,
                                drawdown_threshold=threshold,
                                relative_guard_scope="SELECTED_BID_ONLY",
                            ),
                            "source": market["source"],
                            "slug": market["slug"],
                            "outcome": outcome,
                            "decision_offset": decision_offset,
                        }
                    )
        candidates[f"{threshold:.2f}"] = {
            "overall": summarize(probes),
            "per_source": {
                source: summarize([probe for probe in probes if probe["source"] == source])
                for source in ("V031", "V033")
            },
            "per_outcome": {
                outcome: summarize([probe for probe in probes if probe["outcome"] == outcome])
                for outcome in ("Up", "Down")
            },
        }
    eligible = [
        threshold
        for threshold in THRESHOLDS
        if candidates[f"{threshold:.2f}"]["overall"]["trapped"] == 0
        and candidates[f"{threshold:.2f}"]["overall"]["scheduled_exit_fraction"] >= 0.80
        and candidates[f"{threshold:.2f}"]["overall"]["median_holding_seconds"] >= 25.0
        and candidates[f"{threshold:.2f}"]["overall"]["entry_eligible"] >= 1000
        and all(
            candidates[f"{threshold:.2f}"]["per_outcome"][outcome]["trapped"] == 0
            for outcome in ("Up", "Down")
        )
    ]
    selected = max(eligible) if eligible else None
    payload = {
        "schema": DESIGN_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "closed_selected_bid_guard_calibration_only_no_economic_claim",
        "inputs": {
            key: {"relative_path": relative, "sha256": input_hashes[key]}
            for key, relative in INPUTS.items()
        },
        "broad_guard_diagnostic": {
            "relative_path": "data/diagnostico_v034_relative_depth_guard.json",
            "sha256": sha256_file(broad_design_path),
            "decision": broad_design["decision"],
        },
        "markets": len(markets),
        "market_sources": dict(Counter(str(market["source"]) for market in markets)),
        "frozen_common_contract": {
            "decision_offsets_inclusive": [30, 119],
            "decision_latency_seconds": 1,
            "holding_seconds": 30,
            "exit_grace_seconds": 5,
            "position_shares": 5.0,
            "absolute_entry_buffer_shares": 10.0,
            "relative_baseline": "immediately_previous_one_second_snapshot",
            "relative_guard_scope": "selected_position_bid_only",
            "relative_baseline_minimum_depth": 10.0,
            "selected_exit_requires_fresh_bid_depth": 5.0,
            "absolute_guards_preserved": True,
        },
        "candidate_thresholds": list(THRESHOLDS),
        "selection_rule": {
            "zero_trapped_overall_and_each_outcome": True,
            "minimum_scheduled_exit_fraction": 0.80,
            "minimum_median_holding_seconds": 25.0,
            "minimum_entry_eligible": 1000,
            "choose_largest_eligible_threshold": True,
            "reason": "least_sensitive_non_vacuous_selected_bid_guard_that_eliminates_known_traps",
        },
        "candidates": candidates,
        "selected_threshold": selected,
        "decision": (
            "PREPARE_ONE_FRESH_V034_SELECTED_BID_REPLICATION"
            if selected is not None
            else "DO_NOT_BUILD_SELECTED_BID_GUARD_NO_CANDIDATE_SOLVES_CLOSED_EVIDENCE"
        ),
        "limitations": {
            "posthoc_development_only": True,
            "fresh_validation_required": True,
            "one_tick_direct_collapse_to_zero_cannot_be_prevented_by_observed_depth_guard": True,
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
                and existing.get("broad_guard_diagnostic") == payload["broad_guard_diagnostic"]
                and existing.get("candidate_thresholds") == payload["candidate_thresholds"]
            ):
                return existing
            raise V034GuardDesignError("Existe otro diagnostico focal V0.34")
        _write_atomic(output, payload)
    return payload


__all__ = ["DESIGN_SCHEMA", "run_selected_bid_design"]
