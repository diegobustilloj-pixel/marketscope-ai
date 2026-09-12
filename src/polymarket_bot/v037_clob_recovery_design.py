from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v031_capture import open_read_only
from polymarket_bot.v034_contract import aggregate_probe_results
from polymarket_bot.v036_absolute_exit_design import evaluate_absolute_deadline_probe


DESIGN_SCHEMA = "diagnostic_v037_clob_freshness_recovery_1"
VARIANT = "V0.37_CLOB_FRESHNESS_RECOVERY_DESIGN"
INPUTS = {
    "v036_database": "data/capture_v036_absolute_exit_deadline_4h_retry1.db",
    "v036_result": "data/resultado_v036_absolute_exit_deadline_4h_retry1.json",
    "v036_preregistration": "data/prereg_v036_absolute_exit_deadline_4h.json",
    "v036_implementation": "data/implementation_v036_absolute_exit_deadline_4h.json",
}


class V037DesignError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _stale_intervals(
    snapshots: Mapping[int, Mapping[str, Any]], *, threshold_ms: int
) -> list[dict[str, Any]]:
    stale_offsets = [
        offset
        for offset, row in sorted(snapshots.items())
        if row.get("up_book_age_ms") is None
        or row.get("down_book_age_ms") is None
        or int(row["up_book_age_ms"]) > threshold_ms
        or int(row["down_book_age_ms"]) > threshold_ms
    ]
    intervals: list[list[int]] = []
    for offset in stale_offsets:
        if not intervals or offset != intervals[-1][-1] + 1:
            intervals.append([offset])
        else:
            intervals[-1].append(offset)
    result: list[dict[str, Any]] = []
    available_offsets = set(snapshots)
    for interval in intervals:
        recovery_offset = interval[-1] + 1
        recovered = recovery_offset in available_offsets and recovery_offset not in stale_offsets
        rows = [snapshots[offset] for offset in interval]
        result.append(
            {
                "start_offset": interval[0],
                "end_offset": interval[-1],
                "observed_seconds": len(interval),
                "recovery_offset": recovery_offset if recovered else None,
                "maximum_up_book_age_ms": max(
                    int(row.get("up_book_age_ms") or 0) for row in rows
                ),
                "maximum_down_book_age_ms": max(
                    int(row.get("down_book_age_ms") or 0) for row in rows
                ),
            }
        )
    return result


