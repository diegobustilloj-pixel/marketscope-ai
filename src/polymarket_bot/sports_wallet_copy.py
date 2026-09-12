from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import threading
import time
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .sports_wallet_analysis import (
    load_market_maps,
    market_resolution_timestamp,
    number,
    resolved_payouts,
)
from .sports_wallet_research import (
    CUTOFF_INCLUSIVE_UNIX,
    DATA_API,
    PublicApiClient,
    SportsResearchError,
    TRADERS,
    read_jsonl,
    row_digest,
    stable_json,
    utc_iso,
    write_csv,
    write_json,
    write_jsonl,
)


COPY_SCHEMA = "sports_wallet_copy_v001"
DELAYS_SECONDS = (0, 1, 5, 15, 30, 60, 120, 300)
REQUESTED_STAKES_USD = (10, 50, 100, 250, 500, 1000, 5000)
ADVERSE_IMPACTS = (0.0, 0.01, 0.02, 0.05)
PRIMARY_DELAY_SECONDS = 15
PRIMARY_STAKE_USD = 100
PRIMARY_ADVERSE_IMPACT = 0.01
MAX_PARTICIPATION = 0.25
FILL_WINDOW_SECONDS = 60
TAPE_END_SECONDS = max(DELAYS_SECONDS) + FILL_WINDOW_SECONDS
TAPE_LIMIT = 10_000
TAPE_BATCH_SIZE = 100
TAPE_WORKERS = 6
MIN_REQUEST_INTERVAL_SECONDS = 0.065


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _query_url(path: str, params: dict[str, Any]) -> str:
    encoded = urllib.parse.urlencode(
        [(key, value) for key, value in params.items() if value is not None]
    )
    return f"{DATA_API}{path}?{encoded}"


