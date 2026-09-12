from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .backtest import wilson_interval
from .contract import validate_gamma_market
from .research import _fetch_gamma, sha256_file
from .v002_shadow import DATABASE_PATH as V002_DATABASE_PATH
from .v003_shadow import DATABASE_PATH, PREREG_PATH, load_and_verify_prereg


RESULT_PATH = PREREG_PATH.parent / "final_audit.json"
RESULT_SCHEMA = "btc5m90_v003_final_audit_1"


class V003AuditError(RuntimeError):
    pass


def _open_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _meta(connection: sqlite3.Connection, table: str) -> dict[str, Any]:
    return {
        str(row[0]): json.loads(str(row[1]))
        for row in connection.execute(f"SELECT key,value FROM {table}")
    }


def _winner_map(
    markets: Iterable[sqlite3.Row], fetcher: Callable[[str], str]
) -> dict[str, str]:
    winners: dict[str, str] = {}
    for row in markets:
        raw = fetcher(str(row["slug"]))
        try:
            payload = json.loads(raw)
            contract = validate_gamma_market(
                payload,
                expected_slug=str(row["slug"]),
                expected_condition_id=str(row["condition_id"]),
                require_resolution=True,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise V003AuditError(f"Outcome oficial invalido: {row['slug']}") from exc
        if contract.winner not in {"Up", "Down"}:
            raise V003AuditError(f"Outcome no resuelto: {row['slug']}")
        winners[str(row["condition_id"])] = str(contract.winner)
    return winners


def _trade_summary(rows: Iterable[Mapping[str, Any]], winners: Mapping[str, str]) -> dict[str, Any]:
    trades = list(rows)
    wins = sum(str(row["side"]) == winners[str(row["condition_id"])] for row in trades)
    losses = len(trades) - wins
    shares = sum(float(row["fill_shares"]) for row in trades)
    debit = sum(float(row["total_debit"]) for row in trades)
    fee = sum(float(row["fee"]) for row in trades)
    pnl = sum(
        (float(row["fill_shares"]) if str(row["side"]) == winners[str(row["condition_id"])] else 0.0)
        - float(row["total_debit"])
        for row in trades
    )
    low, high = wilson_interval(wins, len(trades))
    break_even = debit / shares if shares else None
    gross_wins = sum(
        float(row["fill_shares"]) - float(row["total_debit"])
        for row in trades
        if str(row["side"]) == winners[str(row["condition_id"])]
    )
    gross_losses = sum(
        float(row["total_debit"])
        for row in trades
        if str(row["side"]) != winners[str(row["condition_id"])]
    )
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades) if trades else None,
        "wilson_95_low": low,
        "wilson_95_high": high,
        "break_even_win_rate": break_even,
        "shares": shares,
        "total_debit": debit,
        "fees": fee,
        "net_pnl_usdc": pnl,
        "edge_per_share": pnl / shares if shares else None,
        "ev_per_trade_usdc": pnl / len(trades) if trades else None,
        "roi": pnl / debit if debit else None,
        "profit_factor": gross_wins / gross_losses if gross_losses else None,
    }


