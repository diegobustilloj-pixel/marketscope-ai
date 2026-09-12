from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.phase2 import GammaResolutionClient
from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v019_runner import load_and_verify_prereg


ROOT = Path(__file__).resolve().parents[2]
EVALUATION_PREREG = ROOT / "data" / "prereg_v019_evaluation.json"
RESULT = ROOT / "data" / "resultado_v019_fifo_pair.json"
CACHE = ROOT / "data" / "v019_resolution_cache.json"
EVALUATION_SCHEMA = "prereg_v019_evaluation_1"
RESULT_SCHEMA = "resultado_v019_fifo_pair_1"
CACHE_SCHEMA = "v019_gamma_resolution_cache_1"


class V019EvaluationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise V019EvaluationError(f"JSON incompatible: {path}")
    return payload


def _write_once(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _write_cache(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def verify_evaluation_prereg(
    path: str | Path = EVALUATION_PREREG,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    prereg_path = Path(path).resolve()
    payload = _load_json(prereg_path)
    if payload.get("schema") != EVALUATION_SCHEMA:
        raise V019EvaluationError("Prerregistro de evaluacion V0.19 incompatible")
    if payload.get("status") != "FROZEN_BEFORE_OUTCOMES":
        raise V019EvaluationError("Evaluacion V0.19 no congelada")
    if payload.get("evaluator_sha256") != sha256_file(Path(__file__)):
        raise V019EvaluationError("El evaluador V0.19 cambio despues del freeze")
    expected_dependencies = {
        "gamma_resolution_parser": sha256_file(
            ROOT / "src" / "polymarket_bot" / "phase2.py"
        )
    }
    if payload.get("dependencies") != expected_dependencies:
        raise V019EvaluationError("Dependencias del evaluador V0.19 no coinciden")
    source = payload.get("source")
    if not isinstance(source, dict):
        raise V019EvaluationError("Fuente V0.19 ausente")
    database = (ROOT / str(source.get("database"))).resolve()
    main_prereg_path = (ROOT / str(source.get("main_prereg"))).resolve()
    if not database.is_file() or sha256_file(database) != source.get("database_sha256"):
        raise V019EvaluationError("Hash de base V0.19 no coincide")
    if (
        not main_prereg_path.is_file()
        or sha256_file(main_prereg_path) != source.get("main_prereg_sha256")
    ):
        raise V019EvaluationError("Hash del prerregistro principal V0.19 no coincide")
    main_prereg = load_and_verify_prereg(main_prereg_path)
    expected_formulas = {
        "market_payout": "winning_side_shares",
        "market_pnl": "market_payout-up_cost-down_cost",
        "paired_pnl": "paired_shares-paired_cost",
        "residual_pnl": "winning_unmatched_shares-unmatched_cost",
        "roi": "sum_market_pnl/sum_cash_deployed",
        "halves": "market_start_before_or_after_24h_midpoint",
        "largest_positive_share": "max_positive_market_pnl/sum_positive_market_pnl",
    }
    if payload.get("formulas") != expected_formulas:
        raise V019EvaluationError("Formulas V0.19 no coinciden")
    return payload, main_prereg, database


def load_completed_snapshot(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(
        f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=10
    )
    connection.row_factory = sqlite3.Row
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        meta = {
            str(key): json.loads(str(value))
            for key, value in connection.execute("SELECT key,value FROM v019_meta")
        }
        run = connection.execute(
            "SELECT status,error,finished_at FROM v019_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        markets = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM v019_markets ORDER BY market_start_ms,condition_id"
            )
        ]
        seconds = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT condition_id,COUNT(*) FROM v019_seconds GROUP BY condition_id"
            )
        }
        maximum_pair_set_cost = connection.execute(
            "SELECT MAX(set_cost) FROM v019_pairs"
        ).fetchone()[0]
        real_fill_rows = int(
            connection.execute(
                "SELECT COALESCE(SUM(real_money),0) FROM v019_fills"
            ).fetchone()[0]
        )
        real_orders = int(
            connection.execute(
                "SELECT COALESCE(SUM(real_order),0) FROM v019_quotes"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    if not meta.get("experiment_completed_at"):
        raise V019EvaluationError("V0.19 aun no completo; outcomes prohibidos")
    if run is None or str(run["status"]) != "COMPLETED" or run["error"]:
        raise V019EvaluationError("Run V0.19 no termino limpiamente")
    if str(meta.get("experiment_completed_at")) != str(run["finished_at"]):
        raise V019EvaluationError("Completion V0.19 inconsistente")
    return {
        "quick_check": quick_check,
        "meta": meta,
        "run": dict(run),
        "markets": markets,
        "seconds": seconds,
        "maximum_pair_set_cost": maximum_pair_set_cost,
        "real_fill_rows": real_fill_rows,
        "real_orders": real_orders,
    }


def evaluate_pre_outcome_gates(
    snapshot: Mapping[str, Any], prereg: Mapping[str, Any]
) -> dict[str, Any]:
    markets = list(snapshot["markets"])
    complete = [row for row in markets if row["status"] == "COMPLETE"]
    interrupted = [
        row for row in markets if row["status"] == "INTERRUPTED_EXCLUDED"
    ]
    seconds_counts = [
        int(snapshot["seconds"].get(str(row["condition_id"]), 0))
        for row in complete
    ]
    filled = [row for row in complete if int(row["fill_count"]) > 0]
    paired = [row for row in complete if float(row["paired_shares"]) > 0]
    total_fills = sum(int(row["fill_count"]) for row in complete)
    total_quotes = sum(int(row["quote_count"]) for row in complete)
    total_cash = sum(float(row["cash_deployed"]) for row in complete)
    total_paired_shares = sum(float(row["paired_shares"]) for row in complete)
    paired_cost = sum(float(row["paired_cost"]) for row in complete)
    total_unmatched = sum(float(row["unmatched_shares"]) for row in complete)
    complete_fraction = len(complete) / len(markets) if markets else 0.0
    interrupted_fraction = len(interrupted) / len(markets) if markets else 1.0
    minimum_seconds_fraction = min(seconds_counts) / 300.0 if seconds_counts else 0.0
    paired_capital_fraction = paired_cost / total_cash if total_cash else 0.0
    weighted_set_cost = paired_cost / total_paired_shares if total_paired_shares else None
    maximum_unmatched = max(
        (float(row["unmatched_shares"]) for row in complete), default=0.0
    )
    average_unmatched_filled = (
        sum(float(row["unmatched_shares"]) for row in filled) / len(filled)
        if filled else 0.0
    )
    maximum_cash = max(
        (float(row["cash_deployed"]) for row in complete), default=0.0
    )
    maximum_pair = snapshot["maximum_pair_set_cost"]
    safety_failures: list[str] = []
    if snapshot["real_orders"] != 0:
        safety_failures.append("REAL_ORDERS")
    if snapshot["real_fill_rows"] != 0:
        safety_failures.append("REAL_MONEY_ROWS")
    if int(snapshot["meta"].get("outcomes_read", 0)) != 0:
        safety_failures.append("OUTCOMES_READ_BEFORE_EVALUATION")
    technical = prereg["technical_gates"]
    technical_failures: list[str] = []
    if snapshot["quick_check"] != str(technical["sqlite_quick_check"]):
        technical_failures.append("SQLITE_QUICK_CHECK")
    if len(complete) < int(technical["minimum_complete_markets"]):
        technical_failures.append("MIN_COMPLETE_MARKETS")
    if complete_fraction < float(technical["minimum_complete_market_fraction"]):
        technical_failures.append("COMPLETE_MARKET_FRACTION")
    if minimum_seconds_fraction < float(
        technical["minimum_seconds_coverage_per_included_market"]
    ):
        technical_failures.append("SECONDS_COVERAGE")
    if interrupted_fraction > float(technical["maximum_interrupted_market_fraction"]):
        technical_failures.append("INTERRUPTED_MARKET_FRACTION")
    frequency_failures: list[str] = []
    if len(filled) < int(technical["minimum_markets_with_fill"]):
        frequency_failures.append("MIN_MARKETS_WITH_FILL")
    if total_fills < int(technical["minimum_total_fills"]):
        frequency_failures.append("MIN_TOTAL_FILLS")
    behavior = prereg["behavior_gates"]
    behavior_failures: list[str] = []
    if len(paired) < int(behavior["minimum_markets_with_matched_pairs"]):
        behavior_failures.append("MIN_MARKETS_WITH_MATCHED_PAIRS")
    if total_paired_shares < float(behavior["minimum_total_paired_shares"]):
        behavior_failures.append("MIN_TOTAL_PAIRED_SHARES")
    if paired_capital_fraction < float(
        behavior["minimum_aggregate_paired_capital_fraction"]
    ):
        behavior_failures.append("PAIRED_CAPITAL_FRACTION_MIN")
    if weighted_set_cost is None or weighted_set_cost > float(
        behavior["maximum_weighted_complete_set_cost"]
    ) + 1e-12:
        behavior_failures.append("WEIGHTED_COMPLETE_SET_COST")
    if maximum_pair is None or float(maximum_pair) > float(
        behavior["maximum_individual_pair_set_cost"]
    ) + 1e-12:
        behavior_failures.append("MAX_INDIVIDUAL_PAIR_SET_COST")
    if maximum_unmatched > float(
        behavior["maximum_final_unmatched_shares_per_market"]
    ) + 1e-9:
        behavior_failures.append("MAX_FINAL_UNMATCHED_PER_MARKET")
    if average_unmatched_filled > float(
        behavior["maximum_average_final_unmatched_shares_among_filled_markets"]
    ) + 1e-9:
        behavior_failures.append("AVG_FINAL_UNMATCHED_AMONG_FILLED")
    if maximum_cash > float(behavior["maximum_cash_per_market"]) + 1e-9:
        behavior_failures.append("MAX_CASH_PER_MARKET")
    return {
        "safety_failures": safety_failures,
        "technical_failures": technical_failures,
        "frequency_failures": frequency_failures,
        "behavior_failures": behavior_failures,
        "metrics": {
            "markets_seen": len(markets),
            "markets_complete": len(complete),
            "markets_interrupted_excluded": len(interrupted),
            "complete_market_fraction": complete_fraction,
            "interrupted_market_fraction": interrupted_fraction,
            "minimum_seconds": min(seconds_counts) if seconds_counts else 0,
            "average_seconds": sum(seconds_counts) / len(seconds_counts) if seconds_counts else 0.0,
            "minimum_seconds_fraction": minimum_seconds_fraction,
            "markets_with_fill": len(filled),
            "total_fills": total_fills,
            "total_quotes": total_quotes,
            "markets_paired": len(paired),
            "total_cash_deployed": total_cash,
            "paired_shares": total_paired_shares,
            "paired_cost": paired_cost,
            "paired_gross_locked_pnl": total_paired_shares - paired_cost,
            "aggregate_paired_capital_fraction": paired_capital_fraction,
            "weighted_complete_set_cost": weighted_set_cost,
            "total_final_unmatched_shares": total_unmatched,
            "average_final_unmatched_shares_among_filled": average_unmatched_filled,
            "maximum_final_unmatched_shares": maximum_unmatched,
            "maximum_individual_pair_set_cost": maximum_pair,
            "maximum_cash_per_market": maximum_cash,
            "orders_sent": snapshot["real_orders"],
            "real_money_rows": snapshot["real_fill_rows"],
        },
    }


def _load_cache(path: Path, completed_at: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema": CACHE_SCHEMA,
            "created_at": utc_now(),
            "experiment_completed_at": completed_at,
            "markets": {},
        }
    payload = _load_json(path)
    if payload.get("schema") != CACHE_SCHEMA:
        raise V019EvaluationError("Cache de outcomes V0.19 incompatible")
    if payload.get("experiment_completed_at") != completed_at:
        raise V019EvaluationError("Cache V0.19 creado para otro experimento")
    if not isinstance(payload.get("markets"), dict):
        raise V019EvaluationError("Cache V0.19 sin mercados")
    return payload


def fetch_verified_labels(
    markets: list[Mapping[str, Any]],
    *,
    completed_at: str,
    cache_path: Path = CACHE,
    client: GammaResolutionClient | None = None,
) -> tuple[dict[str, str], list[str]]:
    cache = _load_cache(cache_path, completed_at)
    resolver = client or GammaResolutionClient(request_pause_seconds=0.10)
    labels: dict[str, str] = {}
    missing: list[str] = []
    dirty = 0
    for index, market in enumerate(markets, start=1):
        slug = str(market["slug"])
        raw = cache["markets"].get(slug)
        resolution = None
        if isinstance(raw, str):
            try:
                resolution = GammaResolutionClient.parse(raw)
            except (ValueError, json.JSONDecodeError):
                cache["markets"].pop(slug, None)
        if resolution is None or not resolution.verified:
            resolution = resolver.fetch(slug)
            if resolution.verified and resolution.payload_raw:
                cache["markets"][slug] = resolution.payload_raw
                dirty += 1
        if resolution.verified and resolution.label in {"Up", "Down"}:
            labels[str(market["condition_id"])] = resolution.label
        else:
            missing.append(slug)
        if dirty >= 10:
            _write_cache(cache_path, cache)
            dirty = 0
        if index % 25 == 0:
            print(f"[OUTCOMES] {index}/{len(markets)} | verificados={len(labels)}", flush=True)
    if dirty or not cache_path.exists():
        _write_cache(cache_path, cache)
    return labels, missing


def evaluate_performance(
    markets: list[Mapping[str, Any]],
    labels: Mapping[str, str],
    gates: Mapping[str, Any],
    *,
    midpoint_ms: int,
) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    for row in markets:
        if row["status"] != "COMPLETE" or int(row["fill_count"]) <= 0:
            continue
        condition_id = str(row["condition_id"])
        outcome = labels[condition_id]
        payout = float(row["up_shares"] if outcome == "Up" else row["down_shares"])
        cost = float(row["up_cost"]) + float(row["down_cost"])
        paired_shares = float(row["paired_shares"])
        paired_cost = float(row["paired_cost"])
        paired_pnl = paired_shares - paired_cost
        unmatched_shares = float(row["unmatched_shares"])
        unmatched_cost = float(row["unmatched_cost"])
        winning_side = outcome.upper()
        residual_payout = (
            unmatched_shares if row["unmatched_side"] == winning_side else 0.0
        )
        residual_pnl = residual_payout - unmatched_cost
        pnl = payout - cost
        if abs(pnl - paired_pnl - residual_pnl) > 1e-7:
            raise V019EvaluationError("Descomposicion paired/residual inconsistente")
        details.append(
            {
                "condition_id": condition_id,
                "slug": str(row["slug"]),
                "market_start_ms": int(row["market_start_ms"]),
                "outcome": outcome,
                "up_shares": float(row["up_shares"]),
                "down_shares": float(row["down_shares"]),
                "cost": cost,
                "payout": payout,
                "pnl": pnl,
                "paired_shares": paired_shares,
                "paired_cost": paired_cost,
                "paired_pnl": paired_pnl,
                "unmatched_side": row["unmatched_side"],
                "unmatched_shares": unmatched_shares,
                "unmatched_cost": unmatched_cost,
                "residual_payout": residual_payout,
                "residual_pnl": residual_pnl,
            }
        )
    details.sort(key=lambda item: (item["market_start_ms"], item["condition_id"]))
    capital = sum(item["cost"] for item in details)
    net = sum(item["pnl"] for item in details)
    paired_net = sum(item["paired_pnl"] for item in details)
    residual_net = sum(item["residual_pnl"] for item in details)
    residual_capital = sum(item["unmatched_cost"] for item in details)
    residual_markets = [item for item in details if item["unmatched_shares"] > 0]
    residual_wins = [item for item in residual_markets if item["residual_payout"] > 0]
    first = sum(item["pnl"] for item in details if item["market_start_ms"] < midpoint_ms)
    second = sum(item["pnl"] for item in details if item["market_start_ms"] >= midpoint_ms)
    positives = [item["pnl"] for item in details if item["pnl"] > 0]
    gross_positive = sum(positives)
    largest_positive_share = max(positives) / gross_positive if gross_positive > 0 else 1.0
    equity = peak = maximum_drawdown = 0.0
    for item in details:
        equity += item["pnl"]
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, peak - equity)
    failures: list[str] = []
    if len(details) < int(gates["minimum_traded_markets"]):
        failures.append("MIN_TRADED_MARKETS")
    if gates["net_pnl_must_be_positive"] is True and not net > 0:
        failures.append("NET_PNL")
    roi = net / capital if capital else None
    if gates["roi_must_be_positive"] is True and (roi is None or not roi > 0):
        failures.append("ROI")
    if gates["first_half_net_pnl_must_be_nonnegative"] is True and first < 0:
        failures.append("FIRST_HALF")
    if gates["second_half_net_pnl_must_be_nonnegative"] is True and second < 0:
        failures.append("SECOND_HALF")
    if largest_positive_share > float(gates["maximum_largest_positive_market_share_of_total_profit"]):
        failures.append("LARGEST_POSITIVE_SHARE")
    return {
        "traded_markets": len(details),
        "positive_markets": sum(item["pnl"] > 0 for item in details),
        "negative_markets": sum(item["pnl"] < 0 for item in details),
        "flat_markets": sum(abs(item["pnl"]) <= 1e-12 for item in details),
        "capital_deployed": capital,
        "net_pnl": net,
        "roi_on_cost": roi,
        "paired_net_pnl": paired_net,
        "residual_capital": residual_capital,
        "residual_net_pnl": residual_net,
        "residual_roi_on_cost": residual_net / residual_capital if residual_capital else None,
        "residual_markets": len(residual_markets),
        "residual_wins": len(residual_wins),
        "residual_win_rate": len(residual_wins) / len(residual_markets) if residual_markets else None,
        "first_half_net_pnl": first,
        "second_half_net_pnl": second,
        "gross_positive_pnl": gross_positive,
        "largest_positive_market_share": largest_positive_share,
        "maximum_drawdown": maximum_drawdown,
        "failures": failures,
        "details": details,
    }


def final_status(
    pre: Mapping[str, Any], performance_failures: list[str]
) -> tuple[str, list[str]]:
    ordered = (
        ("FAIL_SAFETY", list(pre["safety_failures"])),
        ("FAIL_TECHNICAL_QUALITY", list(pre["technical_failures"])),
        ("FAIL_INSUFFICIENT_FREQUENCY", list(pre["frequency_failures"])),
        ("FAIL_BEHAVIOR_REPLICATION", list(pre["behavior_failures"])),
        ("FAIL_PERFORMANCE", list(performance_failures)),
    )
    for status, failures in ordered:
        if failures:
            return status, failures
    return "PASS_PAPER_ONLY", []


def run_evaluation(
    prereg_path: str | Path = EVALUATION_PREREG,
    *,
    result_path: Path = RESULT,
    cache_path: Path = CACHE,
) -> dict[str, Any]:
    result_path = Path(result_path).resolve()
    cache_path = Path(cache_path).resolve()
    if result_path.exists():
        evaluation_prereg, _, _ = verify_evaluation_prereg(prereg_path)
        existing = _load_json(result_path)
        if existing.get("schema") != RESULT_SCHEMA:
            raise V019EvaluationError("Resultado V0.19 incompatible")
        if existing.get("source_database_sha256") != evaluation_prereg["source"]["database_sha256"]:
            raise V019EvaluationError("Resultado V0.19 pertenece a otra base")
        if not cache_path.is_file() or sha256_file(cache_path) != existing.get("resolution_cache_sha256"):
            raise V019EvaluationError("Cache de outcomes V0.19 no coincide con resultado")
        return existing
    evaluation_prereg, main_prereg, database = verify_evaluation_prereg(prereg_path)
    snapshot = load_completed_snapshot(database)
    pre = evaluate_pre_outcome_gates(snapshot, main_prereg)
    complete = [row for row in snapshot["markets"] if row["status"] == "COMPLETE"]
    labels, missing = fetch_verified_labels(
        complete,
        completed_at=str(snapshot["meta"]["experiment_completed_at"]),
        cache_path=cache_path,
    )
    if missing:
        raise V019EvaluationError(f"Faltan {len(missing)} outcomes; no se escribe resultado")
    start = datetime.fromisoformat(str(snapshot["meta"]["experiment_started_at"]).replace("Z", "+00:00"))
    end = datetime.fromisoformat(str(snapshot["meta"]["target_end_at"]).replace("Z", "+00:00"))
    midpoint_ms = int((start.timestamp() + (end.timestamp() - start.timestamp()) / 2) * 1000)
    performance = evaluate_performance(
        complete,
        labels,
        main_prereg["post_completion_performance_gates"],
        midpoint_ms=midpoint_ms,
    )
    status, decisive_failures = final_status(pre, performance["failures"])
    payload = {
        "schema": RESULT_SCHEMA,
        "created_at": utc_now(),
        "status": status,
        "decisive_failures": decisive_failures,
        "experiment_started_at": snapshot["meta"]["experiment_started_at"],
        "experiment_completed_at": snapshot["meta"]["experiment_completed_at"],
        "target_end_at": snapshot["meta"]["target_end_at"],
        "source_database_sha256": evaluation_prereg["source"]["database_sha256"],
        "main_prereg_sha256": evaluation_prereg["source"]["main_prereg_sha256"],
        "evaluation_prereg_sha256": sha256_file(prereg_path),
        "evaluator_sha256": evaluation_prereg["evaluator_sha256"],
        "resolution_cache_sha256": sha256_file(cache_path),
        "pre_outcome_evaluation": pre,
        "performance": performance,
        "outcomes_read": len(labels),
        "outcomes_read_only_after_experiment_completed_at": True,
        "orders_enabled": False,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
        "active_forward_read": False,
        "active_forward_modified": False,
    }
    _write_once(result_path, payload)
    return payload


__all__ = [
    "CACHE",
    "EVALUATION_PREREG",
    "RESULT",
    "V019EvaluationError",
    "evaluate_performance",
    "evaluate_pre_outcome_gates",
    "final_status",
    "run_evaluation",
    "verify_evaluation_prereg",
]