class ThrottledPublicApiClient(PublicApiClient):
    """Public client with a process-wide request-start throttle."""

    def __init__(self, *args: Any, min_interval: float = MIN_REQUEST_INTERVAL_SECONDS, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.min_interval = min_interval
        self._throttle_lock = threading.Lock()
        self._last_request_started = 0.0

    def get_json(self, url: str) -> Any:
        with self._throttle_lock:
            now = time.monotonic()
            remaining = self.min_interval - (now - self._last_request_started)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request_started = time.monotonic()
        return super().get_json(url)


def load_raw_market_parameters(output_root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(output_root / "raw" / "markets.jsonl"):
        condition = str(row.get("conditionId") or "").lower()
        if not condition:
            continue
        result[condition] = {
            "fees_enabled": bool(row.get("feesEnabled")),
            "fee_schedule": row.get("feeSchedule") if isinstance(row.get("feeSchedule"), dict) else None,
        }
    return result


def assign_event_splits(signals: list[dict[str, Any]]) -> None:
    by_trader: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for signal in signals:
        event_key = str(signal.get("event_id") or signal.get("event_slug") or signal["condition_id"])
        by_trader[str(signal["trader_key"])][event_key].append(signal)

    for trader_groups in by_trader.values():
        ordered_groups = sorted(
            trader_groups.values(),
            key=lambda rows: (
                min(int(row["signal_timestamp"]) for row in rows),
                str(rows[0].get("event_id") or rows[0].get("event_slug") or rows[0]["condition_id"]),
            ),
        )
        total = sum(len(rows) for rows in ordered_groups)
        assigned = 0
        for rows in ordered_groups:
            midpoint = assigned + (len(rows) / 2)
            fraction = midpoint / total if total else 0
            split = "TRAIN" if fraction <= 0.60 else "VALIDATION" if fraction <= 0.80 else "TEST"
            for row in rows:
                row["split"] = split
            assigned += len(rows)


def build_signals(output_root: Path) -> dict[str, Any]:
    catalog, raw_minimal = load_market_maps(output_root)
    raw_parameters = load_raw_market_parameters(output_root)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in read_jsonl(output_root / "derived" / "master_sports_trades.jsonl"):
        grouped[(str(row["trader_key"]), str(row["condition_id"]).lower())].append(row)

    exclusions: Counter[str] = Counter()
    signals: list[dict[str, Any]] = []
    for (trader_key, condition), rows in sorted(grouped.items()):
        market = catalog.get(condition)
        if not market or market.get("scope") != "SPORTS_INCLUDED":
            exclusions["not_sports_included"] += 1
            continue
        payouts = resolved_payouts(market)
        resolution_ts = market_resolution_timestamp(condition, catalog, raw_minimal)
        if payouts is None or resolution_ts is None or resolution_ts > CUTOFF_INCLUSIVE_UNIX:
            exclusions["not_resolved_before_cutoff"] += 1
            continue
        buys = [row for row in rows if str(row.get("side") or "").upper() == "BUY"]
        if not buys:
            exclusions["no_buy"] += 1
            continue
        first_ts = min(int(row["timestamp"]) for row in buys)
        exact_unique: dict[str, dict[str, Any]] = {}
        for row in buys:
            if int(row["timestamp"]) == first_ts:
                exact_unique[row_digest(row)] = row
        first_rows = list(exact_unique.values())
        assets = {str(row.get("asset") or "") for row in first_rows}
        if "" in assets or len(assets) != 1:
            exclusions["ambiguous_first_second_multiple_assets"] += 1
            continue
        asset = next(iter(assets))
        total_size = sum(number(row.get("size")) for row in first_rows)
        total_notional = sum(number(row.get("size")) * number(row.get("price")) for row in first_rows)
        if total_size <= 0 or total_notional <= 0:
            exclusions["invalid_first_buy"] += 1
            continue
        outcome_indices = {int(row.get("outcome_index") or 0) for row in first_rows}
        if len(outcome_indices) != 1:
            exclusions["ambiguous_outcome_index"] += 1
            continue
        outcome_index = next(iter(outcome_indices))
        if outcome_index not in payouts:
            exclusions["missing_outcome_payout"] += 1
            continue
        params = raw_parameters.get(condition, {})
        signal_id = f"{trader_key}:{condition}"
        first = sorted(first_rows, key=lambda row: stable_json(row))[0]
        signals.append(
            {
                "signal_id": signal_id,
                "trader_key": trader_key,
                "condition_id": condition,
                "event_id": first.get("event_id"),
                "event_slug": first.get("event_slug"),
                "slug": first.get("slug"),
                "title": first.get("title"),
                "sport_code": first.get("sport_code"),
                "league": first.get("league"),
                "bet_family": first.get("bet_family"),
                "timing": first.get("timing"),
                "signal_timestamp": first_ts,
                "signal_utc": utc_iso(first_ts),
                "resolution_timestamp": resolution_ts,
                "resolution_utc": utc_iso(resolution_ts),
                "asset": asset,
                "outcome": first.get("outcome"),
                "outcome_index": outcome_index,
                "payout": payouts[outcome_index],
                "leader_first_second_rows": len(first_rows),
                "leader_first_size": total_size,
                "leader_first_notional_usd": total_notional,
                "leader_first_vwap": total_notional / total_size,
                "fees_enabled": bool(params.get("fees_enabled")),
                "fee_schedule": params.get("fee_schedule"),
            }
        )

    assign_event_splits(signals)
    signals.sort(key=lambda row: (str(row["trader_key"]), int(row["signal_timestamp"]), str(row["condition_id"])))
    copy_root = output_root / "copy"
    write_jsonl(copy_root / "signals.jsonl", signals)
    write_csv(copy_root / "signals.csv", signals)
    by_trader = Counter(str(row["trader_key"]) for row in signals)
    by_split = Counter(f"{row['trader_key']}:{row['split']}" for row in signals)
    input_files = {
        name: sha256_file(output_root / path)
        for name, path in {
            "master_sports_trades": Path("derived/master_sports_trades.jsonl"),
            "market_catalog": Path("derived/market_catalog.jsonl"),
            "raw_markets": Path("raw/markets.jsonl"),
            "analysis_summary": Path("analysis/analysis_summary.json"),
        }.items()
    }
    protocol = {
        "schema": COPY_SCHEMA,
        "cutoff_inclusive_unix": CUTOFF_INCLUSIVE_UNIX,
        "delays_seconds": list(DELAYS_SECONDS),
        "requested_stakes_usd": list(REQUESTED_STAKES_USD),
        "adverse_impacts": list(ADVERSE_IMPACTS),
        "primary": {
            "delay_seconds": PRIMARY_DELAY_SECONDS,
            "stake_usd": PRIMARY_STAKE_USD,
            "adverse_impact": PRIMARY_ADVERSE_IMPACT,
            "max_participation": MAX_PARTICIPATION,
            "fill_window_seconds": FILL_WINDOW_SECONDS,
        },
        "signal_rule": "first unambiguous BUY second per trader-condition; resolved sports before cutoff",
        "execution_rule": "next public same-asset print at/after target within 60s; worst price at earliest second",
        "split_rule": "chronological event groups per trader, approximately 60/20/20",
        "selection_rule": "positive train+validation PnL/ROI/PF, minimum fills; rank by minimum PF then ROI; test confirms",
        "safety": {
            "credentials_required": False,
            "orders_enabled": False,
            "paper_only": True,
            "real_money": "BLOQUEADO",
            "wallet_connection_required": False,
        },
        "input_sha256": input_files,
    }
    protocol["protocol_sha256"] = hashlib.sha256(stable_json(protocol).encode("utf-8")).hexdigest()
    write_json(copy_root / "protocol.json", protocol)
    manifest = {
        "schema": COPY_SCHEMA,
        "status": "SIGNALS_FROZEN",
        "signals": len(signals),
        "signals_by_trader": dict(by_trader),
        "signals_by_trader_split": dict(by_split),
        "excluded_trader_conditions": dict(exclusions),
        "signals_sha256": sha256_file(copy_root / "signals.jsonl"),
        "protocol_sha256": protocol["protocol_sha256"],
    }
    write_json(copy_root / "audit" / "signal_manifest.json", manifest)
    return manifest


def compact_tape_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "proxy_wallet": str(row.get("proxyWallet") or "").lower(),
        "side": row.get("side"),
        "asset": str(row.get("asset") or ""),
        "condition_id": str(row.get("conditionId") or "").lower(),
        "size": number(row.get("size")),
        "price": number(row.get("price")),
        "timestamp": int(row.get("timestamp") or 0),
        "outcome": row.get("outcome"),
        "outcome_index": row.get("outcomeIndex"),
        "transaction_hash": str(row.get("transactionHash") or "").lower(),
    }


def tape_row_key(row: dict[str, Any]) -> str:
    return stable_json(
        [
            row.get("proxy_wallet"),
            row.get("side"),
            row.get("asset"),
            row.get("size"),
            row.get("price"),
            row.get("timestamp"),
            row.get("transaction_hash"),
        ]
    )


def fetch_tape_interval(
    client: PublicApiClient,
    condition: str,
    start: int,
    end: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[int]]:
    if end < start:
        return [], [], []
    payload = client.get_json(
        _query_url(
            "/trades",
            {
                "market": condition,
                "start": start,
                "end": end,
                "limit": TAPE_LIMIT,
                "offset": 0,
                "takerOnly": "false",
            },
        )
    )
    if not isinstance(payload, list):
        raise SportsResearchError(f"Respuesta no-lista en cinta {condition} {start}-{end}")
    audit = [{"start": start, "end_inclusive": end, "rows": len(payload)}]
    if len(payload) < TAPE_LIMIT:
        return [compact_tape_row(row) for row in payload if isinstance(row, dict)], audit, []
    if start == end:
        return [compact_tape_row(row) for row in payload if isinstance(row, dict)], audit, [start]
    midpoint = (start + end) // 2
    left, left_audit, left_saturated = fetch_tape_interval(client, condition, start, midpoint)
    right, right_audit, right_saturated = fetch_tape_interval(client, condition, midpoint + 1, end)
    return left + right, audit + left_audit + right_audit, left_saturated + right_saturated


def fetch_signal_tape(client: PublicApiClient, signal: dict[str, Any]) -> dict[str, Any]:
    start = int(signal["signal_timestamp"]) + 1
    end = min(int(signal["signal_timestamp"]) + TAPE_END_SECONDS, CUTOFF_INCLUSIVE_UNIX)
    rows, page_audit, saturated = fetch_tape_interval(
        client, str(signal["condition_id"]), start, end
    )
    unique = {tape_row_key(row): row for row in rows}
    ordered = sorted(
        unique.values(),
        key=lambda row: (int(row["timestamp"]), str(row["transaction_hash"]), str(row["asset"])),
    )
    return {
        "signal_id": signal["signal_id"],
        "trader_key": signal["trader_key"],
        "condition_id": signal["condition_id"],
        "start": start,
        "end_inclusive": end,
        "complete": not saturated,
        "saturated_seconds": saturated,
        "api_rows_before_dedup": len(rows),
        "tape_rows": ordered,
        "page_audit": page_audit,
    }


def _valid_batch(path: Path, expected_ids: set[str]) -> bool:
    if not path.exists():
        return False
    try:
        rows = list(read_jsonl(path))
    except (OSError, json.JSONDecodeError, SportsResearchError):
        return False
    return (
        {str(row.get("signal_id")) for row in rows} == expected_ids
        and all(bool(row.get("complete")) for row in rows)
    )


def capture_tapes(output_root: Path, client: PublicApiClient | None = None) -> dict[str, Any]:
    copy_root = output_root / "copy"
    signals_path = copy_root / "signals.jsonl"
    protocol_path = copy_root / "protocol.json"
    if not signals_path.exists() or not protocol_path.exists():
        raise SportsResearchError("Faltan señales/protocolo de copia; ejecute --prepare-copy")
    signals = list(read_jsonl(signals_path))
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if protocol.get("schema") != COPY_SCHEMA:
        raise SportsResearchError("Protocolo de copia incompatible")
    if sha256_file(signals_path) != json.loads(
        (copy_root / "audit" / "signal_manifest.json").read_text(encoding="utf-8")
    ).get("signals_sha256"):
        raise SportsResearchError("signals.jsonl cambió después del prerregistro")

    api = client or ThrottledPublicApiClient(timeout_seconds=60, retries=10)
    batch_dir = copy_root / "raw" / "tape_batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    total_batches = math.ceil(len(signals) / TAPE_BATCH_SIZE)
    skipped = downloaded = 0
    started = time.monotonic()
    for batch_index in range(total_batches):
        batch = signals[batch_index * TAPE_BATCH_SIZE : (batch_index + 1) * TAPE_BATCH_SIZE]
        path = batch_dir / f"batch_{batch_index:05d}.jsonl"
        expected = {str(row["signal_id"]) for row in batch}
        if _valid_batch(path, expected):
            skipped += 1
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=TAPE_WORKERS) as executor:
                futures = {executor.submit(fetch_signal_tape, api, signal): signal for signal in batch}
                rows = [future.result() for future in concurrent.futures.as_completed(futures)]
            rows.sort(key=lambda row: str(row["signal_id"]))
            if not all(row.get("complete") for row in rows):
                saturated = [row["signal_id"] for row in rows if not row.get("complete")]
                raise SportsResearchError(f"Segundos saturados en cinta: {saturated[:5]}")
            write_jsonl(path, rows)
            downloaded += 1
        completed = batch_index + 1
        if completed == 1 or completed % 10 == 0 or completed == total_batches:
            elapsed = time.monotonic() - started
            rate = completed / elapsed if elapsed else 0
            eta = (total_batches - completed) / rate if rate else None
            print(
                stable_json(
                    {
                        "status": "COPY_TAPE_PROGRESS",
                        "batches": f"{completed}/{total_batches}",
                        "downloaded_this_run": downloaded,
                        "resumed_batches": skipped,
                        "eta_seconds": round(eta, 1) if eta is not None else None,
                    }
                ),
                flush=True,
            )

    all_ids: set[str] = set()
    tape_rows = api_rows = pages = 0
    incomplete: list[str] = []
    files: list[dict[str, Any]] = []
    for path in sorted(batch_dir.glob("batch_*.jsonl")):
        files.append({"path": str(path.relative_to(output_root)), "sha256": sha256_file(path), "bytes": path.stat().st_size})
        for row in read_jsonl(path):
            signal_id = str(row["signal_id"])
            if signal_id in all_ids:
                raise SportsResearchError(f"Señal de cinta duplicada: {signal_id}")
            all_ids.add(signal_id)
            tape_rows += len(row.get("tape_rows") or [])
            api_rows += int(row.get("api_rows_before_dedup") or 0)
            pages += len(row.get("page_audit") or [])
            if not row.get("complete"):
                incomplete.append(signal_id)
    expected_ids = {str(row["signal_id"]) for row in signals}
    missing = sorted(expected_ids - all_ids)
    extra = sorted(all_ids - expected_ids)
    manifest = {
        "schema": COPY_SCHEMA,
        "status": "TAPE_CAPTURE_COMPLETE" if not missing and not extra and not incomplete else "TAPE_CAPTURE_INCOMPLETE",
        "signals": len(signals),
        "captured_signals": len(all_ids),
        "missing_signal_ids": missing,
        "extra_signal_ids": extra,
        "incomplete_signal_ids": incomplete,
        "api_rows_before_dedup": api_rows,
        "unique_tape_rows": tape_rows,
        "api_pages": pages,
        "requests_this_run": api.requests,
        "files": files,
        "safety": protocol["safety"],
    }
    write_json(copy_root / "audit" / "tape_manifest.json", manifest)
    if manifest["status"] != "TAPE_CAPTURE_COMPLETE":
        raise SportsResearchError("La cinta no quedó completa; revise tape_manifest.json")
    return manifest


