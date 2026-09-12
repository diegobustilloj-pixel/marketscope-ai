from __future__ import annotations

import csv
import json
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Sequence

from .sports_wallet_analysis import number, quantile
from .sports_wallet_copy import (
    Aggregate,
    PRIMARY_ADVERSE_IMPACT,
    PRIMARY_DELAY_SECONDS,
    PRIMARY_STAKE_USD,
    simulate_execution,
)
from .sports_wallet_research import (
    CUTOFF_INCLUSIVE_UNIX,
    CUTOFF_UTC,
    TRADERS,
    read_jsonl,
    sha256_file,
    utc_iso,
    write_csv,
    write_json,
    write_jsonl,
)


FINAL_SCHEMA = "polymarket_sports_final_report_v001"
PRICE_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0.00, 0.10, "0.01-0.10"),
    (0.10, 0.20, "0.10-0.20"),
    (0.20, 0.30, "0.20-0.30"),
    (0.30, 0.40, "0.30-0.40"),
    (0.40, 0.50, "0.40-0.50"),
    (0.50, 0.60, "0.50-0.60"),
    (0.60, 0.70, "0.60-0.70"),
    (0.70, 0.80, "0.70-0.80"),
    (0.80, 0.90, "0.80-0.90"),
    (0.90, 0.95, "0.90-0.95"),
    (0.95, 1.001, "0.95-0.99"),
)


def price_bucket(value: Any) -> str:
    price = number(value, -1.0)
    if price < 0:
        return "UNKNOWN"
    for lower, upper, label in PRICE_BUCKETS:
        if lower <= price < upper:
            return label
    return "OUT_OF_RANGE"


def timing_bucket(seconds_to_start: Any) -> str:
    if seconds_to_start is None:
        return "UNKNOWN"
    seconds = int(number(seconds_to_start))
    if seconds < 0:
        elapsed = -seconds
        if elapsed <= 30 * 60:
            return "LIVE_EARLY_0_30M"
        if elapsed <= 90 * 60:
            return "LIVE_MIDDLE_30_90M"
        return "LIVE_LATE_OR_AFTER_90M"
    if seconds > 24 * 3600:
        return "PREGAME_GT_24H"
    if seconds > 6 * 3600:
        return "PREGAME_6_24H"
    if seconds > 3600:
        return "PREGAME_1_6H"
    if seconds > 30 * 60:
        return "PREGAME_30_60M"
    if seconds > 10 * 60:
        return "PREGAME_10_30M"
    return "PREGAME_0_10M"


def _json_rows(path: Path) -> list[dict[str, Any]]:
    return list(read_jsonl(path))


def _profit_factor(profit: float, loss: float) -> float | None:
    return profit / loss if loss > 0 else None


def _drawdown(rows: Sequence[dict[str, Any]], pnl_field: str, time_field: str) -> dict[str, Any]:
    ordered = sorted(
        rows,
        key=lambda row: (int(row.get(time_field) or 0), str(row.get("condition_id") or row.get("signal_id") or "")),
    )
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    peak_time: int | None = None
    drawdown_start: int | None = None
    longest = 0
    worst_day: dict[str, Any] | None = None
    by_day: defaultdict[str, float] = defaultdict(float)
    by_week: defaultdict[str, float] = defaultdict(float)
    equity: list[dict[str, Any]] = []
    for row in ordered:
        timestamp = int(row.get(time_field) or 0)
        pnl = number(row.get(pnl_field))
        cumulative += pnl
        equity.append({"timestamp": timestamp, "timestamp_utc": utc_iso(timestamp), "pnl": pnl, "equity": cumulative})
        dt = datetime.fromtimestamp(timestamp, timezone.utc)
        by_day[dt.date().isoformat()] += pnl
        iso = dt.isocalendar()
        by_week[f"{iso.year}-W{iso.week:02d}"] += pnl
        if cumulative >= peak:
            if drawdown_start is not None:
                longest = max(longest, timestamp - drawdown_start)
            peak = cumulative
            peak_time = timestamp
            drawdown_start = None
        else:
            if drawdown_start is None:
                drawdown_start = peak_time or timestamp
            max_drawdown = min(max_drawdown, cumulative - peak)
    if drawdown_start is not None and ordered:
        longest = max(longest, int(ordered[-1].get(time_field) or 0) - drawdown_start)
    if by_day:
        day, pnl = min(by_day.items(), key=lambda item: item[1])
        worst_day = {"period": day, "pnl": pnl}
    worst_week = None
    if by_week:
        week, pnl = min(by_week.items(), key=lambda item: item[1])
        worst_week = {"period": week, "pnl": pnl}
    return {
        "max_drawdown_usd": max_drawdown,
        "longest_drawdown_seconds": longest,
        "longest_drawdown_days": longest / 86400 if longest else 0.0,
        "worst_day": worst_day,
        "worst_week": worst_week,
        "equity_curve": equity,
    }


def summarize_rows(
    rows: Sequence[dict[str, Any]],
    *,
    pnl_field: str,
    cost_field: str,
    time_field: str,
) -> dict[str, Any]:
    pnl_values = [number(row.get(pnl_field)) for row in rows]
    costs = [number(row.get(cost_field)) for row in rows]
    wins = [value for value in pnl_values if value > 1e-9]
    losses = [value for value in pnl_values if value < -1e-9]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    risk = _drawdown(rows, pnl_field, time_field)
    n = len(rows)
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "flat": n - len(wins) - len(losses),
        "win_rate": len(wins) / n if n else None,
        "net_pnl_usd": sum(pnl_values),
        "total_cost_usd": sum(costs),
        "roi_on_cost": sum(pnl_values) / sum(costs) if sum(costs) else None,
        "profit_factor": _profit_factor(gross_profit, gross_loss),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "average_pnl_usd": sum(pnl_values) / n if n else None,
        "median_pnl_usd": quantile(pnl_values, 0.5),
        "average_win_usd": sum(wins) / len(wins) if wins else None,
        "average_loss_usd": sum(losses) / len(losses) if losses else None,
        "win_loss_ratio": (sum(wins) / len(wins)) / abs(sum(losses) / len(losses)) if wins and losses else None,
        "ev_per_100_cost_usd": 100 * sum(pnl_values) / sum(costs) if sum(costs) else None,
        "max_drawdown_usd": risk["max_drawdown_usd"],
        "longest_drawdown_days": risk["longest_drawdown_days"],
        "worst_day": risk["worst_day"],
        "worst_week": risk["worst_week"],
    }


def grouped_rankings(
    rows: Sequence[dict[str, Any]],
    field: str,
    *,
    pnl_field: str,
    cost_field: str,
    time_field: str,
) -> list[dict[str, Any]]:
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "UNKNOWN")].append(row)
    result = []
    for key, group in groups.items():
        summary = summarize_rows(group, pnl_field=pnl_field, cost_field=cost_field, time_field=time_field)
        confidence = min(100.0, 20.0 + 20.0 * math.log10(max(1, summary["n"])))
        result.append({field: key, **summary, "confidence_score": confidence})
    result.sort(key=lambda row: (-number(row["net_pnl_usd"]), -int(row["n"]), str(row[field])))
    for index, row in enumerate(result, 1):
        row["pnl_rank"] = index
    return result


def capital_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    events: list[tuple[int, int, float]] = []
    event_cost: defaultdict[str, float] = defaultdict(float)
    holding: list[float] = []
    for row in rows:
        start = int(row.get("first_trade_timestamp") or 0)
        end = int(row.get("resolution_timestamp") or 0)
        cost = number(row.get("official_total_bought_cost"))
        if start <= 0 or end < start or cost <= 0:
            continue
        events.append((start, 1, cost))
        events.append((end, 0, -cost))
        holding.append(end - start)
        event_cost[str(row.get("event_id") or row.get("event_slug") or row.get("condition_id"))] += cost
    current = peak = area = 0.0
    previous: int | None = None
    segments: list[tuple[float, int]] = []
    for timestamp, _, delta in sorted(events):
        if previous is not None and timestamp > previous:
            duration = timestamp - previous
            area += current * duration
            segments.append((current, duration))
        current += delta
        peak = max(peak, current)
        previous = timestamp
    span = (max(t for t, _, _ in events) - min(t for t, _, _ in events)) if events else 0
    average = area / span if span else 0.0
    total_duration = sum(duration for _, duration in segments)
    midpoint = total_duration / 2
    running = 0
    weighted_median = 0.0
    for value, duration in sorted(segments, key=lambda item: item[0]):
        running += duration
        if running >= midpoint:
            weighted_median = value
            break
    total_pnl = sum(number(row.get("official_realized_pnl")) for row in rows)
    turnover = sum(number(row.get("official_total_bought_cost")) for row in rows)
    return {
        "peak_concurrent_capital_proxy_usd": peak,
        "average_concurrent_capital_proxy_usd": average,
        "median_concurrent_capital_proxy_usd": weighted_median,
        "maximum_event_cost_proxy_usd": max(event_cost.values()) if event_cost else 0.0,
        "turnover_usd": turnover,
        "median_lockup_seconds": quantile(holding, 0.5),
        "average_lockup_seconds": sum(holding) / len(holding) if holding else None,
        "pnl_over_peak_capital": total_pnl / peak if peak else None,
        "pnl_over_average_capital": total_pnl / average if average else None,
        "pnl_over_turnover": total_pnl / turnover if turnover else None,
        "pnl_per_1000_peak_capital_usd": 1000 * total_pnl / peak if peak else None,
        "method": "UPPER_BOUND_PROXY: total bought cost is held from first fill until resolution; rotations and exits can overstate capital.",
    }


