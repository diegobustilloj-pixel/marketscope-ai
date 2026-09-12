from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import sqlite3
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from .climate_research import GAMMA, iso, stable_json, utc_now, write_csv, write_json
from .climate_shadow_monitor import http_json


SCHEMA = "climate_manual_strategy_v001"
DEFAULT_DB = Path("data/climate_shadow_forward_v001.db")
DEFAULT_RESEARCH_ROOT = Path("data/climate_research_v001")
DEFAULT_OUTPUT = Path("data/climate_manual_strategy_v001")
EXPOSED_SLUGS = {
    "lowest-temperature-in-madrid-on-september-3-2026",
    "highest-temperature-in-jeddah-on-september-3-2026",
    "highest-temperature-in-shanghai-on-september-3-2026",
}
PRIMARY = {
    "local_entry_hour": 15,
    "entry_window_minutes": 70,
    "probability_min": 0.30,
    "edge_min": 0.05,
    "price_min": 0.10,
    "price_max": 0.75,
    "spread_max": 0.10,
    "visible_notional_min": 25.0,
    "stake_usdc": 25.0,
    "adverse_price_impact": 0.01,
    "train_match_rate_min": 0.98,
    "train_comparable_min": 30,
    "sample_grade": "STRONG",
    "lead_days": 1,
    "side": "YES_TOP_BUCKET_ONLY",
    "max_entries_per_event": 1,
}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def classify_event(event: dict[str, Any]) -> dict[str, Any]:
    markets = []
    winners = []
    for market in event.get("markets") or []:
        prices = _json_list(market.get("outcomePrices"))
        yes_price = float(prices[0]) if prices else None
        row = {
            "market_id": str(market.get("id") or ""),
            "label": str(market.get("groupItemTitle") or ""),
            "yes_price": yes_price,
            "closed": bool(market.get("closed")),
            "accepting_orders": bool(market.get("acceptingOrders")),
            "fees_enabled": market.get("feesEnabled"),
            "fee_schedule": market.get("feeSchedule"),
        }
        markets.append(row)
        if yes_price is not None and yes_price >= 0.99:
            winners.append(row)
    uniquely_priced = len(winners) == 1 and all(
        row["yes_price"] is not None and (row in winners or row["yes_price"] <= 0.01) for row in markets
    )
    officially_closed = bool(event.get("closed")) or (markets and all(row["closed"] for row in markets))
    if uniquely_priced and officially_closed:
        status = "OFFICIAL_RESOLVED"
    elif uniquely_priced:
        status = "PROVISIONAL_CONSENSUS"
    else:
        status = "OPEN_OR_AMBIGUOUS"
    winner = winners[0] if uniquely_priced else None
    return {
        "slug": str(event.get("slug") or ""),
        "event_id": str(event.get("id") or ""),
        "status": status,
        "event_closed": bool(event.get("closed")),
        "event_active": bool(event.get("active")),
        "end_date": event.get("endDate"),
        "winner_market_id": winner["market_id"] if winner else None,
        "winner_label": winner["label"] if winner else None,
        "markets": markets,
        "captured_at": iso(utc_now()),
    }


def fetch_outcomes(slugs: Sequence[str], workers: int = 10) -> list[dict[str, Any]]:
    def fetch(slug: str) -> dict[str, Any]:
        event = http_json(f"{GAMMA}/events/slug/{urllib.parse.quote(slug)}")
        if not isinstance(event, dict):
            raise RuntimeError(f"Gamma inválido para {slug}")
        return classify_event(event)

    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch, slug): slug for slug in slugs}
        for future in as_completed(futures):
            slug = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"slug": slug, "status": "FETCH_ERROR", "error": f"{type(exc).__name__}: {exc}", "captured_at": iso(utc_now())})
    return sorted(results, key=lambda row: row["slug"])


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(stable_json(row) + "\n")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _entry_time(market_date: str, timezone_name: str, local_hour: int) -> datetime:
    local_date = datetime.fromisoformat(market_date).date() - timedelta(days=1)
    local = datetime.combine(local_date, time(local_hour), tzinfo=ZoneInfo(timezone_name))
    return local.astimezone(ZoneInfo("UTC"))


