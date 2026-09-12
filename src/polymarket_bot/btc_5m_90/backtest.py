from __future__ import annotations

import csv
import json
import math
import random
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence

from .contract import MarketContract, Signal, ThresholdDetector
from .research import (
    OUTPUT_DIR,
    PROTOCOL_PATH,
    ROOT,
    SOURCES,
    SourceSpec,
    load_and_verify_protocol,
    load_universe,
    load_verified_contracts,
    sha256_file,
    utc_now,
)


RESULT_PATH = OUTPUT_DIR / "backtest_result.json"
TRADES_PATH = OUTPUT_DIR / "base_trades.csv"
DATABASE_PATH = OUTPUT_DIR / "backtest.db"
REPORT_PATH = OUTPUT_DIR / "REPORT.md"
RESULT_SCHEMA = "btc5m90_backtest_result_v001"


class BacktestError(RuntimeError):
    pass


@dataclass(frozen=True)
class Observation:
    timestamp_ms: int
    source_timestamp_ms: int
    up_ask: float
    down_ask: float
    up_depth_proxy: float | None
    down_depth_proxy: float | None


@dataclass(frozen=True)
class MarketSeries:
    source: str
    split: str
    condition_id: str
    slug: str
    start_ms: int
    end_ms: int
    sampling_ms: int
    execution_quality: str
    observations: tuple[Observation, ...]


@dataclass(frozen=True)
class Trade:
    source: str
    split: str
    condition_id: str
    slug: str
    side: str
    winner: str
    signal_timestamp_ms: int
    fill_timestamp_ms: int
    seconds_elapsed: float
    seconds_remaining: float
    signal_price: float
    fill_price: float
    latency_ms: int
    quantity: float
    fee: float
    gross_pnl: float
    net_pnl: float
    debit: float
    won: bool
    depth_proxy: float | None
    exact_level_size_known: bool
    closed_at_ms: int


ELAPSED_BUCKETS = (
    (0, 30),
    (31, 60),
    (61, 90),
    (91, 120),
    (121, 150),
    (151, 180),
    (181, 210),
    (211, 240),
    (241, 255),
    (256, 270),
    (271, 280),
    (281, 290),
    (291, 295),
    (296, 300),
)


def _parse_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _connect_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _load_one_source(source: SourceSpec) -> list[MarketSeries]:
    path = ROOT / source.database
    with _connect_ro(path) as connection:
        markets = connection.execute(
            f"SELECT condition_id,slug,market_start_ms,market_end_ms,status "
            f"FROM {source.market_table} ORDER BY market_start_ms,condition_id"
        ).fetchall()
        result: list[MarketSeries] = []
        for market in markets:
            condition_id = str(market["condition_id"])
            if str(market["status"]) != "COMPLETE":
                raise BacktestError(f"Mercado incompleto: {market['slug']}")
            if source.name in {"v018", "v019", "v020"}:
                rows = connection.execute(
                    f"SELECT captured_at,up_best_ask,down_best_ask"
                    + (
                        ",up_ask_depth_1c,down_ask_depth_1c"
                        if source.name == "v020"
                        else ""
                    )
                    + f" FROM {source.sample_table} WHERE condition_id=? "
                    "ORDER BY second_offset",
                    (condition_id,),
                ).fetchall()
                observations = tuple(
                    Observation(
                        timestamp_ms=_parse_ms(str(row["captured_at"])),
                        source_timestamp_ms=_parse_ms(str(row["captured_at"])),
                        up_ask=float(row["up_best_ask"]),
                        down_ask=float(row["down_best_ask"]),
                        up_depth_proxy=(
                            float(row["up_ask_depth_1c"])
                            if source.name == "v020" and row["up_ask_depth_1c"] is not None
                            else None
                        ),
                        down_depth_proxy=(
                            float(row["down_ask_depth_1c"])
                            if source.name == "v020" and row["down_ask_depth_1c"] is not None
                            else None
                        ),
                    )
                    for row in rows
                    if row["up_best_ask"] is not None and row["down_best_ask"] is not None
                )
            else:
                rows = connection.execute(
                    "SELECT source_timestamp_ms,received_timestamp_ms,up_best_ask,down_best_ask,"
                    "up_ask_depth,down_ask_depth FROM v021_samples WHERE condition_id=? "
                    "ORDER BY bucket_ms",
                    (condition_id,),
                ).fetchall()
                observations = tuple(
                    Observation(
                        timestamp_ms=int(row["received_timestamp_ms"]),
                        source_timestamp_ms=int(row["source_timestamp_ms"]),
                        up_ask=float(row["up_best_ask"]),
                        down_ask=float(row["down_best_ask"]),
                        up_depth_proxy=float(row["up_ask_depth"]),
                        down_depth_proxy=float(row["down_ask_depth"]),
                    )
                    for row in rows
                )
            if not observations:
                raise BacktestError(f"Sin observaciones: {market['slug']}")
            result.append(
                MarketSeries(
                    source=source.name,
                    split=source.split,
                    condition_id=condition_id,
                    slug=str(market["slug"]),
                    start_ms=int(market["market_start_ms"]),
                    end_ms=int(market["market_end_ms"]),
                    sampling_ms=source.sampling_ms,
                    execution_quality=source.execution_quality,
                    observations=observations,
                )
            )
    return result