def iter_tape_results(copy_root: Path) -> Iterator[dict[str, Any]]:
    for path in sorted((copy_root / "raw" / "tape_batches").glob("batch_*.jsonl")):
        yield from read_jsonl(path)


def select_next_print(
    tape_rows: Sequence[dict[str, Any]],
    asset: str,
    target_timestamp: int,
    deadline_timestamp: int,
) -> dict[str, Any] | None:
    matching = [
        row
        for row in tape_rows
        if str(row.get("asset") or "") == asset
        and target_timestamp <= int(row.get("timestamp") or 0) <= deadline_timestamp
        and 0 < number(row.get("price")) < 1
        and number(row.get("size")) > 0
    ]
    if not matching:
        return None
    first_timestamp = min(int(row["timestamp"]) for row in matching)
    first_second = [row for row in matching if int(row["timestamp"]) == first_timestamp]
    worst_price = max(number(row["price"]) for row in first_second)
    worst_rows = [row for row in first_second if abs(number(row["price"]) - worst_price) <= 1e-12]
    return {
        "print_timestamp": first_timestamp,
        "print_price": worst_price,
        "print_size": sum(number(row["size"]) for row in worst_rows),
        "same_second_matching_rows": len(first_second),
        "rows_at_selected_price": len(worst_rows),
    }