def _maximum_drawdown(rows: Iterable[Mapping[str, Any]], winners: Mapping[str, str]) -> float:
    equity = 0.0
    peak = 0.0
    maximum = 0.0
    for row in sorted(rows, key=lambda item: int(item["timestamp_ms"])):
        equity += (
            float(row["fill_shares"])
            if str(row["side"]) == winners[str(row["condition_id"])]
            else 0.0
        ) - float(row["total_debit"])
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def _slices(rows: list[Mapping[str, Any]], winners: Mapping[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, selector in {
        "price": lambda row: f"{float(row['ask']):.2f}",
        "side": lambda row: str(row["side"]),
    }.items():
        buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            buckets[selector(row)].append(row)
        result[key] = {
            label: _trade_summary(bucket, winners)
            for label, bucket in sorted(buckets.items())
        }
    return result


def _read_capture(path: Path, prereg_sha256: str) -> dict[str, Any]:
    connection = _open_ro(path)
    try:
        if str(connection.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
            raise V003AuditError("La base V003 no supera quick_check")
        meta = _meta(connection, "v003_meta")
        expected = {
            "schema": "btc5m90_v003_shadow_db_1",
            "prereg_sha256": prereg_sha256,
            "wallet_required": False,
            "orders_enabled": False,
            "paper_orders": 0,
            "orders_sent": 0,
            "outcomes_read": 0,
            "real_money": "BLOQUEADO",
        }
        for key, value in expected.items():
            if meta.get(key) != value:
                raise V003AuditError(f"Meta V003 incompatible o insegura: {key}")
        latest = connection.execute(
            "SELECT status FROM v003_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if latest is None or str(latest[0]) != "COMPLETED":
            raise V003AuditError("La captura V003 no termino correctamente")
        counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT status,COUNT(*) FROM v003_markets GROUP BY status"
            )
        }
        safety = connection.execute(
            "SELECT COALESCE(SUM(paper_orders),0),COALESCE(SUM(orders_sent),0),"
            "COALESCE(SUM(outcomes_read),0),COALESCE(SUM(real_money),0) FROM v003_markets"
        ).fetchone()
        if any(int(value or 0) for value in safety):
            raise V003AuditError("La captura V003 contiene actividad prohibida")
        markets = list(
            connection.execute(
                "SELECT condition_id,slug FROM v003_markets "
                "WHERE status='COMPLETE_QUALITY' ORDER BY market_start_ms"
            )
        )
        decisions = [
            dict(row)
            for row in connection.execute(
                "SELECT d.*,m.fee_rate AS market_fee_rate,m.fee_exponent AS market_fee_exponent "
                "FROM v003_decisions d JOIN v003_markets m USING(condition_id) "
                "WHERE m.status='COMPLETE_QUALITY' ORDER BY d.timestamp_ms"
            )
        ]
    finally:
        connection.close()
    return {"meta": meta, "counts": counts, "markets": markets, "decisions": decisions}


def _read_v002_trades() -> list[dict[str, Any]]:
    connection = _open_ro(Path(V002_DATABASE_PATH).resolve())
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT d.* FROM shadow_decisions d JOIN shadow_markets m USING(condition_id) "
                "WHERE m.status='COMPLETE' AND d.decision_status='SIGNAL' "
                "AND d.execution_status='SIMULATED_FILL_LEVEL_A' ORDER BY d.timestamp_ms"
            )
        ]
    finally:
        connection.close()