def _market_id_for_label(buckets_json: str, label: str) -> str | None:
    buckets = json.loads(buckets_json)
    for bucket in buckets:
        if bucket.get("label") == label:
            return str(bucket.get("market_id") or "")
    return None


def _fee_per_share(entry_price: float, fees_enabled: Any, fee_schedule: Any) -> float | None:
    """Return the Gamma-advertised taker fee curve, or None when unverified."""
    if fees_enabled is False:
        return 0.0
    if fees_enabled is not True or not isinstance(fee_schedule, dict):
        return None
    try:
        rate = float(fee_schedule["rate"])
        exponent = float(fee_schedule.get("exponent", 1.0))
    except (KeyError, TypeError, ValueError):
        return None
    if rate < 0 or exponent <= 0 or not bool(fee_schedule.get("takerOnly", False)):
        return None
    return rate * ((entry_price * (1.0 - entry_price)) ** exponent)


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": None, "cost_usdc": 0.0,
            "net_pnl_usdc": 0.0, "roi_on_cost": None, "ev_per_trade_usdc": None,
            "profit_factor": None, "max_drawdown_usdc": None, "bootstrap_mean_pnl_lower_95": None,
        }
    ordered = sorted(rows, key=lambda row: (row["entry_at"], row["slug"]))
    pnls = [float(row["pnl_usdc"]) for row in ordered]
    cost = sum(float(row["cost_usdc"]) for row in ordered)
    wins = sum(value > 0 for value in pnls)
    losses = sum(value < 0 for value in pnls)
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = -sum(value for value in pnls if value < 0)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    rng = random.Random(20260903)
    boot = []
    for _ in range(10_000):
        sample = [pnls[rng.randrange(len(pnls))] for _ in pnls]
        boot.append(sum(sample) / len(sample))
    boot.sort()
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(rows),
        "cost_usdc": cost,
        "net_pnl_usdc": sum(pnls),
        "roi_on_cost": sum(pnls) / cost if cost else None,
        "ev_per_trade_usdc": sum(pnls) / len(rows),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "max_drawdown_usdc": max_drawdown,
        "bootstrap_mean_pnl_lower_95": boot[int(0.025 * (len(boot) - 1))],
    }


