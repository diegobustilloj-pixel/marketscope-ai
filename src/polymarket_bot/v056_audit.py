from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v056_contract import VARIANT, load_and_verify_preregistration
from polymarket_bot.v056_rfq_observer import database_footprint


RESULT_SCHEMA = "result_v056_bounded_rfq_observer_1"


class V056AuditError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _meta(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        str(row[0]): json.loads(str(row[1]))
        for row in connection.execute("SELECT key,value FROM v056_meta ORDER BY key")
    }


def _clock_phase(
    connection: sqlite3.Connection,
    *,
    phase: str,
    minimum_samples: int,
    maximum_width_ms: int,
) -> dict[str, Any]:
    rows = list(
        connection.execute(
            """
            SELECT round_trip_ms,server_minus_local_lower_ms,server_minus_local_upper_ms
            FROM v056_clock_samples WHERE phase=? ORDER BY sample_id
            """,
            (phase,),
        )
    )
    if not rows:
        return {
            "phase": phase,
            "samples": 0,
            "consistent_interval": False,
            "conclusion_allowed": False,
            "server_minus_local_lower_ms": None,
            "server_minus_local_upper_ms": None,
            "interval_width_ms": None,
            "offset_midpoint_ms": None,
            "minimum_round_trip_ms": None,
        }
    lower = max(int(row[1]) for row in rows)
    upper = min(int(row[2]) for row in rows)
    consistent = lower <= upper
    width = upper - lower if consistent else None
    enough = len(rows) >= minimum_samples
    return {
        "phase": phase,
        "samples": len(rows),
        "consistent_interval": consistent,
        "conclusion_allowed": bool(
            enough and consistent and width is not None and width <= maximum_width_ms
        ),
        "server_minus_local_lower_ms": lower,
        "server_minus_local_upper_ms": upper,
        "interval_width_ms": width,
        "offset_midpoint_ms": ((lower + upper) / 2) if consistent else None,
        "minimum_round_trip_ms": min(int(row[0]) for row in rows),
    }


def _histogram_summary(
    rows: list[sqlite3.Row], *, width_ms: int, minimum_ms: int, maximum_ms: int
) -> dict[str, Any]:
    total = sum(int(row[1]) for row in rows)
    if total == 0:
        return {
            "count": 0,
            "median_bucket_lower_ms": None,
            "median_bucket_upper_ms": None,
            "underflow_count": 0,
            "overflow_count": 0,
        }
    target = (total + 1) // 2
    cumulative = 0
    median_lower: int | None = None
    for row in rows:
        cumulative += int(row[1])
        if cumulative >= target:
            median_lower = int(row[0])
            break
    underflow_key = minimum_ms - width_ms
    overflow_key = maximum_ms + width_ms
    counts = {int(row[0]): int(row[1]) for row in rows}
    if median_lower == underflow_key:
        median_upper = minimum_ms
    elif median_lower == overflow_key:
        median_upper = None
    else:
        median_upper = median_lower + width_ms
    return {
        "count": total,
        "median_bucket_lower_ms": median_lower,
        "median_bucket_upper_ms": median_upper,
        "underflow_count": counts.get(underflow_key, 0),
        "overflow_count": counts.get(overflow_key, 0),
    }


