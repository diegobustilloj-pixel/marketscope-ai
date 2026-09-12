from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v031_capture import open_read_only
from polymarket_bot.v031_quality_reaudit import classify_snapshot


DESIGN_SCHEMA = "diagnostic_v034_relative_depth_guard_1"
VARIANT = "V0.34_RELATIVE_DEPTH_DRAWDOWN_GUARD_DESIGN"
INPUTS = {
    "v031_database": "data/capture_v031_path_execution_1h.db",
    "v031r_result": "data/resultado_v031_quality_semantics_reaudit_v2.json",
    "v032_result": "data/resultado_v032_exit_safety_capacity.json",
    "v033_database": "data/capture_v033_fresh_exit_safety_4h.db",
    "v033_result": "data/resultado_v033_fresh_exit_safety_4h.json",
}
THRESHOLDS = (0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90)
DEPTH_KEYS = (
    "up_bid_depth_top5",
    "up_ask_depth_top5",
    "down_bid_depth_top5",
    "down_ask_depth_top5",
)


class V034GuardDesignError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _number(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, parsed) if math.isfinite(parsed) else 0.0


def _all_depth(row: Mapping[str, Any], required: float) -> bool:
    return all(_number(row.get(key)) >= required for key in DEPTH_KEYS)


def _exit_ready(row: Mapping[str, Any], outcome: str, shares: float) -> bool:
    selected = outcome.lower()
    return bool(row.get(f"{selected}_book_fresh")) and _number(
        row.get(f"{selected}_bid_depth_top5")
    ) >= shares


def _relative_guard_reasons(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    *,
    drawdown_threshold: float,
    baseline_minimum_depth: float,
    depth_keys: Sequence[str] = DEPTH_KEYS,
) -> list[str]:
    if previous is None:
        return []
    reasons: list[str] = []
    for key in depth_keys:
        before = _number(previous.get(key))
        after = _number(current.get(key))
        if before < baseline_minimum_depth:
            continue
        drawdown = 1.0 - after / before
        if drawdown >= drawdown_threshold:
            reasons.append(f"RELATIVE_DRAWDOWN:{key}")
    return reasons