def run_v037_design(
    *, output_path: str | Path | None = None, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    input_hashes = {key: sha256_file(root / relative) for key, relative in INPUTS.items()}
    v036_result = json.loads((root / INPUTS["v036_result"]).read_text(encoding="utf-8"))
    v036_prereg = json.loads(
        (root / INPUTS["v036_preregistration"]).read_text(encoding="utf-8")
    )
    if v036_result.get("verdict") != "FAIL_FIRST_TRAPPED_POSITION":
        raise V037DesignError("El resultado V0.36 no corresponde al fallo observado")
    if not v036_result.get("safety_passed"):
        raise V037DesignError("V0.36 no supero la puerta de seguridad")

    freshness_max_age_ms = int(v036_prereg["capture_contract"]["freshness_max_age_ms"])
    watchdog_timeout_ms = 3000
    watchdog_poll_ms = 250
    reconnect_backoff_ms = 250
    recovery_service_level_ms = 5000
    earliest_irreducible_exit = int(
        v036_prereg["probe_contract"]["earliest_observed_irreducible_exit_offset"]
    )
    exit_grace_seconds = int(v036_prereg["probe_contract"]["exit_grace_seconds"])
    freshness_uncertainty_seconds = freshness_max_age_ms // 1000
    recovery_budget_seconds = recovery_service_level_ms // 1000
    absolute_exit_deadline = earliest_irreducible_exit - (
        freshness_uncertainty_seconds + exit_grace_seconds + recovery_budget_seconds
    )
    decision_latency_seconds = 1
    minimum_holding_seconds = 20
    decision_max = absolute_exit_deadline - decision_latency_seconds - minimum_holding_seconds

    connection = open_read_only(root / INPUTS["v036_database"])
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        markets = [
            dict(row)
            for row in connection.execute(
                "SELECT condition_id,slug,capture_status FROM v036_markets ORDER BY market_start_ms"
            )
            if row["capture_status"] == "COMPLETED"
        ]
        intervals: list[dict[str, Any]] = []
        replay_probes: list[dict[str, Any]] = []
        for market in markets:
            snapshots = {
                int(row["second_offset"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM v036_snapshots WHERE condition_id=? ORDER BY second_offset",
                    (market["condition_id"],),
                )
            }
            intervals.extend(
                {
                    "slug": str(market["slug"]),
                    **interval,
                }
                for interval in _stale_intervals(
                    snapshots, threshold_ms=watchdog_timeout_ms
                )
            )
            for decision_offset in range(30, decision_max + 1):
                for outcome in ("Up", "Down"):
                    replay_probes.append(
                        {
                            **evaluate_absolute_deadline_probe(
                                snapshots,
                                decision_offset=decision_offset,
                                outcome=outcome,
                                absolute_exit_deadline=absolute_exit_deadline,
                                maximum_holding_seconds=30,
                                drawdown_threshold=0.8,
                            ),
                            "slug": str(market["slug"]),
                            "outcome": outcome,
                            "decision_offset": decision_offset,
                        }
                    )
    finally:
        connection.close()

    trapped_v036 = []
    for slug, market_summary in v036_result.get("per_market", {}).items():
        if int(market_summary.get("trapped_positions", 0)):
            trapped_v036.append(
                {"slug": slug, "trapped_positions": int(market_summary["trapped_positions"])}
            )
    v036_per_outcome = v036_result.get("per_outcome", {})
    symmetric_traps = (
        int(v036_per_outcome.get("Up", {}).get("trapped_positions", -1))
        == int(v036_per_outcome.get("Down", {}).get("trapped_positions", -2))
        == 5
    )
    replay = aggregate_probe_results(replay_probes)
    gates = {
        "v036_safety_passed": bool(v036_result.get("safety_passed")),
        "v036_failure_is_symmetric_clob_staleness": symmetric_traps,
        "v036_exactly_one_market_trapped": len(trapped_v036) == 1,
        "watchdog_precedes_existing_freshness_rejection": watchdog_timeout_ms < freshness_max_age_ms,
        "watchdog_poll_is_subsecond": watchdog_poll_ms < 1000,
        "reconnect_backoff_is_subsecond": reconnect_backoff_ms < 1000,
        "recovery_budget_is_reserved_before_irreducible_zone": (
            earliest_irreducible_exit - absolute_exit_deadline
        )
        >= freshness_uncertainty_seconds + exit_grace_seconds + recovery_budget_seconds,
        "decision_window_retains_at_least_sixty_offsets": decision_max - 29 >= 60,
        "sqlite_quick_check_passed": quick_check == "ok",
    }
    payload = {
        "schema": DESIGN_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "repair_connected_but_stale_clob_transport_without_economic_claim",
        "inputs": {
            key: {"relative_path": relative, "sha256": input_hashes[key]}
            for key, relative in INPUTS.items()
        },
        "observed_failure": {
            "v036_verdict": v036_result["verdict"],
            "trapped_markets": trapped_v036,
            "trapped_up": int(v036_per_outcome["Up"]["trapped_positions"]),
            "trapped_down": int(v036_per_outcome["Down"]["trapped_positions"]),
            "clob_stale_intervals_over_3000ms": intervals,
            "connection_label_was_insufficient": True,
            "stale_depth_must_never_count_as_an_exit": True,
        },
        "derivation": {
            "parameter_grid_used": False,
            "watchdog_timeout_ms": watchdog_timeout_ms,
            "watchdog_poll_ms": watchdog_poll_ms,
            "reconnect_backoff_ms": reconnect_backoff_ms,
            "recovery_service_level_ms": recovery_service_level_ms,
            "freshness_max_age_ms": freshness_max_age_ms,
            "earliest_observed_irreducible_exit_offset": earliest_irreducible_exit,
            "freshness_uncertainty_seconds": freshness_uncertainty_seconds,
            "exit_grace_seconds": exit_grace_seconds,
            "recovery_budget_seconds": recovery_budget_seconds,
            "absolute_exit_deadline_offset": absolute_exit_deadline,
            "decision_offset_max_inclusive": decision_max,
            "decision_offset_count": decision_max - 29,
            "expected_capacity_probes_48_markets": (decision_max - 29) * 2 * 48,
            "formula": "deadline=earliest_irreducible-(freshness+exit_grace+recovery_budget); decision_max=deadline-latency-minimum_hold",
        },
        "closed_replay_without_watchdog": {
            **replay,
            "interpretation": "diagnostic_only; historical snapshots cannot simulate data recovered by a new reconnect",
            "validation_credit": False,
        },
        "required_runtime_mechanism": {
            "age_source": "OLDEST_UP_OR_DOWN_CLOB_SOURCE_TIMESTAMP",
            "connected_label_alone_is_never_sufficient": True,
            "force_resubscription_when_stale": True,
            "fresh_generation_requires_both_books_updated_after_reconnect": True,
            "record_every_trigger_and_recovery": True,
            "unrecovered_or_slow_incident_fails_closed": True,
            "entry_and_exit_freshness_rules_unchanged": True,
        },
        "gates": gates,
        "all_design_gates_passed": all(gates.values()),
        "decision": (
            "PREPARE_ONE_FRESH_V037_CLOB_RECOVERY_REPLICATION"
            if all(gates.values())
            else "DO_NOT_BUILD_V037_DESIGN_GATES_FAILED"
        ),
        "limitations": {
            "fresh_forward_validation_required": True,
            "closed_replay_proves_reconnect_recovery": False,
            "network_recovery_can_be_guaranteed": False,
            "fail_closed_if_fresh_data_does_not_return": True,
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
            raise V037DesignError("Existe otro diagnostico V0.37")
        _write_atomic(output, payload)
    return payload


__all__ = ["DESIGN_SCHEMA", "V037DesignError", "run_v037_design"]
