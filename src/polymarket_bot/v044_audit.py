from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v031_capture import open_read_only
from polymarket_bot.v040_transport_guard_design import evaluate_transport_guard_probe
from polymarket_bot.v044_contract import (
    SOURCE_FILES,
    VARIANT,
    load_and_verify_preregistration,
)


RESULT_SCHEMA = "result_v044_short_horizon_repricing_rejection_1"


class V044AuditError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _levels(value: Any) -> list[list[float]]:
    try:
        raw = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    result: list[list[float]] = []
    if not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, list) or len(item) != 2:
            return []
        try:
            price, size = float(item[0]), float(item[1])
        except (TypeError, ValueError):
            return []
        if not (math.isfinite(price) and math.isfinite(size)):
            return []
        if not (0.0 < price < 1.0 and size > 0.0):
            return []
        result.append([price, size])
    return result


def depth_vwap(levels: Sequence[Sequence[float]], shares: float) -> float | None:
    remaining = float(shares)
    cash = 0.0
    for item in levels:
        price, size = float(item[0]), float(item[1])
        take = min(remaining, size)
        cash += take * price
        remaining -= take
        if remaining <= 1e-9:
            return cash / float(shares)
    return None


def _buy_cost(observed_vwap: float, *, fee_rate: float, slippage: float) -> dict[str, float]:
    fill = min(0.999, max(0.001, float(observed_vwap) + float(slippage)))
    fee = float(fee_rate) * fill * (1.0 - fill)
    return {"observed_vwap": observed_vwap, "fill_price": fill, "fee": fee, "cash": fill + fee}


def _sell_proceeds(observed_vwap: float, *, fee_rate: float, slippage: float) -> dict[str, float]:
    fill = min(0.999, max(0.001, float(observed_vwap) - float(slippage)))
    fee = float(fee_rate) * fill * (1.0 - fill)
    return {"observed_vwap": observed_vwap, "fill_price": fill, "fee": fee, "cash": fill - fee}