def taker_fee_usd(
    shares: float,
    price: float,
    fees_enabled: bool,
    fee_schedule: dict[str, Any] | None,
) -> float:
    if not fees_enabled:
        return 0.0
    if not isinstance(fee_schedule, dict):
        raise SportsResearchError("Mercado fee-enabled sin feeSchedule")
    rate = number(fee_schedule.get("rate"), -1)
    if rate < 0:
        raise SportsResearchError("feeSchedule sin rate válido")
    fee = shares * rate * price * (1 - price)
    return round(fee, 5) if fee >= 0.000005 else 0.0


def simulate_execution(
    opportunity: dict[str, Any],
    requested_stake: float,
    adverse_impact: float,
) -> dict[str, Any]:
    base = {
        "trader_key": opportunity["trader_key"],
        "signal_id": opportunity["signal_id"],
        "split": opportunity["split"],
        "delay_seconds": opportunity["delay_seconds"],
        "requested_stake_usd": requested_stake,
        "adverse_impact": adverse_impact,
        "available": bool(opportunity["available"]),
    }
    if not opportunity["available"]:
        return {
            **base,
            "executed_stake_usd": 0.0,
            "capacity_limited": False,
            "execution_price": None,
            "shares": 0.0,
            "fee_usd": 0.0,
            "payout_usd": 0.0,
            "net_pnl_usd": 0.0,
        }
    price = min(0.999, max(0.001, number(opportunity["print_price"]) + adverse_impact))
    printed_notional = number(opportunity["print_price"]) * number(opportunity["print_size"])
    capacity = printed_notional * MAX_PARTICIPATION
    stake = min(requested_stake, capacity)
    if stake <= 0:
        return {**base, "available": False, "executed_stake_usd": 0.0, "capacity_limited": False, "execution_price": None, "shares": 0.0, "fee_usd": 0.0, "payout_usd": 0.0, "net_pnl_usd": 0.0}
    shares = stake / price
    fee = taker_fee_usd(
        shares,
        price,
        bool(opportunity.get("fees_enabled")),
        opportunity.get("fee_schedule"),
    )
    payout = shares * number(opportunity["payout"])
    return {
        **base,
        "executed_stake_usd": stake,
        "capacity_limited": stake + 1e-12 < requested_stake,
        "execution_price": price,
        "shares": shares,
        "fee_usd": fee,
        "payout_usd": payout,
        "net_pnl_usd": payout - stake - fee,
    }


