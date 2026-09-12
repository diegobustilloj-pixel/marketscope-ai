from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v055_contract import VARIANT, load_and_verify_preregistration


RESULT_SCHEMA = "result_v055_authenticated_rfq_activity_observer_1"


class V055AuditError(RuntimeError):
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
        for row in connection.execute("SELECT key,value FROM v055_meta ORDER BY key")
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
                raise V055AuditError(f"Base V0.55 incompatible o insegura: {key}")
        columns = {
            str(row[1])
            for table in ("v055_meta", "v055_runs", "v055_events")
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
            raise V055AuditError(f"Columnas sensibles V0.55: {present_forbidden}")
        runs = [dict(row) for row in connection.execute("SELECT * FROM v055_runs ORDER BY run_id")]
        type_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT message_type,COUNT(*) FROM v055_events GROUP BY message_type ORDER BY message_type"
            )
        }
        auth_successes = int(
            connection.execute(
                "SELECT COUNT(*) FROM v055_events WHERE message_type='auth' AND auth_success=1"
            ).fetchone()[0]
        )
        request_count = int(type_counts.get("RFQ_REQUEST", 0))
        trade_count = int(type_counts.get("RFQ_TRADE", 0))
        distinct_conditions = int(
            connection.execute(
                "SELECT COUNT(DISTINCT condition_id) FROM v055_events WHERE condition_id IS NOT NULL"
            ).fetchone()[0]
        )
        direction_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT direction,COUNT(*) FROM v055_events WHERE message_type='RFQ_REQUEST' GROUP BY direction ORDER BY direction"
            )
            if row[0] is not None
        }
        leg_counts: Counter[int] = Counter()
        headroom: list[int] = []
        for row in connection.execute(
            "SELECT received_at_ms,submission_deadline_ms,leg_position_ids_json FROM v055_events WHERE message_type='RFQ_REQUEST'"
        ):
            if row[2]:
                parsed = json.loads(str(row[2]))
                if isinstance(parsed, list):
                    leg_counts[len(parsed)] += 1
            if row[1] is not None:
                headroom.append(int(row[1]) - int(row[0]))
        prices_e6 = [
            int(str(row[0]))
            for row in connection.execute(
                "SELECT price_e6 FROM v055_events WHERE message_type='RFQ_TRADE' AND price_e6 GLOB '[0-9]*'"
            )
            if str(row[0]).isdigit()
        ]
    finally:
        connection.close()
    last_status = str(runs[-1]["status"]) if runs else "NOT_STARTED"
    terminal = last_status in {"COMPLETED", "AUTH_FAILED", "TRANSPORT_FAILED", "INTERRUPTED"}
    return {
        "status": last_status,
        "database_exists": True,
        "terminal": terminal,
        "runs": runs,
        "message_type_counts": type_counts,
        "auth_successes": auth_successes,
        "rfq_requests": request_count,
        "rfq_trades": trade_count,
        "distinct_conditions": distinct_conditions,
        "request_directions": direction_counts,
        "request_leg_counts": {str(key): value for key, value in sorted(leg_counts.items())},
        "submission_headroom_ms": {
            "minimum": min(headroom) if headroom else None,
            "median": median(headroom) if headroom else None,
            "maximum": max(headroom) if headroom else None,
        },
        "observed_trade_price_e6": {
            "count": len(prices_e6),
            "minimum": min(prices_e6) if prices_e6 else None,
            "maximum": max(prices_e6) if prices_e6 else None,
        },
        "database_bytes": database.stat().st_size,
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
    if snapshot["status"] == "AUTH_FAILED" or int(snapshot["auth_successes"]) == 0:
        verdict = decision["auth_failure"]
        next_step = "VERIFY_CLOB_CREDENTIALS_AND_QUOTER_IDENTITY_WITHOUT_ENABLING_QUOTES"
    elif snapshot["status"] != "COMPLETED":
        verdict = decision["incomplete_runtime"]
        next_step = "REVIEW_TRANSPORT_FAILURE_BEFORE_ANY_FRESH_OBSERVER"
    elif (
        int(snapshot["rfq_requests"]) < int(decision["minimum_rfq_requests"])
        or int(snapshot["rfq_trades"]) < int(decision["minimum_rfq_trades"])
    ):
        verdict = decision["insufficient_activity"]
        next_step = "DO_NOT_INFER_ECONOMICS_FROM_SPARSE_RFQ_ACTIVITY"
    else:
        verdict = decision["activity_observable"]
        next_step = "DESIGN_SEPARATE_PREREGISTERED_CLOB_JOINED_PAPER_REPLAY_WITHOUT_QUOTES"
    return {
        "verdict": verdict,
        "next_step": next_step,
        "profitability_conclusion_allowed": False,
        "pnl_conclusion_allowed": False,
    }


def audit_v055(
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
        raise V055AuditError("V0.55 no tiene una captura terminal auditable")
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
            "requests_have_no_competing_quote_prices": True,
            "trade_prices_are_not_strategy_pnl": True,
            "clob_leg_books_joined": False,
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
            raise V055AuditError("Ya existe otro resultado V0.55")
        return existing
    _write_atomic(output, payload)
    return payload


__all__ = [
    "RESULT_SCHEMA",
    "V055AuditError",
    "audit_v055",
    "classify_audit",
    "inspect_database",
]