def evaluate_strategy(
    database: Path,
    research_root: Path,
    outcomes: dict[str, dict[str, Any]],
    *,
    local_entry_hour: int,
    probability_min: float,
    edge_min: float,
    net_edge_min: float | None,
    allow_provisional: bool,
    require_entry_fee_schedule: bool,
) -> dict[str, Any]:
    gates = {row["station_id"]: row for row in _read_csv(research_root / "derived" / "station_train_quality_gate.csv")}
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    markets = [dict(row) for row in db.execute("SELECT * FROM shadow_markets ORDER BY slug")]
    timezone_by_station = {
        row[0]: row[1]
        for row in db.execute(
            "SELECT station_id,MAX(timezone) FROM shadow_forecasts WHERE timezone<>'' GROUP BY station_id"
        )
    }
    accepted: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for market in markets:
        reasons: list[str] = []
        if market["slug"] in EXPOSED_SLUGS:
            reasons.append("DISCOVERY_EXPOSED")
        if market["sample_grade"] != PRIMARY["sample_grade"]:
            reasons.append("SAMPLE_NOT_STRONG")
        gate = gates.get(market["station_id"])
        if not gate or int(gate["train_comparable"]) < PRIMARY["train_comparable_min"] or float(gate["train_match_rate"]) < PRIMARY["train_match_rate_min"]:
            reasons.append("STATION_GATE")
        timezone_name = timezone_by_station.get(market["station_id"])
        if not timezone_name:
            reasons.append("NO_DERIVED_TIMEZONE")
            audit.append({"slug": market["slug"], "accepted": False, "reasons": ",".join(reasons)})
            continue
        target = _entry_time(market["market_date"], timezone_name, local_entry_hour)
        end = target + timedelta(minutes=PRIMARY["entry_window_minutes"])
        probability = db.execute(
            """SELECT * FROM shadow_probabilities WHERE slug=? AND observed_at>=? AND observed_at<=?
            ORDER BY observed_at,probability_id LIMIT 1""",
            (market["slug"], iso(target), iso(end)),
        ).fetchone()
        if not probability:
            reasons.append("NO_SNAPSHOT_IN_WINDOW")
            audit.append({"slug": market["slug"], "accepted": False, "reasons": ",".join(reasons)})
            continue
        probability = dict(probability)
        if int(probability["lead_days"]) != PRIMARY["lead_days"]:
            reasons.append("NOT_D1")
        if float(probability["top_probability"]) < probability_min:
            reasons.append("PROBABILITY")
        quote = db.execute(
            """SELECT * FROM shadow_quotes WHERE cycle_id=? AND slug=? AND bucket=? AND side='YES'
            ORDER BY quote_id LIMIT 1""",
            (probability["cycle_id"], market["slug"], probability["top_bucket"]),
        ).fetchone()
        if not quote or quote["best_ask"] is None:
            reasons.append("NO_EXECUTABLE_ASK")
            audit.append({"slug": market["slug"], "accepted": False, "reasons": ",".join(reasons)})
            continue
        quote = dict(quote)
        entry_price = min(0.99, float(quote["best_ask"]) + PRIMARY["adverse_price_impact"])
        gross_edge = float(probability["top_probability"]) - entry_price
        visible_notional = float(quote["best_ask"]) * float(quote["ask_size"])
        if gross_edge < edge_min:
            reasons.append("EDGE")
        if not PRIMARY["price_min"] <= entry_price <= PRIMARY["price_max"]:
            reasons.append("PRICE")
        if quote["spread"] is None or float(quote["spread"]) > PRIMARY["spread_max"]:
            reasons.append("SPREAD")
        if visible_notional < PRIMARY["visible_notional_min"]:
            reasons.append("DEPTH")
        outcome = outcomes.get(market["slug"], {"status": "MISSING"})
        permitted_status = {"OFFICIAL_RESOLVED"}
        if allow_provisional:
            permitted_status.add("PROVISIONAL_CONSENSUS")
        if outcome.get("status") not in permitted_status:
            reasons.append(f"OUTCOME_{outcome.get('status')}")
        market_id = _market_id_for_label(market["buckets_json"], probability["top_bucket"])
        if not market_id:
            reasons.append("BUCKET_ID")
        outcome_market = next(
            (row for row in outcome.get("markets", []) if row.get("market_id") == market_id),
            {},
        )
        outcome_fees = outcome_market.get("fees_enabled")
        outcome_fee_schedule = outcome_market.get("fee_schedule")
        entry_fee_schedule = None
        if market.get("fee_schedule_json"):
            try:
                entry_fee_schedule = json.loads(market["fee_schedule_json"])
            except json.JSONDecodeError:
                entry_fee_schedule = None
        if require_entry_fee_schedule and not isinstance(entry_fee_schedule, dict):
            reasons.append("ENTRY_FEE_SCHEDULE_MISSING")
        if (
            require_entry_fee_schedule
            and isinstance(entry_fee_schedule, dict)
            and isinstance(outcome_fee_schedule, dict)
            and stable_json(entry_fee_schedule) != stable_json(outcome_fee_schedule)
        ):
            reasons.append("FEE_SCHEDULE_CHANGED")
        fee_schedule = entry_fee_schedule if isinstance(entry_fee_schedule, dict) else outcome_fee_schedule
        fees = True if isinstance(entry_fee_schedule, dict) else outcome_fees
        fee_per_share = _fee_per_share(entry_price, fees, fee_schedule)
        if fee_per_share is None:
            reasons.append("FEES_UNVERIFIED")
        net_edge = None if fee_per_share is None else gross_edge - fee_per_share
        if net_edge_min is not None and (net_edge is None or net_edge < net_edge_min):
            reasons.append("NET_EDGE")
        base = {
            "slug": market["slug"], "city": market["city"], "market_type": market["market_type"],
            "station_id": market["station_id"], "market_date": market["market_date"], "timezone": timezone_name,
            "entry_at": probability["observed_at"], "entry_window_end": iso(end),
            "candidate": probability["candidate"],
            "bucket": probability["top_bucket"], "market_id": market_id, "model_probability": float(probability["top_probability"]),
            "best_ask": float(quote["best_ask"]), "entry_price": entry_price, "spread": quote["spread"],
            "visible_notional": visible_notional, "gross_edge_after_impact": gross_edge,
            "net_edge_after_fee": net_edge, "outcome_status": outcome.get("status"),
            "winner_market_id": outcome.get("winner_market_id"), "winner_label": outcome.get("winner_label"),
            "manual_contract_check_required": True, "fees_enabled": fees,
            "fee_schedule": stable_json(fee_schedule) if isinstance(fee_schedule, dict) else None,
            "fee_schedule_source": "ENTRY_CAPTURE" if isinstance(entry_fee_schedule, dict) else "OUTCOME_FETCH",
            "fee_per_share": fee_per_share,
        }
        if reasons:
            audit.append({**base, "accepted": False, "reasons": ",".join(reasons)})
            continue
        shares = PRIMARY["stake_usdc"] / entry_price
        fee_usdc = shares * float(fee_per_share or 0.0)
        won = market_id == outcome.get("winner_market_id")
        total_cost = PRIMARY["stake_usdc"] + fee_usdc
        pnl = shares * (1.0 if won else 0.0) - total_cost
        trade = {
            **base, "accepted": True, "reasons": "OK", "won": won, "shares": shares,
            "trade_value_usdc": PRIMARY["stake_usdc"], "fee_usdc": fee_usdc,
            "cost_usdc": total_cost, "payout_usdc": shares if won else 0.0, "pnl_usdc": pnl,
        }
        accepted.append(trade)
        audit.append(trade)
    db.close()
    return {
        "parameters": {
            **PRIMARY,
            "local_entry_hour": local_entry_hour,
            "probability_min": probability_min,
            "edge_min": edge_min,
            "net_edge_min": net_edge_min,
            "require_entry_fee_schedule": require_entry_fee_schedule,
        },
        "allow_provisional": allow_provisional,
        "metrics": _metrics(accepted),
        "trades": accepted,
        "audit": audit,
        "rejections": dict(Counter(reason for row in audit if not row.get("accepted") for reason in str(row.get("reasons") or "").split(",") if reason)),
    }