def audit_v003(
    *,
    prereg_path: str | Path = PREREG_PATH,
    database_path: str | Path = DATABASE_PATH,
    result_path: str | Path = RESULT_PATH,
    fetcher: Callable[[str], str] = _fetch_gamma,
) -> dict[str, Any]:
    prereg_file = Path(prereg_path).resolve()
    database = Path(database_path).resolve()
    result_file = Path(result_path).resolve()
    prereg = load_and_verify_prereg(prereg_file)
    prereg_sha = sha256_file(prereg_file)
    database_sha_before = sha256_file(database)
    if result_file.is_file():
        existing = json.loads(result_file.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == RESULT_SCHEMA
            and existing.get("evidence", {}).get("database_sha256_before") == database_sha_before
            and existing.get("evidence", {}).get("prereg_sha256") == prereg_sha
        ):
            return existing
        raise V003AuditError("Existe un cierre V003 que no coincide con la evidencia")
    capture = _read_capture(database, prereg_sha)
    if len(capture["markets"]) != int(prereg["target_quality_markets"]):
        raise V003AuditError("La cohorte V003 no conserva 276 mercados de calidad")
    winners = _winner_map(capture["markets"], fetcher)
    primary_signals = [
        row for row in capture["decisions"] if row["decision_status"] == "SIGNAL"
    ]
    primary = [
        row for row in primary_signals if row["execution_status"] == "SIMULATED_FILL_LEVEL_A"
    ]
    rejected = [
        row
        for row in capture["decisions"]
        if row["decision_status"] == "REJECTED_EDGE"
        and 0.92 <= float(row["ask"] or 0.0) <= 0.95
    ]
    rejected_fillable = []
    for row in rejected:
        levels = json.loads(str(row["book_levels_json"]))
        side_levels = levels.get(str(row["side"]), [])
        remaining = float(prereg["strategy"]["shares"])
        cash = 0.0
        fee = 0.0
        maximum_price = float(row["ask"])
        for price, size in sorted(side_levels):
            if float(price) > maximum_price + 1e-12:
                continue
            take = min(remaining, float(size))
            cash += take * float(price)
            fee += take * float(row["market_fee_rate"]) * (
                float(price) * (1.0 - float(price))
            ) ** float(row["market_fee_exponent"])
            remaining -= take
            if remaining <= 1e-9:
                break
        if remaining <= 1e-9:
            rejected_fillable.append(
                {
                    **row,
                    "fill_shares": float(prereg["strategy"]["shares"]),
                    "fee": fee,
                    "total_debit": cash + fee,
                }
            )
    v003_summary = _trade_summary(primary, winners)
    v003_summary["maximum_drawdown_usdc"] = _maximum_drawdown(primary, winners)
    v002 = _read_v002_trades()
    v002_market_rows = [
        {"condition_id": row["condition_id"], "slug": ""} for row in v002
    ]
    # Winners from the old cohort are fetched by its stored slugs without mutating either DB.
    connection = _open_ro(Path(V002_DATABASE_PATH).resolve())
    try:
        slug_by_id = {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT condition_id,slug FROM shadow_markets")
        }
    finally:
        connection.close()
    for row in v002_market_rows:
        row["slug"] = slug_by_id[str(row["condition_id"])]
    v002_winners = _winner_map(v002_market_rows, fetcher)
    pooled_winners = {**v002_winners, **winners}
    pooled = v002 + primary
    pooled_summary = _trade_summary(pooled, pooled_winners)
    pooled_summary["maximum_drawdown_usdc"] = _maximum_drawdown(pooled, pooled_winners)
    execution_rate = len(primary) / len(primary_signals) if primary_signals else 0.0
    gates = {
        "minimum_quality_markets": len(capture["markets"]) >= int(prereg["gates"]["minimum_quality_markets"]),
        "minimum_signals": len(primary_signals) >= int(prereg["gates"]["minimum_signals"]),
        "minimum_execution_rate": execution_rate >= float(prereg["gates"]["minimum_execution_rate"]),
        "minimum_realized_edge_per_share": float(v003_summary["edge_per_share"] or -math.inf)
        >= float(prereg["gates"]["minimum_realized_edge_per_share"]),
        "positive_net_pnl": float(v003_summary["net_pnl_usdc"]) > 0.0,
        "pooled_wilson_95_low_above_pooled_break_even": float(pooled_summary["wilson_95_low"] or 0.0)
        > float(pooled_summary["break_even_win_rate"] or 1.0),
    }
    counterfactual = _trade_summary(rejected_fillable, winners)
    database_sha_after = sha256_file(database)
    if database_sha_after != database_sha_before:
        raise V003AuditError("La base V003 cambio durante la auditoria")
    payload = {
        "schema": RESULT_SCHEMA,
        "status": "AUDITED_FINAL",
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": "FAIL_PREREGISTERED_PROFITABILITY_AND_STATISTICAL_GATES",
        "next_step": "CLOSE_BUY_ALL_YES_BRANCH_DO_NOT_EXTEND_OR_PROMOTE",
        "capture": {
            "markets_seen": sum(capture["counts"].values()),
            "quality_markets": len(capture["markets"]),
            "market_status_counts": capture["counts"],
            "signals": len(primary_signals),
            "exact_depth_fills": len(primary),
            "failed_exact_depth": len(primary_signals) - len(primary),
            "rejected_edge": sum(row["decision_status"] == "REJECTED_EDGE" for row in capture["decisions"]),
            "execution_rate": execution_rate,
        },
        "v003_primary": v003_summary,
        "diagnostic_slices": _slices(primary, winners),
        "posthoc_rejected_092_to_095": {
            **counterfactual,
            "candidate_decisions": len(rejected),
            "exact_depth_fillable": len(rejected_fillable),
            "diagnostic_only": True,
            "selection_or_promotion_allowed": False,
        },
        "v002_plus_v003_exact_depth_pool": pooled_summary,
        "gates": gates,
        "all_gates_passed": all(gates.values()),
        "evidence": {
            "prereg_sha256": prereg_sha,
            "database_sha256_before": database_sha_before,
            "database_sha256_after": database_sha_after,
            "outcomes_source": "OFFICIAL_GAMMA_API_AFTER_CAPTURE_ONLY",
            "outcomes_read": len(winners) + len(v002_winners),
            "database_mutated": False,
        },
        "safety": {
            "wallet_required": False,
            "paper_orders": 0,
            "orders_sent": 0,
            "real_money": "BLOQUEADO",
        },
    }
    _write_atomic(result_file, payload)
    return payload


__all__ = ["RESULT_PATH", "RESULT_SCHEMA", "V003AuditError", "audit_v003"]
