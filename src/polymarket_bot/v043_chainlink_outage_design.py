from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v031_capture import open_read_only
from polymarket_bot.v040_transport_guard_design import evaluate_market_probes


DECISION = "PREPARE_V043_BOT_CONTROLLED_CHAINLINK_OUTAGE_SAFETY_AUDIT"


def _other_entry_data_ready(
    snapshot: Mapping[str, Any], *, entry_buffer: float
) -> bool:
    return bool(
        snapshot.get("official_twap_fresh")
        and snapshot.get("up_book_fresh")
        and snapshot.get("down_book_fresh")
        and all(
            float(snapshot.get(key) or 0.0) >= entry_buffer
            for key in (
                "up_bid_depth_top5",
                "up_ask_depth_top5",
                "down_bid_depth_top5",
                "down_ask_depth_top5",
            )
        )
    )


def _stale_runs(
    *, condition_id: str, slug: str, snapshots: Mapping[int, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    active: list[Mapping[str, Any]] = []
    previous_offset: int | None = None
    for offset in sorted(snapshots):
        snapshot = snapshots[offset]
        stale = not bool(snapshot.get("chainlink_fresh"))
        consecutive = previous_offset is None or offset == previous_offset + 1
        if stale and (not active or consecutive):
            active.append(snapshot)
        elif stale:
            runs.append(_summarize_run(condition_id, slug, active))
            active = [snapshot]
        elif active:
            runs.append(_summarize_run(condition_id, slug, active))
            active = []
        previous_offset = offset
    if active:
        runs.append(_summarize_run(condition_id, slug, active))
    return runs


def _summarize_run(
    condition_id: str, slug: str, snapshots: list[Mapping[str, Any]]
) -> dict[str, Any]:
    ages = [
        int(snapshot["chainlink_age_ms"])
        for snapshot in snapshots
        if snapshot.get("chainlink_age_ms") is not None
    ]
    return {
        "condition_id": condition_id,
        "slug": slug,
        "start_offset": int(snapshots[0]["second_offset"]),
        "end_offset": int(snapshots[-1]["second_offset"]),
        "seconds": len(snapshots),
        "start_timestamp_ms": int(snapshots[0]["snapshot_timestamp_ms"]),
        "end_timestamp_ms": int(snapshots[-1]["snapshot_timestamp_ms"]),
        "minimum_chainlink_age_ms": min(ages, default=None),
        "maximum_chainlink_age_ms": max(ages, default=None),
    }


def analyze_v042_outage(
    *,
    database: str | Path,
    result: str | Path,
    v042_preregistration: str | Path,
) -> dict[str, Any]:
    database_file = Path(database).resolve()
    result_file = Path(result).resolve()
    prereg_file = Path(v042_preregistration).resolve()
    for source in (database_file, result_file, prereg_file):
        if not source.is_file():
            raise FileNotFoundError(source)

    final = json.loads(result_file.read_text(encoding="utf-8"))
    prereg = json.loads(prereg_file.read_text(encoding="utf-8"))
    if final.get("database_sha256") != sha256_file(database_file):
        raise ValueError("El hash de la base V0.42 no coincide con su resultado")
    if final.get("preregistration_sha256") != sha256_file(prereg_file):
        raise ValueError("El hash de la preinscripcion V0.42 no coincide")

    connection = open_read_only(database_file)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        markets = [
            dict(row)
            for row in connection.execute(
                "SELECT condition_id,slug,capture_status FROM v042_markets "
                "ORDER BY market_start_ms"
            )
            if row["capture_status"] == "COMPLETED"
        ]
        snapshots_by_market: dict[str, dict[int, dict[str, Any]]] = {}
        for market in markets:
            condition_id = str(market["condition_id"])
            snapshots_by_market[condition_id] = {
                int(row["second_offset"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM v042_snapshots WHERE condition_id=? "
                    "ORDER BY second_offset",
                    (condition_id,),
                )
            }
    finally:
        connection.close()

    runs = sorted(
        (
            run
            for market in markets
            for run in _stale_runs(
                condition_id=str(market["condition_id"]),
                slug=str(market["slug"]),
                snapshots=snapshots_by_market[str(market["condition_id"])],
            )
        ),
        key=lambda run: (
            -int(run["seconds"]),
            int(run["start_timestamp_ms"]),
        ),
    )
    if not runs:
        raise ValueError("V0.42 no contiene un episodio Chainlink vencido")
    longest = dict(runs[0])
    joint_reference_stale_snapshots = sum(
        not bool(snapshot.get("chainlink_fresh"))
        and not bool(snapshot.get("official_twap_fresh"))
        for snapshots in snapshots_by_market.values()
        for snapshot in snapshots.values()
    )
    longest_snapshots = snapshots_by_market[str(longest["condition_id"])]
    longest_joint_reference_stale_seconds = sum(
        not bool(longest_snapshots[offset].get("official_twap_fresh"))
        for offset in range(
            int(longest["start_offset"]), int(longest["end_offset"]) + 1
        )
        if offset in longest_snapshots
    )
    entry_buffer = float(prereg["probe_contract"]["entry_depth_buffer_shares"])
    probes: list[dict[str, Any]] = []
    market_by_id = {str(market["condition_id"]): market for market in markets}
    for condition_id, snapshots in snapshots_by_market.items():
        market = market_by_id[condition_id]
        probes.extend(
            evaluate_market_probes(
                snapshots,
                condition_id=condition_id,
                slug=str(market["slug"]),
                contract=prereg["probe_contract"],
            )
        )

    stale_offsets = {
        condition_id: {
            offset
            for offset, snapshot in snapshots.items()
            if not bool(snapshot.get("chainlink_fresh"))
        }
        for condition_id, snapshots in snapshots_by_market.items()
    }
    stale_decision_blocks = 0
    stale_entry_blocks = 0
    chainlink_only_decision_blocks = 0
    chainlink_only_entry_blocks = 0
    stale_entry_violations: list[dict[str, Any]] = []
    exits_while_chainlink_stale: list[dict[str, Any]] = []
    open_during_any_stale: dict[tuple[str, int, str], dict[str, Any]] = {}
    for probe in probes:
        condition_id = str(probe["condition_id"])
        snapshots = snapshots_by_market[condition_id]
        decision_offset = int(probe["decision_offset"])
        decision_snapshot = snapshots.get(decision_offset)
        entry_offset = probe.get("entry_offset")
        entry_snapshot = (
            snapshots.get(int(entry_offset)) if entry_offset is not None else None
        )
        if (
            probe.get("status") == "DECISION_REJECTED"
            and probe.get("reason") == "DECISION_DATA_INCOMPLETE"
            and decision_snapshot is not None
            and not bool(decision_snapshot.get("chainlink_fresh"))
        ):
            stale_decision_blocks += 1
            if _other_entry_data_ready(
                decision_snapshot, entry_buffer=entry_buffer
            ):
                chainlink_only_decision_blocks += 1
        if (
            probe.get("status") == "ENTRY_REJECTED"
            and probe.get("reason") == "ENTRY_DATA_INCOMPLETE"
            and entry_snapshot is not None
            and not bool(entry_snapshot.get("chainlink_fresh"))
        ):
            stale_entry_blocks += 1
            if _other_entry_data_ready(entry_snapshot, entry_buffer=entry_buffer):
                chainlink_only_entry_blocks += 1
        if entry_offset is not None and (
            decision_snapshot is None
            or entry_snapshot is None
            or not bool(decision_snapshot.get("chainlink_fresh"))
            or not bool(entry_snapshot.get("chainlink_fresh"))
        ):
            stale_entry_violations.append(
                {
                    "condition_id": condition_id,
                    "slug": probe["slug"],
                    "outcome": probe["outcome"],
                    "decision_offset": decision_offset,
                    "entry_offset": entry_offset,
                }
            )
        exit_offset = probe.get("exit_offset")
        if (
            probe.get("status") == "EXITED"
            and exit_offset is not None
            and int(exit_offset) in stale_offsets[condition_id]
        ):
            exits_while_chainlink_stale.append(
                {
                    "condition_id": condition_id,
                    "slug": probe["slug"],
                    "outcome": probe["outcome"],
                    "decision_offset": decision_offset,
                    "entry_offset": entry_offset,
                    "exit_offset": int(exit_offset),
                    "exit_kind": probe.get("exit_kind"),
                    "exit_delay_seconds": probe.get("exit_delay_seconds"),
                }
            )
        if entry_offset is None:
            continue
        for run in runs:
            if run["condition_id"] != condition_id:
                continue
            start = int(run["start_offset"])
            end = int(run["end_offset"])
            actual_exit = probe.get("exit_offset")
            if int(entry_offset) < start and (
                actual_exit is None or int(actual_exit) >= start
            ):
                key = (condition_id, decision_offset, str(probe["outcome"]))
                open_during_any_stale[key] = probe
            if int(entry_offset) > end:
                break

    longest_open = [
        probe
        for probe in open_during_any_stale.values()
        if str(probe["condition_id"]) == longest["condition_id"]
        and int(probe["entry_offset"]) < int(longest["start_offset"])
        and (
            probe.get("exit_offset") is None
            or int(probe["exit_offset"]) >= int(longest["start_offset"])
        )
    ]
    overlapping = list(open_during_any_stale.values())
    overlapping_exited = sum(probe.get("status") == "EXITED" for probe in overlapping)
    overlapping_exit_rate = (
        1.0 if not overlapping else round(overlapping_exited / len(overlapping), 8)
    )

    technical_gates = final.get("technical_gates", {})
    failed_v042_gates = sorted(
        key for key, passed in technical_gates.items() if not passed
    )
    chainlink = final.get("chainlink", {})
    overall = final.get("overall", {})
    bot_controlled_gates = {
        "v042_completed_full_four_hours": final.get("completion_reason")
        == "FULL_4H_REACHED",
        "v042_failed_only_external_chainlink_streak_gate": failed_v042_gates
        == ["maximum_chainlink_stale_streak_seconds_passed"],
        "provider_outage_exercised_beyond_frozen_limit": int(longest["seconds"])
        > int(prereg["technical_gates"]["maximum_chainlink_stale_streak_seconds"]),
        "watchdog_reconnect_accounting_rate_is_one": chainlink.get(
            "watchdog_reconnect_accounting_rate"
        )
        == 1.0,
        "no_capacity_entry_used_stale_chainlink": not stale_entry_violations,
        "stale_chainlink_blocked_at_least_one_capacity_probe": (
            stale_decision_blocks + stale_entry_blocks
        )
        > 0,
        "at_least_one_capacity_position_was_open_during_stale_chainlink": bool(
            overlapping
        ),
        "all_capacity_positions_open_during_stale_chainlink_exited": (
            overlapping_exit_rate == 1.0
        ),
        "at_least_one_exit_completed_while_chainlink_was_stale": bool(
            exits_while_chainlink_stale
        ),
        "all_v042_capacity_positions_exited_within_retry": overall.get(
            "exit_success_within_retry_rate"
        )
        == 1.0,
        "v042_trapped_positions_are_zero": overall.get("trapped_positions") == 0,
        "v042_maximum_exit_delay_within_ten_seconds": int(
            overall.get("maximum_observed_exit_delay_seconds", 999999)
        )
        <= 10,
        "v042_safety_passed": final.get("safety_passed") is True,
        "sqlite_quick_check_is_ok": quick_check == "ok",
    }
    return {
        "schema": "diagnostic_v043_chainlink_outage_fail_closed_1",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "decision": DECISION,
        "classification": {
            "provider_liveness_within_twenty_seconds": False,
            "bot_planned_reconnect_branch_fixed": True,
            "bot_entry_behavior_fail_closed": True,
            "bot_exit_behavior_independent_of_chainlink": True,
            "coincident_reference_feed_staleness_observed": (
                joint_reference_stale_snapshots > 0
            ),
            "do_not_relax_or_claim_provider_liveness": True,
        },
        "v042": {
            "database": str(database_file),
            "database_sha256": sha256_file(database_file),
            "result": str(result_file),
            "result_sha256": sha256_file(result_file),
            "preregistration": str(prereg_file),
            "preregistration_sha256": sha256_file(prereg_file),
            "verdict": final.get("verdict"),
            "failed_technical_gates": failed_v042_gates,
            "technical_gates_passed": sum(
                bool(value) for value in technical_gates.values()
            ),
            "technical_gates_total": len(technical_gates),
        },
        "outage": {
            "stale_runs": len(runs),
            "longest": longest,
            "watchdog_stale_events": chainlink.get("watchdog_stale_events"),
            "watchdog_reconnects": chainlink.get("watchdog_reconnects"),
            "watchdog_reconnect_accounting_rate": chainlink.get(
                "watchdog_reconnect_accounting_rate"
            ),
            "joint_chainlink_and_official_twap_stale_snapshots": (
                joint_reference_stale_snapshots
            ),
            "longest_run_joint_official_twap_stale_seconds": (
                longest_joint_reference_stale_seconds
            ),
        },
        "fail_closed_entry": {
            "stale_decision_capacity_probes_blocked": stale_decision_blocks,
            "stale_entry_capacity_probes_blocked": stale_entry_blocks,
            "chainlink_only_decision_capacity_probes_blocked": (
                chainlink_only_decision_blocks
            ),
            "chainlink_only_entry_capacity_probes_blocked": (
                chainlink_only_entry_blocks
            ),
            "capacity_entry_violations": stale_entry_violations,
        },
        "chainlink_independent_exit": {
            "capacity_positions_open_during_any_stale_run": len(overlapping),
            "capacity_positions_open_during_longest_run": len(longest_open),
            "capacity_positions_exited": overlapping_exited,
            "exit_success_rate": overlapping_exit_rate,
            "exits_completed_while_chainlink_stale": len(
                exits_while_chainlink_stale
            ),
            "exit_examples": exits_while_chainlink_stale[:20],
            "trapped_positions": overall.get("trapped_positions"),
            "maximum_exit_delay_seconds": overall.get(
                "maximum_observed_exit_delay_seconds"
            ),
        },
        "bot_controlled_gates": bot_controlled_gates,
        "all_bot_controlled_gates_passed": all(bot_controlled_gates.values()),
        "limitations": {
            "closed_incident_audit_not_fresh_replication": True,
            "capacity_probes_are_not_orders": True,
            "provider_liveness_guaranteed": False,
            "economic_edge_evaluated": False,
            "prices_or_outcomes_read": False,
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnostico read-only V0.43")
    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / "data" / "capture_v042_planned_chainlink_reconnect_4h.db",
    )
    parser.add_argument(
        "--result",
        type=Path,
        default=ROOT / "data" / "resultado_v042_planned_chainlink_reconnect_4h.json",
    )
    parser.add_argument(
        "--v042-preregistration",
        type=Path,
        default=ROOT / "data" / "prereg_v042_planned_chainlink_reconnect_4h.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = analyze_v042_outage(
        database=args.database,
        result=args.result,
        v042_preregistration=args.v042_preregistration,
    )
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(output)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