def evaluate_relative_guard_probe(
    snapshots_by_offset: Mapping[int, Mapping[str, Any]],
    *,
    decision_offset: int,
    outcome: str,
    drawdown_threshold: float,
    position_shares: float = 5.0,
    entry_depth_buffer_shares: float = 10.0,
    decision_latency_seconds: int = 1,
    holding_seconds_after_entry: int = 30,
    exit_grace_seconds: int = 5,
    relative_guard_scope: str = "ANY_FOUR_BOOK_SIDES",
) -> dict[str, Any]:
    selected = outcome.lower()
    if selected not in {"up", "down"}:
        raise ValueError("outcome debe ser Up o Down")
    if not (0.0 < drawdown_threshold < 1.0):
        raise ValueError("drawdown_threshold debe estar entre 0 y 1")
    if relative_guard_scope not in {"ANY_FOUR_BOOK_SIDES", "SELECTED_BID_ONLY"}:
        raise ValueError("relative_guard_scope invalido")
    decision = snapshots_by_offset.get(decision_offset)
    if decision is None:
        return {"status": "DECISION_REJECTED", "reason": "DECISION_SNAPSHOT_MISSING"}
    if not bool(decision.get("complete_v2")):
        return {"status": "DECISION_REJECTED", "reason": "DECISION_DATA_INCOMPLETE"}
    if not _all_depth(decision, entry_depth_buffer_shares):
        return {"status": "DECISION_REJECTED", "reason": "DECISION_BUFFER_DEPTH_INSUFFICIENT"}
    entry_offset = decision_offset + decision_latency_seconds
    entry = snapshots_by_offset.get(entry_offset)
    if entry is None:
        return {"status": "ENTRY_REJECTED", "reason": "ENTRY_SNAPSHOT_MISSING"}
    if not bool(entry.get("complete_v2")):
        return {"status": "ENTRY_REJECTED", "reason": "ENTRY_DATA_INCOMPLETE"}
    if not _all_depth(entry, entry_depth_buffer_shares):
        return {"status": "ENTRY_REJECTED", "reason": "ENTRY_BUFFER_DEPTH_INSUFFICIENT"}

    target = entry_offset + holding_seconds_after_entry
    exit_rejections: Counter[str] = Counter()
    for offset in range(entry_offset + 1, target + exit_grace_seconds + 1):
        current = snapshots_by_offset.get(offset)
        if current is None:
            exit_rejections["EXIT_SNAPSHOT_MISSING"] += 1
            continue
        if offset < target:
            reasons: list[str] = []
            if _number(current.get(f"{selected}_bid_depth_top5")) < entry_depth_buffer_shares:
                reasons.append("SELECTED_BID_BELOW_ABSOLUTE_BUFFER")
            if not _all_depth(current, position_shares):
                reasons.append("ANY_BOOK_SIDE_BELOW_POSITION_SIZE")
            reasons.extend(
                _relative_guard_reasons(
                    snapshots_by_offset.get(offset - 1),
                    current,
                    drawdown_threshold=drawdown_threshold,
                    baseline_minimum_depth=entry_depth_buffer_shares,
                    depth_keys=(
                        (f"{selected}_bid_depth_top5",)
                        if relative_guard_scope == "SELECTED_BID_ONLY"
                        else DEPTH_KEYS
                    ),
                )
            )
            if reasons and _exit_ready(current, selected, position_shares):
                return {
                    "status": "EXITED",
                    "exit_kind": "PROACTIVE_GUARD",
                    "guard_reasons": sorted(set(reasons)),
                    "entry_offset": entry_offset,
                    "target_exit_offset": target,
                    "exit_offset": offset,
                    "holding_seconds": offset - entry_offset,
                    "exit_delay_seconds": offset - target,
                    "exit_rejections": dict(exit_rejections),
                }
            if reasons:
                exit_rejections["GUARD_TRIGGERED_BUT_SELECTED_EXIT_UNAVAILABLE"] += 1
            continue
        if not _exit_ready(current, selected, position_shares):
            exit_rejections["TARGET_OR_GRACE_SELECTED_EXIT_UNAVAILABLE"] += 1
            continue
        return {
            "status": "EXITED",
            "exit_kind": "SCHEDULED" if offset == target else "GRACE_RETRY",
            "guard_reasons": [],
            "entry_offset": entry_offset,
            "target_exit_offset": target,
            "exit_offset": offset,
            "holding_seconds": offset - entry_offset,
            "exit_delay_seconds": offset - target,
            "exit_rejections": dict(exit_rejections),
        }
    return {
        "status": "TRAPPED",
        "reason": "NO_FRESH_FULL_DEPTH_SELECTED_BID_BY_TARGET_PLUS_GRACE",
        "exit_kind": None,
        "guard_reasons": [],
        "entry_offset": entry_offset,
        "target_exit_offset": target,
        "exit_offset": None,
        "holding_seconds": None,
        "exit_delay_seconds": None,
        "exit_rejections": dict(exit_rejections),
    }


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 8) if denominator else 0.0


