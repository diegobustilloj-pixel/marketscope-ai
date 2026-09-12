from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .backtest import (
    MarketSeries,
    Trade,
    fill_signal,
    load_series,
    metrics,
    simulate_bankroll,
)
from .contract import MarketContract, Signal
from .research import ROOT, load_verified_contracts, sha256_file, utc_now
from .v002 import CALIBRATION, MIN_EXPECTED_EDGE, PRICE_GRID, LateDecision, LateWindowEdgeEngine


OUTPUT_DIR = ROOT / "data" / "btc5m90_v002"
PROTOCOL_PATH = OUTPUT_DIR / "protocol.json"
RESULT_PATH = OUTPUT_DIR / "development_backtest.json"
REPORT_PATH = OUTPUT_DIR / "REPORT.md"


class V002BacktestError(RuntimeError):
    pass


def first_decision(series: MarketSeries, contract: MarketContract) -> LateDecision | None:
    engine = LateWindowEdgeEngine(
        condition_id=series.condition_id,
        market_start_ms=series.start_ms,
        market_end_ms=series.end_ms,
        fee=contract.fee,
    )
    for observation in series.observations:
        decision = engine.observe(
            timestamp_ms=observation.timestamp_ms,
            up_ask=observation.up_ask,
            down_ask=observation.down_ask,
        )
        if decision is not None:
            return decision
    return None


def decision_trade(
    series: MarketSeries, contract: MarketContract, decision: LateDecision
) -> Trade | None:
    if decision.status != "SIGNAL" or decision.side is None or decision.ask is None:
        return None
    signal = Signal(
        condition_id=decision.condition_id,
        side=decision.side,
        timestamp_ms=decision.timestamp_ms,
        threshold=decision.ask,
        ask=decision.ask,
        status="SIGNAL",
    )
    return fill_signal(series, contract, signal, latency_ms=0, quantity=5.0)


def _protocol() -> dict[str, Any]:
    code_paths = [
        ROOT / "src" / "polymarket_bot" / "btc_5m_90" / "v002.py",
        ROOT / "src" / "polymarket_bot" / "btc_5m_90" / "v002_backtest.py",
        ROOT / "btc5m90_v002.py",
    ]
    return {
        "schema": "btc5m90_v002_protocol_1",
        "status": "FROZEN_FOR_FUTURE_OOS_SHADOW",
        "frozen_at": utc_now(),
        "historical_status": "POST_V001_DEVELOPMENT_NOT_NEW_OOS",
        "strategy": {
            "wait_seconds": 240,
            "decision_window": "[240,300) seconds elapsed",
            "candidate_prices": list(PRICE_GRID),
            "minimum_expected_edge_per_share": MIN_EXPECTED_EDGE,
            "quantity_shares": 5.0,
            "one_first_candidate_lock_per_condition_id": True,
            "rejected_first_candidate_locks_market": True,
            "approved_prices": [price for price, row in CALIBRATION.items() if row.approved],
            "blocked_prices": [price for price, row in CALIBRATION.items() if not row.approved],
            "calibration": {
                f"{price:.2f}": asdict(row) for price, row in CALIBRATION.items()
            },
        },
        "future_oos_gates": {
            "minimum_complete_markets": 96,
            "minimum_signals": 15,
            "positive_net_ev_after_actual_fee": True,
            "minimum_realized_edge_per_share": MIN_EXPECTED_EDGE,
            "minimum_level_a_execution_rate": 0.80,
            "no_live_promotion": True,
        },
        "sources": {
            "v001_protocol_sha256": sha256_file(ROOT / "data" / "btc5m90_v001" / "protocol.json"),
            "v001_labels_sha256": sha256_file(
                ROOT / "data" / "btc5m90_v001" / "raw" / "gamma_markets.json"
            ),
        },
        "safety": {
            "wallet_required": False,
            "orders_enabled": False,
            "paper_only": True,
            "real_money": "BLOQUEADO",
        },
        "code_sha256": {
            str(path.relative_to(ROOT)).replace("\\", "/"): sha256_file(path)
            for path in code_paths
        },
    }