def _fmt(value: Any, digits: int = 2) -> str:
    return "NO EVALUABLE" if value is None else f"{float(value):.{digits}f}"


def _pct(value: Any) -> str:
    return "NO EVALUABLE" if value is None else f"{100 * float(value):.1f}%"


def _render(payload: dict[str, Any]) -> str:
    official = payload["primary_official"]["metrics"]
    provisional = payload["primary_provisional_diagnostic"]["metrics"]
    highest = payload["highest_probability_observed"]
    gate = payload["profitability_gate"]
    statuses = payload["outcome_status_counts"]
    parameters = payload["primary_official"]["parameters"]
    edge_rule = (
        f"net edge after fee >= {100 * float(parameters['net_edge_min']):.1f} points"
        if parameters.get("net_edge_min") is not None
        else "gross edge after 1-cent impact >= 5 points"
    )
    fee_rule = (
        "entry-captured fee schedule required"
        if parameters.get("require_entry_fee_schedule")
        else "official taker fee curve"
    )
    return f"""# POLYMARKET CLIMATE — MANUAL STRATEGY BACKTEST V001

## Outcome coverage

- Official resolved: {statuses.get('OFFICIAL_RESOLVED', 0)}
- Provisional market consensus: {statuses.get('PROVISIONAL_CONSENSUS', 0)}
- Open or ambiguous: {statuses.get('OPEN_OR_AMBIGUOUS', 0)}
- Fetch errors: {statuses.get('FETCH_ERROR', 0)}
- Discovery-exposed markets excluded: {len(EXPOSED_SLUGS)}

## Frozen primary strategy

One manual YES entry on the model's top bucket, first snapshot after 15:00 station-local on D-1, probability >=30%, {edge_rule}, price 0.10–0.75, spread <=0.10, visible depth >=$25, STRONG family, station match >=98%, $25 trade value, {fee_rule}, hold to resolution. One entry per event and no side flipping.

## Confirmatory official backtest

- Trades: {official['trades']}
- Wins/Losses: {official['wins']}/{official['losses']}
- Win rate: {_pct(official['win_rate'])}
- PnL: {_fmt(official['net_pnl_usdc'])} USDC
- ROI: {_pct(official['roi_on_cost'])}
- EV/trade: {_fmt(official['ev_per_trade_usdc'])} USDC
- Profit factor: {_fmt(official['profit_factor'])}
- Max drawdown: {_fmt(official['max_drawdown_usdc'])} USDC
- Bootstrap lower 95% mean PnL: {_fmt(official['bootstrap_mean_pnl_lower_95'])} USDC

## Provisional diagnostic

This section uses near-binary market-consensus outcomes that are not yet official resolutions. It cannot establish profitability.

- Trades: {provisional['trades']}
- Wins/Losses: {provisional['wins']}/{provisional['losses']}
- Win rate: {_pct(provisional['win_rate'])}
- PnL: {_fmt(provisional['net_pnl_usdc'])} USDC
- ROI: {_pct(provisional['roi_on_cost'])}
- EV/trade: {_fmt(provisional['ev_per_trade_usdc'])} USDC
- Profit factor: {_fmt(provisional['profit_factor'])}
- Max drawdown: {_fmt(provisional['max_drawdown_usdc'])} USDC
- Bootstrap lower 95% mean PnL: {_fmt(provisional['bootstrap_mean_pnl_lower_95'])} USDC

## Highest probability observed

{highest.get('city')} {highest.get('market_type')} / {highest.get('station_id')} / {highest.get('top_bucket')}: {_pct(highest.get('top_probability'))}, observed {highest.get('observed_at')}.

This is a model probability, not a buy recommendation. Entry also requires price, edge, depth, spread and manual resolution-contract verification.

## Verdict

{gate['verdict']}.

Reason: {gate['reason']}

Real-money execution remains disabled. The semi-automatic scanner may display `MANUAL_REVIEW`, but it must not label any setup `PROVEN_PROFITABLE` until all preregistered gates pass on at least 30 officially resolved, independent events.
"""


