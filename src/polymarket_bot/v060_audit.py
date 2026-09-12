from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v056_rfq_observer import database_footprint
from polymarket_bot.v057_audit import evaluate_trade, fetch_market_info
from polymarket_bot.v060_contract import VARIANT, load_and_verify_preregistration


RESULT_SCHEMA = "result_v060_absolute_active_map_rfq_clob_replay_1"


class V060AuditError(RuntimeError):
    pass


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _open_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _meta(connection: sqlite3.Connection) -> dict[str, Any]:
    return {str(row[0]): json.loads(str(row[1])) for row in connection.execute("SELECT key,value FROM v058_meta")}


def inspect_database(
    *, prereg_path: str | Path, database_path: str | Path, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    database = Path(database_path).resolve()
    load_and_verify_preregistration(prereg_file, project_root=root)
    if not database.is_file():
        return {
            "status": "NOT_STARTED",
            "database_exists": False,
            "terminal": False,
            "orders_created": 0,
            "paper_orders": 0,
            "quotes_submitted": 0,
            "transactions_created": 0,
            "real_money": "BLOQUEADO",
        }
    connection = _open_ro(database)
    try:
        if str(connection.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
            raise V060AuditError("La base V0.60 no supera quick_check")
        meta = _meta(connection)
        expected = {
            "schema_version": "1",
            "variant": VARIANT,
            "preregistration_sha256": sha256_file(prereg_file),
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "quote_submission_enabled": False,
            "signatures_enabled": False,
            "transactions_enabled": False,
            "credential_values_stored": False,
            "addresses_stored": False,
            "raw_payloads_stored": False,
            "realized_pnl_measured": False,
            "real_money": "BLOQUEADO",
        }
        for key, value in expected.items():
            if meta.get(key) != value:
                raise V060AuditError(f"Base V0.60 incompatible o insegura: {key}")
        tables = (
            "v058_meta", "v058_runs", "v058_position_map", "v058_message_counts",
            "v058_totals", "v058_joined_requests", "v058_trades",
        )
        forbidden = {
            "api_key", "api_secret", "passphrase", "signer_address", "maker_address",
            "private_key", "raw_payload", "requestor_public_id", "requester_id",
        }
        columns = {str(row[1]) for table in tables for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns.intersection(forbidden):
            raise V060AuditError("La base V0.60 contiene columnas prohibidas")
        mapping_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM v058_position_map ORDER BY CAST(position_id AS INTEGER)"
            )
        ]
        mapping_sha = hashlib.sha256(_compact(mapping_rows).encode("utf-8")).hexdigest()
        mapping_summary = meta.get("mapping_summary", {})
        if mapping_sha != mapping_summary.get("sha256") or len(mapping_rows) != int(mapping_summary.get("positions", -1)):
            raise V060AuditError("El mapa activo position-to-token no coincide con su evidencia")
        run = connection.execute("SELECT * FROM v058_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        totals = connection.execute("SELECT * FROM v058_totals ORDER BY run_id DESC LIMIT 1").fetchone()
        message_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute("SELECT message_type,event_count FROM v058_message_counts")
        }
        statuses = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT book_status,COUNT(*) FROM v058_joined_requests GROUP BY book_status"
            )
        }
        matched = int(
            connection.execute(
                "SELECT COUNT(*) FROM v058_trades t JOIN v058_joined_requests r USING(rfq_id_sha256) "
                "WHERE t.direction='BUY' AND t.side='YES' AND r.book_status='SUCCESS'"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    run_data = dict(run) if run is not None else {}
    totals_data = dict(totals) if totals is not None else {}
    sampled = int(totals_data.get("sampled_requests", 0))
    success = int(totals_data.get("book_fetch_success", 0))
    dropped = int(totals_data.get("queue_dropped", 0))
    eligible = int(totals_data.get("structurally_eligible_requests", 0))
    status = str(run_data.get("status", "NOT_STARTED"))
    return {
        "status": status,
        "database_exists": True,
        "terminal": status in {
            "COMPLETED", "AUTH_FAILED", "TRANSPORT_FAILED", "STORAGE_LIMIT_REACHED",
            "LOCAL_STORAGE_FAILED", "INTERRUPTED",
        },
        "run": run_data,
        "mapping": mapping_summary,
        "message_type_counts": message_counts,
        "totals": totals_data,
        "book_status_counts": statuses,
        "mapping_coverage": int(totals_data.get("mapped_requests", 0)) / eligible if eligible else 0.0,
        "join_success_rate": success / sampled if sampled else 0.0,
        "queue_drop_rate": dropped / sampled if sampled else 0.0,
        "joined_observed_trades": matched,
        "database_bytes": database_footprint(database),
        "database_sha256": sha256_file(database),
        "orders_created": 0,
        "paper_orders": 0,
        "quotes_submitted": 0,
        "confirmations_sent": 0,
        "transactions_created": 0,
        "profitability_conclusion_allowed": False,
        "realized_pnl_measured": False,
        "real_money": "BLOQUEADO",
    }


def audit_v060(
    *,
    prereg_path: str | Path,
    database_path: str | Path,
    result_path: str | Path,
    project_root: str | Path = ROOT,
    market_info_fetcher: Callable[[str, str, float], Any] = fetch_market_info,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    database = Path(database_path).resolve()
    result_file = Path(result_path).resolve()
    prereg = load_and_verify_preregistration(prereg_file, project_root=root)
    snapshot = inspect_database(prereg_path=prereg_file, database_path=database, project_root=root)
    if not snapshot.get("terminal"):
        raise V060AuditError("V0.60 no tiene captura terminal auditable")
    if result_file.is_file():
        existing = json.loads(result_file.read_text(encoding="utf-8"))
        if existing.get("schema") == RESULT_SCHEMA and existing.get("evidence", {}).get("database_sha256") == snapshot["database_sha256"]:
            return existing
        raise V060AuditError("Existe otro resultado V0.60 incompatible")
    info_cache: dict[str, Mapping[str, Any]] = {}
    template = str(prereg["contract"]["transport"]["clob_market_endpoint_template"])

    def info(condition_id: str) -> Mapping[str, Any]:
        if condition_id not in info_cache:
            payload = market_info_fetcher(template, condition_id, 5.0)
            if not isinstance(payload, Mapping):
                raise V060AuditError("Metadata CLOB invalida")
            info_cache[condition_id] = payload
        return info_cache[condition_id]

    evaluations: list[dict[str, Any]] = []
    connection = _open_ro(database)
    try:
        for raw in connection.execute(
            """SELECT r.*,t.direction,t.side,t.price_e6,t.size_e6,
            t.leg_position_ids_json AS trade_leg_position_ids_json
            FROM v058_joined_requests r JOIN v058_trades t USING(rfq_id_sha256)
            WHERE r.book_status='SUCCESS' AND t.direction='BUY' AND t.side='YES'
            ORDER BY t.executed_at_ms"""
        ):
            request = dict(raw)
            trade = {
                "direction": request["direction"],
                "side": request["side"],
                "price_e6": request["price_e6"],
                "size_e6": request["size_e6"],
                "leg_position_ids_json": request["trade_leg_position_ids_json"],
            }
            evaluations.append(evaluate_trade(request, trade, info, prereg["contract"]["scope"]))
    finally:
        connection.close()
    evaluable = [row for row in evaluations if row.get("evaluable")]
    candidates = [row for row in evaluable if row.get("candidate")]
    reasons = Counter(str(row.get("reason")) for row in evaluations if not row.get("evaluable"))
    totals = snapshot["totals"]
    frozen = prereg["contract"]["gates"]
    gates = {
        "completed_runtime": snapshot["status"] == "COMPLETED",
        "minimum_all_rfq_requests": int(totals.get("all_requests", 0)) >= int(frozen["minimum_all_rfq_requests"]),
        "minimum_mapped_buy_yes_requests": int(totals.get("mapped_requests", 0)) >= int(frozen["minimum_mapped_buy_yes_requests"]),
        "minimum_mapping_coverage": float(snapshot["mapping_coverage"]) >= float(frozen["minimum_mapping_coverage"]),
        "minimum_completed_book_joins": int(totals.get("book_fetch_completed", 0)) >= int(frozen["minimum_completed_book_joins"]),
        "minimum_join_success_rate": float(snapshot["join_success_rate"]) >= float(frozen["minimum_join_success_rate"]),
        "maximum_queue_drop_rate": float(snapshot["queue_drop_rate"]) <= float(frozen["maximum_queue_drop_rate"]),
        "minimum_joined_observed_trades": int(snapshot["joined_observed_trades"]) >= int(frozen["minimum_joined_observed_trades"]),
        "minimum_economically_evaluable_trades": len(evaluable) >= int(frozen["minimum_economically_evaluable_trades"]),
        "minimum_locked_superhedge_candidates": len(candidates) >= int(frozen["minimum_locked_superhedge_candidates"]),
    }
    technical = {key: value for key, value in gates.items() if key != "minimum_locked_superhedge_candidates"}
    if not all(technical.values()):
        verdict = "FAIL_V060_TECHNICAL_OR_SAMPLE_GATES"
        next_step = "FIX_ONLY_PROVEN_TECHNICAL_FAILURE_BEFORE_ANY_FRESH_CAPTURE"
    elif not gates["minimum_locked_superhedge_candidates"]:
        verdict = "FAIL_NO_LOCKED_ACTIVE_MAPPED_SINGLE_LEG_SUPERHEDGE_CANDIDATE"
        next_step = prereg["contract"]["interpretation"]["negative_result_next_step"]
    else:
        verdict = "PASS_COUNTERFACTUAL_ABSOLUTE_ACTIVE_MAP_RFQ_SUPERHEDGE_VIABILITY_ONLY"
        next_step = prereg["contract"]["interpretation"]["positive_result_next_step"]
    edges = sorted(float(row["locked_edge_per_share"]) for row in candidates)
    payload = {
        "schema": RESULT_SCHEMA,
        "variant": VARIANT,
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": verdict,
        "next_step": next_step,
        "gates": gates,
        "observation": snapshot,
        "economic_replay": {
            "joined_observed_trades": len(evaluations),
            "economically_evaluable_trades": len(evaluable),
            "locked_superhedge_candidates": len(candidates),
            "not_evaluable_reasons": dict(sorted(reasons.items())),
            "candidate_edge_per_share_minimum": edges[0] if edges else None,
            "candidate_edge_per_share_median": edges[len(edges) // 2] if edges else None,
            "candidate_edge_per_share_maximum": edges[-1] if edges else None,
            "candidate_details": candidates[:100],
            "actual_maker_allocation_observed": False,
            "realized_fill_probability_measured": False,
            "realized_pnl_measured": False,
            "profitability_conclusion_allowed": False,
        },
        "evidence": {
            "prereg_sha256": sha256_file(prereg_file),
            "database_sha256": snapshot["database_sha256"],
            "market_metadata_requests": len(info_cache),
            "database_mutated_by_audit": False,
        },
        "safety": {
            "orders_created": 0,
            "paper_orders": 0,
            "quotes_submitted": 0,
            "confirmations_sent": 0,
            "signatures_created": 0,
            "transactions_created": 0,
            "real_money": "BLOQUEADO",
        },
    }
    _write_atomic(result_file, payload)
    return payload


__all__ = ["RESULT_SCHEMA", "V060AuditError", "audit_v060", "inspect_database"]