def freeze_protocol() -> dict[str, Any]:
    if PROTOCOL_PATH.exists():
        raise V002BacktestError("El protocolo V002 ya existe y no se sobrescribe")
    payload = _protocol()
    PROTOCOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROTOCOL_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def verify_protocol() -> dict[str, Any]:
    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    if payload.get("schema") != "btc5m90_v002_protocol_1":
        raise V002BacktestError("Protocolo V002 incompatible")
    for relative, expected in payload["code_sha256"].items():
        if sha256_file(ROOT / relative) != expected:
            raise V002BacktestError(f"Código V002 cambió después del freeze: {relative}")
    return payload


def run_development_backtest() -> dict[str, Any]:
    protocol = verify_protocol()
    if RESULT_PATH.exists() or REPORT_PATH.exists():
        raise V002BacktestError("El resultado V002 ya existe y no se sobrescribe")
    contracts = load_verified_contracts()
    series = load_series()
    decisions: list[tuple[MarketSeries, LateDecision]] = []
    trades: list[Trade] = []
    for market in series:
        decision = first_decision(market, contracts[market.condition_id])
        if decision is None:
            continue
        decisions.append((market, decision))
        trade = decision_trade(market, contracts[market.condition_id], decision)
        if trade is not None:
            trades.append(trade)

    split_metrics = {}
    for split in ("TRAIN", "VALIDATION", "TEST", "ALL"):
        split_value = None if split == "ALL" else split
        subset = [trade for trade in trades if split_value is None or trade.split == split_value]
        signal_count = sum(
            decision.status == "SIGNAL"
            and (split_value is None or market.split == split_value)
            for market, decision in decisions
        )
        market_count = sum(split_value is None or market.split == split_value for market in series)
        split_metrics[split] = metrics(
            subset, markets=market_count, signals=signal_count
        )

    candidate_counts = Counter(
        (f"{decision.ask:.2f}" if decision.ask is not None else decision.status)
        + ":"
        + decision.status
        for _, decision in decisions
    )
    stressed: list[Trade] = []
    for market, decision in decisions:
        if decision.status != "SIGNAL" or decision.side is None or decision.ask is None:
            continue
        signal = Signal(
            decision.condition_id,
            decision.side,
            decision.timestamp_ms,
            decision.ask,
            decision.ask,
            "SIGNAL",
        )
        trade = fill_signal(
            market,
            contracts[market.condition_id],
            signal,
            latency_ms=0,
            quantity=5.0,
            adverse_ticks=1,
        )
        if trade is not None:
            stressed.append(trade)

    result = {
        "schema": "btc5m90_v002_development_backtest_1",
        "created_at": utc_now(),
        "protocol_sha256": sha256_file(PROTOCOL_PATH),
        "historical_status": protocol["historical_status"],
        "markets": len(series),
        "first_candidates": len(decisions),
        "accepted_signals": len(trades),
        "rejected_or_expired": len(decisions) - len(trades),
        "candidate_counts": dict(sorted(candidate_counts.items())),
        "metrics": split_metrics,
        "one_tick_stress": metrics(
            stressed, markets=len(series), signals=len(trades)
        ),
        "bankroll_10": {
            f"stake_{stake:g}": simulate_bankroll(trades, 10.0, stake)
            for stake in (1.0, 2.0, 5.0, 10.0)
        },
        "interpretation": (
            "Positive replay is hypothesis generation only; V001 outcomes were already opened. "
            "Only a newly collected forward can be OOS for V002."
        ),
        "safety": protocol["safety"],
    }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    total = split_metrics["ALL"]
    report = (
        "# BTC5M90 V002 — desarrollo histórico\n\n"
        "Espera 240s, evalúa el primer precio exacto entre 0,90–0,95 y bloquea el mercado. "
        "Sólo 0,90/0,91 superan la puerta histórica separada de edge >=0,002; los demás "
        "precios se observan y rechazan.\n\n"
        f"Candidatos: {len(decisions)}; aceptados: {len(trades)}; wins: {total['wins']}; "
        f"losses: {total['losses']}; WR: {100*total['win_rate']:.3f}%; "
        f"PnL a 5 shares: {total['net_pnl']:.4f}; EV/trade: {total['net_ev_per_trade']:.5f}.\n\n"
        "Este replay no es nuevo OOS. Requiere forward nuevo antes de cualquier conclusión. "
        "Dinero real permanece bloqueado.\n"
    )
    REPORT_PATH.write_text(report, encoding="utf-8")
    return result