def run_backtest(
    database: Path,
    research_root: Path,
    output: Path,
    *,
    fetch: bool,
    workers: int,
    net_edge_min: float | None = None,
    require_entry_fee_schedule: bool = False,
    preregistration: Path | None = None,
) -> dict[str, Any]:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    database = database.resolve()
    research_root = research_root.resolve()
    db = sqlite3.connect(database)
    slugs = [row[0] for row in db.execute("SELECT DISTINCT slug FROM shadow_probabilities ORDER BY slug")]
    highest_row = db.execute(
        """SELECT m.city,m.market_type,m.station_id,m.market_date,p.slug,p.observed_at,p.top_bucket,p.top_probability,
        p.candidate,p.lead_days FROM shadow_probabilities p JOIN shadow_markets m ON m.slug=p.slug
        ORDER BY p.top_probability DESC LIMIT 1"""
    ).fetchone()
    highest_columns = [column[0] for column in db.execute(
        """SELECT m.city,m.market_type,m.station_id,m.market_date,p.slug,p.observed_at,p.top_bucket,p.top_probability,
        p.candidate,p.lead_days FROM shadow_probabilities p JOIN shadow_markets m ON m.slug=p.slug LIMIT 0"""
    ).description]
    db.close()
    outcome_path = output / "gamma_outcomes_latest.jsonl"
    if fetch or not outcome_path.exists():
        outcome_rows = fetch_outcomes(slugs, workers=workers)
        _write_jsonl(outcome_path, outcome_rows)
    else:
        outcome_rows = _load_jsonl(outcome_path)
    outcomes = {row["slug"]: row for row in outcome_rows}
    primary_official = evaluate_strategy(
        database, research_root, outcomes, local_entry_hour=PRIMARY["local_entry_hour"],
        probability_min=PRIMARY["probability_min"], edge_min=PRIMARY["edge_min"], net_edge_min=net_edge_min,
        allow_provisional=False, require_entry_fee_schedule=require_entry_fee_schedule,
    )
    primary_provisional = evaluate_strategy(
        database, research_root, outcomes, local_entry_hour=PRIMARY["local_entry_hour"],
        probability_min=PRIMARY["probability_min"], edge_min=PRIMARY["edge_min"], net_edge_min=net_edge_min,
        allow_provisional=True, require_entry_fee_schedule=require_entry_fee_schedule,
    )
    metrics = primary_official["metrics"]
    passed = (
        metrics["trades"] >= 30 and metrics["net_pnl_usdc"] > 0 and metrics["roi_on_cost"] is not None
        and metrics["roi_on_cost"] > 0 and metrics["profit_factor"] is not None and metrics["profit_factor"] >= 1.25
        and metrics["bootstrap_mean_pnl_lower_95"] is not None and metrics["bootstrap_mean_pnl_lower_95"] > 0
    )
    verdict = "PROVISIONALLY PROFITABLE" if passed else "MORE DATA REQUIRED"
    reason = (
        "All preregistered profitability gates passed"
        if passed
        else "Fewer than 30 independent official trades or one/more economic gates failed"
    )
    payload = {
        "schema": SCHEMA,
        "generated_at": iso(utc_now()),
        "database": str(database),
        "research_root": str(research_root),
        "preregistration": str((preregistration or (research_root.parent.parent / "docs" / "PREREG_CLIMATE_MANUAL_STRATEGY_V001_20260903.md")).resolve()),
        "fee_method": {
            "source": "https://docs.polymarket.com/trading/fees",
            "market_field": "feeSchedule",
            "formula": "shares * rate * (price * (1-price)) ** exponent",
            "rounding_note": "Aggregate simulation; the protocol rounds fees to 5 decimals at match time",
        },
        "outcome_status_counts": dict(Counter(row["status"] for row in outcome_rows)),
        "discovery_exposed_exclusions": sorted(EXPOSED_SLUGS),
        "highest_probability_observed": dict(zip(highest_columns, highest_row)) if highest_row else {},
        "primary_official": primary_official,
        "primary_provisional_diagnostic": primary_provisional,
        "profitability_gate": {"passed": passed, "verdict": verdict, "reason": reason},
        "real_money_allowed": False,
    }
    write_json(output / "backtest_report.json", payload)
    write_csv(output / "primary_official_trades.csv", primary_official["trades"])
    write_csv(output / "primary_provisional_trades.csv", primary_provisional["trades"])
    write_csv(output / "primary_entry_audit.csv", primary_provisional["audit"])
    report_path = output / "CLIMATE_MANUAL_STRATEGY_BACKTEST.md"
    report_path.write_text(_render(payload), encoding="utf-8")
    manifest = {
        "schema": SCHEMA,
        "generated_at": payload["generated_at"],
        "files": [
            {"path": path.name, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(output.iterdir()) if path.name != "manifest.json"
        ],
    }
    write_json(output / "manifest.json", manifest)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest preregistrado de entradas climáticas manuales")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--research-root", type=Path, default=DEFAULT_RESEARCH_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--fetch-outcomes", action="store_true")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--net-edge-min", type=float)
    parser.add_argument("--require-entry-fee-schedule", action="store_true")
    parser.add_argument("--preregistration", type=Path)
    args = parser.parse_args(argv)
    result = run_backtest(
        args.database,
        args.research_root,
        args.output,
        fetch=args.fetch_outcomes,
        workers=args.workers,
        net_edge_min=args.net_edge_min,
        require_entry_fee_schedule=args.require_entry_fee_schedule,
        preregistration=args.preregistration,
    )
    print(json.dumps({
        "outcome_status_counts": result["outcome_status_counts"],
        "official_metrics": result["primary_official"]["metrics"],
        "provisional_metrics": result["primary_provisional_diagnostic"]["metrics"],
        "highest_probability_observed": result["highest_probability_observed"],
        "verdict": result["profitability_gate"]["verdict"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