def inspect_database(
    *, prereg_path: str | Path, database_path: str | Path, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    database = Path(database_path).resolve()
    prereg = load_and_verify_preregistration(prereg_file, project_root=root)
    if not database.is_file():
        return {
            "status": "NOT_STARTED",
            "database_exists": False,
            "credential_values_exposed": False,
            "orders_created": 0,
            "paper_orders": 0,
            "transactions_created": 0,
            "real_money": "BLOQUEADO",
        }
    storage = prereg["contract"]["storage"]
    clock_contract = prereg["contract"]["clock"]
    connection = _open_read_only(database)
    try:
        meta = _meta(connection)
        expected_meta = {
            "preregistration_sha256": sha256_file(prereg_file),
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "quote_submission_enabled": False,
            "signatures_enabled": False,
            "transactions_enabled": False,
            "credential_values_stored": False,
            "addresses_stored": False,
            "raw_payloads_stored": False,
            "profitability_measured": False,
            "pnl_measured": False,
            "real_money": "BLOQUEADO",
        }
        for key, expected in expected_meta.items():
            if meta.get(key) != expected:
                raise V056AuditError(f"Base V0.56 incompatible o insegura: {key}")
        tables = (
            "v056_meta",
            "v056_runs",
            "v056_message_counts",
            "v056_request_totals",
            "v056_request_buckets",
            "v056_headroom_histogram",
            "v056_conditions",
            "v056_request_samples",
            "v056_non_request_events",
            "v056_clock_samples",
        )
        columns = {
            str(row[1])
            for table in tables
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        forbidden = {
            "api_key",
            "api_secret",
            "passphrase",
            "signer_address",
            "maker_address",
            "private_key",
            "raw_payload",
        }
        present_forbidden = sorted(columns.intersection(forbidden))
        if present_forbidden:
            raise V056AuditError(f"Columnas sensibles V0.56: {present_forbidden}")
        runs = [dict(row) for row in connection.execute("SELECT * FROM v056_runs ORDER BY run_id")]
        type_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT message_type,event_count FROM v056_message_counts ORDER BY message_type"
            )
        }
        totals_row = connection.execute(
            "SELECT * FROM v056_request_totals ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        totals = dict(totals_row) if totals_row is not None else {}
        distinct_conditions = int(
            connection.execute("SELECT COUNT(*) FROM v056_conditions").fetchone()[0]
        )
        direction_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT direction,SUM(request_count) FROM v056_request_buckets GROUP BY direction ORDER BY direction"
            )
        }
        leg_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT leg_count,SUM(request_count) FROM v056_request_buckets GROUP BY leg_count ORDER BY leg_count"
            )
        }
        minute_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT minute_index,SUM(request_count) FROM v056_request_buckets GROUP BY minute_index ORDER BY minute_index"
            )
        }
        histogram_rows = list(
            connection.execute(
                "SELECT bucket_lower_ms,request_count FROM v056_headroom_histogram ORDER BY bucket_lower_ms"
            )
        )
        histogram = _histogram_summary(
            histogram_rows,
            width_ms=int(storage["headroom_histogram_width_ms"]),
            minimum_ms=int(storage["headroom_histogram_minimum_ms"]),
            maximum_ms=int(storage["headroom_histogram_maximum_ms"]),
        )
        clock_start = _clock_phase(
            connection,
            phase="START",
            minimum_samples=int(clock_contract["minimum_successful_samples_per_phase"]),
            maximum_width_ms=int(clock_contract["maximum_interval_width_ms"]),
        )
        clock_end = _clock_phase(
            connection,
            phase="END",
            minimum_samples=int(clock_contract["minimum_successful_samples_per_phase"]),
            maximum_width_ms=int(clock_contract["maximum_interval_width_ms"]),
        )
        auth_successes = int(
            connection.execute(
                "SELECT COUNT(*) FROM v056_non_request_events WHERE message_type='auth' AND auth_success=1"
            ).fetchone()[0]
        )
        stored_non_request_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT message_type,COUNT(*) FROM v056_non_request_events GROUP BY message_type ORDER BY message_type"
            )
        }
        prices = [
            int(str(row[0]))
            for row in connection.execute(
                "SELECT price_e6 FROM v056_non_request_events WHERE message_type='RFQ_TRADE' AND price_e6 GLOB '[0-9]*'"
            )
            if str(row[0]).isdigit()
        ]
    finally:
        connection.close()
    last_status = str(runs[-1]["status"]) if runs else "NOT_STARTED"
    terminal = last_status in {
        "COMPLETED",
        "AUTH_FAILED",
        "TRANSPORT_FAILED",
        "STORAGE_LIMIT_REACHED",
        "LOCAL_STORAGE_FAILED",
        "INTERRUPTED",
    }
    database_bytes = database_footprint(database)
    maximum_bytes = int(storage["maximum_database_bytes"])
    request_count = int(totals.get("request_count", 0))
    deadline_count = int(totals.get("with_deadline_count", 0))
    raw_mean = (
        int(totals.get("headroom_sum_ms", 0)) / deadline_count if deadline_count else None
    )
    clock_conclusion_allowed = bool(
        clock_start["conclusion_allowed"] and clock_end["conclusion_allowed"]
    )
    offset_midpoint = None
    if clock_conclusion_allowed:
        offset_midpoint = (
            float(clock_start["offset_midpoint_ms"])
            + float(clock_end["offset_midpoint_ms"])
        ) / 2
    corrected_median = None
    if offset_midpoint is not None and histogram["median_bucket_lower_ms"] is not None:
        corrected_median = {
            "lower_ms": histogram["median_bucket_lower_ms"] - offset_midpoint,
            "upper_ms": (
                histogram["median_bucket_upper_ms"] - offset_midpoint
                if histogram["median_bucket_upper_ms"] is not None
                else None
            ),
        }
    return {
        "status": last_status,
        "database_exists": True,
        "terminal": terminal,
        "runs": runs,
        "message_type_counts": type_counts,
        "stored_non_request_type_counts": stored_non_request_counts,
        "auth_successes": auth_successes,
        "rfq_requests": request_count,
        "rfq_trades": int(type_counts.get("RFQ_TRADE", 0)),
        "distinct_conditions": distinct_conditions,
        "request_directions": direction_counts,
        "request_leg_counts": leg_counts,
        "request_minute_counts": minute_counts,
        "request_sampling": {
            "eligible": int(totals.get("sample_eligible_count", 0)),
            "stored": int(totals.get("sample_stored_count", 0)),
            "maximum": int(storage["maximum_request_samples"]),
            "configured_fraction": 1 / int(storage["request_sample_hash_modulus"]),
            "observed_fraction": (
                int(totals.get("sample_stored_count", 0)) / request_count
                if request_count
                else None
            ),
        },
        "submission_headroom_raw_ms": {
            "count": deadline_count,
            "negative_count": int(totals.get("negative_headroom_count", 0)),
            "nonnegative_count": int(totals.get("nonnegative_headroom_count", 0)),
            "minimum": totals.get("headroom_min_ms"),
            "mean": raw_mean,
            "maximum": totals.get("headroom_max_ms"),
            **histogram,
        },
        "server_clock": {
            "start": clock_start,
            "end": clock_end,
            "deadline_clock_conclusion_allowed": clock_conclusion_allowed,
            "average_offset_midpoint_ms": offset_midpoint,
            "corrected_headroom_median_bucket_ms": corrected_median,
        },
        "observed_trade_price_e6": {
            "count": len(prices),
            "minimum": min(prices) if prices else None,
            "maximum": max(prices) if prices else None,
        },
        "all_observed_trade_records_stored": (
            int(stored_non_request_counts.get("RFQ_TRADE", 0))
            == int(type_counts.get("RFQ_TRADE", 0))
        ),
        "database_bytes": database_bytes,
        "maximum_database_bytes": maximum_bytes,
        "database_utilization": database_bytes / maximum_bytes,
        "database_sha256": sha256_file(database),
        "credential_values_exposed": False,
        "orders_created": 0,
        "paper_orders": 0,
        "quotes_submitted": 0,
        "confirmations_sent": 0,
        "transactions_created": 0,
        "profitability_measured": False,
        "pnl_measured": False,
        "real_money": "BLOQUEADO",
        "contract": prereg["contract"],
    }


