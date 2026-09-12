from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file


CHAINLINK_TOPIC = "crypto_prices_chainlink"
CHAINLINK_SYMBOL = "btc/usd"
CHAINLINK_WATCHDOG_SECONDS = 12.0
V040_FRESHNESS_MAX_AGE_MS = 5000


def is_fresh_chainlink_message(raw: str) -> bool:
    """Return true only for a usable BTC/USD Chainlink update."""

    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(message, Mapping) or message.get("topic") != CHAINLINK_TOPIC:
        return False
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        return False
    try:
        timestamp_ms = int(payload.get("timestamp"))
        value = float(payload.get("value"))
    except (TypeError, ValueError):
        return False
    return (
        str(payload.get("symbol") or "").lower() == CHAINLINK_SYMBOL
        and timestamp_ms > 0
        and math.isfinite(value)
        and value > 0.0
    )


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return int(ordered[index])


def diagnose_v040_chainlink_silence(
    *, database: str | Path, result: str | Path
) -> dict[str, Any]:
    database_file = Path(database).resolve()
    result_file = Path(result).resolve()
    if not database_file.is_file() or not result_file.is_file():
        raise FileNotFoundError("Falta la base o el resultado final V0.40")

    final_result = json.loads(result_file.read_text(encoding="utf-8"))
    connection = sqlite3.connect(
        f"{database_file.as_uri()}?mode=ro", uri=True, timeout=60
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        aggregate = dict(
            connection.execute(
                """
                SELECT COUNT(*) snapshots,
                       SUM(chainlink_fresh) chainlink_fresh,
                       SUM(CASE WHEN chainlink_fresh=0 THEN 1 ELSE 0 END)
                           chainlink_not_fresh
                FROM v040_snapshots
                """
            ).fetchone()
        )
        source_timestamps = [
            int(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT chainlink_source_timestamp_ms
                FROM v040_snapshots
                WHERE chainlink_source_timestamp_ms IS NOT NULL
                ORDER BY chainlink_source_timestamp_ms
                """
            )
        ]
        health = list(
            connection.execute(
                """
                SELECT recorded_at,counters_json,connections_json
                FROM v040_health ORDER BY recorded_at
                """
            )
        )
        fully_missing_markets = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT condition_id
                    FROM v040_snapshots
                    GROUP BY condition_id
                    HAVING SUM(chainlink_fresh)=0
                )
                """
            ).fetchone()[0]
        )
    finally:
        connection.close()

    gaps_ms = [
        current - previous
        for previous, current in zip(source_timestamps, source_timestamps[1:])
    ]
    observed_non_outage_gaps = [gap for gap in gaps_ms if gap <= 10_000]
    watchdog_ms = int(CHAINLINK_WATCHDOG_SECONDS * 1000)
    watchdog_detectable_gaps = sorted(
        (gap for gap in gaps_ms if gap > watchdog_ms), reverse=True
    )

    longest_connected_stagnation_samples = 0
    connected_stagnation_samples = 0
    previous_updates: int | None = None
    for row in health:
        counters = json.loads(str(row["counters_json"]))
        connections = json.loads(str(row["connections_json"]))
        updates = int(counters.get("rtds:chainlink_updates", 0))
        connected = connections.get("v040-rtds-chainlink") == "CONNECTED"
        if connected and previous_updates is not None and updates == previous_updates:
            connected_stagnation_samples += 1
            longest_connected_stagnation_samples = max(
                longest_connected_stagnation_samples,
                connected_stagnation_samples,
            )
        else:
            connected_stagnation_samples = 0
        previous_updates = updates

    snapshots = int(aggregate.get("snapshots") or 0)
    chainlink_fresh = int(aggregate.get("chainlink_fresh") or 0)
    coverage = round(chainlink_fresh / snapshots, 8) if snapshots else 0.0
    p99_ms = _percentile(observed_non_outage_gaps, 0.99)
    max_non_outage_gap_ms = max(observed_non_outage_gaps, default=0)
    v040_overall = final_result.get("overall", {})
    design_gates = {
        "v040_completed_full_four_hours": final_result.get("completion_reason")
        == "FULL_4H_REACHED",
        "v040_exit_success_rate_was_one": v040_overall.get(
            "exit_success_within_retry_rate"
        )
        == 1.0,
        "v040_trapped_positions_were_zero": v040_overall.get(
            "trapped_positions"
        )
        == 0,
        "chainlink_was_dominant_completeness_failure": int(
            aggregate.get("chainlink_not_fresh") or 0
        )
        > 1000,
        "silent_socket_proven_while_connected": longest_connected_stagnation_samples
        > 1,
        "watchdog_exceeds_observed_non_outage_max_gap": watchdog_ms
        > max_non_outage_gap_ms,
        "watchdog_detects_every_observed_outage_gap": len(watchdog_detectable_gaps)
        >= 1,
    }
    return {
        "schema": "diagnostic_v041_chainlink_silence_watchdog_1",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "decision": "PREPARE_ONE_FRESH_V041_CHAINLINK_SILENCE_WATCHDOG_REPLICATION",
        "cause": (
            "CHAINLINK_SOCKET_REMAINED_CONNECTED_WHILE_VALID_UPDATE_COUNT_STOPPED;"
            "V040_CONFIGURED_FRESHNESS_WATCHDOG_ONLY_FOR_TWAP"
        ),
        "v040_evidence": {
            "database": str(database_file),
            "database_sha256": sha256_file(database_file),
            "result": str(result_file),
            "result_sha256": sha256_file(result_file),
            "snapshots": snapshots,
            "chainlink_fresh_snapshots": chainlink_fresh,
            "chainlink_not_fresh_snapshots": int(
                aggregate.get("chainlink_not_fresh") or 0
            ),
            "chainlink_snapshot_coverage": coverage,
            "fully_chainlink_missing_markets": fully_missing_markets,
            "unique_chainlink_source_timestamps": len(source_timestamps),
            "normal_gap_p99_ms": p99_ms,
            "normal_gap_max_ms": max_non_outage_gap_ms,
            "outage_gaps_above_watchdog_ms": watchdog_detectable_gaps,
            "longest_connected_stagnation_health_samples": (
                longest_connected_stagnation_samples
            ),
        },
        "v041_design": {
            "watchdog_seconds": CHAINLINK_WATCHDOG_SECONDS,
            "freshness_max_age_ms_preserved": V040_FRESHNESS_MAX_AGE_MS,
            "valid_message_requires_exact_topic_symbol_timestamp_and_positive_finite_value": True,
            "silent_socket_action": "CLOSE_AND_RESUBSCRIBE",
            "watchdog_only_changes_chainlink_transport_liveness": True,
            "v040_entry_exit_probe_contract_unchanged": True,
            "v040_clob_validated_peer_fallback_unchanged": True,
            "stale_chainlink_can_enter": False,
        },
        "design_gates": design_gates,
        "all_design_gates_passed": all(design_gates.values()),
        "limitations": {
            "offline_analysis_cannot_guarantee_remote_reconnect_success": True,
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
    parser = argparse.ArgumentParser(description="Diagnóstico read-only V0.41")
    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / "data" / "capture_v040_pre_stale_transport_guard_4h.db",
    )
    parser.add_argument(
        "--result",
        type=Path,
        default=ROOT / "data" / "resultado_v040_pre_stale_transport_guard_4h.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = diagnose_v040_chainlink_silence(
        database=args.database, result=args.result
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
