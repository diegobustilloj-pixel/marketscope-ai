from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request
import zlib
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v045_census import _clob_market_values, executable_buy_cost
from polymarket_bot.v056_rfq_observer import database_footprint
from polymarket_bot.v057_contract import VARIANT, load_and_verify_preregistration


RESULT_SCHEMA = "result_v057_rfq_clob_joined_replay_1"


class V057AuditError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _open_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _meta(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        str(row[0]): json.loads(str(row[1]))
        for row in connection.execute("SELECT key,value FROM v057_meta")
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
            "status": "NOT_STARTED", "database_exists": False, "terminal": False,
            "orders_created": 0, "paper_orders": 0, "quotes_submitted": 0,
            "transactions_created": 0, "real_money": "BLOQUEADO",
        }
    connection = _open_ro(database)
    try:
        if str(connection.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
            raise V057AuditError("La base V0.57 no supera quick_check")
        meta = _meta(connection)
        expected = {
            "schema_version": "1", "variant": VARIANT,
            "preregistration_sha256": sha256_file(prereg_file),
            "orders_enabled": False, "paper_orders_enabled": False,
            "quote_submission_enabled": False, "signatures_enabled": False,
            "transactions_enabled": False, "credential_values_stored": False,
            "addresses_stored": False, "raw_payloads_stored": False,
            "realized_pnl_measured": False, "real_money": "BLOQUEADO",
        }
        for key, value in expected.items():
            if meta.get(key) != value:
                raise V057AuditError(f"Base V0.57 incompatible o insegura: {key}")
        forbidden = {
            "api_key", "api_secret", "passphrase", "signer_address", "maker_address",
            "private_key", "raw_payload", "requestor_public_id", "requester_id",
        }
        columns = {
            str(row[1])
            for table in ("v057_meta", "v057_runs", "v057_message_counts", "v057_totals", "v057_joined_requests", "v057_trades")
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if columns.intersection(forbidden):
            raise V057AuditError("La base V0.57 contiene columnas prohibidas")
        run = connection.execute("SELECT * FROM v057_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        totals = connection.execute("SELECT * FROM v057_totals ORDER BY run_id DESC LIMIT 1").fetchone()
        message_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute("SELECT message_type,event_count FROM v057_message_counts")
        }
        statuses = {
            str(row[0]): int(row[1])
            for row in connection.execute("SELECT book_status,COUNT(*) FROM v057_joined_requests GROUP BY book_status")
        }
        matched = int(
            connection.execute(
                "SELECT COUNT(*) FROM v057_trades t JOIN v057_joined_requests r USING(rfq_id_sha256) "
                "WHERE t.direction='BUY' AND t.side='YES' AND r.book_status='SUCCESS'"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    run_data = dict(run) if run is not None else {}
    total_data = dict(totals) if totals is not None else {}
    completed = int(total_data.get("book_fetch_completed", 0))
    success = int(total_data.get("book_fetch_success", 0))
    sampled = int(total_data.get("sampled_requests", 0))
    dropped = int(total_data.get("queue_dropped", 0))
    status = str(run_data.get("status", "NOT_STARTED"))
    return {
        "status": status,
        "database_exists": True,
        "terminal": status in {"COMPLETED", "AUTH_FAILED", "TRANSPORT_FAILED", "STORAGE_LIMIT_REACHED", "LOCAL_STORAGE_FAILED", "INTERRUPTED"},
        "run": run_data,
        "message_type_counts": message_counts,
        "totals": total_data,
        "book_status_counts": statuses,
        "join_success_rate": success / sampled if sampled else 0.0,
        "queue_drop_rate": dropped / sampled if sampled else 0.0,
        "joined_observed_trades": matched,
        "database_bytes": database_footprint(database),
        "database_sha256": sha256_file(database),
        "orders_created": 0, "paper_orders": 0, "quotes_submitted": 0,
        "confirmations_sent": 0, "transactions_created": 0,
        "profitability_conclusion_allowed": False,
        "realized_pnl_measured": False,
        "real_money": "BLOQUEADO",
    }


def fetch_market_info(endpoint_template: str, condition_id: str, timeout_seconds: float = 5.0) -> Any:
    url = endpoint_template.format(condition_id=urllib.parse.quote(condition_id, safe=""))
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "PolyMarkerQuantBot-V0.57-audit/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))


def _token_fee(info: Mapping[str, Any], token_id: str) -> dict[str, float]:
    minimum, tick, _neg_risk, rate, exponent, _taker_only = _clob_market_values(info)
    tokens = info.get("t", info.get("tokens"))
    if isinstance(tokens, list):
        ids = {
            str(token.get("t", token.get("token_id", token.get("tokenId", ""))))
            for token in tokens if isinstance(token, Mapping)
        }
        if ids and token_id not in ids:
            raise V057AuditError("Token no pertenece al mercado CLOB informado")
    return {
        "minimum_order_size": minimum,
        "tick_size": tick,
        "fee_rate": rate,
        "fee_exponent": exponent,
    }


def evaluate_trade(
    request: Mapping[str, Any],
    trade: Mapping[str, Any],
    market_info: Callable[[str], Mapping[str, Any]],
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    if str(trade["direction"]) != "BUY" or str(trade["side"]) != "YES":
        return {"evaluable": False, "reason": "TRADE_SCOPE_MISMATCH"}
    try:
        price = int(str(trade["price_e6"])) / 1_000_000
        shares = int(str(trade["size_e6"])) / 1_000_000
        books = json.loads(zlib.decompress(bytes(request["asks_zlib"])).decode("utf-8"))
        request_legs = json.loads(str(request["leg_position_ids_json"]))
        trade_legs = json.loads(str(trade["leg_position_ids_json"]))
    except (TypeError, ValueError, json.JSONDecodeError, zlib.error):
        return {"evaluable": False, "reason": "STORED_DATA_INVALID"}
    if shares <= 0 or not 0 < price < 1 or request_legs != trade_legs:
        return {"evaluable": False, "reason": "TRADE_OR_LEGS_INVALID"}
    latency = int(request["book_finished_at_ms"]) - int(request["received_at_ms"])
    source_timely = int(request["book_source_max_ms"]) <= int(request["submission_deadline_ms"])
    within_budget = latency <= int(scope["rfq_submission_budget_ms"]) and source_timely
    quote_price = price - int(scope["quote_improvement_e6"]) / 1_000_000
    if quote_price <= 0:
        return {"evaluable": False, "reason": "QUOTE_PRICE_NONPOSITIVE"}
    candidates: list[dict[str, Any]] = []
    for book in books:
        token = str(book.get("asset_id", ""))
        condition_id = str(book.get("market", ""))
        try:
            fee = _token_fee(market_info(condition_id), token)
        except Exception:
            continue
        if shares + 1e-9 < fee["minimum_order_size"]:
            continue
        asks = [{"price": level[0], "size": level[1]} for level in book.get("asks", [])]
        fill = executable_buy_cost(
            asks, shares=shares, fee_rate=fee["fee_rate"], fee_exponent=fee["fee_exponent"]
        )
        if fill is not None:
            candidates.append({"token_id": token, "market": condition_id, **fee, **fill})
    if not candidates:
        return {"evaluable": False, "reason": "NO_SINGLE_LEG_EXACT_DEPTH"}
    hedge = min(candidates, key=lambda item: float(item["total_cost"]))
    revenue = quote_price * shares
    edge = revenue - float(hedge["total_cost"])
    threshold = float(scope["minimum_locked_edge_per_share"])
    return {
        "evaluable": True,
        "technical_within_400ms_budget": within_budget,
        "request_to_book_finish_ms": latency,
        "source_timestamp_before_submission_deadline": source_timely,
        "observed_trade_price": price,
        "counterfactual_quote_price": quote_price,
        "matched_combo_shares": shares,
        "quote_revenue_usdc": revenue,
        "selected_hedge_token_id": hedge["token_id"],
        "selected_hedge_market": hedge["market"],
        "hedge_cash_usdc": hedge["cash"],
        "hedge_taker_fee_usdc": hedge["fee"],
        "hedge_total_cost_usdc": hedge["total_cost"],
        "locked_edge_usdc": edge,
        "locked_edge_per_share": edge / shares,
        "candidate": bool(within_budget and edge / shares >= threshold),
        "candidate_interpretation": "COUNTERFACTUAL_VIABILITY_NOT_REALIZED_FILL_OR_PNL",
    }


def audit_v057(
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
        raise V057AuditError("V0.57 no tiene captura terminal auditable")
    if result_file.is_file():
        existing = json.loads(result_file.read_text(encoding="utf-8"))
        if existing.get("schema") == RESULT_SCHEMA and existing.get("evidence", {}).get("database_sha256") == snapshot["database_sha256"]:
            return existing
        raise V057AuditError("Existe otro resultado V0.57 incompatible")
    evaluations: list[dict[str, Any]] = []
    info_cache: dict[str, Mapping[str, Any]] = {}
    template = str(prereg["contract"]["transport"]["clob_market_endpoint_template"])

    def info(condition_id: str) -> Mapping[str, Any]:
        if condition_id not in info_cache:
            payload = market_info_fetcher(template, condition_id, 5.0)
            if not isinstance(payload, Mapping):
                raise V057AuditError("Respuesta de metadata CLOB invalida")
            info_cache[condition_id] = payload
        return info_cache[condition_id]

    connection = _open_ro(database)
    try:
        rows = list(
            connection.execute(
                """SELECT r.*,t.direction,t.side,t.price_e6,t.size_e6,t.executed_at_ms,
                t.leg_position_ids_json AS trade_leg_position_ids_json
                FROM v057_joined_requests r JOIN v057_trades t USING(rfq_id_sha256)
                WHERE r.book_status='SUCCESS' AND t.direction='BUY' AND t.side='YES'
                ORDER BY t.executed_at_ms"""
            )
        )
        for raw in rows:
            row = dict(raw)
            trade = {
                "direction": row["direction"], "side": row["side"], "price_e6": row["price_e6"],
                "size_e6": row["size_e6"], "leg_position_ids_json": row["trade_leg_position_ids_json"],
            }
            evaluations.append(evaluate_trade(row, trade, info, prereg["contract"]["scope"]))
    finally:
        connection.close()
    evaluable = [row for row in evaluations if row.get("evaluable")]
    candidates = [row for row in evaluable if row.get("candidate")]
    reasons = Counter(str(row.get("reason")) for row in evaluations if not row.get("evaluable"))
    gates_contract = prereg["contract"]["gates"]
    totals = snapshot["totals"]
    gates = {
        "completed_runtime": snapshot["status"] == "COMPLETED",
        "minimum_all_rfq_requests": int(totals.get("all_requests", 0)) >= int(gates_contract["minimum_all_rfq_requests"]),
        "minimum_eligible_buy_yes_requests": int(totals.get("eligible_requests", 0)) >= int(gates_contract["minimum_eligible_buy_yes_requests"]),
        "minimum_completed_book_joins": int(totals.get("book_fetch_completed", 0)) >= int(gates_contract["minimum_completed_book_joins"]),
        "minimum_join_success_rate": float(snapshot["join_success_rate"]) >= float(gates_contract["minimum_join_success_rate"]),
        "maximum_queue_drop_rate": float(snapshot["queue_drop_rate"]) <= float(gates_contract["maximum_queue_drop_rate"]),
        "minimum_joined_observed_trades": int(snapshot["joined_observed_trades"]) >= int(gates_contract["minimum_joined_observed_trades"]),
        "minimum_economically_evaluable_trades": len(evaluable) >= int(gates_contract["minimum_economically_evaluable_trades"]),
        "minimum_locked_superhedge_candidates": len(candidates) >= int(gates_contract["minimum_locked_superhedge_candidates"]),
    }
    technical_gates = {key: value for key, value in gates.items() if key != "minimum_locked_superhedge_candidates"}
    if not all(technical_gates.values()):
        verdict = "FAIL_V057_TECHNICAL_OR_SAMPLE_GATES"
        next_step = "FIX_TECHNICAL_CAPTURE_WITHOUT_QUOTES_BEFORE_NEW_ECONOMIC_TEST"
    elif not gates["minimum_locked_superhedge_candidates"]:
        verdict = "FAIL_NO_LOCKED_SINGLE_LEG_SUPERHEDGE_CANDIDATE"
        next_step = prereg["contract"]["interpretation"]["negative_result_next_step"]
    else:
        verdict = "PASS_COUNTERFACTUAL_RFQ_SUPERHEDGE_VIABILITY_ONLY"
        next_step = prereg["contract"]["interpretation"]["positive_result_next_step"]
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
            "candidate_edge_per_share_minimum": min((row["locked_edge_per_share"] for row in candidates), default=None),
            "candidate_edge_per_share_median": (
                sorted(row["locked_edge_per_share"] for row in candidates)[len(candidates) // 2]
                if candidates else None
            ),
            "candidate_edge_per_share_maximum": max((row["locked_edge_per_share"] for row in candidates), default=None),
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
            "orders_created": 0, "paper_orders": 0, "quotes_submitted": 0,
            "confirmations_sent": 0, "signatures_created": 0,
            "transactions_created": 0, "real_money": "BLOQUEADO",
        },
    }
    _write_atomic(result_file, payload)
    return payload


__all__ = ["RESULT_SCHEMA", "V057AuditError", "audit_v057", "evaluate_trade", "fetch_market_info", "inspect_database"]