def load_series() -> list[MarketSeries]:
    result: list[MarketSeries] = []
    for source in SOURCES:
        result.extend(_load_one_source(source))
    result.sort(key=lambda item: (item.start_ms, item.condition_id))
    universe = load_universe()
    if [item.condition_id for item in result] != [row["condition_id"] for row in universe]:
        raise BacktestError("Series no coincide con universo congelado")
    return result


def literal_control(series: MarketSeries, threshold: float = 0.90) -> str:
    first = series.observations[0]
    sides = [
        side
        for side, ask in (("Up", first.up_ask), ("Down", first.down_ask))
        if ask <= threshold
    ]
    if len(sides) == 2:
        return "AMBIGUOUS_BOTH_AT_FIRST_OBSERVATION"
    if len(sides) == 1:
        return f"{sides[0].upper()}_AT_FIRST_OBSERVATION"
    return "NO_LITERAL_SIGNAL"


def detect_signal(
    series: MarketSeries, contract: MarketContract, threshold: float
) -> Signal | None:
    detector = ThresholdDetector(threshold=threshold, tick_size=contract.tick_size)
    for observation in series.observations:
        signal = detector.observe(
            series.condition_id,
            observation.timestamp_ms,
            observation.up_ask,
            observation.down_ask,
        )
        if signal is not None:
            return signal
    return None


def _closed_ms(contract: MarketContract) -> int:
    if contract.closed_at:
        return _parse_ms(contract.closed_at)
    return contract.market_end_ms + 120_000


def fill_signal(
    series: MarketSeries,
    contract: MarketContract,
    signal: Signal,
    *,
    latency_ms: int,
    quantity: float = 5.0,
    adverse_ticks: int = 0,
) -> Trade | None:
    if signal.status != "SIGNAL" or signal.side not in {"Up", "Down"}:
        return None
    if latency_ms > 0 and series.sampling_ms > latency_ms:
        return None
    target_ms = signal.timestamp_ms + latency_ms
    observation = next(
        (item for item in series.observations if item.timestamp_ms >= target_ms), None
    )
    if observation is None:
        return None
    observed_ask = observation.up_ask if signal.side == "Up" else observation.down_ask
    maximum = signal.threshold
    if observed_ask > maximum + max(1e-9, contract.tick_size / 2.0):
        return None
    fill_price = min(0.999, observed_ask + adverse_ticks * contract.tick_size)
    depth = (
        observation.up_depth_proxy if signal.side == "Up" else observation.down_depth_proxy
    )
    fee = contract.fee.fee_per_share(fill_price, taker=True) * quantity
    won = signal.side == contract.winner
    gross_pnl = (quantity if won else 0.0) - fill_price * quantity
    net_pnl = gross_pnl - fee
    return Trade(
        source=series.source,
        split=series.split,
        condition_id=series.condition_id,
        slug=series.slug,
        side=signal.side,
        winner=str(contract.winner),
        signal_timestamp_ms=signal.timestamp_ms,
        fill_timestamp_ms=observation.timestamp_ms,
        seconds_elapsed=max(0.0, (signal.timestamp_ms - series.start_ms) / 1000.0),
        seconds_remaining=max(0.0, (series.end_ms - signal.timestamp_ms) / 1000.0),
        signal_price=float(signal.ask),
        fill_price=fill_price,
        latency_ms=latency_ms,
        quantity=quantity,
        fee=fee,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        debit=fill_price * quantity + fee,
        won=won,
        depth_proxy=depth,
        exact_level_size_known=False,
        closed_at_ms=_closed_ms(contract),
    )