@dataclass
class Aggregate:
    opportunities: int = 0
    fills: int = 0
    requested_stake: float = 0.0
    executed_stake: float = 0.0
    fees: float = 0.0
    payout: float = 0.0
    pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    wins: int = 0
    losses: int = 0
    flat: int = 0
    capacity_limited: int = 0
    slippage_sum: float = 0.0
    cumulative: float = 0.0
    peak: float = 0.0
    max_drawdown: float = 0.0

    def add(self, execution: dict[str, Any], opportunity: dict[str, Any]) -> None:
        self.opportunities += 1
        self.requested_stake += number(execution["requested_stake_usd"])
        stake = number(execution["executed_stake_usd"])
        if stake <= 0:
            return
        self.fills += 1
        self.executed_stake += stake
        self.fees += number(execution["fee_usd"])
        self.payout += number(execution["payout_usd"])
        pnl = number(execution["net_pnl_usd"])
        self.pnl += pnl
        if pnl > 1e-9:
            self.wins += 1
            self.gross_profit += pnl
        elif pnl < -1e-9:
            self.losses += 1
            self.gross_loss -= pnl
        else:
            self.flat += 1
        if execution["capacity_limited"]:
            self.capacity_limited += 1
        self.slippage_sum += number(opportunity.get("print_price")) - number(opportunity.get("leader_price"))
        self.cumulative += pnl
        self.peak = max(self.peak, self.cumulative)
        self.max_drawdown = min(self.max_drawdown, self.cumulative - self.peak)

    def as_dict(self) -> dict[str, Any]:
        total_cost = self.executed_stake + self.fees
        return {
            "opportunities": self.opportunities,
            "fills": self.fills,
            "fill_rate": self.fills / self.opportunities if self.opportunities else None,
            "total_requested_stake_usd": self.requested_stake,
            "executed_stake_usd": self.executed_stake,
            "fees_usd": self.fees,
            "payout_usd": self.payout,
            "net_pnl_usd": self.pnl,
            "roi_on_executed_cost": self.pnl / total_cost if total_cost else None,
            "gross_profit": self.gross_profit,
            "gross_loss": self.gross_loss,
            "profit_factor": self.gross_profit / self.gross_loss if self.gross_loss else None,
            "wins": self.wins,
            "losses": self.losses,
            "flat": self.flat,
            "win_rate_on_fills": self.wins / self.fills if self.fills else None,
            "capacity_limited_fills": self.capacity_limited,
            "capacity_limited_fraction": self.capacity_limited / self.fills if self.fills else None,
            "mean_price_slippage_vs_leader": self.slippage_sum / self.fills if self.fills else None,
            "max_drawdown_usd": self.max_drawdown,
        }