def bootstrap_roi(rows: Sequence[dict[str, Any]], samples: int = 500, seed: int = 20260831) -> dict[str, Any]:
    pairs = [
        (number(row.get("official_realized_pnl")), number(row.get("official_total_bought_cost")))
        for row in rows
        if number(row.get("official_total_bought_cost")) > 0
    ]
    if not pairs:
        return {"samples": 0, "roi_ci95": [None, None], "positive_roi_probability": None}
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(samples):
        draw = [pairs[rng.randrange(len(pairs))] for _ in range(len(pairs))]
        cost = sum(item[1] for item in draw)
        values.append(sum(item[0] for item in draw) / cost if cost else 0.0)
    return {
        "samples": samples,
        "roi_ci95": [quantile(values, 0.025), quantile(values, 0.975)],
        "positive_roi_probability": sum(value > 0 for value in values) / len(values),
        "method": "market-level nonparametric bootstrap; descriptive because markets within an event may be correlated",
    }


def remove_top_winners(rows: Sequence[dict[str, Any]]) -> dict[str, float]:
    values = sorted((number(row.get("official_realized_pnl")) for row in rows), reverse=True)
    return {
        f"remove_top_{count}": sum(values[count:])
        for count in (1, 3, 5, 10)
        if len(values) > count
    }


def tail_shocks(rows: Sequence[dict[str, Any]]) -> dict[str, float | None]:
    values = [number(row.get("official_realized_pnl")) for row in rows]
    if not values:
        return {"one_extra_worst_loss": None, "two_extra_worst_losses": None, "three_extra_worst_losses": None}
    total = sum(values)
    worst = min(values)
    return {
        "one_extra_worst_loss": total + worst,
        "two_extra_worst_losses": total + 2 * worst,
        "three_extra_worst_losses": total + 3 * worst,
    }