def wilson_interval(wins: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = wins / total
    denominator = 1.0 + z * z / total
    centre = p + z * z / (2.0 * total)
    spread = z * math.sqrt(p * (1.0 - p) / total + z * z / (4.0 * total * total))
    return (centre - spread) / denominator, (centre + spread) / denominator


def _max_drawdown(trades: Sequence[Trade]) -> float:
    equity = 0.0
    peak = 0.0
    maximum = 0.0
    for trade in sorted(trades, key=lambda row: row.signal_timestamp_ms):
        equity += trade.net_pnl
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def _max_loss_streak(trades: Sequence[Trade]) -> int:
    maximum = 0
    current = 0
    for trade in sorted(trades, key=lambda row: row.signal_timestamp_ms):
        if trade.won:
            current = 0
        else:
            current += 1
            maximum = max(maximum, current)
    return maximum


def _bootstrap_ev(trades: Sequence[Trade], samples: int = 5000) -> tuple[float | None, float | None]:
    if not trades:
        return None, None
    rng = random.Random(20260907)
    values = [trade.net_pnl / trade.quantity for trade in trades]
    estimates = sorted(
        mean(rng.choices(values, k=len(values))) for _ in range(samples)
    )
    return estimates[int(samples * 0.025)], estimates[min(samples - 1, int(samples * 0.975))]


def metrics(
    trades: Sequence[Trade],
    *,
    markets: int,
    signals: int,
    supported: bool = True,
) -> dict[str, Any]:
    if not supported:
        return {
            "supported": False,
            "reason": "sampling resolution is coarser than requested latency; no interpolation",
            "markets": markets,
            "signals": signals,
        }
    ordered = sorted(trades, key=lambda row: row.signal_timestamp_ms)
    wins = sum(trade.won for trade in ordered)
    losses = len(ordered) - wins
    pnl = sum(trade.net_pnl for trade in ordered)
    gross = sum(trade.gross_pnl for trade in ordered)
    debit = sum(trade.debit for trade in ordered)
    positive = sum(max(0.0, trade.net_pnl) for trade in ordered)
    negative = -sum(min(0.0, trade.net_pnl) for trade in ordered)
    daily: dict[str, float] = defaultdict(float)
    for trade in ordered:
        day = datetime.fromtimestamp(trade.signal_timestamp_ms / 1000, timezone.utc).date().isoformat()
        daily[day] += trade.net_pnl
    low, high = wilson_interval(wins, len(ordered))
    bootstrap_low, bootstrap_high = _bootstrap_ev(ordered)
    best_removed = pnl - (max((trade.net_pnl for trade in ordered), default=0.0))
    return {
        "supported": True,
        "markets": markets,
        "signals": signals,
        "trades": len(ordered),
        "no_trades": markets - len(ordered),
        "execution_rate": len(ordered) / signals if signals else None,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(ordered) if ordered else None,
        "wilson_95_low": low,
        "wilson_95_high": high,
        "break_even_win_rate": (
            sum(trade.debit / trade.quantity for trade in ordered) / len(ordered)
            if ordered
            else None
        ),
        "gross_pnl": gross,
        "net_pnl": pnl,
        "net_ev_per_trade": pnl / len(ordered) if ordered else None,
        "net_ev_per_share": pnl / sum(trade.quantity for trade in ordered) if ordered else None,
        "roi": pnl / debit if debit else None,
        "profit_factor": positive / negative if negative else (math.inf if positive else None),
        "max_drawdown": _max_drawdown(ordered),
        "max_consecutive_losses": _max_loss_streak(ordered),
        "worst_day": min(daily.items(), key=lambda item: item[1]) if daily else None,
        "best_day": max(daily.items(), key=lambda item: item[1]) if daily else None,
        "capital_deployed": debit,
        "bootstrap_ev_per_share_95_low": bootstrap_low,
        "bootstrap_ev_per_share_95_high": bootstrap_high,
        "net_pnl_remove_best_trade": best_removed,
    }


def _filter_trades(trades: Sequence[Trade], split: str | None = None) -> list[Trade]:
    return [trade for trade in trades if split is None or trade.split == split]


def _signal_count(signals: Sequence[tuple[MarketSeries, Signal]], split: str | None) -> int:
    return sum(signal.status == "SIGNAL" and (split is None or series.split == split) for series, signal in signals)


def _market_count(series: Sequence[MarketSeries], split: str | None) -> int:
    return sum(split is None or market.split == split for market in series)


def select_time_window(train_trades: Sequence[Trade]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for left in range(len(ELAPSED_BUCKETS)):
        for right in range(left, len(ELAPSED_BUCKETS)):
            start = ELAPSED_BUCKETS[left][0]
            end = ELAPSED_BUCKETS[right][1]
            selected = [trade for trade in train_trades if start <= trade.seconds_elapsed <= end]
            if len(selected) < 50:
                continue
            result = metrics(selected, markets=len(selected), signals=len(selected))
            if float(result["net_ev_per_share"]) <= 0.0:
                continue
            candidates.append(
                {
                    "elapsed_start": start,
                    "elapsed_end": end,
                    "train_metrics": result,
                }
            )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            row["train_metrics"]["net_ev_per_share"],
            row["train_metrics"]["trades"],
            -(row["elapsed_end"] - row["elapsed_start"]),
        ),
    )


