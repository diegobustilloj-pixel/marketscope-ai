from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file


PLANNED_STALE_RECONNECT_BACKOFF_MS = 250
CHAINLINK_WATCHDOG_SECONDS = 12.0
MAXIMUM_CHAINLINK_STALE_STREAK_SECONDS = 20


def diagnose_v041_planned_reconnect(
    *, result: str | Path, stderr_log: str | Path
) -> dict[str, Any]:
    result_file = Path(result).resolve()
    log_file = Path(stderr_log).resolve()
    if not result_file.is_file() or not log_file.is_file():
        raise FileNotFoundError("Falta el resultado o el log final V0.41")
    final = json.loads(result_file.read_text(encoding="utf-8"))
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    chainlink_stale_lines = [
        line for line in lines if "v041-rtds-chainlink sin Chainlink" in line
    ]
    chainlink_disconnect_lines = [
        line for line in lines
        if "v041-rtds-chainlink desconectado; reconexion en" in line
    ]
    backoffs = []
    for line in chainlink_disconnect_lines:
        match = re.search(r"reconexion en ([0-9]+(?:\.[0-9]+)?)s", line)
        if match:
            backoffs.append(float(match.group(1)))

    gates = final.get("technical_gates", {})
    failed_gates = sorted(key for key, passed in gates.items() if not passed)
    chainlink = final.get("chainlink", {})
    overall = final.get("overall", {})
    minimum_observed_backoff = min(backoffs, default=math.inf)
    saved_seconds = max(
        0.0,
        minimum_observed_backoff
        - PLANNED_STALE_RECONNECT_BACKOFF_MS / 1000.0,
    )
    counterfactual_worst_streak = math.ceil(
        max(0.0, float(chainlink.get("longest_stale_streak_seconds", 0)) - saved_seconds)
    )
    design_gates = {
        "v041_failed_only_chainlink_streak_gate": failed_gates
        == ["maximum_chainlink_stale_streak_seconds_passed"],
        "v041_longest_streak_missed_by_one_second": chainlink.get(
            "longest_stale_streak_seconds"
        )
        == MAXIMUM_CHAINLINK_STALE_STREAK_SECONDS + 1,
        "v041_chainlink_coverage_exceeded_ninety_nine_percent": float(
            chainlink.get("snapshot_coverage", 0.0)
        )
        > 0.99,
        "v041_watchdog_detected_three_silences": chainlink.get(
            "watchdog_stale_events"
        )
        == 3,
        "v041_dedicated_reconnect_counter_missed_events": chainlink.get(
            "watchdog_reconnects"
        )
        == 0,
        "logs_show_watchdog_then_general_disconnect_path": len(
            chainlink_stale_lines
        )
        == 3
        and len(chainlink_disconnect_lines) >= 3,
        "observed_general_backoff_exceeded_planned_backoff": bool(backoffs)
        and minimum_observed_backoff
        > PLANNED_STALE_RECONNECT_BACKOFF_MS / 1000.0,
        "counterfactual_is_compatible_with_existing_twenty_second_gate": (
            counterfactual_worst_streak
            <= MAXIMUM_CHAINLINK_STALE_STREAK_SECONDS
        ),
        "v041_exit_success_rate_was_one": overall.get(
            "exit_success_within_retry_rate"
        )
        == 1.0,
        "v041_trapped_positions_were_zero": overall.get("trapped_positions")
        == 0,
        "v041_safety_passed": final.get("safety_passed") is True,
    }
    return {
        "schema": "diagnostic_v042_planned_chainlink_reconnect_1",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "decision": "PREPARE_ONE_FRESH_V042_PLANNED_CHAINLINK_RECONNECT_REPLICATION",
        "cause": (
            "WATCHDOG_INITIATED_CLOSE_RAISED_CONNECTION_CLOSED_AND_WAS_HANDLED_AS_"
            "A_GENERAL_DISCONNECT_WITH_RANDOM_EXPONENTIAL_BACKOFF"
        ),
        "v041_evidence": {
            "result": str(result_file),
            "result_sha256": sha256_file(result_file),
            "stderr_log": str(log_file),
            "stderr_log_sha256": sha256_file(log_file),
            "verdict": final.get("verdict"),
            "failed_technical_gates": failed_gates,
            "technical_gates_passed": sum(bool(value) for value in gates.values()),
            "technical_gates_total": len(gates),
            "chainlink_snapshot_coverage": chainlink.get("snapshot_coverage"),
            "chainlink_longest_stale_streak_seconds": chainlink.get(
                "longest_stale_streak_seconds"
            ),
            "chainlink_watchdog_stale_events": chainlink.get(
                "watchdog_stale_events"
            ),
            "chainlink_watchdog_reconnects": chainlink.get(
                "watchdog_reconnects"
            ),
            "chainlink_stale_log_lines": len(chainlink_stale_lines),
            "chainlink_general_disconnect_log_lines": len(
                chainlink_disconnect_lines
            ),
            "observed_general_backoff_seconds": backoffs,
        },
        "v042_design": {
            "chainlink_watchdog_seconds_preserved": CHAINLINK_WATCHDOG_SECONDS,
            "maximum_chainlink_stale_streak_seconds_preserved": (
                MAXIMUM_CHAINLINK_STALE_STREAK_SECONDS
            ),
            "planned_stale_reconnect_backoff_ms": (
                PLANNED_STALE_RECONNECT_BACKOFF_MS
            ),
            "planned_close_connection_closed_action": (
                "COUNT_DEDICATED_RECONNECT_AND_USE_DETERMINISTIC_BACKOFF"
            ),
            "unplanned_disconnect_action_unchanged": True,
            "v041_chainlink_predicate_unchanged": True,
            "v041_market_entry_exit_contracts_unchanged": True,
            "parameter_grid_used": False,
            "threshold_relaxed_after_result": False,
            "counterfactual_worst_streak_seconds": counterfactual_worst_streak,
        },
        "design_gates": design_gates,
        "all_design_gates_passed": all(design_gates.values()),
        "limitations": {
            "counterfactual_replay_is_not_fresh_network_proof": True,
            "fresh_capture_required": True,
            "economic_edge_evaluated": False,
            "actual_order_submission_tested": False,
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnóstico read-only V0.42")
    parser.add_argument(
        "--result",
        type=Path,
        default=ROOT / "data" / "resultado_v041_chainlink_silence_watchdog_4h.json",
    )
    parser.add_argument(
        "--stderr-log",
        type=Path,
        default=ROOT / "logs" / "v041_monitor_stderr.log",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = diagnose_v041_planned_reconnect(
        result=args.result, stderr_log=args.stderr_log
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