def enrich_positions(
    lifecycle_rows: Sequence[dict[str, Any]],
    catalog: dict[str, dict[str, Any]],
    market_rows: Sequence[dict[str, Any]],
    event_rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    trader_map = {row["key"]: row for row in TRADERS}
    market_map = {(str(row["trader_key"]), str(row["condition_id"]).lower()): row for row in market_rows}
    event_map = {(str(row["trader_key"]), str(row["event_key"])): row for row in event_rows}
    result: list[dict[str, Any]] = []
    for row in lifecycle_rows:
        trader = str(row["trader_key"])
        condition = str(row["condition_id"]).lower()
        market = catalog.get(condition, {})
        market_pnl = market_map.get((trader, condition), {})
        event_key = str(market_pnl.get("event_id") or market_pnl.get("event_slug") or condition)
        exposure = event_map.get((trader, event_key), {})
        avg_entry = number(row.get("buy_cost")) / number(row.get("buy_shares")) if number(row.get("buy_shares")) else None
        avg_exit = number(row.get("sell_proceeds")) / number(row.get("sell_shares")) if number(row.get("sell_shares")) else None
        official_price = row.get("official_avg_price")
        official_total = row.get("official_total_bought")
        official_cost = number(official_price) * number(official_total) if official_price is not None and official_total is not None else None
        official_pnl = row.get("official_realized_pnl")
        official_roi = number(official_pnl) / official_cost if official_pnl is not None and official_cost else None
        resolution = row.get("resolution_timestamp")
        first = row.get("first_trade_timestamp")
        sell_shares = number(row.get("sell_shares"))
        remaining = number(row.get("remaining_shares"))
        outcome = str(row.get("outcome") or "")
        normalized = outcome.strip().lower()
        outcome_role = "YES" if normalized == "yes" else "NO" if normalized == "no" else "NAMED_OUTCOME"
        result.append(
            {
                "trader_key": trader,
                "trader_name": trader_map.get(trader, {}).get("profile_name"),
                "wallet": trader_map.get(trader, {}).get("proxy_wallet"),
                "condition_id": condition,
                "market_id": market.get("market_id"),
                "event_id": market_pnl.get("event_id"),
                "event_slug": market.get("event_slug"),
                "market_slug": market.get("slug"),
                "market_name": market.get("question") or market_pnl.get("title"),
                "sport_code": market.get("sport_code") or row.get("sport_code"),
                "league": market.get("league") or row.get("league"),
                "sports_market_type": market.get("sports_market_type"),
                "bet_family": market.get("bet_family") or row.get("bet_family"),
                "outcome": outcome,
                "outcome_role": outcome_role,
                "outcome_index": row.get("outcome_index"),
                "asset": row.get("asset"),
                "first_trade_timestamp": first,
                "first_trade_utc": utc_iso(int(first)) if first else None,
                "last_trade_timestamp": row.get("last_trade_timestamp"),
                "resolution_timestamp": resolution,
                "resolution_utc": utc_iso(int(resolution)) if resolution else None,
                "market_start_timestamp": market.get("market_start_unix"),
                "timing_style": market_pnl.get("timing_style"),
                "behavioral_style": market_pnl.get("behavioral_style"),
                "fills": row.get("fills"),
                "buy_shares": row.get("buy_shares"),
                "buy_cost_usd": row.get("buy_cost"),
                "weighted_average_entry": avg_entry,
                "sell_shares": row.get("sell_shares"),
                "sell_proceeds_usd": row.get("sell_proceeds"),
                "weighted_average_exit": avg_exit,
                "fifo_trade_pnl_usd": row.get("fifo_trade_pnl"),
                "remaining_shares": row.get("remaining_shares"),
                "remaining_cost_usd": row.get("remaining_cost"),
                "settlement_payout": row.get("settlement_payout"),
                "settlement_pnl_usd": row.get("settlement_pnl"),
                "gross_reconstructed_pnl_usd": row.get("gross_reconstructed_pnl"),
                "official_average_entry": official_price,
                "official_total_bought_shares": official_total,
                "official_cost_usd": official_cost,
                "official_realized_pnl_usd": official_pnl,
                "official_roi": official_roi,
                "price_bucket": price_bucket(official_price if official_price is not None else avg_entry),
                "holding_seconds_to_resolution": int(resolution) - int(first) if resolution and first and int(resolution) >= int(first) else None,
                "partial_exit": sell_shares > 0 and remaining > 1e-9,
                "full_exit_before_resolution": sell_shares > 0 and remaining <= 1e-9,
                "held_exposure_to_resolution": row.get("settlement_payout") is not None and remaining > 1e-9,
                "official_closed_position": official_pnl is not None,
                "event_conditions": exposure.get("conditions"),
                "event_fills": exposure.get("fills"),
                "event_notional_usd": exposure.get("notional_usd"),
                "event_official_resolved_pnl_usd": exposure.get("official_resolved_pnl"),
            }
        )
    return result


def current_position_coverage(output_root: Path, catalog: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_trader: dict[str, Any] = {}
    total_rows = open_rows = redeemable_rows = 0
    for trader in (row["key"] for row in TRADERS):
        path = output_root / "raw" / f"{trader}_positions.jsonl"
        rows = []
        for row in read_jsonl(path):
            condition = str(row.get("conditionId") or "").lower()
            market = catalog.get(condition)
            if market and market.get("scope") == "SPORTS_INCLUDED":
                rows.append(row)
        open_at_cutoff = [
            row for row in rows
            if catalog.get(str(row.get("conditionId") or "").lower(), {}).get("closed") is False
        ]
        redeemable = [row for row in rows if bool(row.get("redeemable"))]
        by_trader[trader] = {
            "current_position_api_rows": len(rows),
            "open_market_rows_at_capture": len(open_at_cutoff),
            "redeemable_rows": len(redeemable),
            "current_value_usd": sum(number(row.get("currentValue")) for row in rows),
            "initial_value_usd": sum(number(row.get("initialValue")) for row in rows),
        }
        total_rows += len(rows)
        open_rows += len(open_at_cutoff)
        redeemable_rows += len(redeemable)
    return {
        "by_trader": by_trader,
        "sports_current_position_api_rows": total_rows,
        "open_market_rows_at_capture": open_rows,
        "redeemable_rows": redeemable_rows,
        "note": "Current-position rows are an API snapshot and are not added to closed realized PnL.",
    }


def _grid_row(
    rows: Sequence[dict[str, Any]],
    trader: str,
    delay: int,
    stake: float,
    impact: float,
    split: str = "ALL",
) -> dict[str, Any] | None:
    for row in rows:
        if (
            str(row.get("trader_key")) == trader
            and str(row.get("split")) == split
            and int(row.get("delay_seconds") or 0) == delay
            and abs(number(row.get("requested_stake_usd")) - stake) <= 1e-12
            and abs(number(row.get("adverse_impact")) - impact) <= 1e-12
        ):
            return row
    return None


def copy_analysis(
    grid_rows: Sequence[dict[str, Any]],
    copy_summary: dict[str, Any],
    trader_pnl: dict[str, float],
) -> dict[str, Any]:
    delays = (0, 1, 5, 15, 30, 60, 120, 300)
    comparison: list[dict[str, Any]] = []
    score_rows: list[dict[str, Any]] = []
    for trader in (row["key"] for row in TRADERS):
        perfect = _grid_row(grid_rows, trader, 0, PRIMARY_STAKE_USD, 0.0)
        one = _grid_row(grid_rows, trader, 1, PRIMARY_STAKE_USD, PRIMARY_ADVERSE_IMPACT)
        sixty = _grid_row(grid_rows, trader, 60, PRIMARY_STAKE_USD, PRIMARY_ADVERSE_IMPACT)
        split_eval = copy_summary.get("selection", {}).get("evaluations", {}).get(trader, {})
        positive_splits = sum(
            number(split_eval.get(split, {}).get("net_pnl_usd")) > 0
            for split in ("train", "validation", "test")
        )
        primary = _grid_row(grid_rows, trader, PRIMARY_DELAY_SECONDS, PRIMARY_STAKE_USD, PRIMARY_ADVERSE_IMPACT)
        if primary:
            fill_component = 35 * number(primary.get("fill_rate"))
            capacity_component = 20 * (1 - number(primary.get("capacity_limited_fraction")))
            latency_ratio = min(1.0, number(sixty.get("fill_rate")) / number(one.get("fill_rate"))) if one and sixty and number(one.get("fill_rate")) else 0.0
            latency_component = 15 * latency_ratio
            stability_component = 20 * positive_splits / 3
            sample_component = 10 * min(1.0, number(primary.get("fills")) / 100)
            score_rows.append(
                {
                    "trader_key": trader,
                    "copyability_score": round(fill_component + capacity_component + latency_component + stability_component + sample_component, 2),
                    "fill_component": fill_component,
                    "capacity_component": capacity_component,
                    "latency_component": latency_component,
                    "stability_component": stability_component,
                    "sample_component": sample_component,
                    "dependency_risk_0_10": 9,
                }
            )
        for delay in delays:
            impact = 0.0 if delay == 0 else PRIMARY_ADVERSE_IMPACT
            row = _grid_row(grid_rows, trader, delay, PRIMARY_STAKE_USD, impact)
            if not row:
                continue
            comparison.append(
                {
                    "trader_key": trader,
                    "scenario": "PERFECT_COPY_CEILING" if delay == 0 else f"COPY_PLUS_{delay}S",
                    "delay_seconds": delay,
                    "adverse_impact": impact,
                    "original_trader_pnl_usd": trader_pnl.get(trader),
                    "copy_net_pnl_usd": row.get("net_pnl_usd"),
                    "copy_roi": row.get("roi_on_executed_cost"),
                    "copy_profit_factor": row.get("profit_factor"),
                    "copy_max_drawdown_usd": row.get("max_drawdown_usd"),
                    "fill_rate": row.get("fill_rate"),
                    "capacity_limited_fraction": row.get("capacity_limited_fraction"),
                    "mean_market_price_move_vs_leader": row.get("mean_price_slippage_vs_leader"),
                    "fills": row.get("fills"),
                    "copy_decay_pnl_vs_perfect": number(row.get("net_pnl_usd")) - number(perfect.get("net_pnl_usd")) if perfect else None,
                    "copy_decay_roi_vs_perfect": number(row.get("roi_on_executed_cost")) - number(perfect.get("roi_on_executed_cost")) if perfect else None,
                }
            )
    score_rows.sort(key=lambda row: -number(row["copyability_score"]))
    return {
        "comparison": comparison,
        "copyability_scores": score_rows,
        "preregistered_selection": copy_summary.get("selection"),
        "score_formula": "35% fill rate at 15s; 20% printed-capacity headroom; 15% 60s/1s fill retention; 20% positive train/validation/test splits; 10% sample size.",
        "warning": "Score measures operational copyability, not profitability. The preregistered economic gate remains authoritative.",
    }


def posthoc_2500_sensitivity(opportunities: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregates: defaultdict[tuple[str, str, int], Aggregate] = defaultdict(Aggregate)
    for opportunity in opportunities:
        execution = simulate_execution(opportunity, 2500.0, PRIMARY_ADVERSE_IMPACT)
        for split in (str(opportunity["split"]), "ALL"):
            aggregates[(str(opportunity["trader_key"]), split, int(opportunity["delay_seconds"]))].add(execution, opportunity)
    result = []
    for (trader, split, delay), aggregate in sorted(aggregates.items()):
        result.append(
            {
                "trader_key": trader,
                "split": split,
                "delay_seconds": delay,
                "requested_stake_usd": 2500.0,
                "adverse_impact": PRIMARY_ADVERSE_IMPACT,
                "preregistration_status": "POST_HOC_SENSITIVITY_REQUESTED_BY_MASTER_PROMPT",
                **aggregate.as_dict(),
            }
        )
    return result


def _execution_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (int(row.get("signal_timestamp") or 0), str(row.get("signal_id") or "")))
    filled = [row for row in ordered if number(row.get("executed_stake_usd")) > 0]
    pnl = [number(row.get("net_pnl_usd")) for row in filled]
    gross_profit = sum(value for value in pnl if value > 0)
    gross_loss = -sum(value for value in pnl if value < 0)
    total_cost = sum(number(row.get("executed_stake_usd")) + number(row.get("fee_usd")) for row in filled)
    cumulative = peak = max_drawdown = 0.0
    for value in pnl:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    return {
        "opportunities": len(ordered),
        "fills": len(filled),
        "fill_rate": len(filled) / len(ordered) if ordered else None,
        "net_pnl_usd": sum(pnl),
        "executed_cost_usd": total_cost,
        "roi": sum(pnl) / total_cost if total_cost else None,
        "profit_factor": _profit_factor(gross_profit, gross_loss),
        "max_drawdown_usd": max_drawdown,
        "wins": sum(value > 0 for value in pnl),
        "losses": sum(value < 0 for value in pnl),
    }


def exploratory_strategy_matrix(primary_rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for row in primary_rows if int(row.get("delay_seconds") or 0) == PRIMARY_DELAY_SECONDS]
    groups: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        trader = str(row.get("trader_key") or "UNKNOWN")
        sport = str(row.get("sport_code") or "UNKNOWN")
        bet = str(row.get("bet_family") or "UNKNOWN")
        timing = str(row.get("timing") or "UNKNOWN")
        definitions = (
            ("COPY_TRADER", trader),
            ("TRADER_SPORT", f"{trader}|{sport}"),
            ("TRADER_BET_TYPE", f"{trader}|{bet}"),
            ("TRADER_TIMING", f"{trader}|{timing}"),
            ("TRADER_SPORT_BET_TIMING", f"{trader}|{sport}|{bet}|{timing}"),
            ("CROSS_TRADER_SPORT_BET_TIMING", f"{sport}|{bet}|{timing}"),
        )
        for kind, label in definitions:
            groups[(kind, label)].append(row)
    matrix: list[dict[str, Any]] = []
    eligible_before_test: list[dict[str, Any]] = []
    for (kind, label), group in groups.items():
        by_split = {split: _execution_summary([row for row in group if row.get("split") == split]) for split in ("TRAIN", "VALIDATION", "TEST")}
        all_summary = _execution_summary(group)
        train = by_split["TRAIN"]
        validation = by_split["VALIDATION"]
        test = by_split["TEST"]
        sample_ok = train["fills"] >= 40 and validation["fills"] >= 15 and test["fills"] >= 15
        train_ok = train["fills"] >= 40 and number(train["net_pnl_usd"]) > 0 and number(train["roi"]) > 0 and number(train["profit_factor"]) > 1
        validation_ok = validation["fills"] >= 15 and number(validation["net_pnl_usd"]) > 0 and number(validation["roi"]) > 0 and number(validation["profit_factor"]) > 1
        qualifies = sample_ok and train_ok and validation_ok
        test_confirmed = test["fills"] >= 15 and number(test["net_pnl_usd"]) > 0 and number(test["roi"]) > 0 and number(test["profit_factor"]) > 1
        record = {
            "candidate_type": kind,
            "candidate": label,
            "sample_gate_passed": sample_ok,
            "qualified_before_test": qualifies,
            "test_confirmed": test_confirmed if qualifies else False,
            "train": train,
            "validation": validation,
            "test": test,
            "all": all_summary,
        }
        matrix.append(record)
        if qualifies:
            eligible_before_test.append(record)
    eligible_before_test.sort(
        key=lambda row: (
            -min(number(row["train"]["profit_factor"]), number(row["validation"]["profit_factor"])),
            -min(number(row["train"]["roi"]), number(row["validation"]["roi"])),
            -(int(row["train"]["fills"]) + int(row["validation"]["fills"])),
        )
    )
    selected = eligible_before_test[0] if eligible_before_test else None
    matrix.sort(
        key=lambda row: (
            not row["qualified_before_test"],
            -number(row["validation"]["net_pnl_usd"]),
            -int(row["all"]["fills"]),
        )
    )
    return {
        "candidate_universe": [
            "copy each trader",
            "trader x sport",
            "trader x bet type",
            "trader x timing",
            "trader x sport x bet type x timing",
            "cross-trader sport x bet type x timing",
        ],
        "selection_uses_test": False,
        "multiple_testing_warning": "Exploratory family created after the original capture; any survivor requires a new preregistered forward test.",
        "eligible_before_test_count": len(eligible_before_test),
        "selected_before_opening_test": selected,
        "selected_test_confirmed": bool(selected and selected["test_confirmed"]),
        "matrix": matrix,
    }


def consensus_and_leaders(
    signals: Sequence[dict[str, Any]],
    primary_rows: Sequence[dict[str, Any]],
    window_seconds: int = 300,
) -> dict[str, Any]:
    executions = {
        str(row["signal_id"]): row
        for row in primary_rows
        if int(row.get("delay_seconds") or 0) == PRIMARY_DELAY_SECONDS
    }
    by_asset: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        by_asset[str(signal.get("asset") or "")].append(signal)
    consensus_rows: list[dict[str, Any]] = []
    leader_pairs: Counter[str] = Counter()
    lag_values: defaultdict[str, list[int]] = defaultdict(list)
    for asset, rows in by_asset.items():
        if not asset:
            continue
        ordered = sorted(rows, key=lambda row: (int(row["signal_timestamp"]), str(row["trader_key"])))
        traders = {str(row["trader_key"]) for row in ordered}
        if len(traders) < 2:
            continue
        for first_index, first in enumerate(ordered):
            for second in ordered[first_index + 1 :]:
                if str(first["trader_key"]) == str(second["trader_key"]):
                    continue
                lag = int(second["signal_timestamp"]) - int(first["signal_timestamp"])
                if lag > window_seconds:
                    break
                pair = f"{first['trader_key']}->{second['trader_key']}"
                leader_pairs[pair] += 1
                lag_values[pair].append(lag)
        first_time = int(ordered[0]["signal_timestamp"])
        cluster = [row for row in ordered if int(row["signal_timestamp"]) - first_time <= window_seconds]
        distinct: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in cluster:
            trader = str(row["trader_key"])
            if trader not in seen:
                distinct.append(row)
                seen.add(trader)
        if len(distinct) < 2:
            continue
        trigger = distinct[1]
        execution = executions.get(str(trigger["signal_id"]))
        if execution:
            consensus_rows.append({**execution, "consensus_traders": len(distinct), "consensus_window_seconds": window_seconds})
    leader_table = [
        {
            "pair": pair,
            "observations": count,
            "median_lag_seconds": median(lag_values[pair]) if lag_values[pair] else None,
        }
        for pair, count in leader_pairs.most_common()
    ]
    return {
        "window_seconds": window_seconds,
        "consensus_signals": len(consensus_rows),
        "consensus_result": _execution_summary(consensus_rows),
        "leader_follower_pairs": leader_table,
        "warning": "Exploratory. Shared assets can reflect common public information rather than copying between traders.",
    }


def bankroll_simulation(rows: Sequence[dict[str, Any]], bankrolls: Sequence[float]) -> list[dict[str, Any]]:
    ordered = sorted(
        [row for row in rows if int(row.get("delay_seconds") or 0) == PRIMARY_DELAY_SECONDS],
        key=lambda row: (int(row.get("signal_timestamp") or 0), str(row.get("signal_id") or "")),
    )
    results: list[dict[str, Any]] = []
    for initial in bankrolls:
        cash = float(initial)
        realized_equity = float(initial)
        peak_equity = float(initial)
        max_drawdown = 0.0
        open_positions: list[tuple[int, float]] = []
        executed = missed = partial = 0
        pnl_total = 0.0
        for row in ordered:
            timestamp = int(row.get("signal_timestamp") or 0)
            remaining_positions = []
            for release_time, payout in open_positions:
                if release_time <= timestamp:
                    cash += payout
                else:
                    remaining_positions.append((release_time, payout))
            open_positions = remaining_positions
            desired_cost = number(row.get("executed_stake_usd")) + number(row.get("fee_usd"))
            if desired_cost <= 0:
                missed += 1
                continue
            scale = min(1.0, cash / desired_cost) if desired_cost else 0.0
            if scale <= 1e-12:
                missed += 1
                continue
            if scale < 1.0:
                partial += 1
            cash -= desired_cost * scale
            payout = number(row.get("payout_usd")) * scale
            release = int(row.get("resolution_timestamp") or timestamp)
            open_positions.append((release, payout))
            pnl = number(row.get("net_pnl_usd")) * scale
            pnl_total += pnl
            realized_equity += pnl
            peak_equity = max(peak_equity, realized_equity)
            max_drawdown = min(max_drawdown, realized_equity - peak_equity)
            executed += 1
        cash += sum(payout for _, payout in open_positions)
        results.append(
            {
                "initial_bankroll_usd": initial,
                "ending_bankroll_usd": initial + pnl_total,
                "net_pnl_usd": pnl_total,
                "return_on_bankroll": pnl_total / initial if initial else None,
                "max_drawdown_usd": max_drawdown,
                "executed_signals": executed,
                "missed_or_unavailable_signals": missed,
                "partially_scaled_signals": partial,
                "model": "fixed $100 requested per signal, 15s delay, +1c impact, 25% print participation; cash blocked until resolution; partial down-sizing allowed",
            }
        )
    return results


def _flat_strategy_rows(matrix: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in matrix:
        flat: dict[str, Any] = {
            "candidate_type": row["candidate_type"],
            "candidate": row["candidate"],
            "sample_gate_passed": row["sample_gate_passed"],
            "qualified_before_test": row["qualified_before_test"],
            "test_confirmed": row["test_confirmed"],
        }
        for split in ("train", "validation", "test", "all"):
            for key, value in row[split].items():
                flat[f"{split}_{key}"] = value
        result.append(flat)
    return result


def _fmt_money(value: Any) -> str:
    return "N/A" if value is None else f"${number(value):,.2f}"


def _fmt_pct(value: Any) -> str:
    return "N/A" if value is None else f"{100 * number(value):.2f}%"


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    coverage = report["data_coverage"]
    traders = report["trader_ranking"]
    best = traders[0]
    copy = report["copy_trading"]
    comparison = copy["comparison"]
    flaznorp_copy = [row for row in comparison if row["trader_key"] == "flaznorp"]
    best_sport = report["rankings"]["sports"][0] if report["rankings"]["sports"] else {}
    best_bet = report["rankings"]["bet_types"][0] if report["rankings"]["bet_types"] else {}
    risk_bets = [row for row in report["rankings"]["bet_types"] if int(row["n"]) >= 100 and number(row.get("profit_factor")) > 1]
    risk_bets.sort(key=lambda row: (-number(row.get("profit_factor")), -number(row.get("roi_on_cost"))))
    best_risk_bet = risk_bets[0] if risk_bets else {}
    worst_bet = min(report["rankings"]["bet_types"], key=lambda row: number(row["net_pnl_usd"])) if report["rankings"]["bet_types"] else {}
    best_price = next((row for row in report["rankings"]["price_buckets"] if int(row["n"]) >= 100), {})
    efficient_sports = [row for row in report["rankings"]["sports"] if int(row["n"]) >= 100 and number(row.get("roi_on_cost")) > 0]
    efficient_sports.sort(key=lambda row: (-number(row.get("roi_on_cost")), -int(row["n"])))
    most_efficient_sport = efficient_sports[0] if efficient_sports else {}
    consistent_sports = [row for row in report["rankings"]["sports"] if int(row["n"]) >= 100 and number(row.get("profit_factor")) > 1]
    consistent_sports.sort(key=lambda row: (-number(row.get("profit_factor")), -int(row["n"])))
    most_consistent_sport = consistent_sports[0] if consistent_sports else {}
    timing_rows = {row["timing_style"]: row for row in report["rankings"]["timing"]}
    side_rows = {row["outcome_role"]: row for row in report["rankings"]["outcome_sides"]}
    best_behavior = report["rankings"]["behavior"][0] if report["rankings"]["behavior"] else {}
    archetype = report["exploratory_strategy_matrix"].get("selected_before_opening_test")
    lines = [
        "# POLYMARKET SPORTS — FINAL REPORT",
        "",
        f"Corte congelado: **{CUTOFF_UTC}**. Investigación exclusivamente deportiva; esports y categorías no deportivas están excluidos.",
        "",
        "## DATA COVERAGE",
        "",
        f"- Traders investigated: {coverage['traders_investigated']}",
        f"- Sports trades recovered: {coverage['sports_trades_recovered']:,}",
        f"- Sports positions reconstructed: {coverage['sports_positions_reconstructed']:,}",
        f"- Closed position rows with official PnL: {coverage['closed_position_rows']:,}",
        f"- Open-market position rows at capture: {coverage['active']['open_market_rows_at_capture']:,}",
        f"- API activity records inspected: {coverage['api_activity_records_inspected']:,}",
        f"- Trade records excluded as esports: {coverage['esports_trade_rows_excluded']:,}",
        f"- Trade records excluded as non-sports: {coverage['non_sports_trade_rows_excluded']:,}",
        f"- Missing sports trade metadata: {coverage['missing_sports_trade_metadata']}",
        f"- Completeness confidence: **{coverage['completeness_confidence_score']}/100**",
        "",
        "La actividad fue paginada hasta los límites documentados y la cinta de copia terminó con cero señales faltantes o incompletas. La confianza no es 100 porque parte de la primera captura fue recuperada y verificada por hash sin conservar el detalle original de cada página, y porque la API pública no revela órdenes no llenadas, cola ni libro histórico completo.",
        "",
        "## AUDITORÍA DE CALIDAD",
        "",
        f"- Claves de posición únicas: {report['quality_assurance']['position_keys_unique']} ({report['quality_assurance']['unique_position_keys']:,}/{report['quality_assurance']['position_rows']:,}).",
        f"- PnL posición ↔ mercado reconciliado: {report['quality_assurance']['pnl_reconciled']}; diferencia {_fmt_money(report['quality_assurance']['pnl_reconciliation_difference_usd'])}.",
        f"- Coste posición ↔ mercado reconciliado: {report['quality_assurance']['cost_reconciled']}; diferencia {_fmt_money(report['quality_assurance']['cost_reconciliation_difference_usd'])}.",
        f"- Oportunidades de copia esperadas/observadas: {report['quality_assurance']['expected_copy_opportunity_rows']:,}/{report['quality_assurance']['actual_copy_opportunity_rows']:,}.",
        f"- Señales faltantes/incompletas: {report['quality_assurance']['copy_missing_signals']}/{report['quality_assurance']['copy_incomplete_signals']}.",
        "",
        "## TRADER RANKING",
        "",
    ]
    lines += _markdown_table(
        ["Rank", "Trader", "PnL", "ROI", "PF", "Max DD", "Markets", "Peak capital proxy", "Copyability"],
        [
            (
                index,
                row["trader_key"],
                _fmt_money(row["net_pnl_usd"]),
                _fmt_pct(row["roi_on_cost"]),
                f"{number(row['profit_factor']):.3f}",
                _fmt_money(row["max_drawdown_usd"]),
                row["n"],
                _fmt_money(row["capital"]["peak_concurrent_capital_proxy_usd"]),
                f"{number(row['copyability_score']):.1f}/100",
            )
            for index, row in enumerate(traders, 1)
        ],
    )
    lines += [
        "",
        "## BEST SPORTS TRADER",
        "",
        f"Trader: **{best['trader_key']}**  ",
        f"Sports PnL: {_fmt_money(best['net_pnl_usd'])}  ",
        f"ROI: {_fmt_pct(best['roi_on_cost'])}  ",
        f"Peak capital proxy: {_fmt_money(best['capital']['peak_concurrent_capital_proxy_usd'])}  ",
        f"Return on peak capital proxy: {_fmt_pct(best['capital']['pnl_over_peak_capital'])}  ",
        f"Win rate: {_fmt_pct(best['win_rate'])}  ",
        f"Profit factor: {number(best['profit_factor']):.3f}  ",
        f"Max drawdown: {_fmt_money(best['max_drawdown_usd'])}  ",
        f"Resolved markets: {best['n']}  ",
        f"Copyability: {number(best['copyability_score']):.1f}/100",
        "",
        "El ranking del trader original no equivale al ranking del copiador. Trader B combina PnL positivo, muestra superior a 100 mercados, mayoría de semanas positivas y buen win rate, pero su copia realista falla train y test.",
        "",
        "## INFORMES INDIVIDUALES",
        "",
    ]
    for row in traders:
        ci = row["bootstrap"]["roi_ci95"]
        lines += [
            f"### {row['trader_key']}",
            "",
            f"- Rentable en Deportes: **{'Sí' if number(row['net_pnl_usd']) > 0 else 'No'}**; PnL {_fmt_money(row['net_pnl_usd'])}, ROI {_fmt_pct(row['roi_on_cost'])}, PF {number(row['profit_factor']):.3f}.",
            f"- Consistencia: {row['positive_week_fraction']:.1%} de semanas positivas; drawdown máximo {_fmt_money(row['max_drawdown_usd'])}.",
            f"- Muestra: {row['n']} mercados resueltos; IC bootstrap 95% del ROI [{_fmt_pct(ci[0])}, {_fmt_pct(ci[1])}].",
            f"- Capital: pico concurrente proxy {_fmt_money(row['capital']['peak_concurrent_capital_proxy_usd'])}; retorno sobre pico {_fmt_pct(row['capital']['pnl_over_peak_capital'])}.",
            f"- Robustez: PnL sin top 1 winner {_fmt_money(row['remove_top_winners'].get('remove_top_1'))}; con una pérdida extrema adicional {_fmt_money(row['tail_shocks']['one_extra_worst_loss'])}.",
            f"- ¿Copiable rentablemente?: **No confirmado**. Copyability operacional {number(row['copyability_score']):.1f}/100; dependencia 9/10.",
            "",
        ]
    lines += [
        "## MARKET AND BET RANKINGS",
        "",
        f"- Most profitable sport code: **{best_sport.get('sport_code', 'N/A')}**, PnL {_fmt_money(best_sport.get('net_pnl_usd'))}, N={best_sport.get('n', 0)}.",
        f"- Most capital-efficient sport with N>=100: **{most_efficient_sport.get('sport_code', 'N/A')}**, ROI {_fmt_pct(most_efficient_sport.get('roi_on_cost'))}, PF {number(most_efficient_sport.get('profit_factor')):.3f}.",
        f"- Most consistent sport proxy with N>=100: **{most_consistent_sport.get('sport_code', 'N/A')}**, PF {number(most_consistent_sport.get('profit_factor')):.3f}, N={most_consistent_sport.get('n', 0)}.",
        "- Most copyable sport: **not established**; the copy test is selected by trader and post-hoc sport filters require a fresh preregistered sample.",
        f"- Most profitable bet type: **{best_bet.get('bet_family', 'N/A')}**, PnL {_fmt_money(best_bet.get('net_pnl_usd'))}, ROI {_fmt_pct(best_bet.get('roi_on_cost'))}, N={best_bet.get('n', 0)}.",
        f"- Best risk-adjusted bet type with N>=100: **{best_risk_bet.get('bet_family', 'NONE')}**, PF {number(best_risk_bet.get('profit_factor')):.3f}, ROI {_fmt_pct(best_risk_bet.get('roi_on_cost'))}.",
        f"- Worst bet type: **{worst_bet.get('bet_family', 'N/A')}**, PnL {_fmt_money(worst_bet.get('net_pnl_usd'))}.",
        f"- Highest-PnL reported price bucket with N>=100: **{best_price.get('price_bucket', 'N/A')}**, PnL {_fmt_money(best_price.get('net_pnl_usd'))}, ROI {_fmt_pct(best_price.get('roi_on_cost'))}.",
        "",
        "Detailed sport, league, bet-type, price, timing, side and behavioral rankings are in the CSV appendices. These are descriptive, not automatically deployable signals; correlated markets from the same event remain a source of dependence.",
        "",
        "## PRE-MATCH VS LIVE",
        "",
        f"- PREGAME_ONLY: PnL {_fmt_money(timing_rows.get('PREGAME_ONLY', {}).get('net_pnl_usd'))}, ROI {_fmt_pct(timing_rows.get('PREGAME_ONLY', {}).get('roi_on_cost'))}, N={timing_rows.get('PREGAME_ONLY', {}).get('n', 0)}.",
        f"- LIVE_ONLY: PnL {_fmt_money(timing_rows.get('LIVE_ONLY', {}).get('net_pnl_usd'))}, ROI {_fmt_pct(timing_rows.get('LIVE_ONLY', {}).get('roi_on_cost'))}, N={timing_rows.get('LIVE_ONLY', {}).get('n', 0)}.",
        "- Winner descriptivo: **pre-partido**. Los mercados exclusivamente live fueron negativos en agregado; esto no prueba por sí solo un filtro copiable.",
        "",
        "## YES VS NO",
        "",
        f"- YES: PnL {_fmt_money(side_rows.get('YES', {}).get('net_pnl_usd'))}, ROI {_fmt_pct(side_rows.get('YES', {}).get('roi_on_cost'))}, N={side_rows.get('YES', {}).get('n', 0)}.",
        f"- NO: PnL {_fmt_money(side_rows.get('NO', {}).get('net_pnl_usd'))}, ROI {_fmt_pct(side_rows.get('NO', {}).get('roi_on_cost'))}, N={side_rows.get('NO', {}).get('n', 0)}.",
        f"- Named outcomes: PnL {_fmt_money(side_rows.get('NAMED_OUTCOME', {}).get('net_pnl_usd'))}, ROI {_fmt_pct(side_rows.get('NAMED_OUTCOME', {}).get('roi_on_cost'))}, N={side_rows.get('NAMED_OUTCOME', {}).get('n', 0)}.",
        "- Conclusión: no aparece un edge general de NO; tanto YES como NO fueron negativos en agregado.",
        "",
        "## BETTING VS TRADING",
        "",
        f"La mayor contribución descriptiva procede de **{best_behavior.get('behavioral_style', 'N/A')}**, con PnL {_fmt_money(best_behavior.get('net_pnl_usd'))}. Las etiquetas distinguen hold, round-trip e inventario en ambos lados, pero no permiten afirmar market making o arbitraje sin órdenes y cola históricas.",
        "",
        "## COPY TRADING RESULTS",
        "",
        "The mandatory table below uses the preregistered finalist, Flaznorp. Zero seconds is an unattainable ceiling at the leader VWAP with no added impact. Other delays use +1 cent adverse impact and at most 25% of the next public print.",
        "",
    ]
    lines += _markdown_table(
        ["Scenario", "PnL", "ROI", "PF", "Max DD", "Fill rate", "Capacity limited"],
        [
            (
                row["scenario"],
                _fmt_money(row["copy_net_pnl_usd"]),
                _fmt_pct(row["copy_roi"]),
                f"{number(row['copy_profit_factor']):.3f}",
                _fmt_money(row["copy_max_drawdown_usd"]),
                _fmt_pct(row["fill_rate"]),
                _fmt_pct(row["capacity_limited_fraction"]),
            )
            for row in flaznorp_copy
        ],
    )
    prereg = copy["preregistered_selection"]
    lines += [
        "",
        f"Preregistered winner before test: **{prereg.get('winner')}**. Qualified on train and validation: {', '.join(prereg.get('qualified_before_test') or []) or 'none'}. Test confirmed: **{prereg.get('test_confirmed')}**. Deployment status: **{prereg.get('deployment_status')}**.",
        "",
        "Flaznorp made +$200.45 in train and +$142.89 in validation under the primary copy scenario, then lost $260.35 in test (ROI -5.87%, PF 0.813). Kulijan and nigiri99 were positive in test but had already failed train/validation; Trader B failed train and test. Reading those test gains backward would be data snooping.",
        "",
        "## BEST COPYABLE TRADER",
        "",
        "**None confirmed.** Flaznorp is the only valid pre-test finalist and was rejected by the untouched test. Operational copyability scores do not override this economic gate.",
        "",
        "## BEST COPYABLE STRATEGY",
        "",
        "**No deployable strategy.** The exploratory strategy-filter matrix is retained only to formulate a future preregistration. Any apparent survivor was searched after the original capture and requires a new forward sample.",
        "",
        "## BEST BET ARCHETYPE",
        "",
    ]
    if archetype:
        lines += [
            f"Exploratory archetype: **{archetype['candidate']}** ({archetype['candidate_type']}).",
            "",
            f"- Train: N={archetype['train']['fills']}, PnL {_fmt_money(archetype['train']['net_pnl_usd'])}, ROI {_fmt_pct(archetype['train']['roi'])}, PF {number(archetype['train']['profit_factor']):.3f}.",
            f"- Validation: N={archetype['validation']['fills']}, PnL {_fmt_money(archetype['validation']['net_pnl_usd'])}, ROI {_fmt_pct(archetype['validation']['roi'])}, PF {number(archetype['validation']['profit_factor']):.3f}.",
            f"- Test: N={archetype['test']['fills']}, PnL {_fmt_money(archetype['test']['net_pnl_usd'])}, ROI {_fmt_pct(archetype['test']['roi'])}, PF {number(archetype['test']['profit_factor']):.3f}.",
            "",
            "El arquetipo fue seleccionado sin usar test dentro de una familia exploratoria, pero **falló test** y además la familia fue definida después de la captura. No se promueve como edge.",
            "",
        ]
    else:
        lines += ["No hubo arquetipo que pasara train y validation dentro de la matriz exploratoria.", ""]
    lines += [
        "## CAPITAL",
        "",
        "- Minimum practical real-money capital: **not established; no strategy passed**.",
        "- Recommended next capital: **$0 real; paper/shadow only**.",
        "- Existing bankroll simulations use fixed $100 requested per signal and explicitly block capital until resolution.",
        "- Capacity is the dominant constraint: in the primary Flaznorp test, 96.77% of fills were smaller than the requested $100 under the 25% public-print rule.",
        "- Capital efficiency metrics for original traders are upper-bound proxies because public data cannot reconstruct intraposition capital release exactly.",
        "",
        "## TIME",
        "",
        f"Signals in copy universe: {report['counts']['copy_signals']:,}. Monitoring must be continuous and event-driven. A 15-second delay produced low fill rates for the high-frequency wallets, while live-market interpretation is limited by second-level public timestamps. Time Efficiency Score: **{report['scorecard']['Time Efficiency']}/100**.",
        "",
        "## RISK",
        "",
        "- Biggest risk: apparent trader profitability does not survive realistic copy execution out of sample.",
        "- Tail risk: several profiles combine many small wins with very large single-market losses; top-winner removal and extra-worst-loss scenarios are included per trader.",
        "- Trader dependency: 9/10 for a pure copy design.",
        "- Liquidity risk: next-public-print capacity is frequently below requested size.",
        "- Latency risk: high; the tape proves prints, not executable historical depth.",
        "- Overfitting risk: high for post-hoc sport/league/bet filters.",
        "",
        "## SPORTS BOT",
        "",
        "### SHOULD WE BUILD IT?",
        "",
        "**MORE DATA REQUIRED. Do not build an execution bot yet.** A receive-only shadow monitor is justified; a trading bot is not.",
        "",
        "### BOT TYPE",
        "",
        "Research-only hybrid monitor: wallet detector + sports classifier + theoretical execution recorder.",
        "",
        "### SIGNAL",
        "",
        "First public BUY by a preregistered trader in a resolved sports-market universe, timestamped at detection; no signal may use later outcomes.",
        "",
        "### FILTER",
        "",
        "For the next forward experiment only: require sports metadata, a same-token public print after detection, non-ambiguous first-second asset, and the frozen participation/cost model. No economic filters should be added until preregistered from train/validation evidence.",
        "",
        "### POSITION SIZE",
        "",
        "Paper-only. Record fixed requested stakes and cap theoretical fill at 25% of the observed print. No real allocation is justified.",
        "",
        "### EXIT",
        "",
        "The current copy test holds to binary resolution. Public data do not yet support a faithful reconstruction of copied exits or queue priority.",
        "",
        "### NO-TRADE CONDITIONS",
        "",
        "All real trades are blocked. For shadow records, reject missing metadata, ambiguous first-second assets, no print within the frozen 60-second window, unresolved payouts, or incomplete tape evidence.",
        "",
        "## WINNING STRATEGY",
        "",
        "Name: **NO CONFIRMED SPORTS WINNER**  ",
        "Logic: The only preregistered finalist was fixed-size Flaznorp copy at +15s, +1c impact, 25% participation.  ",
        "Evidence: Positive train and validation.  ",
        "Out-of-sample result: Rejected; test PnL -$260.35, ROI -5.87%, PF 0.813.  ",
        "Capital required: Not established.  ",
        "Confidence that it should not be deployed: 90/100.",
        "",
        "## SPORTS SCORECARD FOR FUTURE CROSS-MARKET COMPARISON",
        "",
    ]
    for key, value in report["scorecard"].items():
        lines.append(f"- {key}: {value}/100")
    lines += [
        "",
        "## FINAL VERDICT",
        "",
        "### ¿HAY EDGE EN ESTOS TRADERS DEPORTIVOS?",
        "",
        "**Sí en el PnL histórico de algunos traders originales; inconcluso como edge replicable.**",
        "",
        "### ¿ES RENTABLE COPIARLOS?",
        "",
        "**No bajo el protocolo realista probado.** Ningún trader pasó train, validation y test en secuencia.",
        "",
        "### ¿A QUIÉN COPIARÍAMOS?",
        "",
        "A nadie con dinero real. Flaznorp sería el único candidato legítimo para una nueva observación shadow porque fue seleccionado antes de abrir test, pero su test fue negativo.",
        "",
        "### ¿QUÉ OPERACIONES COPIARÍAMOS?",
        "",
        "Solo señales teóricas dentro de un nuevo protocolo shadow congelado; no existe todavía un filtro económico autorizado.",
        "",
        "### ¿QUÉ OPERACIONES NO COPIARÍAMOS?",
        "",
        "Señales sin metadatos deportivos, ambiguas, sin print posterior, con evidencia incompleta o que requieran asumir profundidad/cola inexistente.",
        "",
        "### ¿CUÁNTO CAPITAL NECESITARÍAMOS?",
        "",
        "No determinado para real money. Próxima fase: $0 real y simulaciones paper con bankrolls de $100 a $10.000.",
        "",
        "### ¿CUÁNTO RETORNO HISTÓRICO HABRÍA PRODUCIDO?",
        "",
        "Depende del trader y delay; la tabla completa está en `copy_delay_comparison.csv`. El candidato principal perdió 5.87% sobre coste ejecutado en test.",
        "",
        "### ¿QUÉ DRAWDOWN HABRÍAMOS SOPORTADO?",
        "",
        "Para Flaznorp en el test principal: $383.40 de drawdown sobre $4.44k de coste ejecutado más fees.",
        "",
        "### ¿QUÉ DELAY MÁXIMO PUEDE SOPORTAR?",
        "",
        "No existe un delay rentable y validado. No debe definirse uno usando el test.",
        "",
        "### ¿CUÁL ES LA ESTRATEGIA DEPORTIVA MÁS FACTIBLE PARA AUTOMATIZAR?",
        "",
        "Un monitor shadow de primeras compras deportivas, no un ejecutor.",
        "",
        "### ¿CREARÍAS EL BOT?",
        "",
        "**No como bot de dinero real. Sí como monitor shadow para generar una muestra forward nueva.**",
        "",
        "### CONFIANZA EN LA CONCLUSIÓN",
        "",
        "**90/100** para rechazar despliegue actual; menor confianza sobre si una futura estrategia filtrada podría existir.",
        "",
        "## LO QUE APRENDIMOS DEL MERCADO DE DEPORTES",
        "",
        "1. **Observación:** el mejor trader original no fue el mejor copiador. **Evidencia:** Trader B tuvo 7/7 gates originales, pero su copia principal perdió en train y test. **Importancia:** separar siempre trader edge de execution edge. **Cambio al prompt:** exigir selección del trader antes de capturar la cinta.",
        "2. **Observación:** la capacidad del siguiente print es mucho menor que el tamaño solicitado en la mayoría de señales. **Evidencia:** más de 90% de los fills principales de varias wallets quedaron limitados por el 25% del print. **Importancia:** el precio sin tamaño no demuestra copiabilidad. **Cambio al prompt:** congelar participación y ventana de fill antes del backtest.",
        "3. **Observación:** los resultados por test pueden parecer atractivos para traders que fallaron antes. **Evidencia:** Kulijan y nigiri99 fueron positivos en test después de fallar train/validation. **Importancia:** evita rescatar estrategias con información futura. **Cambio al prompt:** obligar a publicar el candidato pre-test y prohibir sustituciones posteriores.",
        "4. **Observación:** la API pública permite reconstruir fills y PnL cerrado, pero no cola, órdenes fallidas ni profundidad histórica. **Importancia:** el siguiente print sigue siendo un proxy. **Cambio al prompt:** diferenciar siempre `observable`, `reconstructed` y `unavailable` para cada métrica.",
        "",
        "## LIMITATIONS",
        "",
    ]
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def build_final_report(output_root: Path) -> dict[str, Any]:
    analysis_dir = output_root / "analysis"
    copy_dir = output_root / "copy"
    final_dir = output_root / "final"
    catalog = {str(row["condition_id"]).lower(): row for row in read_jsonl(output_root / "derived" / "market_catalog.jsonl")}
    lifecycle_rows = _json_rows(analysis_dir / "position_lifecycles.jsonl")
    market_rows = _json_rows(analysis_dir / "market_pnl.jsonl")
    event_rows = _json_rows(analysis_dir / "event_exposure.jsonl")
    master_rows = _json_rows(output_root / "derived" / "master_sports_trades.jsonl")
    grid_rows = _json_rows(copy_dir / "analysis" / "copy_grid_summary.jsonl")
    primary_rows = _json_rows(copy_dir / "analysis" / "primary_executions.jsonl")
    signals = _json_rows(copy_dir / "signals.jsonl")
    analysis_summary = json.loads((analysis_dir / "analysis_summary.json").read_text(encoding="utf-8"))
    copy_summary = json.loads((copy_dir / "analysis" / "copy_summary.json").read_text(encoding="utf-8"))
    coverage_manifest = json.loads((output_root / "audit" / "coverage_manifest.json").read_text(encoding="utf-8"))
    tape_manifest = json.loads((copy_dir / "audit" / "tape_manifest.json").read_text(encoding="utf-8"))

    positions = enrich_positions(lifecycle_rows, catalog, market_rows, event_rows)
    closed_positions = [row for row in positions if row["official_closed_position"]]
    active = current_position_coverage(output_root, catalog)

    sport_rank = grouped_rankings(market_rows, "sport_code", pnl_field="official_realized_pnl", cost_field="official_total_bought_cost", time_field="resolution_timestamp")
    league_rank = grouped_rankings(market_rows, "league", pnl_field="official_realized_pnl", cost_field="official_total_bought_cost", time_field="resolution_timestamp")
    bet_rank = grouped_rankings(market_rows, "bet_family", pnl_field="official_realized_pnl", cost_field="official_total_bought_cost", time_field="resolution_timestamp")
    timing_rank = grouped_rankings(market_rows, "timing_style", pnl_field="official_realized_pnl", cost_field="official_total_bought_cost", time_field="resolution_timestamp")
    behavior_rank = grouped_rankings(market_rows, "behavioral_style", pnl_field="official_realized_pnl", cost_field="official_total_bought_cost", time_field="resolution_timestamp")
    price_rank = grouped_rankings(closed_positions, "price_bucket", pnl_field="official_realized_pnl_usd", cost_field="official_cost_usd", time_field="resolution_timestamp")
    side_rank = grouped_rankings(closed_positions, "outcome_role", pnl_field="official_realized_pnl_usd", cost_field="official_cost_usd", time_field="resolution_timestamp")

    timing_fills: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in master_rows:
        timing_fills[timing_bucket(row.get("seconds_to_start"))].append(row)
    timing_fill_profile = [
        {
            "timing_bucket": key,
            "fills": len(rows),
            "notional_usd": sum(number(row.get("notional_usd")) for row in rows),
            "note": "Fill/notional profile only; PnL cannot be attributed to individual fill timing when a market spans buckets.",
        }
        for key, rows in timing_fills.items()
    ]
    timing_fill_profile.sort(key=lambda row: -int(row["fills"]))

    trader_pnl = {key: number(value["pnl"]["total_official_realized_pnl"]) for key, value in analysis_summary["traders"].items()}
    copy_result = copy_analysis(grid_rows, copy_summary, trader_pnl)
    copy_scores = {row["trader_key"]: row for row in copy_result["copyability_scores"]}
    trader_ranking = []
    for trader in (row["key"] for row in TRADERS):
        rows = [row for row in market_rows if row["trader_key"] == trader]
        summary = summarize_rows(rows, pnl_field="official_realized_pnl", cost_field="official_total_bought_cost", time_field="resolution_timestamp")
        trader_ranking.append(
            {
                "trader_key": trader,
                **summary,
                "capital": capital_metrics(rows),
                "bootstrap": bootstrap_roi(rows, seed=20260831 + len(trader)),
                "remove_top_winners": remove_top_winners(rows),
                "tail_shocks": tail_shocks(rows),
                "positive_week_fraction": analysis_summary["traders"][trader]["stability"]["positive_week_fraction"],
                "gates_passed": analysis_summary["traders"][trader]["score"]["passed_gates"],
                "copyability_score": copy_scores.get(trader, {}).get("copyability_score", 0),
                "dependency_risk_0_10": copy_scores.get(trader, {}).get("dependency_risk_0_10", 9),
            }
        )
    trader_ranking.sort(
        key=lambda row: (
            -int(row["gates_passed"]),
            -number(row["positive_week_fraction"]),
            -number(row["profit_factor"]),
            -number(row["net_pnl_usd"]),
        )
    )

    strategy = exploratory_strategy_matrix(primary_rows)
    consensus = consensus_and_leaders(signals, primary_rows)
    bankrolls = []
    for trader in (row["key"] for row in TRADERS):
        rows = [row for row in primary_rows if row["trader_key"] == trader]
        for item in bankroll_simulation(rows, (100, 250, 500, 1000, 2500, 5000, 10000)):
            bankrolls.append({"trader_key": trader, **item})

    posthoc_2500 = posthoc_2500_sensitivity(read_jsonl(copy_dir / "analysis" / "execution_opportunities.jsonl"))
    capacity_rows = [
        row for row in grid_rows
        if row["split"] == "ALL"
        and int(row["delay_seconds"]) == PRIMARY_DELAY_SECONDS
        and abs(number(row["adverse_impact"]) - PRIMARY_ADVERSE_IMPACT) <= 1e-12
    ]
    capacity_rows.extend(
        row for row in posthoc_2500
        if row["split"] == "ALL" and int(row["delay_seconds"]) == PRIMARY_DELAY_SECONDS
    )
    capacity_rows.sort(key=lambda row: (str(row["trader_key"]), number(row["requested_stake_usd"])))

    capture_totals = coverage_manifest["classification"]["totals"]
    coverage = {
        "traders_investigated": len(TRADERS),
        "sports_trades_recovered": capture_totals["sports_trades"],
        "sports_positions_reconstructed": len(positions),
        "closed_position_rows": len(closed_positions),
        "active": active,
        "date_range": {
            "first_trade_utc": min(row["timestamp_utc"] for row in master_rows),
            "last_trade_utc": max(row["timestamp_utc"] for row in master_rows),
        },
        "api_activity_records_inspected": capture_totals["all_activity"],
        "api_requests_this_completed_capture": coverage_manifest["api_request_count_this_run"],
        "esports_trade_rows_excluded": capture_totals["esports_trades"],
        "non_sports_trade_rows_excluded": capture_totals["non_sports_trades"],
        "missing_sports_trade_metadata": capture_totals["metadata_missing_trades"],
        "copy_signals": tape_manifest["signals"],
        "copy_tape_rows": tape_manifest["unique_tape_rows"],
        "copy_missing_signals": len(tape_manifest["missing_signal_ids"]),
        "copy_incomplete_signals": len(tape_manifest["incomplete_signal_ids"]),
        "completeness_confidence_score": 92,
    }

    eligible_market_keys = {(str(row["trader_key"]), str(row["condition_id"]).lower()) for row in market_rows}
    reconciled_positions = [
        row for row in closed_positions
        if (str(row["trader_key"]), str(row["condition_id"]).lower()) in eligible_market_keys
    ]
    position_pnl_total = sum(number(row.get("official_realized_pnl_usd")) for row in reconciled_positions)
    market_pnl_total = sum(number(row.get("official_realized_pnl")) for row in market_rows)
    position_cost_total = sum(number(row.get("official_cost_usd")) for row in reconciled_positions)
    market_cost_total = sum(number(row.get("official_total_bought_cost")) for row in market_rows)
    quality_assurance = {
        "unique_position_keys": len({(row["trader_key"], row["condition_id"], row["asset"]) for row in positions}),
        "position_rows": len(positions),
        "position_keys_unique": len({(row["trader_key"], row["condition_id"], row["asset"]) for row in positions}) == len(positions),
        "closed_position_rows_with_official_pnl": len(closed_positions),
        "closed_position_rows_in_binary_resolved_pnl_scope": len(reconciled_positions),
        "closed_position_rows_outside_binary_resolved_pnl_scope": len(closed_positions) - len(reconciled_positions),
        "reconciliation_scope": "Only trader/condition pairs included in market_pnl: binary payout verified and resolution timestamp at or before cutoff.",
        "official_position_pnl_total_usd": position_pnl_total,
        "official_market_pnl_total_usd": market_pnl_total,
        "pnl_reconciliation_difference_usd": position_pnl_total - market_pnl_total,
        "pnl_reconciled": abs(position_pnl_total - market_pnl_total) <= 1e-6,
        "official_position_cost_total_usd": position_cost_total,
        "official_market_cost_total_usd": market_cost_total,
        "cost_reconciliation_difference_usd": position_cost_total - market_cost_total,
        "cost_reconciled": abs(position_cost_total - market_cost_total) <= 1e-6,
        "expected_copy_opportunity_rows": len(signals) * 8,
        "actual_copy_opportunity_rows": copy_summary["counts"]["execution_opportunities"],
        "copy_opportunity_count_reconciled": len(signals) * 8 == copy_summary["counts"]["execution_opportunities"],
        "copy_missing_signals": len(tape_manifest["missing_signal_ids"]),
        "copy_incomplete_signals": len(tape_manifest["incomplete_signal_ids"]),
        "sports_trade_metadata_missing": capture_totals["metadata_missing_trades"],
        "real_money": "BLOQUEADO",
    }

    scorecard = {
        "Profitability": 35,
        "Capital Efficiency": 30,
        "Time Efficiency": 35,
        "Scalability": 20,
        "Automation Ease": 75,
        "Copyability": round(max((number(row["copyability_score"]) for row in copy_result["copyability_scores"]), default=0)),
        "Liquidity": 25,
        "Risk Adjusted Return": 25,
        "Data Availability": 85,
        "Strategy Robustness": 20,
    }
    scorecard["FINAL SPORTS SCORE"] = round(sum(scorecard.values()) / len(scorecard))

    result = {
        "schema": FINAL_SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "cutoff_inclusive_unix": CUTOFF_INCLUSIVE_UNIX,
        "cutoff_utc": CUTOFF_UTC,
        "data_coverage": coverage,
        "quality_assurance": quality_assurance,
        "counts": {
            "copy_signals": len(signals),
            "copy_opportunities": copy_summary["counts"]["execution_opportunities"],
            "strategy_candidates_evaluated": len(strategy["matrix"]),
        },
        "trader_ranking": trader_ranking,
        "rankings": {
            "sports": sport_rank,
            "leagues": league_rank,
            "bet_types": bet_rank,
            "timing": timing_rank,
            "behavior": behavior_rank,
            "price_buckets": price_rank,
            "outcome_sides": side_rank,
            "timing_fill_profile": timing_fill_profile,
        },
        "copy_trading": copy_result,
        "exploratory_strategy_matrix": {key: value for key, value in strategy.items() if key != "matrix"},
        "consensus_and_leaders": consensus,
        "bankroll_simulations": bankrolls,
        "scorecard": scorecard,
        "final_verdict": {
            "edge_in_original_traders": "YES_HISTORICALLY_BUT_NOT_PROVEN_REPLICABLE",
            "copy_profitable": False,
            "who_to_copy_real_money": None,
            "build_execution_bot": False,
            "build_shadow_monitor": True,
            "confidence_no_current_deployment_0_100": 90,
        },
        "limitations": [
            "Public activity proves fills, not unfilled orders, cancellations, replacement behavior or queue priority.",
            "The next public print is an execution proxy, not historical order-book depth or guaranteed fill.",
            "Official closed-position realizedPnl does not publicly decompose all fees and rebates.",
            "Capital concurrency is an upper-bound proxy because intraposition capital release cannot be reconstructed exactly.",
            "LIVE_OR_AFTER_START does not identify the sports match minute without historical game-state data.",
            "No authentic historical external sportsbook odds were captured, so sportsbook arbitrage and CLV are unavailable.",
            "Post-hoc strategy-filter and consensus analyses are exploratory and require a fresh preregistered forward test.",
            "The $2,500 per-signal sensitivity was requested after the frozen protocol and is labeled post hoc.",
        ],
        "safety": {
            "paper_only": True,
            "orders_enabled": False,
            "wallet_connection_required": False,
            "real_money": "BLOQUEADO",
        },
    }

    write_jsonl(final_dir / "position_appendix.jsonl", positions)
    write_csv(final_dir / "position_appendix.csv", positions)
    write_csv(final_dir / "trader_ranking.csv", trader_ranking)
    write_csv(final_dir / "sport_ranking.csv", sport_rank)
    write_csv(final_dir / "league_ranking.csv", league_rank)
    write_csv(final_dir / "bet_type_ranking.csv", bet_rank)
    write_csv(final_dir / "price_bucket_ranking.csv", price_rank)
    write_csv(final_dir / "timing_ranking.csv", timing_rank)
    write_csv(final_dir / "outcome_side_ranking.csv", side_rank)
    write_csv(final_dir / "timing_fill_profile.csv", timing_fill_profile)
    write_csv(final_dir / "copy_delay_comparison.csv", copy_result["comparison"])
    write_csv(final_dir / "copyability_scores.csv", copy_result["copyability_scores"])
    write_csv(final_dir / "strategy_matrix.csv", _flat_strategy_rows(strategy["matrix"]))
    write_csv(final_dir / "bankroll_simulations.csv", bankrolls)
    write_csv(final_dir / "capacity_sensitivity.csv", capacity_rows)
    write_csv(final_dir / "posthoc_2500_sensitivity.csv", posthoc_2500)
    write_json(final_dir / "sports_final_report.json", result)
    (final_dir / "POLYMARKET_SPORTS_FINAL_REPORT.md").write_text(render_markdown(result), encoding="utf-8")
    manifest_path = final_dir / "final_manifest.json"
    manifest_files = []
    for path in sorted(final_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.name == manifest_path.name or path.suffix == ".partial":
            continue
        manifest_files.append(
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    write_json(
        manifest_path,
        {
            "schema": "polymarket_sports_final_manifest_v001",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "files": manifest_files,
            "quality_assurance": quality_assurance,
        },
    )
    return result


__all__ = [
    "FINAL_SCHEMA",
    "bootstrap_roi",
    "build_final_report",
    "capital_metrics",
    "grouped_rankings",
    "price_bucket",
    "summarize_rows",
    "timing_bucket",
]