def _time_bucket(seconds_elapsed: float) -> str:
    for start, end in ELAPSED_BUCKETS:
        if start <= seconds_elapsed <= end:
            return f"{start}-{end}"
    return "OUTSIDE"


def simulate_bankroll(trades: Sequence[Trade], starting_balance: float, stake: float) -> dict[str, Any]:
    cash = float(starting_balance)
    open_positions: list[tuple[int, float]] = []
    executed: list[Trade] = []
    skipped_cash = 0
    peak = cash
    max_drawdown = 0.0
    for trade in sorted(trades, key=lambda row: row.signal_timestamp_ms):
        matured = [position for position in open_positions if position[0] <= trade.signal_timestamp_ms]
        cash += sum(payout for _, payout in matured)
        open_positions = [position for position in open_positions if position[0] > trade.signal_timestamp_ms]

        fee_per_share = trade.fee / trade.quantity
        quantity = stake / (trade.fill_price + fee_per_share)
        if quantity < 5.0 or cash + 1e-9 < stake:
            skipped_cash += 1
            continue
        cash -= stake
        payout = quantity if trade.won else 0.0
        open_positions.append((trade.closed_at_ms, payout))
        executed.append(trade)
        marked = cash + sum(payout for _, payout in open_positions)
        peak = max(peak, marked)
        max_drawdown = max(max_drawdown, peak - marked)
    cash += sum(payout for _, payout in open_positions)
    pnl = cash - starting_balance
    return {
        "starting_balance": starting_balance,
        "stake_total_debit": stake,
        "minimum_order_shares": 5.0,
        "technically_feasible_at_090": stake / (0.90 + 0.0063) >= 5.0,
        "trades": len(executed),
        "skipped_insufficient_cash_or_min_size": skipped_cash,
        "wins": sum(trade.won for trade in executed),
        "losses": sum(not trade.won for trade in executed),
        "net_pnl": pnl,
        "final_balance": cash,
        "roi": pnl / starting_balance,
        "max_drawdown": max_drawdown,
        "max_loss_streak": _max_loss_streak(executed),
        "empirical_ruin": cash < 5.0,
    }


