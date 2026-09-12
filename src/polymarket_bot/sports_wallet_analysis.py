from __future__ import annotations

import json
import math
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .sports_wallet_research import (
    CUTOFF_INCLUSIVE_UNIX,
    SCHEMA,
    SportsResearchError,
    read_jsonl,
    row_digest,
    stable_json,
    utc_iso,
    write_csv,
    write_json,
    write_jsonl,
)


ANALYSIS_SCHEMA = "sports_wallet_analysis_v001"


def number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def parse_timestamp(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def parse_json_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def resolved_payouts(market: dict[str, Any]) -> dict[int, float] | None:
    prices = [number(value, -1) for value in parse_json_array(market.get("outcome_prices"))]
    if len(prices) < 2 or any(value < 0 for value in prices):
        return None
    winners = sum(1 for value in prices if value >= 0.999)
    losers = sum(1 for value in prices if value <= 0.001)
    if winners != 1 or winners + losers != len(prices):
        return None
    return {index: (1.0 if value >= 0.999 else 0.0) for index, value in enumerate(prices)}


def load_market_maps(output_root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    catalog = {
        str(row["condition_id"]).lower(): row
        for row in read_jsonl(output_root / "derived" / "market_catalog.jsonl")
    }
    raw_minimal: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(output_root / "raw" / "markets.jsonl"):
        condition = str(row.get("conditionId") or "").lower()
        if not condition:
            continue
        event = next((item for item in row.get("events") or [] if isinstance(item, dict)), {})
        raw_minimal[condition] = {
            "closed_time": row.get("closedTime") or event.get("closedTime"),
            "finished_timestamp": event.get("finishedTimestamp"),
            "event_id": event.get("id"),
            "event_slug": event.get("slug"),
            "event_title": event.get("title"),
        }
    return catalog, raw_minimal


def market_resolution_timestamp(
    condition: str,
    catalog: dict[str, dict[str, Any]],
    raw_minimal: dict[str, dict[str, Any]],
) -> int | None:
    raw = raw_minimal.get(condition, {})
    return parse_timestamp(raw.get("closed_time")) or parse_timestamp(raw.get("finished_timestamp"))


def behavioral_style(rows: Sequence[dict[str, Any]]) -> str:
    buy_by_outcome: dict[str, float] = defaultdict(float)
    has_sell = False
    for row in rows:
        side = str(row.get("side") or "").upper()
        outcome = str(row.get("outcome_index"))
        if side == "BUY":
            buy_by_outcome[outcome] += number(row.get("notional_usd"))
        elif side == "SELL":
            has_sell = True
    positive = sorted((value for value in buy_by_outcome.values() if value > 0), reverse=True)
    both_material = len(positive) >= 2 and positive[1] / sum(positive) >= 0.15
    if both_material and has_sell:
        return "multi_outcome_trading"
    if both_material:
        return "paired_inventory_hold"
    if has_sell:
        return "directional_round_trip"
    if len(positive) >= 2:
        return "multi_outcome_skewed_hold"
    return "directional_hold"


def fifo_lifecycle(
    rows: Sequence[dict[str, Any]], payout: float | None, resolution_ts: int | None
) -> dict[str, Any]:
    lots: deque[list[float]] = deque()
    buy_shares = buy_cost = sell_shares = sell_proceeds = 0.0
    matched_shares = fifo_trade_pnl = 0.0
    unmatched_sell_shares = 0.0
    for row in sorted(rows, key=lambda item: (int(item["timestamp"]), str(item.get("transaction_hash") or ""))):
        side = str(row.get("side") or "").upper()
        size = number(row.get("size"))
        price = number(row.get("price"))
        if side == "BUY":
            buy_shares += size
            buy_cost += size * price
            lots.append([size, price])
            continue
        if side != "SELL":
            continue
        sell_shares += size
        sell_proceeds += size * price
        remaining = size
        while remaining > 1e-12 and lots:
            lot = lots[0]
            matched = min(remaining, lot[0])
            fifo_trade_pnl += matched * (price - lot[1])
            matched_shares += matched
            lot[0] -= matched
            remaining -= matched
            if lot[0] <= 1e-12:
                lots.popleft()
        unmatched_sell_shares += remaining
    remaining_shares = sum(lot[0] for lot in lots)
    remaining_cost = sum(lot[0] * lot[1] for lot in lots)
    settlement_pnl = (
        remaining_shares * payout - remaining_cost
        if payout is not None and resolution_ts is not None
        else None
    )
    gross_reconstructed_pnl = (
        fifo_trade_pnl + settlement_pnl
        if settlement_pnl is not None and unmatched_sell_shares <= 1e-9
        else None
    )
    return {
        "buy_shares": buy_shares,
        "buy_cost": buy_cost,
        "sell_shares": sell_shares,
        "sell_proceeds": sell_proceeds,
        "fifo_matched_shares": matched_shares,
        "fifo_trade_pnl": fifo_trade_pnl,
        "remaining_shares": remaining_shares,
        "remaining_cost": remaining_cost,
        "unmatched_sell_shares": unmatched_sell_shares,
        "settlement_payout": payout,
        "settlement_pnl": settlement_pnl,
        "gross_reconstructed_pnl": gross_reconstructed_pnl,
    }


def summarize_pnl(markets: Sequence[dict[str, Any]]) -> dict[str, Any]:
    values = [number(row.get("official_realized_pnl")) for row in markets]
    investments = [number(row.get("official_total_bought_cost")) for row in markets]
    wins = [value for value in values if value > 1e-9]
    losses = [value for value in values if value < -1e-9]
    flat = len(values) - len(wins) - len(losses)
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    losing_streak = 0
    worst_losing_streak = 0
    ordered = sorted(
        markets,
        key=lambda row: (int(row.get("resolution_timestamp") or 0), str(row.get("condition_id") or "")),
    )
    for row in ordered:
        value = number(row.get("official_realized_pnl"))
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
        if value < -1e-9:
            losing_streak += 1
            worst_losing_streak = max(worst_losing_streak, losing_streak)
        else:
            losing_streak = 0
    p05 = quantile(values, 0.05)
    cvar_values = [value for value in values if p05 is not None and value <= p05]
    return {
        "resolved_markets": len(values),
        "wins": len(wins),
        "losses": len(losses),
        "flat": flat,
        "win_rate": len(wins) / len(values) if values else None,
        "total_official_realized_pnl": sum(values),
        "official_total_bought_cost": sum(investments),
        "roi_on_total_bought_cost": sum(values) / sum(investments) if sum(investments) else None,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "mean_market_pnl": sum(values) / len(values) if values else None,
        "median_market_pnl": quantile(values, 0.5),
        "p05_market_pnl": p05,
        "cvar05_market_pnl": sum(cvar_values) / len(cvar_values) if cvar_values else None,
        "best_market_pnl": max(values) if values else None,
        "worst_market_pnl": min(values) if values else None,
        "max_drawdown_by_resolution": max_drawdown,
        "worst_losing_streak_markets": worst_losing_streak,
    }


def grouped_pnl(
    markets: Sequence[dict[str, Any]], fields: Sequence[str]
) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in markets:
        grouped[tuple(row.get(field) for field in fields)].append(row)
    result = []
    for key, rows in grouped.items():
        summary = summarize_pnl(rows)
        result.append({**dict(zip(fields, key)), **summary})
    result.sort(
        key=lambda row: (
            str(row.get("trader_key") or ""),
            -number(row.get("total_official_realized_pnl")),
            stable_json([row.get(field) for field in fields]),
        )
    )
    return result


def concurrent_gross_cost_proxy(markets: Sequence[dict[str, Any]]) -> float:
    events: list[tuple[int, int, float]] = []
    for row in markets:
        start = row.get("first_trade_timestamp")
        end = row.get("resolution_timestamp")
        cost = number(row.get("official_total_bought_cost"))
        if start is None or end is None or end < start or cost <= 0:
            continue
        events.append((int(start), 1, cost))
        events.append((int(end), 0, -cost))
    current = maximum = 0.0
    for _, _, delta in sorted(events):
        current += delta
        maximum = max(maximum, current)
    return maximum


def behavioral_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    notionals = [number(row.get("notional_usd")) for row in rows]
    conditions = {str(row.get("condition_id")) for row in rows}
    events = {str(row.get("event_id")) for row in rows if row.get("event_id") is not None}
    transactions = {str(row.get("transaction_hash")) for row in rows if row.get("transaction_hash")}
    timestamps = [int(row["timestamp"]) for row in rows]
    pregame = sum(1 for row in rows if row.get("timing") == "PREGAME")
    live = sum(1 for row in rows if row.get("timing") == "LIVE_OR_AFTER_START")
    unknown = len(rows) - pregame - live
    buys = sum(1 for row in rows if str(row.get("side")).upper() == "BUY")
    sells = len(rows) - buys
    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_condition[str(row["condition_id"])].append(row)
    styles = Counter(behavioral_style(group) for group in by_condition.values())
    paired = styles["paired_inventory_hold"] + styles["multi_outcome_trading"]
    event_notional: Counter[str] = Counter()
    for row in rows:
        event_notional[str(row.get("event_id") or row.get("event_slug"))] += number(row.get("notional_usd"))
    total_notional = sum(notionals)
    top_events = sorted(event_notional.values(), reverse=True)
    timing_values = [
        number(row.get("seconds_to_start"))
        for row in rows
        if row.get("seconds_to_start") is not None
    ]
    return {
        "fills": len(rows),
        "transactions": len(transactions),
        "markets": len(conditions),
        "events": len(events),
        "first_fill_timestamp": min(timestamps) if timestamps else None,
        "first_fill_utc": utc_iso(min(timestamps)) if timestamps else None,
        "last_fill_timestamp": max(timestamps) if timestamps else None,
        "last_fill_utc": utc_iso(max(timestamps)) if timestamps else None,
        "active_span_days": (max(timestamps) - min(timestamps)) / 86400 if timestamps else 0,
        "buy_fills": buys,
        "sell_fills": sells,
        "buy_fraction": buys / len(rows) if rows else None,
        "notional_usd": total_notional,
        "mean_fill_notional_usd": sum(notionals) / len(notionals) if notionals else None,
        "median_fill_notional_usd": quantile(notionals, 0.5),
        "p90_fill_notional_usd": quantile(notionals, 0.9),
        "max_fill_notional_usd": max(notionals) if notionals else None,
        "pregame_fills": pregame,
        "live_or_after_start_fills": live,
        "unknown_timing_fills": unknown,
        "pregame_fraction_known": pregame / (pregame + live) if pregame + live else None,
        "median_seconds_to_start": quantile(timing_values, 0.5),
        "paired_behavior_markets": paired,
        "paired_behavior_fraction": paired / len(by_condition) if by_condition else None,
        "behavioral_styles": dict(styles),
        "top_1_event_notional_fraction": top_events[0] / total_notional if top_events and total_notional else None,
        "top_10_event_notional_fraction": sum(top_events[:10]) / total_notional if total_notional else None,
    }


def weekly_stability(markets: Sequence[dict[str, Any]]) -> dict[str, Any]:
    weeks: dict[str, float] = defaultdict(float)
    for row in markets:
        timestamp = int(row["resolution_timestamp"])
        iso = datetime.fromtimestamp(timestamp, timezone.utc).isocalendar()
        weeks[f"{iso.year}-W{iso.week:02d}"] += number(row.get("official_realized_pnl"))
    ordered = sorted(weeks.items())
    positive = sum(1 for _, value in ordered if value > 0)
    return {
        "weeks": len(ordered),
        "positive_weeks": positive,
        "positive_week_fraction": positive / len(ordered) if ordered else None,
        "weekly_pnl": [{"week": week, "pnl": value} for week, value in ordered],
    }


def score_trader(summary: dict[str, Any]) -> dict[str, Any]:
    pnl = summary["pnl"]
    behavior = summary["behavior"]
    stability = summary["stability"]
    gates = {
        "positive_pnl": number(pnl.get("total_official_realized_pnl")) > 0,
        "profit_factor_above_1": number(pnl.get("profit_factor")) > 1,
        "positive_roi": number(pnl.get("roi_on_total_bought_cost")) > 0,
        "at_least_100_resolved_markets": int(pnl.get("resolved_markets") or 0) >= 100,
        "at_least_30_days_observed": number(behavior.get("active_span_days")) >= 30,
        "positive_weeks_majority": number(stability.get("positive_week_fraction")) > 0.5,
        "drawdown_smaller_than_gross_profit": abs(number(pnl.get("max_drawdown_by_resolution"))) < number(pnl.get("gross_profit")),
    }
    return {
        "passed_gates": sum(gates.values()),
        "total_gates": len(gates),
        "gates": gates,
        "eligible_for_copy_research": all(
            gates[key]
            for key in (
                "positive_pnl",
                "profit_factor_above_1",
                "positive_roi",
                "at_least_100_resolved_markets",
            )
        ),
    }


def analyze(output_root: Path) -> dict[str, Any]:
    manifest_path = output_root / "audit" / "coverage_manifest.json"
    if not manifest_path.exists():
        raise SportsResearchError("Falta coverage_manifest.json")
    capture_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if capture_manifest.get("schema") != SCHEMA:
        raise SportsResearchError("Captura incompatible")
    if capture_manifest.get("classification", {}).get("totals", {}).get("metadata_missing_trades") != 0:
        raise SportsResearchError("No se analizará PnL con fills sin metadatos")

    catalog, raw_minimal = load_market_maps(output_root)
    rows_by_trader: dict[str, list[dict[str, Any]]] = defaultdict(list)
    exact_seen: dict[str, set[str]] = defaultdict(set)
    duplicate_counts: Counter[str] = Counter()
    for row in read_jsonl(output_root / "derived" / "master_sports_trades.jsonl"):
        key = str(row["trader_key"])
        digest = row_digest(row)
        if digest in exact_seen[key]:
            duplicate_counts[key] += 1
        exact_seen[key].add(digest)
        rows_by_trader[key].append(row)

    condition_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    asset_rows: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for key, rows in rows_by_trader.items():
        for row in rows:
            condition = str(row["condition_id"]).lower()
            asset = str(row.get("asset") or "")
            condition_rows[(key, condition)].append(row)
            asset_rows[(key, condition, asset)].append(row)

    closed_by_asset: dict[tuple[str, str, str], dict[str, Any]] = {}
    closed_by_condition: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for key in rows_by_trader:
        path = output_root / "raw" / f"{key}_closed_positions.jsonl"
        for row in read_jsonl(path):
            condition = str(row.get("conditionId") or "").lower()
            asset = str(row.get("asset") or "")
            if condition not in catalog or catalog[condition].get("scope") != "SPORTS_INCLUDED":
                continue
            if (key, condition) not in condition_rows:
                continue
            closed_by_asset[(key, condition, asset)] = row
            closed_by_condition[(key, condition)].append(row)

    lifecycle_rows: list[dict[str, Any]] = []
    for (key, condition, asset), rows in sorted(asset_rows.items()):
        market = catalog[condition]
        payout_map = resolved_payouts(market)
        outcome_index = int(rows[0].get("outcome_index") or 0)
        resolution_ts = market_resolution_timestamp(condition, catalog, raw_minimal)
        payout = payout_map.get(outcome_index) if payout_map else None
        if resolution_ts is None or resolution_ts > CUTOFF_INCLUSIVE_UNIX:
            payout = None
        lifecycle = fifo_lifecycle(rows, payout, resolution_ts)
        official = closed_by_asset.get((key, condition, asset))
        lifecycle_rows.append(
            {
                "trader_key": key,
                "condition_id": condition,
                "asset": asset,
                "outcome": rows[0].get("outcome"),
                "outcome_index": outcome_index,
                "sport_code": market.get("sport_code"),
                "league": market.get("league"),
                "bet_family": market.get("bet_family"),
                "first_trade_timestamp": min(int(row["timestamp"]) for row in rows),
                "last_trade_timestamp": max(int(row["timestamp"]) for row in rows),
                "resolution_timestamp": resolution_ts,
                "fills": len(rows),
                **lifecycle,
                "official_avg_price": official.get("avgPrice") if official else None,
                "official_total_bought": official.get("totalBought") if official else None,
                "official_realized_pnl": official.get("realizedPnl") if official else None,
            }
        )

    market_pnl_rows: list[dict[str, Any]] = []
    for (key, condition), rows in sorted(condition_rows.items()):
        market = catalog[condition]
        payout_map = resolved_payouts(market)
        resolution_ts = market_resolution_timestamp(condition, catalog, raw_minimal)
        official_rows = closed_by_condition.get((key, condition), [])
        if (
            payout_map is None
            or resolution_ts is None
            or resolution_ts > CUTOFF_INCLUSIVE_UNIX
            or not official_rows
        ):
            continue
        official_pnl = sum(number(row.get("realizedPnl")) for row in official_rows)
        official_cost = sum(
            number(row.get("avgPrice")) * number(row.get("totalBought"))
            for row in official_rows
        )
        event_id = rows[0].get("event_id")
        style = behavioral_style(rows)
        timing_set = {str(row.get("timing")) for row in rows}
        timing_style = (
            "PREGAME_ONLY"
            if timing_set == {"PREGAME"}
            else "LIVE_ONLY"
            if timing_set == {"LIVE_OR_AFTER_START"}
            else "MIXED_OR_UNKNOWN"
        )
        market_pnl_rows.append(
            {
                "trader_key": key,
                "condition_id": condition,
                "event_id": event_id,
                "event_slug": rows[0].get("event_slug"),
                "slug": rows[0].get("slug"),
                "title": rows[0].get("title"),
                "sport_code": market.get("sport_code"),
                "league": market.get("league"),
                "sports_market_type": market.get("sports_market_type"),
                "bet_family": market.get("bet_family"),
                "behavioral_style": style,
                "timing_style": timing_style,
                "first_trade_timestamp": min(int(row["timestamp"]) for row in rows),
                "last_trade_timestamp": max(int(row["timestamp"]) for row in rows),
                "resolution_timestamp": resolution_ts,
                "resolution_utc": utc_iso(resolution_ts),
                "fills": len(rows),
                "transactions": len({str(row.get("transaction_hash")) for row in rows}),
                "notional_usd": sum(number(row.get("notional_usd")) for row in rows),
                "official_position_rows": len(official_rows),
                "official_total_bought_cost": official_cost,
                "official_realized_pnl": official_pnl,
                "official_roi_on_bought_cost": official_pnl / official_cost if official_cost else None,
            }
        )

    event_exposure: dict[tuple[str, str], dict[str, Any]] = {}
    for (key, condition), rows in condition_rows.items():
        event_key = str(rows[0].get("event_id") or rows[0].get("event_slug") or condition)
        target = event_exposure.setdefault(
            (key, event_key),
            {
                "trader_key": key,
                "event_key": event_key,
                "event_slug": rows[0].get("event_slug"),
                "conditions": set(),
                "fills": 0,
                "notional_usd": 0.0,
                "official_resolved_pnl": 0.0,
                "resolved_conditions": 0,
            },
        )
        target["conditions"].add(condition)
        target["fills"] += len(rows)
        target["notional_usd"] += sum(number(row.get("notional_usd")) for row in rows)
    for row in market_pnl_rows:
        event_key = str(row.get("event_id") or row.get("event_slug") or row["condition_id"])
        target = event_exposure[(row["trader_key"], event_key)]
        target["official_resolved_pnl"] += number(row.get("official_realized_pnl"))
        target["resolved_conditions"] += 1
    event_rows = []
    for target in event_exposure.values():
        event_rows.append(
            {
                **target,
                "conditions": len(target["conditions"]),
            }
        )
    event_rows.sort(key=lambda row: (row["trader_key"], -row["notional_usd"], row["event_key"]))

    trader_summaries: dict[str, dict[str, Any]] = {}
    for key, rows in sorted(rows_by_trader.items()):
        resolved = [row for row in market_pnl_rows if row["trader_key"] == key]
        pnl_summary = summarize_pnl(resolved)
        max_concurrent = concurrent_gross_cost_proxy(resolved)
        pnl_summary["max_concurrent_total_bought_cost_proxy"] = max_concurrent
        pnl_summary["pnl_over_max_concurrent_cost_proxy"] = (
            pnl_summary["total_official_realized_pnl"] / max_concurrent
            if max_concurrent
            else None
        )
        holding = [
            row["resolution_timestamp"] - row["first_trade_timestamp"]
            for row in resolved
            if row["resolution_timestamp"] >= row["first_trade_timestamp"]
        ]
        pnl_summary["median_holding_seconds_to_resolution"] = quantile(holding, 0.5)
        pnl_summary["p90_holding_seconds_to_resolution"] = quantile(holding, 0.9)
        behavior = behavioral_metrics(rows)
        stability = weekly_stability(resolved)
        summary = {
            "trader_key": key,
            "behavior": behavior,
            "pnl": pnl_summary,
            "stability": stability,
            "exact_duplicate_sensitivity": {
                "exact_duplicate_rows_preserved": duplicate_counts[key],
                "raw_rows": len(rows),
                "unique_exact_rows": len(exact_seen[key]),
                "duplicate_fraction": duplicate_counts[key] / len(rows) if rows else 0,
                "official_pnl_impact": "NONE; official closed-position PnL is not summed from duplicated fill rows.",
            },
        }
        summary["score"] = score_trader(summary)
        trader_summaries[key] = summary

    candidates = [
        summary
        for summary in trader_summaries.values()
        if summary["score"]["eligible_for_copy_research"]
    ]
    candidates.sort(
        key=lambda row: (
            -row["score"]["passed_gates"],
            -number(row["stability"].get("positive_week_fraction")),
            -number(row["pnl"].get("profit_factor")),
            -number(row["pnl"].get("total_official_realized_pnl")),
        )
    )
    selection = {
        "selected_for_copy_feasibility": candidates[0]["trader_key"] if candidates else None,
        "eligible_traders": [row["trader_key"] for row in candidates],
        "selection_rule": "Gates de PnL/PF/ROI/muestra; después estabilidad semanal, PF y PnL. No usa el backtest de copia.",
    }

    analysis_dir = output_root / "analysis"
    write_jsonl(analysis_dir / "position_lifecycles.jsonl", lifecycle_rows)
    write_csv(analysis_dir / "position_lifecycles.csv", lifecycle_rows)
    write_jsonl(analysis_dir / "market_pnl.jsonl", market_pnl_rows)
    write_csv(analysis_dir / "market_pnl.csv", market_pnl_rows)
    write_jsonl(analysis_dir / "event_exposure.jsonl", event_rows)
    write_csv(analysis_dir / "event_exposure.csv", event_rows)
    breakdown = grouped_pnl(
        market_pnl_rows,
        ["trader_key", "sport_code", "league", "bet_family", "behavioral_style", "timing_style"],
    )
    write_jsonl(analysis_dir / "strategy_breakdown.jsonl", breakdown)
    write_csv(analysis_dir / "strategy_breakdown.csv", breakdown)
    result = {
        "schema": ANALYSIS_SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "cutoff_inclusive_unix": CUTOFF_INCLUSIVE_UNIX,
        "cutoff_utc": utc_iso(CUTOFF_INCLUSIVE_UNIX),
        "pnl_scope": "realizedPnl oficial de posiciones cerradas solo para mercados con payout binario y closedTime/finishedTimestamp <= corte",
        "traders": trader_summaries,
        "selection": selection,
        "counts": {
            "sports_fill_rows": sum(len(rows) for rows in rows_by_trader.values()),
            "position_lifecycles": len(lifecycle_rows),
            "resolved_market_pnl_rows": len(market_pnl_rows),
            "event_exposures": len(event_rows),
            "strategy_breakdown_rows": len(breakdown),
        },
        "limitations": [
            "realizedPnl procede de Data API; la documentación pública no desglosa en closed-positions qué parte corresponde a fees o rebates.",
            "El proxy de capital concurrente usa coste total comprado por mercado durante todo su ciclo y puede sobreestimar capital simultáneo cuando hubo rotación.",
            "Comprar ambos resultados se etiqueta como inventario pareado conductual, no como arbitraje probado.",
            "Los timings LIVE_OR_AFTER_START no distinguen fill durante el partido de fill posterior al inicio si faltan estados deportivos históricos.",
            "Los fills exactos duplicados se conservan; el PnL oficial evita que esos duplicados alteren el resultado principal.",
        ],
        "safety": capture_manifest["safety"],
    }
    write_json(analysis_dir / "analysis_summary.json", result)
    return result


__all__ = [
    "ANALYSIS_SCHEMA",
    "analyze",
    "behavioral_metrics",
    "behavioral_style",
    "fifo_lifecycle",
    "quantile",
    "resolved_payouts",
    "summarize_pnl",
]