def _metrics(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pnls = [float(trade["net_pnl_at_shares"]) for trade in trades]
    gross = sum(float(trade["gross_pnl_at_shares"]) for trade in trades)
    positive = sum(value for value in pnls if value > 0.0)
    negative = -sum(value for value in pnls if value < 0.0)
    running = 0.0
    peak = 0.0
    maximum_drawdown = 0.0
    for value in pnls:
        running += value
        peak = max(peak, running)
        maximum_drawdown = max(maximum_drawdown, peak - running)
    if not pnls:
        return {
            "trades": 0,
            "wins": 0,
            "win_rate": None,
            "gross_pnl_at_5_shares": 0.0,
            "net_pnl_at_5_shares": 0.0,
            "modeled_friction_at_5_shares": 0.0,
            "mean_net_pnl_at_5_shares": None,
            "profit_factor": None,
            "one_sided_95_lcb_mean_at_5_shares": None,
            "net_without_best_trade_at_5_shares": 0.0,
            "maximum_drawdown_at_5_shares": 0.0,
            "trapped_positions": 0,
        }
    mean = statistics.fmean(pnls)
    deviation = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    lcb = mean - 1.6448536269514722 * deviation / math.sqrt(len(pnls))
    total = sum(pnls)
    return {
        "trades": len(pnls),
        "wins": sum(value > 0.0 for value in pnls),
        "win_rate": round(sum(value > 0.0 for value in pnls) / len(pnls), 8),
        "gross_pnl_at_5_shares": round(gross, 8),
        "net_pnl_at_5_shares": round(total, 8),
        "modeled_friction_at_5_shares": round(gross - total, 8),
        "mean_net_pnl_at_5_shares": round(mean, 8),
        "profit_factor": None if negative == 0.0 else round(positive / negative, 8),
        "one_sided_95_lcb_mean_at_5_shares": round(lcb, 8),
        "net_without_best_trade_at_5_shares": round(total - max(pnls), 8),
        "maximum_drawdown_at_5_shares": round(maximum_drawdown, 8),
        "trapped_positions": sum(bool(trade.get("trapped")) for trade in trades),
    }


def evaluate_variant(
    markets: Sequence[Mapping[str, Any]],
    snapshots_by_market: Mapping[str, Mapping[int, Mapping[str, Any]]],
    *,
    lookback_seconds: int,
    minimum_move_bps: float,
    contract: Mapping[str, Any],
    exit_contract: Mapping[str, Any],
) -> dict[str, Any]:
    shares = float(contract["position_shares"])
    fee_rate = float(contract["taker_fee_rate"])
    slippage = float(contract["slippage_per_share_each_leg"])
    first, last = (int(value) for value in contract["decision_offsets_inclusive"])
    rejections: Counter[str] = Counter()
    trades: list[dict[str, Any]] = []
    raw_signals = 0
    for market in markets:
        condition_id = str(market["condition_id"])
        snapshots = snapshots_by_market[condition_id]
        enriched = {offset: {**row, "complete_v2": bool(row.get("complete"))} for offset, row in snapshots.items()}
        for offset in range(first, last + 1):
            decision = snapshots.get(offset)
            baseline = snapshots.get(offset - int(lookback_seconds))
            if decision is None or baseline is None:
                rejections["SIGNAL_SNAPSHOT_MISSING"] += 1
                continue
            if not bool(decision.get("complete")) or not bool(baseline.get("complete")):
                rejections["SIGNAL_DATA_INCOMPLETE"] += 1
                continue
            current_reference = decision.get("chainlink_price")
            prior_reference = baseline.get("chainlink_price")
            if current_reference is None or prior_reference is None or float(prior_reference) <= 0.0:
                rejections["CHAINLINK_PRICE_UNAVAILABLE"] += 1
                continue
            move_bps = 10_000.0 * (float(current_reference) / float(prior_reference) - 1.0)
            if abs(move_bps) < float(minimum_move_bps):
                continue
            selected = "up" if move_bps > 0.0 else "down"
            current_bid = decision.get(f"{selected}_best_bid")
            current_ask = decision.get(f"{selected}_best_ask")
            prior_bid = baseline.get(f"{selected}_best_bid")
            prior_ask = baseline.get(f"{selected}_best_ask")
            if any(value is None for value in (current_bid, current_ask, prior_bid, prior_ask)):
                rejections["SELECTED_MID_UNAVAILABLE"] += 1
                continue
            current_mid = (float(current_bid) + float(current_ask)) / 2.0
            prior_mid = (float(prior_bid) + float(prior_ask)) / 2.0
            if current_mid - prior_mid > 1e-12:
                continue
            raw_signals += 1
            probe = evaluate_transport_guard_probe(
                enriched,
                decision_offset=offset,
                outcome=selected.title(),
                contract=exit_contract,
            )
            if probe.get("status") in {"DECISION_REJECTED", "ENTRY_REJECTED"}:
                rejections[str(probe.get("reason") or probe.get("status"))] += 1
                continue
            entry_offset = probe.get("entry_offset")
            if entry_offset is None:
                rejections["ENTRY_OFFSET_UNAVAILABLE"] += 1
                continue
            entry = snapshots.get(int(entry_offset))
            if entry is None:
                rejections["ENTRY_SNAPSHOT_MISSING"] += 1
                continue
            entry_vwap = depth_vwap(_levels(entry.get(f"{selected}_ask_levels_json")), shares)
            if entry_vwap is None:
                rejections["ENTRY_TOP5_DEPTH_UNAVAILABLE"] += 1
                continue
            buy = _buy_cost(entry_vwap, fee_rate=fee_rate, slippage=slippage)
            trapped = probe.get("status") == "TRAPPED"
            exit_offset = probe.get("exit_offset")
            if trapped:
                observed_exit = 0.0
                sell = {"observed_vwap": 0.0, "fill_price": 0.0, "fee": 0.0, "cash": 0.0}
            else:
                exit_snapshot = snapshots.get(int(exit_offset)) if exit_offset is not None else None
                observed_exit = (
                    depth_vwap(_levels(exit_snapshot.get(f"{selected}_bid_levels_json")), shares)
                    if exit_snapshot is not None
                    else None
                )
                if observed_exit is None:
                    rejections["EXIT_TOP5_DEPTH_UNAVAILABLE"] += 1
                    continue
                sell = _sell_proceeds(observed_exit, fee_rate=fee_rate, slippage=slippage)
            trades.append(
                {
                    "condition_id": condition_id,
                    "slug": str(market["slug"]),
                    "decision_offset": offset,
                    "selected_outcome": selected.title(),
                    "chainlink_move_bps": round(move_bps, 8),
                    "selected_mid_change": round(current_mid - prior_mid, 8),
                    "entry_offset": int(entry_offset),
                    "exit_offset": int(exit_offset) if exit_offset is not None else None,
                    "exit_kind": probe.get("exit_kind") if not trapped else "TRAPPED_ZERO_RECOVERY",
                    "trapped": trapped,
                    "gross_pnl_at_shares": shares * (float(observed_exit) - entry_vwap),
                    "net_pnl_at_shares": shares * (float(sell["cash"]) - float(buy["cash"])),
                    "entry": buy,
                    "exit": sell,
                }
            )
            break
    metrics = _metrics(trades)
    return {
        "id": f"lookback_{lookback_seconds}s_move_{minimum_move_bps:g}bps",
        "lookback_seconds": int(lookback_seconds),
        "minimum_move_bps": float(minimum_move_bps),
        "raw_signals_before_execution_gates": raw_signals,
        "rejections": dict(sorted(rejections.items())),
        "metrics": metrics,
        "trades": trades,
    }


def audit_v044(
    *,
    prereg_path: str | Path,
    result_path: str | Path,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    output = Path(result_path).resolve()
    prereg = load_and_verify_preregistration(prereg_file, project_root=root)
    database = root / SOURCE_FILES["v031_database"]
    v031_quality_path = root / SOURCE_FILES["v031_quality_result"]
    v043_result_path = root / SOURCE_FILES["v043_result"]
    v042_prereg_path = root / SOURCE_FILES["v042_preregistration"]
    v031_quality = json.loads(v031_quality_path.read_text(encoding="utf-8"))
    v043_result = json.loads(v043_result_path.read_text(encoding="utf-8"))
    v042_prereg = json.loads(v042_prereg_path.read_text(encoding="utf-8"))
    if v031_quality.get("verdict") != "PASS_TECHNICAL_CAPTURE_ONLY_REAUDITED_SEMANTICS_V2":
        raise V044AuditError("V0.31 no tiene semantica tecnica V2 aprobada")
    if v043_result.get("verdict") != "PASS_BOT_CONTROLLED_CHAINLINK_OUTAGE_SAFETY_ONLY":
        raise V044AuditError("V0.43 no confirma el cierre seguro controlado")
    hash_before = sha256_file(database)
    connection = open_read_only(database)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        query_only = bool(int(connection.execute("PRAGMA query_only").fetchone()[0]))
        markets = [
            dict(row)
            for row in connection.execute(
                "SELECT condition_id,slug,market_start_ms,capture_status FROM v031_markets ORDER BY market_start_ms"
            )
            if row["capture_status"] == "COMPLETED"
        ]
        snapshots_by_market = {
            str(market["condition_id"]): {
                int(row["second_offset"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM v031_snapshots WHERE condition_id=? ORDER BY second_offset",
                    (market["condition_id"],),
                )
            }
            for market in markets
        }
    finally:
        connection.close()
    contract = prereg["economic_contract"]
    variants = [
        evaluate_variant(
            markets,
            snapshots_by_market,
            lookback_seconds=int(lookback),
            minimum_move_bps=float(move),
            contract=contract,
            exit_contract=v042_prereg["probe_contract"],
        )
        for lookback in contract["chainlink_lookbacks_seconds"]
        for move in contract["minimum_absolute_chainlink_moves_bps"]
    ]
    hash_after = sha256_file(database)
    if hash_after != hash_before:
        raise V044AuditError("La base V0.31 cambio durante la auditoria")
    positive = [
        variant
        for variant in variants
        if int(variant["metrics"]["trades"]) > 0
        and float(variant["metrics"]["net_pnl_at_5_shares"]) > 0.0
    ]
    active = [variant for variant in variants if int(variant["metrics"]["trades"]) > 0]
    best = max(
        active,
        key=lambda variant: float(variant["metrics"]["net_pnl_at_5_shares"]),
        default=None,
    )
    decision = (
        "NO_SELECTION_REQUIRE_ONE_FRESH_PREREGISTERED_REPLICATION"
        if positive
        else "CLOSE_SHORT_HORIZON_TAKER_REPRICING_FAMILY"
    )
    payload = {
        "schema": RESULT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "verdict": decision,
        "meaning": "closed_development_family_rejection_not_fresh_validation",
        "source": {
            "database": str(database),
            "database_sha256_before": hash_before,
            "database_sha256_after": hash_after,
            "database_unchanged": True,
            "sqlite_quick_check": quick_check,
            "query_only": query_only,
            "completed_markets": len(markets),
            "source_window_hours": 1.0,
            "v031_quality_result_sha256": sha256_file(v031_quality_path),
            "v043_result_sha256": sha256_file(v043_result_path),
        },
        "contract": contract,
        "family": {
            "variants_tested": len(variants),
            "variants_with_trades": len(active),
            "positive_net_variants": len(positive),
            "all_active_variants_nonpositive": not positive,
            "best_variant_is_diagnostic_not_selected": best["id"] if best else None,
            "best_variant_metrics": best["metrics"] if best else None,
        },
        "variants": variants,
        "decision": {
            "action": decision,
            "fresh_capture_launched": False,
            "posthoc_variant_selected": False,
            "retune_this_family": False,
            "paper_or_money_candidate": False,
            "next_safe_step": "DESIGN_INDEPENDENT_MARKET_UNIVERSE_OR_EXECUTION_HYPOTHESIS",
        },
        "limitations": {
            "fresh_validation": False,
            "source_markets": len(markets),
            "source_hours": 1.0,
            "multi_sensitivity_screen_can_reject_but_not_select": True,
            "v043_safety_is_a_future_execution_requirement_not_retroactive_capture_quality": True,
        },
        "safety": {
            "database_read_only": True,
            "new_capture_hours": 0.0,
            "new_backtest_hours": 0.0,
            "orders_created": 0,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
        "preregistration_sha256": sha256_file(prereg_file),
    }
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        comparable = dict(payload)
        comparable["created_at"] = existing.get("created_at")
        if existing != comparable:
            raise V044AuditError("Ya existe otro resultado V0.44")
        return existing
    _write_atomic(output, payload)
    return payload


__all__ = [
    "RESULT_SCHEMA",
    "V044AuditError",
    "audit_v044",
    "depth_vwap",
    "evaluate_variant",
]