def classify_audit(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    contract = snapshot["contract"]
    decision = contract["decision"]
    if snapshot["status"] == "STORAGE_LIMIT_REACHED":
        verdict = decision["storage_limit"]
        next_step = "REDESIGN_STORAGE_BEFORE_ANY_FRESH_OBSERVER"
    elif snapshot["status"] == "LOCAL_STORAGE_FAILED":
        verdict = decision["local_storage_failure"]
        next_step = "REVIEW_LOCAL_STORAGE_FAILURE_BEFORE_ANY_FRESH_OBSERVER"
    elif snapshot["status"] == "AUTH_FAILED" or int(snapshot["auth_successes"]) == 0:
        verdict = decision["auth_failure"]
        next_step = "VERIFY_CLOB_CREDENTIALS_WITHOUT_ENABLING_QUOTES"
    elif snapshot["status"] != "COMPLETED":
        verdict = decision["incomplete_runtime"]
        next_step = "REVIEW_TRANSPORT_FAILURE_BEFORE_ANY_FRESH_OBSERVER"
    elif (
        int(snapshot["rfq_requests"]) < int(decision["minimum_rfq_requests"])
        or int(snapshot["rfq_trades"]) < int(decision["minimum_rfq_trades"])
    ):
        verdict = decision["insufficient_activity"]
        next_step = "DO_NOT_INFER_ECONOMICS_FROM_SPARSE_RFQ_ACTIVITY"
    elif int(snapshot["request_sampling"]["stored"]) < int(
        decision["minimum_request_samples"]
    ):
        verdict = decision["insufficient_sample"]
        next_step = "REVIEW_REQUEST_SAMPLING_WITHOUT_ENABLING_QUOTES"
    elif float(snapshot["database_utilization"]) > float(
        decision["maximum_database_utilization"]
    ):
        verdict = decision["storage_inefficient"]
        next_step = "REDUCE_STORAGE_UTILIZATION_BEFORE_JOINED_REPLAY"
    else:
        verdict = decision["activity_observable"]
        next_step = "DESIGN_SEPARATE_PREREGISTERED_CLOB_JOINED_PAPER_REPLAY_WITHOUT_QUOTES"
    return {
        "verdict": verdict,
        "next_step": next_step,
        "profitability_conclusion_allowed": False,
        "pnl_conclusion_allowed": False,
    }


def audit_v056(
    *,
    prereg_path: str | Path,
    database_path: str | Path,
    result_path: str | Path,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    snapshot = inspect_database(
        prereg_path=prereg_path,
        database_path=database_path,
        project_root=project_root,
    )
    if not snapshot.get("database_exists") or not snapshot.get("terminal"):
        raise V056AuditError("V0.56 no tiene una captura terminal auditable")
    classification = classify_audit(snapshot)
    contract = snapshot.pop("contract")
    payload = {
        "schema": RESULT_SCHEMA,
        "variant": VARIANT,
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **classification,
        "observation": snapshot,
        "interpretation": {
            "rfq_activity_only": True,
            "all_request_counts_are_exact": True,
            "request_details_are_hash_sampled": True,
            "all_observed_trade_records_are_stored": snapshot[
                "all_observed_trade_records_stored"
            ],
            "requests_have_no_competing_quote_prices": True,
            "trade_prices_are_not_strategy_pnl": True,
            "clob_leg_books_joined": False,
            "deadline_clock_conclusion_allowed": snapshot["server_clock"][
                "deadline_clock_conclusion_allowed"
            ],
            "profitability_measured": False,
            "pnl_measured": False,
            "automatic_followup_launched": False,
            "next_step": classification["next_step"],
        },
        "safety": {
            "authentication_used": int(snapshot["auth_successes"]) > 0,
            "credentials_persisted": False,
            "addresses_persisted": False,
            "orders_created": 0,
            "paper_orders": 0,
            "quotes_submitted": 0,
            "confirmations_sent": 0,
            "transactions_created": 0,
            "private_key_required": False,
            "real_money": "BLOQUEADO",
        },
        "frozen_contract": contract,
    }
    output = Path(result_path).resolve()
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        comparable = dict(existing)
        comparable["completed_at"] = payload["completed_at"]
        if comparable != payload:
            raise V056AuditError("Ya existe otro resultado V0.56")
        return existing
    _write_atomic(output, payload)
    return payload


__all__ = [
    "RESULT_SCHEMA",
    "V056AuditError",
    "audit_v056",
    "classify_audit",
    "inspect_database",
]