def _create_output_database(
    path: Path,
    series: Sequence[MarketSeries],
    signals: Sequence[tuple[MarketSeries, Signal]],
    base_trades: Sequence[Trade],
    result: Mapping[str, Any],
) -> None:
    if path.exists():
        raise BacktestError(f"Resultado ya existe y no se sobrescribe: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE markets(
              condition_id TEXT PRIMARY KEY,source TEXT,split TEXT,slug TEXT,start_ms INTEGER,
              end_ms INTEGER,sampling_ms INTEGER,execution_quality TEXT,observations INTEGER
            );
            CREATE TABLE signals(
              condition_id TEXT PRIMARY KEY,status TEXT,side TEXT,timestamp_ms INTEGER,
              threshold REAL,ask REAL,seconds_elapsed REAL,seconds_remaining REAL
            );
            CREATE TABLE base_trades(
              condition_id TEXT PRIMARY KEY,source TEXT,split TEXT,slug TEXT,side TEXT,winner TEXT,
              signal_timestamp_ms INTEGER,fill_timestamp_ms INTEGER,seconds_elapsed REAL,
              seconds_remaining REAL,signal_price REAL,fill_price REAL,quantity REAL,fee REAL,
              gross_pnl REAL,net_pnl REAL,debit REAL,won INTEGER,depth_proxy REAL,
              exact_level_size_known INTEGER,closed_at_ms INTEGER
            );
            """
        )
        meta = {
            "schema": "btc5m90_backtest_db_v001",
            "created_at": utc_now(),
            "protocol_sha256": sha256_file(PROTOCOL_PATH),
            "real_money": "BLOQUEADO",
            "orders_sent": 0,
            "wallet_required": False,
            "result": result,
        }
        connection.executemany(
            "INSERT INTO meta VALUES(?,?)",
            [(key, json.dumps(value, ensure_ascii=False, sort_keys=True)) for key, value in meta.items()],
        )
        connection.executemany(
            "INSERT INTO markets VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (
                    item.condition_id,
                    item.source,
                    item.split,
                    item.slug,
                    item.start_ms,
                    item.end_ms,
                    item.sampling_ms,
                    item.execution_quality,
                    len(item.observations),
                )
                for item in series
            ],
        )
        by_condition = {item.condition_id: item for item in series}
        connection.executemany(
            "INSERT INTO signals VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    signal.condition_id,
                    signal.status,
                    signal.side,
                    signal.timestamp_ms,
                    signal.threshold,
                    signal.ask,
                    (signal.timestamp_ms - by_condition[signal.condition_id].start_ms) / 1000,
                    (by_condition[signal.condition_id].end_ms - signal.timestamp_ms) / 1000,
                )
                for _, signal in signals
            ],
        )
        connection.executemany(
            "INSERT INTO base_trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    trade.condition_id,
                    trade.source,
                    trade.split,
                    trade.slug,
                    trade.side,
                    trade.winner,
                    trade.signal_timestamp_ms,
                    trade.fill_timestamp_ms,
                    trade.seconds_elapsed,
                    trade.seconds_remaining,
                    trade.signal_price,
                    trade.fill_price,
                    trade.quantity,
                    trade.fee,
                    trade.gross_pnl,
                    trade.net_pnl,
                    trade.debit,
                    int(trade.won),
                    trade.depth_proxy,
                    int(trade.exact_level_size_known),
                    trade.closed_at_ms,
                )
                for trade in base_trades
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _write_csv(path: Path, trades: Sequence[Trade]) -> None:
    if path.exists():
        raise BacktestError(f"Resultado ya existe y no se sobrescribe: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(asdict(trades[0]).keys()) if trades else ["condition_id"]
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(trade) for trade in trades)


def _percent(value: Any) -> str:
    return "N/A" if value is None else f"{100 * float(value):.3f}%"


def _money(value: Any) -> str:
    return "N/A" if value is None else f"${float(value):.4f}"


def _render_report(result: Mapping[str, Any]) -> str:
    overall = result["base"]["ALL"]
    test = result["base"]["TEST"]
    filtered = result["time_filter"]
    filter_test = filtered.get("TEST") if filtered.get("selected") else None
    lines = [
        "# BTC 5M 90¢ STRATEGY — BACKTEST V001",
        "",
        f"Generado: {result['created_at']}",
        "",
        "## Base 90¢",
        "",
        "| Métrica | Total | Test OOS |",
        "|---|---:|---:|",
        f"| Mercados | {overall['markets']} | {test['markets']} |",
        f"| Señales/trades | {overall['trades']} | {test['trades']} |",
        f"| Win rate | {_percent(overall['win_rate'])} | {_percent(test['win_rate'])} |",
        f"| Break-even | {_percent(overall['break_even_win_rate'])} | {_percent(test['break_even_win_rate'])} |",
        f"| EV/trade | {_money(overall['net_ev_per_trade'])} | {_money(test['net_ev_per_trade'])} |",
        f"| PnL neto (5 shares) | {_money(overall['net_pnl'])} | {_money(test['net_pnl'])} |",
        f"| ROI | {_percent(overall['roi'])} | {_percent(test['roi'])} |",
        f"| Max DD | {_money(overall['max_drawdown'])} | {_money(test['max_drawdown'])} |",
        "",
        "## Time filter",
        "",
    ]
    if filter_test:
        lines.extend(
            [
                f"Ventana elegida solo con TRAIN: {filtered['selected']['elapsed_start']}–"
                f"{filtered['selected']['elapsed_end']} segundos transcurridos.",
                "",
                f"Test OOS: {filter_test['trades']} trades, WR {_percent(filter_test['win_rate'])}, "
                f"EV/trade {_money(filter_test['net_ev_per_trade'])}.",
            ]
        )
    else:
        lines.append("TRAIN no produjo una ventana candidata que cumpliera el criterio congelado.")
    lines.extend(
        [
            "",
            "## Execution quality",
            "",
            "Las capturas históricas son Level B/B+: prueban trayectoria del best ask, pero no "
            "el tamaño exacto disponible en el nivel 0,90. Los fills son teóricos; no se presentan "
            "como Level A. La resolución de 100/250/500 ms se reporta N/A cuando la fuente muestrea a 1s.",
            "",
            "## Safety",
            "",
            "Paper/research only. wallet_required=false, orders_sent=0, real_money=BLOQUEADO.",
            "",
            "## Verdict",
            "",
            f"Base: **{result['verdict']['base']}**. Filtrada: **{result['verdict']['filtered']}**. "
            f"Acción: **{result['verdict']['recommended_action']}**.",
            "",
        ]
    )
    return "\n".join(lines)


def run_backtest() -> dict[str, Any]:
    protocol = load_and_verify_protocol()
    for target in (RESULT_PATH, TRADES_PATH, DATABASE_PATH, REPORT_PATH):
        if target.exists():
            raise BacktestError(f"No se sobrescribe resultado existente: {target}")
    contracts: dict[str, MarketContract] = load_verified_contracts()
    series = load_series()

    literal_counts: dict[str, int] = defaultdict(int)
    signals: list[tuple[MarketSeries, Signal]] = []
    base_trades: list[Trade] = []
    for market in series:
        contract = contracts[market.condition_id]
        if (contract.market_start_ms, contract.market_end_ms) != (market.start_ms, market.end_ms):
            raise BacktestError(f"Gamma/DB desalineados: {market.slug}")
        literal_counts[literal_control(market)] += 1
        signal = detect_signal(market, contract, 0.90)
        if signal is None:
            continue
        signals.append((market, signal))
        trade = fill_signal(market, contract, signal, latency_ms=0, quantity=5.0)
        if trade is not None:
            base_trades.append(trade)

    split_results: dict[str, Any] = {}
    for split in ("TRAIN", "VALIDATION", "TEST", "ALL"):
        split_value = None if split == "ALL" else split
        subset = _filter_trades(base_trades, split_value)
        split_results[split] = metrics(
            subset,
            markets=_market_count(series, split_value),
            signals=_signal_count(signals, split_value),
        )

    side_results = {
        side: metrics(
            [trade for trade in base_trades if trade.side == side],
            markets=len([trade for trade in base_trades if trade.side == side]),
            signals=len([trade for trade in base_trades if trade.side == side]),
        )
        for side in ("Up", "Down")
    }
    timing_results = {}
    for start, end in ELAPSED_BUCKETS:
        subset = [trade for trade in base_trades if start <= trade.seconds_elapsed <= end]
        timing_results[f"{start}-{end}"] = metrics(
            subset, markets=len(subset), signals=len(subset)
        )

    threshold_results: dict[str, Any] = {}
    for threshold in protocol["analysis"]["threshold_sensitivity"]:
        threshold_signals: list[tuple[MarketSeries, Signal]] = []
        threshold_trades: list[Trade] = []
        for market in series:
            contract = contracts[market.condition_id]
            signal = detect_signal(market, contract, float(threshold))
            if signal is None:
                continue
            threshold_signals.append((market, signal))
            trade = fill_signal(market, contract, signal, latency_ms=0, quantity=5.0)
            if trade is not None:
                threshold_trades.append(trade)
        threshold_results[f"{float(threshold):.2f}"] = metrics(
            threshold_trades, markets=len(series), signals=len(threshold_signals)
        )

    latency_results: dict[str, Any] = {}
    for latency_ms in protocol["analysis"]["latency_ms"]:
        by_source: dict[str, Any] = {}
        all_supported_trades: list[Trade] = []
        supported_signals = 0
        supported_markets = 0
        for source in SOURCES:
            source_series = [item for item in series if item.source == source.name]
            source_signals = [(item, signal) for item, signal in signals if item.source == source.name]
            supported = latency_ms == 0 or source.sampling_ms <= latency_ms
            trades = (
                [
                    trade
                    for item, signal in source_signals
                    if (
                        trade := fill_signal(
                            item,
                            contracts[item.condition_id],
                            signal,
                            latency_ms=int(latency_ms),
                            quantity=5.0,
                        )
                    )
                    is not None
                ]
                if supported
                else []
            )
            by_source[source.name] = metrics(
                trades,
                markets=len(source_series),
                signals=len(source_signals),
                supported=supported,
            )
            if supported:
                all_supported_trades.extend(trades)
                supported_signals += len(source_signals)
                supported_markets += len(source_series)
        by_source["SUPPORTED_SOURCES_COMBINED"] = metrics(
            all_supported_trades,
            markets=supported_markets,
            signals=supported_signals,
        )
        latency_results[str(latency_ms)] = by_source

    stressed = [
        trade
        for market, signal in signals
        if (
            trade := fill_signal(
                market,
                contracts[market.condition_id],
                signal,
                latency_ms=0,
                quantity=5.0,
                adverse_ticks=1,
            )
        )
        is not None
    ]
    stress_results = {
        split: metrics(
            _filter_trades(stressed, None if split == "ALL" else split),
            markets=_market_count(series, None if split == "ALL" else split),
            signals=_signal_count(signals, None if split == "ALL" else split),
        )
        for split in ("TRAIN", "VALIDATION", "TEST", "ALL")
    }

    selected = select_time_window(_filter_trades(base_trades, "TRAIN"))
    time_filter: dict[str, Any] = {"selected": selected}
    if selected:
        start = selected["elapsed_start"]
        end = selected["elapsed_end"]
        for split in ("TRAIN", "VALIDATION", "TEST", "ALL"):
            split_value = None if split == "ALL" else split
            subset = [
                trade
                for trade in _filter_trades(base_trades, split_value)
                if start <= trade.seconds_elapsed <= end
            ]
            time_filter[split] = metrics(
                subset,
                markets=_market_count(series, split_value),
                signals=len(subset),
            )

    bankroll = {
        f"bankroll_{balance:g}": {
            f"stake_{stake:g}": simulate_bankroll(base_trades, balance, stake)
            for stake in (1.0, 2.0, 5.0, 10.0)
        }
        for balance in (10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 5000.0)
    }

    test = split_results["TEST"]
    test_stress = stress_results["TEST"]
    gates = protocol["promotion_gates"]
    base_pass = bool(
        test["trades"] >= gates["minimum_test_trades"]
        and test["net_ev_per_trade"] > gates["test_net_ev_per_trade_gt"]
        and test_stress["net_ev_per_trade"] > gates["test_one_tick_stress_ev_gt"]
        and test["wilson_95_low"] > test["break_even_win_rate"]
    )
    filtered_test = time_filter.get("TEST")
    filtered_pass = bool(
        filtered_test
        and filtered_test["trades"] >= gates["minimum_test_trades"]
        and filtered_test["net_ev_per_trade"] > 0.0
        and filtered_test["wilson_95_low"] > filtered_test["break_even_win_rate"]
    )
    execution_proven = False
    verdict = {
        "base": "PROFITABLE" if base_pass and execution_proven else (
            "INCONCLUSIVE" if base_pass else "NOT_PROFITABLE"
        ),
        "filtered": "PROFITABLE" if filtered_pass and execution_proven else (
            "INCONCLUSIVE" if filtered_pass else "NOT_PROFITABLE"
        ),
        "execution_level_a": execution_proven,
        "recommended_action": (
            "CONTINUE_SHADOW_TO_PROVE_EXECUTION"
            if base_pass or filtered_pass
            else "ABANDON_OR_MODIFY_ECONOMIC_MECHANISM"
        ),
    }

    data_quality_score = 6.0
    profitability_score = 20.0 if base_pass else max(0.0, 10.0 + 100.0 * float(test["net_ev_per_share"] or 0.0))
    statistical_score = 15.0 if test["wilson_95_low"] > test["break_even_win_rate"] else 5.0
    scorecard = {
        "profitability": round(min(20.0, profitability_score), 2),
        "statistical_robustness": statistical_score,
        "execution_feasibility": 4.0,
        "capital_efficiency": 7.0,
        "drawdown_risk": 5.0,
        "data_quality": data_quality_score,
        "automation": 5.0,
        "scalability": 3.0,
        "operational_robustness": 3.0,
    }
    scorecard["total"] = round(sum(scorecard.values()), 2)

    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "created_at": utc_now(),
        "protocol_sha256": sha256_file(PROTOCOL_PATH),
        "dataset": {
            "markets": len(series),
            "observations": sum(len(item.observations) for item in series),
            "period_start_ms": series[0].start_ms,
            "period_end_ms": series[-1].end_ms,
            "splits": {split: _market_count(series, split) for split in ("TRAIN", "VALIDATION", "TEST")},
            "sources": [
                {
                    "name": source.name,
                    "sampling_ms": source.sampling_ms,
                    "execution_quality": source.execution_quality,
                    "database_sha256": sha256_file(ROOT / source.database),
                }
                for source in SOURCES
            ],
        },
        "semantics_audit": {
            "literal_leq_090": dict(literal_counts),
            "economic_first_exact_touch": {
                "signals": len(signals),
                "ambiguous": sum(signal.status == "AMBIGUOUS" for _, signal in signals),
                "no_touch": len(series) - len(signals),
            },
        },
        "base": split_results,
        "side": side_results,
        "timing_elapsed_seconds": timing_results,
        "threshold_sensitivity": threshold_results,
        "latency": latency_results,
        "one_tick_adverse_stress": stress_results,
        "time_filter": time_filter,
        "capital_scenarios": bankroll,
        "scorecard": scorecard,
        "verdict": verdict,
        "execution_decay": {
            "theoretical_net_pnl": split_results["ALL"]["net_pnl"],
            "realistic_level_a_net_pnl": None,
            "reason": "exact threshold-level size was not stored in the historical snapshots",
        },
        "safety": {
            "wallet_required": False,
            "orders_sent": 0,
            "real_money": "BLOQUEADO",
        },
    }

    _write_csv(TRADES_PATH, base_trades)
    _create_output_database(DATABASE_PATH, series, signals, base_trades, result)
    RESULT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    REPORT_PATH.write_text(_render_report(result), encoding="utf-8")
    return result