def _metric_row(
    rows: Sequence[dict[str, Any]], trader: str, split: str
) -> dict[str, Any] | None:
    for row in rows:
        if (
            row["trader_key"] == trader
            and row["split"] == split
            and int(row["delay_seconds"]) == PRIMARY_DELAY_SECONDS
            and number(row["requested_stake_usd"]) == PRIMARY_STAKE_USD
            and abs(number(row["adverse_impact"]) - PRIMARY_ADVERSE_IMPACT) <= 1e-12
        ):
            return row
    return None


def select_copy_candidate(grid_rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    evaluations: dict[str, Any] = {}
    qualified: list[tuple[float, float, int, str]] = []
    for trader in [spec["key"] for spec in TRADERS]:
        train = _metric_row(grid_rows, trader, "TRAIN")
        validation = _metric_row(grid_rows, trader, "VALIDATION")
        test = _metric_row(grid_rows, trader, "TEST")
        if not train or not validation or not test:
            continue
        train_ok = (
            int(train["fills"]) >= 40
            and number(train["net_pnl_usd"]) > 0
            and number(train["roi_on_executed_cost"]) > 0
            and number(train["profit_factor"]) > 1
        )
        validation_ok = (
            int(validation["fills"]) >= 15
            and number(validation["net_pnl_usd"]) > 0
            and number(validation["roi_on_executed_cost"]) > 0
            and number(validation["profit_factor"]) > 1
        )
        qualifies = train_ok and validation_ok
        test_confirmed = (
            int(test["fills"]) >= 15
            and number(test["net_pnl_usd"]) > 0
            and number(test["roi_on_executed_cost"]) > 0
            and number(test["profit_factor"]) > 1
        )
        evaluations[trader] = {
            "train": train,
            "validation": validation,
            "test": test,
            "train_qualified": train_ok,
            "validation_qualified": validation_ok,
            "qualified_before_test": qualifies,
            "test_confirmed": test_confirmed,
        }
        if qualifies:
            qualified.append(
                (
                    min(number(train["profit_factor"]), number(validation["profit_factor"])),
                    min(number(train["roi_on_executed_cost"]), number(validation["roi_on_executed_cost"])),
                    int(train["fills"]) + int(validation["fills"]),
                    trader,
                )
            )
    qualified.sort(reverse=True)
    candidate = qualified[0][3] if qualified else None
    confirmed = bool(candidate and evaluations[candidate]["test_confirmed"])
    return {
        "primary_scenario": {
            "delay_seconds": PRIMARY_DELAY_SECONDS,
            "requested_stake_usd": PRIMARY_STAKE_USD,
            "adverse_impact": PRIMARY_ADVERSE_IMPACT,
            "max_participation": MAX_PARTICIPATION,
        },
        "evaluations": evaluations,
        "qualified_before_test": [item[3] for item in qualified],
        "winner": candidate,
        "test_confirmed": confirmed,
        "deployment_status": "PAPER_CANDIDATE_CONFIRMED" if confirmed else "NO_CONFIRMED_DEPLOYABLE_WINNER",
    }


def analyze_copy(output_root: Path) -> dict[str, Any]:
    copy_root = output_root / "copy"
    tape_manifest_path = copy_root / "audit" / "tape_manifest.json"
    if not tape_manifest_path.exists():
        raise SportsResearchError("Falta captura de cinta; ejecute --capture-copy")
    tape_manifest = json.loads(tape_manifest_path.read_text(encoding="utf-8"))
    if tape_manifest.get("status") != "TAPE_CAPTURE_COMPLETE":
        raise SportsResearchError("La captura de cinta no está completa")
    signals = {str(row["signal_id"]): row for row in read_jsonl(copy_root / "signals.jsonl")}
    tape_results = {str(row["signal_id"]): row for row in iter_tape_results(copy_root)}
    if set(signals) != set(tape_results):
        raise SportsResearchError("Señales y resultados de cinta no coinciden")

    opportunities: list[dict[str, Any]] = []
    for signal in sorted(signals.values(), key=lambda row: (str(row["trader_key"]), int(row["signal_timestamp"]), str(row["condition_id"]))):
        tape = tape_results[str(signal["signal_id"])]
        for delay in DELAYS_SECONDS:
            if delay == 0:
                selected = {
                    "print_timestamp": int(signal["signal_timestamp"]),
                    "print_price": number(signal["leader_first_vwap"]),
                    "print_size": number(signal["leader_first_size"]),
                    "same_second_matching_rows": int(signal["leader_first_second_rows"]),
                    "rows_at_selected_price": int(signal["leader_first_second_rows"]),
                }
                execution_label = "SIMULTANEOUS_LEADER_BENCHMARK"
            else:
                target = int(signal["signal_timestamp"]) + delay
                deadline = min(target + FILL_WINDOW_SECONDS, CUTOFF_INCLUSIVE_UNIX)
                selected = select_next_print(tape.get("tape_rows") or [], str(signal["asset"]), target, deadline)
                execution_label = "NEXT_PUBLIC_PRINT_PROXY"
            opportunities.append(
                {
                    "signal_id": signal["signal_id"],
                    "trader_key": signal["trader_key"],
                    "split": signal["split"],
                    "condition_id": signal["condition_id"],
                    "event_id": signal.get("event_id"),
                    "sport_code": signal.get("sport_code"),
                    "league": signal.get("league"),
                    "bet_family": signal.get("bet_family"),
                    "timing": signal.get("timing"),
                    "signal_timestamp": signal["signal_timestamp"],
                    "resolution_timestamp": signal["resolution_timestamp"],
                    "asset": signal["asset"],
                    "payout": signal["payout"],
                    "leader_price": signal["leader_first_vwap"],
                    "delay_seconds": delay,
                    "execution_evidence": execution_label,
                    "available": selected is not None,
                    "print_timestamp": selected.get("print_timestamp") if selected else None,
                    "seconds_after_signal": int(selected["print_timestamp"]) - int(signal["signal_timestamp"]) if selected else None,
                    "print_price": selected.get("print_price") if selected else None,
                    "print_size": selected.get("print_size") if selected else None,
                    "same_second_matching_rows": selected.get("same_second_matching_rows") if selected else 0,
                    "fees_enabled": signal.get("fees_enabled"),
                    "fee_schedule": signal.get("fee_schedule"),
                }
            )

    analysis_dir = copy_root / "analysis"
    write_jsonl(analysis_dir / "execution_opportunities.jsonl", opportunities)
    write_csv(analysis_dir / "execution_opportunities.csv", opportunities)
    aggregates: dict[tuple[str, str, int, float, float], Aggregate] = defaultdict(Aggregate)
    primary_rows: list[dict[str, Any]] = []
    for opportunity in opportunities:
        for requested_stake in REQUESTED_STAKES_USD:
            for impact in ADVERSE_IMPACTS:
                execution = simulate_execution(opportunity, requested_stake, impact)
                for split in (str(opportunity["split"]), "ALL"):
                    key = (
                        str(opportunity["trader_key"]),
                        split,
                        int(opportunity["delay_seconds"]),
                        float(requested_stake),
                        float(impact),
                    )
                    aggregates[key].add(execution, opportunity)
                if requested_stake == PRIMARY_STAKE_USD and abs(impact - PRIMARY_ADVERSE_IMPACT) <= 1e-12:
                    primary_rows.append(
                        {
                            **{key: value for key, value in opportunity.items() if key not in {"fee_schedule"}},
                            **execution,
                        }
                    )
    grid_rows: list[dict[str, Any]] = []
    for (trader, split, delay, stake, impact), aggregate in sorted(aggregates.items()):
        grid_rows.append(
            {
                "trader_key": trader,
                "split": split,
                "delay_seconds": delay,
                "requested_stake_usd": stake,
                "adverse_impact": impact,
                **aggregate.as_dict(),
            }
        )
    write_jsonl(analysis_dir / "primary_executions.jsonl", primary_rows)
    write_csv(analysis_dir / "primary_executions.csv", primary_rows)
    write_jsonl(analysis_dir / "copy_grid_summary.jsonl", grid_rows)
    write_csv(analysis_dir / "copy_grid_summary.csv", grid_rows)
    selection = select_copy_candidate(grid_rows)
    result = {
        "schema": COPY_SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "cutoff_inclusive_unix": CUTOFF_INCLUSIVE_UNIX,
        "counts": {
            "signals": len(signals),
            "execution_opportunities": len(opportunities),
            "primary_execution_rows": len(primary_rows),
            "grid_summary_rows": len(grid_rows),
        },
        "selection": selection,
        "limitations": [
            "El siguiente negocio público es un proxy de precio alcanzable, no una prueba de profundidad u orden disponible.",
            "La capacidad usa 25% del tamaño impreso seleccionado; no reconstruye el libro histórico.",
            "El benchmark de 0 segundos replica el VWAP del líder y no es una ejecución alcanzable.",
            "La cinta pública tiene resolución de segundos; dentro de un segundo se usa el precio BUY más adverso.",
            "Se asume taker y no se acreditan rebates maker.",
        ],
        "safety": tape_manifest["safety"],
    }
    write_json(analysis_dir / "copy_summary.json", result)
    return result


__all__ = [
    "ADVERSE_IMPACTS",
    "COPY_SCHEMA",
    "DELAYS_SECONDS",
    "PRIMARY_ADVERSE_IMPACT",
    "PRIMARY_DELAY_SECONDS",
    "PRIMARY_STAKE_USD",
    "REQUESTED_STAKES_USD",
    "analyze_copy",
    "assign_event_splits",
    "build_signals",
    "capture_tapes",
    "fetch_tape_interval",
    "select_next_print",
    "simulate_execution",
    "taker_fee_usd",
]