def summarize(probes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    entered = [probe for probe in probes if probe["status"] in {"EXITED", "TRAPPED"}]
    exited = [probe for probe in entered if probe["status"] == "EXITED"]
    scheduled = [probe for probe in exited if probe.get("exit_kind") == "SCHEDULED"]
    guards = [probe for probe in exited if probe.get("exit_kind") == "PROACTIVE_GUARD"]
    trapped = [probe for probe in entered if probe["status"] == "TRAPPED"]
    holding = sorted(int(probe["holding_seconds"]) for probe in exited)
    guard_reasons = Counter(
        reason for probe in guards for reason in probe.get("guard_reasons", [])
    )
    return {
        "total_probes": len(probes),
        "entry_eligible": len(entered),
        "exited": len(exited),
        "exit_success_rate": _rate(len(exited), len(entered)),
        "scheduled_exits": len(scheduled),
        "scheduled_exit_fraction": _rate(len(scheduled), len(entered)),
        "proactive_guard_exits": len(guards),
        "proactive_guard_fraction": _rate(len(guards), len(entered)),
        "trapped": len(trapped),
        "median_holding_seconds": float(median(holding)) if holding else 0.0,
        "minimum_holding_seconds": min(holding, default=0),
        "guard_reasons": dict(sorted(guard_reasons.items())),
    }


def _load_v031(path: Path) -> list[dict[str, Any]]:
    connection = open_read_only(path)
    try:
        markets = [
            dict(row)
            for row in connection.execute(
                "SELECT condition_id,slug,resolution_twap_window_s,capture_status FROM v031_markets ORDER BY market_start_ms"
            )
        ]
        result: list[dict[str, Any]] = []
        for market in markets:
            if market["capture_status"] != "COMPLETED":
                continue
            rows: dict[int, dict[str, Any]] = {}
            for source in connection.execute(
                "SELECT * FROM v031_snapshots WHERE condition_id=? ORDER BY second_offset",
                (market["condition_id"],),
            ):
                row = dict(source)
                row["complete_v2"] = bool(
                    classify_snapshot(
                        row,
                        resolution_twap_window_s=int(market["resolution_twap_window_s"]),
                    )["data_complete_v2"]
                )
                rows[int(row["second_offset"])] = row
            result.append({"source": "V031", **market, "snapshots": rows})
        return result
    finally:
        connection.close()


def _load_v033(path: Path) -> list[dict[str, Any]]:
    connection = open_read_only(path)
    try:
        markets = [
            dict(row)
            for row in connection.execute(
                "SELECT condition_id,slug,capture_status FROM v033_markets ORDER BY market_start_ms"
            )
        ]
        result: list[dict[str, Any]] = []
        for market in markets:
            if market["capture_status"] != "COMPLETED":
                continue
            rows = {
                int(row["second_offset"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM v033_snapshots WHERE condition_id=? ORDER BY second_offset",
                    (market["condition_id"],),
                )
            }
            result.append({"source": "V033", **market, "snapshots": rows})
        return result
    finally:
        connection.close()


def _evaluate_markets(
    markets: Sequence[Mapping[str, Any]],
    *,
    threshold: float,
) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    for market in markets:
        snapshots = market["snapshots"]
        for decision_offset in range(30, 120):
            for outcome in ("Up", "Down"):
                probes.append(
                    {
                        **evaluate_relative_guard_probe(
                            snapshots,
                            decision_offset=decision_offset,
                            outcome=outcome,
                            drawdown_threshold=threshold,
                        ),
                        "source": market["source"],
                        "slug": market["slug"],
                        "outcome": outcome,
                        "decision_offset": decision_offset,
                    }
                )
    return probes


def run_guard_design(
    *,
    output_path: str | Path | None = None,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    input_hashes = {key: sha256_file(root / relative) for key, relative in INPUTS.items()}
    v033_result = json.loads((root / INPUTS["v033_result"]).read_text(encoding="utf-8"))
    if v033_result.get("verdict") != "FAIL_FIRST_TRAPPED_POSITION":
        raise V034GuardDesignError("Resultado V0.33 incompatible")
    markets = _load_v031(root / INPUTS["v031_database"]) + _load_v033(
        root / INPUTS["v033_database"]
    )
    candidates: dict[str, Any] = {}
    probes_by_threshold: dict[float, list[dict[str, Any]]] = {}
    for threshold in THRESHOLDS:
        probes = _evaluate_markets(markets, threshold=threshold)
        probes_by_threshold[threshold] = probes
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
        and candidates[f"{threshold:.2f}"]["overall"]["entry_eligible"] >= 1000
        and all(
            candidates[f"{threshold:.2f}"]["per_outcome"][outcome]["trapped"] == 0
            for outcome in ("Up", "Down")
        )
    ]
    selected = max(eligible) if eligible else None
    selected_traps: list[dict[str, Any]] = []
    if selected is not None:
        selected_traps = [
            probe
            for probe in probes_by_threshold[selected]
            if probe["status"] == "TRAPPED"
        ]
    payload = {
        "schema": DESIGN_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "closed_technical_guard_calibration_only_no_economic_claim",
        "inputs": {
            key: {"relative_path": relative, "sha256": input_hashes[key]}
            for key, relative in INPUTS.items()
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
            "relative_guard_scope": "any_of_four_book_sides",
            "relative_baseline_minimum_depth": 10.0,
            "selected_exit_requires_fresh_bid_depth": 5.0,
        },
        "candidate_thresholds": list(THRESHOLDS),
        "selection_rule": {
            "zero_trapped_overall_and_each_outcome": True,
            "minimum_scheduled_exit_fraction": 0.80,
            "minimum_entry_eligible": 1000,
            "choose_largest_eligible_threshold": True,
            "reason": "least_sensitive_non_vacuous_guard_that_eliminates_known_traps",
        },
        "candidates": candidates,
        "selected_threshold": selected,
        "selected_threshold_traps": selected_traps,
        "decision": (
            "PREPARE_ONE_FRESH_V034_REPLICATION"
            if selected is not None
            else "DO_NOT_BUILD_RELATIVE_GUARD_NO_CANDIDATE_SOLVES_CLOSED_EVIDENCE"
        ),
        "limitations": {
            "posthoc_development_only": True,
            "fresh_validation_required": True,
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
                and existing.get("candidate_thresholds") == payload["candidate_thresholds"]
            ):
                return existing
            raise V034GuardDesignError("Existe otro diagnostico V0.34")
        _write_atomic(output, payload)
    return payload


__all__ = [
    "DESIGN_SCHEMA",
    "THRESHOLDS",
    "V034GuardDesignError",
    "evaluate_relative_guard_probe",
    "run_guard_design",
    "summarize",
]
