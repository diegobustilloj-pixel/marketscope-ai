from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v031_capture import V031_EXPECTED_MARKETS, V031_MARKET_SECONDS, open_read_only
from polymarket_bot.v031_prereg import load_and_verify_frozen_prereg
from polymarket_bot.v031_runner import load_and_verify_implementation


RESULT_SCHEMA = "result_v031_path_execution_capture_1"
FINAL_REASONS = {"FULL_1H_REACHED", "FREEZE_COLLECTOR_SAFETY"}


class V031AuditError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def audit_v031(
    *,
    database: str | Path,
    prereg_path: str | Path,
    implementation_path: str | Path,
    result_path: str | Path | None = None,
) -> dict[str, Any]:
    database_path = Path(database).resolve()
    prereg_file = Path(prereg_path).resolve()
    implementation_file = Path(implementation_path).resolve()
    output = Path(result_path).resolve() if result_path is not None else None
    prereg = load_and_verify_frozen_prereg(prereg_file)
    load_and_verify_implementation(implementation_file)
    if not database_path.is_file():
        raise V031AuditError("Base V0.31 no encontrada")
    database_hash_before = sha256_file(database_path)
    prereg_hash = sha256_file(prereg_file)
    implementation_hash = sha256_file(implementation_file)
    if output is not None and output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == RESULT_SCHEMA
            and existing.get("database_sha256") == database_hash_before
            and existing.get("preregistration_sha256") == prereg_hash
            and existing.get("implementation_sha256") == implementation_hash
        ):
            return existing
        raise V031AuditError("Existe otro resultado V0.31")

    connection = open_read_only(database_path)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        query_only = bool(int(connection.execute("PRAGMA query_only").fetchone()[0]))
        meta = {
            str(row[0]): json.loads(str(row[1]))
            for row in connection.execute("SELECT key,value FROM v031_meta")
        }
        completion_reason = meta.get("completion_reason")
        if completion_reason not in FINAL_REASONS or not meta.get("observation_ended_at"):
            raise V031AuditError("V0.31 todavia no alcanzo un estado terminal")
        market = connection.execute(
            """
            SELECT COUNT(*) AS markets,
             SUM(CASE WHEN capture_status='COMPLETED' THEN 1 ELSE 0 END) AS completed,
             SUM(CASE WHEN resolution_contract_status='VERIFIED' THEN 1 ELSE 0 END) AS verified
            FROM v031_markets
            """
        ).fetchone()
        snapshot = connection.execute(
            """
            SELECT COUNT(*) AS rows,SUM(complete) AS complete,
             SUM(chainlink_fresh) AS chainlink_fresh,
             SUM(official_twap_fresh) AS twap_fresh,
             SUM(up_book_fresh AND down_book_fresh) AS books_fresh
            FROM v031_snapshots
            """
        ).fetchone()
        per_market = connection.execute(
            """
            SELECT m.condition_id,m.slug,m.resolution_twap_window_s,
             COUNT(s.second_offset) AS snapshots,
             COUNT(DISTINCT s.second_offset) AS unique_seconds,
             SUM(CASE WHEN s.official_twap_window_s!=m.resolution_twap_window_s THEN 1 ELSE 0 END) AS misaligned
            FROM v031_markets AS m
            LEFT JOIN v031_snapshots AS s USING(condition_id)
            GROUP BY m.condition_id,m.slug,m.resolution_twap_window_s
            ORDER BY m.market_start_ms
            """
        ).fetchall()
        schema_columns = {
            str(row[1]).lower()
            for table in ("v031_markets", "v031_snapshots")
            for row in connection.execute(f"PRAGMA table_info([{table}])")
        }
    finally:
        connection.close()

    markets = int(market["markets"] or 0)
    completed = int(market["completed"] or 0)
    verified = int(market["verified"] or 0)
    rows = int(snapshot["rows"] or 0)
    complete = int(snapshot["complete"] or 0)
    chainlink_fresh = int(snapshot["chainlink_fresh"] or 0)
    twap_fresh = int(snapshot["twap_fresh"] or 0)
    books_fresh = int(snapshot["books_fresh"] or 0)
    market_coverage = min(1.0, markets / V031_EXPECTED_MARKETS)
    snapshot_coverage = rows / (markets * V031_MARKET_SECONDS) if markets else 0.0
    complete_coverage = complete / rows if rows else 0.0
    chainlink_coverage = chainlink_fresh / rows if rows else 0.0
    twap_coverage = twap_fresh / rows if rows else 0.0
    books_coverage = books_fresh / rows if rows else 0.0
    contract_coverage = verified / markets if markets else 0.0
    minimum_snapshots = min((int(row["snapshots"]) for row in per_market), default=0)
    maximum_missing = max(
        (V031_MARKET_SECONDS - int(row["unique_seconds"]) for row in per_market),
        default=V031_MARKET_SECONDS,
    )
    misaligned = sum(int(row["misaligned"] or 0) for row in per_market)
    gates = prereg["technical_gates"]
    technical_gates = {
        "sqlite_quick_check_passed": quick_check == str(gates["sqlite_quick_check_required"]),
        "market_coverage_passed": market_coverage >= float(gates["minimum_market_coverage"]),
        "snapshot_coverage_passed": snapshot_coverage >= float(gates["minimum_snapshot_coverage"]),
        "complete_snapshot_coverage_passed": complete_coverage >= float(gates["minimum_complete_snapshot_coverage"]),
        "chainlink_fresh_coverage_passed": chainlink_coverage >= float(gates["minimum_chainlink_fresh_coverage"]),
        "official_twap_fresh_coverage_passed": twap_coverage >= float(gates["minimum_official_twap_fresh_coverage"]),
        "both_books_fresh_coverage_passed": books_coverage >= float(gates["minimum_both_books_fresh_coverage"]),
        "resolution_contract_coverage_passed": contract_coverage >= float(gates["required_resolution_contract_coverage"]),
        "minimum_snapshots_per_market_passed": minimum_snapshots >= int(gates["minimum_snapshots_per_captured_market"]),
        "maximum_missing_seconds_passed": maximum_missing <= int(gates["maximum_missing_seconds_per_captured_market"]),
        "twap_contract_alignment_passed": misaligned == 0,
        "completed_markets_passed": completed == markets and markets > 0,
    }
    forbidden_fragments = ("label", "outcome", "pnl", "signal", "order")
    forbidden_columns = sorted(
        column
        for column in schema_columns
        if any(fragment in column for fragment in forbidden_fragments)
    )
    safety_gates = {
        "orders_disabled_passed": meta.get("orders_enabled") is False,
        "paper_orders_disabled_passed": meta.get("paper_orders_enabled") is False,
        "wallet_not_required_passed": meta.get("wallet_required") is False,
        "real_money_disabled_passed": meta.get("money_real_enabled") is False,
        "real_money_blocked_passed": meta.get("real_money") == "BLOQUEADO",
        "outcomes_not_read_passed": meta.get("outcomes_read") == 0,
        "pnl_not_calculated_passed": meta.get("pnl_calculated") is False,
        "signals_not_generated_passed": meta.get("signals_generated") is False,
        "forbidden_schema_columns_absent_passed": not forbidden_columns,
    }
    technical_passed = all(technical_gates.values())
    safety_passed = all(safety_gates.values())
    if not safety_passed:
        verdict = "FAIL_SAFETY"
    elif completion_reason == "FREEZE_COLLECTOR_SAFETY" or not technical_passed:
        verdict = "FAIL_TECHNICAL_QUALITY"
    else:
        verdict = "PASS_TECHNICAL_CAPTURE_ONLY"

    database_hash_after = sha256_file(database_path)
    if database_hash_after != database_hash_before:
        raise V031AuditError("La base V0.31 cambio durante la auditoria")
    result = {
        "schema": RESULT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": prereg["variant"],
        "verdict": verdict,
        "meaning": "technical_capture_only_no_economic_claim",
        "database": str(database_path),
        "database_sha256": database_hash_before,
        "database_sha256_after": database_hash_after,
        "database_read_only_verified": query_only,
        "preregistration_sha256": prereg_hash,
        "implementation_sha256": implementation_hash,
        "completion_reason": completion_reason,
        "window": {
            "capture_start_at": meta.get("capture_start_at"),
            "target_end_at": meta.get("target_end_at"),
            "observation_ended_at": meta.get("observation_ended_at"),
            "technical_pilot_hours": 1.0,
        },
        "coverage": {
            "expected_markets": V031_EXPECTED_MARKETS,
            "markets": markets,
            "completed_markets": completed,
            "market_coverage": round(market_coverage, 8),
            "snapshots": rows,
            "snapshot_coverage": round(snapshot_coverage, 8),
            "complete_snapshots": complete,
            "complete_snapshot_coverage": round(complete_coverage, 8),
            "chainlink_fresh_coverage": round(chainlink_coverage, 8),
            "official_twap_fresh_coverage": round(twap_coverage, 8),
            "both_books_fresh_coverage": round(books_coverage, 8),
            "verified_resolution_contracts": verified,
            "resolution_contract_coverage": round(contract_coverage, 8),
            "minimum_snapshots_per_market": minimum_snapshots,
            "maximum_missing_seconds_per_market": maximum_missing,
            "twap_window_misaligned_snapshots": misaligned,
        },
        "per_market": [dict(row) for row in per_market],
        "technical_passed": technical_passed,
        "technical_gates": technical_gates,
        "safety_passed": safety_passed,
        "safety_gates": safety_gates,
        "forbidden_schema_columns": forbidden_columns,
        "economic_strategy_present": False,
        "economic_edge_evaluated": False,
        "outcomes_read": 0,
        "pnl_calculated": False,
        "signals_generated": False,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
        "promotion": {
            "fresh_strategy_preregistration_required": True,
            "automatic_followup_launch": False,
            "paper_or_money_candidate": False,
        },
    }
    if output is not None:
        _write_atomic(output, result)
    return result


__all__ = ["RESULT_SCHEMA", "V031AuditError", "audit_v031"]
