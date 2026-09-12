from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import sqlite3
import statistics
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


SCHEMA = "climate_automatic_realistic_backtest_v003"
CLOB_HISTORY = "https://clob.polymarket.com/prices-history"
DEFAULT_RESEARCH = Path("data/climate_research_v001/derived")
DEFAULT_OUTPUT = Path("data/climate_realistic_backtest_v003")
PREREGISTRATION = Path("docs/PREREG_CLIMATE_AUTOMATIC_BACKTEST_V003_20260903.md")
EXPECTED_HASHES = {
    "model_selection.json": "a41ee83c94bc8f16b07de5a0eb5e0851d168a4f1994ebfe54d0b3b89934b64d1",
    "test_selected_event_predictions.csv": "2cad2d23c998943224f11534d3975e41761b1a73bed7b0748868b32b32a9f4e1",
    "markets.csv": "6dcc3975372b03586ea503caa0875f127df694ad5f2b6e8d273a5db55dd4223b",
    "climate_weather.db": "96d26e1966adb7bcb1d1381a148aabf498c5f7e33bf2ac50cb90b67429f639d8",
    "events.jsonl": "03cb17971bb7b3027df6eafb5c3d3d61cae8f76a48866392fb0597da62924610",
}
SCENARIOS = {
    "FAVORABLE_1C": 0.01,
    "BASE_3C": 0.03,
    "SEVERE_5C": 0.05,
}
PRIMARY = {
    "split": "TEST",
    "lead_days": 1,
    "local_entry_hour": 15,
    "max_price_delay_seconds": 300,
    "probability_min": 0.30,
    "net_edge_min": 0.05,
    "price_min": 0.10,
    "price_max": 0.75,
    "stake_usdc": 25.0,
    "initial_capital_usdc": 1_000.0,
    "max_open_positions": 10,
    "max_open_exposure_usdc": 250.0,
    "train_comparable_min": 30,
    "train_match_rate_min": 0.98,
    "sample_grade": "STRONG",
}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def iso_utc(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str] | None = None) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        if not fields:
            return
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def verify_inputs(research: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, expected in EXPECTED_HASHES.items():
        path = research / name
        actual = sha256_path(path)
        result[name] = {"expected": expected, "actual": actual, "match": actual == expected}
        if actual != expected:
            raise RuntimeError(f"Input congelado cambió: {path} ({actual} != {expected})")
    return result


def _readonly_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def _entry_at(market_date: str, timezone_name: str) -> datetime:
    target = date.fromisoformat(market_date) - timedelta(days=1)
    local = datetime.combine(target, day_time(PRIMARY["local_entry_hour"]), ZoneInfo(timezone_name))
    return local.astimezone(timezone.utc)


def build_candidates(research: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    gates = {row["station_id"]: row for row in read_csv(research / "station_train_quality_gate.csv")}
    grades = {
        (row["station_id"], row["market_type"]): row["sample_grade"]
        for row in read_csv(research / "climate_forecastability_scorecard.csv")
    }
    markets = {
        row["slug"]: row
        for row in read_csv(research / "markets.csv")
        if row["split"] == PRIMARY["split"]
    }
    with _readonly_connection(research / "climate_weather.db") as db:
        timezones = {row[0]: row[1] for row in db.execute("SELECT station_id,timezone FROM station_metadata")}

    counters: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []
    for prediction in read_csv(research / "test_selected_event_predictions.csv"):
        counters["prediction_rows"] += 1
        if int(prediction["lead"]) != PRIMARY["lead_days"]:
            counters["excluded_not_d1"] += 1
            continue
        market = markets.get(prediction["slug"])
        if market is None:
            counters["excluded_market_missing"] += 1
            continue
        gate = gates.get(prediction["station_id"])
        if (
            gate is None
            or int(gate["train_comparable"]) < PRIMARY["train_comparable_min"]
            or float(gate["train_match_rate"]) < PRIMARY["train_match_rate_min"]
            or str(gate["eligible"]).lower() != "true"
        ):
            counters["excluded_station_gate"] += 1
            continue
        if grades.get((prediction["station_id"], prediction["market_type"])) != PRIMARY["sample_grade"]:
            counters["excluded_sample_grade"] += 1
            continue
        timezone_name = timezones.get(prediction["station_id"])
        if not timezone_name:
            counters["excluded_timezone"] += 1
            continue
        probabilities = json.loads(prediction["probabilities_json"])
        predicted_bucket, model_probability = max(probabilities.items(), key=lambda item: (float(item[1]), item[0]))
        buckets = json.loads(market["buckets_json"])
        bucket = next((row for row in buckets if row.get("label") == predicted_bucket), None)
        if not bucket or not bucket.get("yes_token"):
            counters["excluded_token"] += 1
            continue
        entry_at = _entry_at(market["market_date"], timezone_name)
        candidates.append({
            "slug": prediction["slug"],
            "event_id": market["event_id"],
            "city": prediction["city"],
            "station_id": prediction["station_id"],
            "market_date": market["market_date"],
            "market_type": prediction["market_type"],
            "unit": prediction["unit"],
            "timezone": timezone_name,
            "entry_at": iso_utc(entry_at),
            "entry_ts": int(entry_at.timestamp()),
            "predicted_bucket": predicted_bucket,
            "winner_bucket": market["winner_bucket"],
            "outcome": int(predicted_bucket == market["winner_bucket"]),
            "model_probability": float(model_probability),
            "yes_token": str(bucket["yes_token"]),
            "market_id": str(bucket.get("market_id") or ""),
            "train_comparable": int(gate["train_comparable"]),
            "train_match_rate": float(gate["train_match_rate"]),
        })
        counters["eligible_candidates"] += 1
    candidates.sort(key=lambda row: (row["entry_at"], row["slug"]))
    return candidates, dict(counters)


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


def load_contract_metadata(research: Path, output: Path, candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    cache = output / "contract_metadata.json"
    tokens = {row["yes_token"] for row in candidates}
    if cache.exists():
        payload = json.loads(cache.read_text(encoding="utf-8"))
        if payload.get("events_sha256") == EXPECTED_HASHES["events.jsonl"] and set(payload.get("contracts", {})) >= tokens:
            return payload["contracts"]

    slugs = {row["slug"] for row in candidates}
    contracts: dict[str, dict[str, Any]] = {}
    with (research / "events.jsonl").open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            event = json.loads(line)
            if str(event.get("slug") or "") not in slugs:
                continue
            for market in event.get("markets") or []:
                ids = _json_list(market.get("clobTokenIds"))
                if not ids:
                    continue
                token = str(ids[0])
                if token not in tokens:
                    continue
                contracts[token] = {
                    "slug": event.get("slug"),
                    "market_id": str(market.get("id") or ""),
                    "fees_enabled": market.get("feesEnabled"),
                    "fee_schedule": market.get("feeSchedule"),
                    "tick_size": market.get("orderPriceMinTickSize"),
                    "minimum_order_size": market.get("orderMinSize"),
                    "accepting_orders_at": market.get("acceptingOrdersTimestamp"),
                    "closed_at": market.get("closedTime") or event.get("closedTime"),
                    "enable_order_book": market.get("enableOrderBook"),
                }
    write_json(cache, {
        "schema": SCHEMA,
        "generated_at": iso_utc(),
        "events_sha256": EXPECTED_HASHES["events.jsonl"],
        "requested_tokens": len(tokens),
        "contracts": contracts,
    })
    return contracts


def init_cache(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            token TEXT PRIMARY KEY,
            slug TEXT NOT NULL,
            entry_at TEXT NOT NULL,
            start_ts INTEGER NOT NULL,
            end_ts INTEGER NOT NULL,
            fetched_at TEXT NOT NULL,
            status TEXT NOT NULL,
            points INTEGER NOT NULL,
            history_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            elapsed_seconds REAL NOT NULL,
            error TEXT
        )
    """)
    db.commit()
    return db


def _fetch_history(candidate: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
    start = time.perf_counter()
    start_ts = int(candidate["entry_ts"])
    end_ts = start_ts + 70 * 60
    query = urllib.parse.urlencode({
        "market": candidate["yes_token"],
        "startTs": start_ts,
        "endTs": end_ts,
        "fidelity": 1,
    })
    url = f"{CLOB_HISTORY}?{query}"
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "PolymarketClimateBacktestV003/1.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            history = payload.get("history")
            if not isinstance(history, list):
                raise ValueError("Respuesta sin history")
            clean = [
                {"t": int(row["t"]), "p": float(row["p"])}
                for row in history
                if isinstance(row, dict) and "t" in row and "p" in row
            ]
            clean.sort(key=lambda row: row["t"])
            serialized = stable_json(clean)
            return {
                "token": candidate["yes_token"], "slug": candidate["slug"],
                "entry_at": candidate["entry_at"], "start_ts": start_ts, "end_ts": end_ts,
                "fetched_at": iso_utc(), "status": "OK" if clean else "EMPTY", "points": len(clean),
                "history_json": serialized, "payload_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
                "elapsed_seconds": time.perf_counter() - start, "error": None,
            }
        except Exception as exc:  # network errors are preserved in the audit cache
            last_error = exc
            if attempt < 3:
                time.sleep(0.35 * (2**attempt))
    serialized = "[]"
    return {
        "token": candidate["yes_token"], "slug": candidate["slug"],
        "entry_at": candidate["entry_at"], "start_ts": start_ts, "end_ts": end_ts,
        "fetched_at": iso_utc(), "status": "ERROR", "points": 0,
        "history_json": serialized, "payload_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "elapsed_seconds": time.perf_counter() - start,
        "error": f"{type(last_error).__name__}: {last_error}",
    }


def collect_histories(
    db: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    workers: int,
) -> dict[str, Any]:
    requested_tokens = {row["yes_token"] for row in candidates}
    cached = {
        row[0]: row[1]
        for row in db.execute("SELECT token,status FROM price_history")
        if row[0] in requested_tokens and row[1] in {"OK", "EMPTY"}
    }
    pending = [row for row in candidates if row["yes_token"] not in cached]
    started = time.perf_counter()
    status = Counter(cached.values())
    elapsed_requests: list[float] = []
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {executor.submit(_fetch_history, row): row["yes_token"] for row in pending}
            for number, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                db.execute("""
                    INSERT INTO price_history(
                        token,slug,entry_at,start_ts,end_ts,fetched_at,status,points,
                        history_json,payload_sha256,elapsed_seconds,error
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(token) DO UPDATE SET
                        slug=excluded.slug,entry_at=excluded.entry_at,start_ts=excluded.start_ts,
                        end_ts=excluded.end_ts,fetched_at=excluded.fetched_at,status=excluded.status,
                        points=excluded.points,history_json=excluded.history_json,
                        payload_sha256=excluded.payload_sha256,elapsed_seconds=excluded.elapsed_seconds,
                        error=excluded.error
                """, (
                    result["token"], result["slug"], result["entry_at"], result["start_ts"],
                    result["end_ts"], result["fetched_at"], result["status"], result["points"],
                    result["history_json"], result["payload_sha256"], result["elapsed_seconds"], result["error"],
                ))
                status[result["status"]] += 1
                elapsed_requests.append(float(result["elapsed_seconds"]))
                if number % 100 == 0:
                    db.commit()
                    print(f"price-history {number}/{len(pending)}", flush=True)
        db.commit()
    return {
        "candidates": len(candidates),
        "already_cached": len(candidates) - len(pending),
        "requested": len(pending),
        "status": dict(status),
        "wall_seconds": time.perf_counter() - started,
        "mean_request_seconds": statistics.fmean(elapsed_requests) if elapsed_requests else 0.0,
        "max_request_seconds": max(elapsed_requests, default=0.0),
    }


def fee_per_share(price: float, contract: dict[str, Any]) -> float | None:
    if contract.get("fees_enabled") is False:
        return 0.0
    schedule = contract.get("fee_schedule")
    if contract.get("fees_enabled") is not True or not isinstance(schedule, dict):
        return None
    try:
        rate = float(schedule["rate"])
        exponent = float(schedule.get("exponent", 1.0))
    except (KeyError, TypeError, ValueError):
        return None
    if rate < 0 or exponent <= 0 or schedule.get("takerOnly") is not True:
        return None
    return rate * ((price * (1.0 - price)) ** exponent)


def round_up_tick(value: float, tick: float) -> float:
    return min(0.99, math.ceil((value - 1e-12) / tick) * tick)


def _parse_datetime(value: Any, fallback: datetime) -> datetime:
    if not value:
        return fallback
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return fallback


def prepare_replay_rows(
    db: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    contracts: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cache = {
        row["token"]: dict(row)
        for row in db.execute("SELECT * FROM price_history")
    }
    audit: list[dict[str, Any]] = []
    covered: list[dict[str, Any]] = []
    for candidate in candidates:
        reasons: list[str] = []
        history_row = cache.get(candidate["yes_token"])
        contract = contracts.get(candidate["yes_token"])
        if not history_row:
            reasons.append("NO_HISTORY_REQUEST")
        elif history_row["status"] != "OK":
            reasons.append(f"HISTORY_{history_row['status']}")
        if not contract:
            reasons.append("CONTRACT_MISSING")
        elif contract.get("enable_order_book") is not True:
            reasons.append("ORDERBOOK_DISABLED")

        point: dict[str, Any] | None = None
        if history_row and history_row["status"] == "OK":
            history = json.loads(history_row["history_json"])
            point = next(
                (
                    row for row in history
                    if candidate["entry_ts"] <= int(row["t"])
                    <= candidate["entry_ts"] + PRIMARY["max_price_delay_seconds"]
                ),
                None,
            )
            if point is None:
                reasons.append("NO_PRICE_WITHIN_5M")
        if point is not None and not (0.0 < float(point["p"]) < 1.0):
            reasons.append("INVALID_REFERENCE_PRICE")
        if contract and fee_per_share(0.5, contract) is None:
            reasons.append("FEE_UNVERIFIED")
        try:
            tick = float(contract.get("tick_size")) if contract else 0.0
        except (TypeError, ValueError):
            tick = 0.0
        if not 0.0 < tick <= 0.1:
            reasons.append("INVALID_TICK")

        base = {
            **candidate,
            "history_status": history_row["status"] if history_row else None,
            "history_points": int(history_row["points"]) if history_row else 0,
            "contract_available": bool(contract),
            "audit_status": "ELIGIBLE" if not reasons else "EXCLUDED",
            "audit_reasons": ",".join(reasons),
        }
        if reasons:
            audit.append(base)
            continue
        assert point is not None and contract is not None
        fallback_close = datetime.fromisoformat(candidate["market_date"]).replace(tzinfo=timezone.utc) + timedelta(days=2)
        close_at = _parse_datetime(contract.get("closed_at"), fallback_close)
        row = {
            **base,
            "reference_ts": int(point["t"]),
            "reference_at": iso_utc(datetime.fromtimestamp(int(point["t"]), timezone.utc)),
            "price_delay_seconds": int(point["t"]) - int(candidate["entry_ts"]),
            "reference_price": float(point["p"]),
            "tick_size": tick,
            "minimum_order_size": float(contract.get("minimum_order_size") or 0.0),
            "fee_schedule": contract.get("fee_schedule"),
            "fees_enabled": contract.get("fees_enabled"),
            "closed_at": iso_utc(close_at),
            "model_brier_binary": (float(candidate["model_probability"]) - int(candidate["outcome"])) ** 2,
            "market_brier_binary": (float(point["p"]) - int(candidate["outcome"])) ** 2,
        }
        covered.append(row)
        audit.append(row)
    return covered, audit


def scenario_candidates(
    covered: list[dict[str, Any]],
    shift: float,
    *,
    probability_min: float = PRIMARY["probability_min"],
    net_edge_min: float = PRIMARY["net_edge_min"],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in covered:
        entry_price = round_up_tick(float(row["reference_price"]) + shift, float(row["tick_size"]))
        fee = fee_per_share(entry_price, {
            "fees_enabled": row["fees_enabled"],
            "fee_schedule": row["fee_schedule"],
        })
        assert fee is not None
        net_edge = float(row["model_probability"]) - entry_price - fee
        reasons = []
        if float(row["model_probability"]) < probability_min:
            reasons.append("PROBABILITY")
        if not PRIMARY["price_min"] <= entry_price <= PRIMARY["price_max"]:
            reasons.append("PRICE")
        if net_edge < net_edge_min:
            reasons.append("NET_EDGE")
        total_share_cost = entry_price + fee
        minimum_cost = float(row["minimum_order_size"]) * total_share_cost
        if minimum_cost > PRIMARY["stake_usdc"]:
            reasons.append("MINIMUM_ORDER_SIZE")
        rows.append({
            **row,
            "execution_shift": shift,
            "entry_price": entry_price,
            "fee_per_share": fee,
            "total_cost_per_share": total_share_cost,
            "net_edge": net_edge,
            "signal": not reasons,
            "signal_reasons": ",".join(reasons),
        })
    return rows


def _settle_position(position: dict[str, Any], state: dict[str, Any]) -> None:
    payout = float(position["shares"]) if int(position["outcome"]) else 0.0
    pnl = payout - float(position["stake_usdc"])
    state["cash"] += payout
    state["open_exposure"] -= float(position["stake_usdc"])
    state["realized_pnl"] += pnl
    position["payout_usdc"] = payout
    position["pnl_usdc"] = pnl
    position["settled"] = True
    equity = state["cash"] + state["open_exposure"]
    state["peak_equity"] = max(state["peak_equity"], equity)
    state["max_drawdown"] = min(state["max_drawdown"], equity - state["peak_equity"])


def hypothetical_trade(signal: dict[str, Any]) -> dict[str, Any]:
    stake = PRIMARY["stake_usdc"]
    shares = stake / float(signal["total_cost_per_share"])
    payout = shares if int(signal["outcome"]) else 0.0
    return {
        **signal,
        "stake_usdc": stake,
        "shares": shares,
        "expected_pnl_usdc": shares * float(signal["net_edge"]),
        "payout_usdc": payout,
        "pnl_usdc": payout - stake,
        "settled": True,
    }


def simulate_capital(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    signals = [dict(row) for row in rows if row["signal"]]
    signals.sort(key=lambda row: (row["entry_at"], -float(row["net_edge"]), row["slug"]))
    state = {
        "cash": PRIMARY["initial_capital_usdc"], "open_exposure": 0.0, "realized_pnl": 0.0,
        "peak_equity": PRIMARY["initial_capital_usdc"], "max_drawdown": 0.0,
    }
    open_positions: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for signal in signals:
        entry_at = datetime.fromisoformat(signal["entry_at"].replace("Z", "+00:00"))
        still_open = []
        for position in open_positions:
            close_at = datetime.fromisoformat(position["closed_at"].replace("Z", "+00:00"))
            if close_at <= entry_at:
                _settle_position(position, state)
            else:
                still_open.append(position)
        open_positions = still_open
        reason = None
        if len(open_positions) >= PRIMARY["max_open_positions"]:
            reason = "MAX_OPEN_POSITIONS"
        elif state["open_exposure"] + PRIMARY["stake_usdc"] > PRIMARY["max_open_exposure_usdc"] + 1e-9:
            reason = "MAX_OPEN_EXPOSURE"
        elif state["cash"] + 1e-9 < PRIMARY["stake_usdc"]:
            reason = "INSUFFICIENT_CASH"
        if reason:
            skipped.append({**signal, "capital_skip_reason": reason})
            continue
        stake = PRIMARY["stake_usdc"]
        shares = stake / float(signal["total_cost_per_share"])
        position = {
            **signal,
            "stake_usdc": stake,
            "shares": shares,
            "expected_pnl_usdc": shares * float(signal["net_edge"]),
            "capital_before_usdc": state["cash"] + state["open_exposure"],
            "cash_after_entry_usdc": state["cash"] - stake,
            "open_positions_after_entry": len(open_positions) + 1,
            "settled": False,
        }
        state["cash"] -= stake
        state["open_exposure"] += stake
        open_positions.append(position)
        trades.append(position)
    for position in sorted(open_positions, key=lambda row: row["closed_at"]):
        _settle_position(position, state)
    return trades, skipped, {
        "ending_capital_usdc": state["cash"],
        "realized_pnl_usdc": state["realized_pnl"],
        "max_drawdown_usdc": state["max_drawdown"],
    }


def _wilson_interval(wins: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total == 0:
        return None, None
    p = wins / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return centre - margin, centre + margin


def _daily_block_bootstrap(trades: list[dict[str, Any]], samples: int = 10_000) -> tuple[float | None, float | None]:
    if not trades:
        return None, None
    groups: defaultdict[str, float] = defaultdict(float)
    for trade in trades:
        groups[trade["market_date"]] += float(trade["pnl_usdc"])
    values = list(groups.values())
    rng = random.Random(20260903)
    totals = [sum(values[rng.randrange(len(values))] for _ in values) for _ in range(samples)]
    totals.sort()
    return totals[int(0.025 * (samples - 1))], totals[int(0.975 * (samples - 1))]


def metrics(trades: list[dict[str, Any]], skipped: list[dict[str, Any]], capital: dict[str, Any]) -> dict[str, Any]:
    pnls = [float(row["pnl_usdc"]) for row in trades]
    wins = sum(value > 0 for value in pnls)
    losses = sum(value < 0 for value in pnls)
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = -sum(value for value in pnls if value < 0)
    cost = sum(float(row["stake_usdc"]) for row in trades)
    wilson_low, wilson_high = _wilson_interval(wins, len(trades))
    boot_low, boot_high = _daily_block_bootstrap(trades)
    return {
        "signals_before_capital": len(trades) + len(skipped),
        "trades": len(trades),
        "capital_skips": len(skipped),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades) if trades else None,
        "win_rate_wilson_lower_95": wilson_low,
        "win_rate_wilson_upper_95": wilson_high,
        "cost_usdc": cost,
        "net_pnl_usdc": sum(pnls),
        "roi_on_cost": sum(pnls) / cost if cost else None,
        "mean_pnl_usdc": statistics.fmean(pnls) if pnls else None,
        "median_pnl_usdc": statistics.median(pnls) if pnls else None,
        "expected_pnl_from_model_usdc": sum(float(row["expected_pnl_usdc"]) for row in trades),
        "mean_model_probability": statistics.fmean(float(row["model_probability"]) for row in trades) if trades else None,
        "mean_reference_price": statistics.fmean(float(row["reference_price"]) for row in trades) if trades else None,
        "mean_entry_price": statistics.fmean(float(row["entry_price"]) for row in trades) if trades else None,
        "mean_net_edge": statistics.fmean(float(row["net_edge"]) for row in trades) if trades else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "max_drawdown_usdc": capital["max_drawdown_usdc"],
        "ending_capital_usdc": capital["ending_capital_usdc"],
        "daily_block_bootstrap_total_pnl_lower_95": boot_low,
        "daily_block_bootstrap_total_pnl_upper_95": boot_high,
    }


def probability_metrics(covered: list[dict[str, Any]]) -> dict[str, Any]:
    if not covered:
        return {}
    accuracy = statistics.fmean(float(row["outcome"]) for row in covered)
    model_brier = statistics.fmean(float(row["model_brier_binary"]) for row in covered)
    market_brier = statistics.fmean(float(row["market_brier_binary"]) for row in covered)
    eps = 1e-12
    model_log_loss = statistics.fmean(
        -(int(row["outcome"]) * math.log(max(eps, float(row["model_probability"])))
          + (1 - int(row["outcome"])) * math.log(max(eps, 1 - float(row["model_probability"]))))
        for row in covered
    )
    calibration = []
    for lower in [i / 10 for i in range(10)]:
        upper = lower + 0.1
        bucket = [row for row in covered if lower <= float(row["model_probability"]) < upper or (upper == 1.0 and float(row["model_probability"]) == 1.0)]
        if bucket:
            calibration.append({
                "probability_bin": f"{lower:.1f}-{upper:.1f}",
                "n": len(bucket),
                "mean_model_probability": statistics.fmean(float(row["model_probability"]) for row in bucket),
                "observed_win_rate": statistics.fmean(float(row["outcome"]) for row in bucket),
                "binary_brier": statistics.fmean(float(row["model_brier_binary"]) for row in bucket),
            })
    return {
        "covered_predictions": len(covered),
        "top_bucket_accuracy": accuracy,
        "binary_model_brier": model_brier,
        "binary_market_reference_brier": market_brier,
        "binary_brier_improvement_vs_market": market_brier - model_brier,
        "binary_model_log_loss": model_log_loss,
        "calibration": calibration,
    }


def temporal_split_overlaps(research: Path) -> list[dict[str, Any]]:
    with _readonly_connection(research / "climate_weather.db") as db:
        rows = db.execute("""
            SELECT date,GROUP_CONCAT(DISTINCT split),COUNT(*)
            FROM event_truth
            GROUP BY date
            HAVING COUNT(DISTINCT split)>1
            ORDER BY date
        """).fetchall()
    return [{"date": row[0], "splits": row[1], "events": int(row[2])} for row in rows]


def daily_rows(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[trade["market_date"]].append(trade)
    result = []
    cumulative = 0.0
    for day in sorted(grouped):
        rows = grouped[day]
        pnl = sum(float(row["pnl_usdc"]) for row in rows)
        cumulative += pnl
        result.append({
            "market_date": day,
            "trades": len(rows),
            "wins": sum(float(row["pnl_usdc"]) > 0 for row in rows),
            "pnl_usdc": pnl,
            "cumulative_pnl_usdc": cumulative,
        })
    return result


def segment_rows(trades: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[str(trade[key])].append(trade)
    result = []
    for value, rows in grouped.items():
        pnl = sum(float(row["pnl_usdc"]) for row in rows)
        cost = sum(float(row["stake_usdc"]) for row in rows)
        result.append({
            key: value,
            "trades": len(rows),
            "wins": sum(float(row["pnl_usdc"]) > 0 for row in rows),
            "win_rate": statistics.fmean(float(row["pnl_usdc"]) > 0 for row in rows),
            "pnl_usdc": pnl,
            "roi_on_cost": pnl / cost if cost else None,
            "mean_model_probability": statistics.fmean(float(row["model_probability"]) for row in rows),
            "mean_reference_price": statistics.fmean(float(row["reference_price"]) for row in rows),
            "mean_net_edge": statistics.fmean(float(row["net_edge"]) for row in rows),
        })
    return sorted(result, key=lambda row: (-int(row["trades"]), str(row[key])))


def evidence_verdict(primary_metrics: dict[str, Any]) -> tuple[str, list[str]]:
    reasons = []
    if int(primary_metrics["trades"]) < 30:
        return "MORE_DATA_REQUIRED", ["Menos de 30 operaciones"]
    if float(primary_metrics["net_pnl_usdc"]) <= 0:
        reasons.append("PnL no positivo")
    if float(primary_metrics["roi_on_cost"] or 0.0) <= 0:
        reasons.append("ROI no positivo")
    if primary_metrics["profit_factor"] is None or float(primary_metrics["profit_factor"]) < 1.25:
        reasons.append("Profit factor menor que 1.25")
    if (
        primary_metrics["daily_block_bootstrap_total_pnl_lower_95"] is None
        or float(primary_metrics["daily_block_bootstrap_total_pnl_lower_95"]) <= 0
    ):
        reasons.append("Límite bootstrap 95% no positivo")
    return ("EVIDENCE_PASS", []) if not reasons else ("EVIDENCE_FAIL", reasons)


def render_report(summary: dict[str, Any]) -> str:
    primary = summary["scenarios"]["BASE_3C"]
    probability = summary["probability_metrics"]
    coverage = summary["coverage"]
    timing = summary["timing"]
    scenario_lines = []
    for name in SCENARIOS:
        row = summary["scenarios"][name]
        pf = "n/a" if row["profit_factor"] is None else f"{row['profit_factor']:.3f}"
        scenario_lines.append(
            f"| {name} | {row['trades']} | {row['win_rate']:.1%} | {row['net_pnl_usdc']:.2f} | "
            f"{row['roi_on_cost']:.1%} | {row['unconstrained']['net_pnl_usdc']:.2f} | {pf} | {row['max_drawdown_usdc']:.2f} |"
            if row["trades"] else f"| {name} | 0 | n/a | 0.00 | n/a | 0.00 | n/a | 0.00 |"
        )
    fixed_lines = [
        f"| {name} | {row['trades']} | {row['net_pnl_usdc']:.2f} | {row['roi_on_cost']:.2%} |"
        for name, row in summary["fixed_primary_cohort_stress"].items()
    ]
    type_lines = [
        f"| {row['market_type']} | {row['trades']} | {row['win_rate']:.1%} | {row['pnl_usdc']:.2f} | {row['roi_on_cost']:.1%} |"
        for row in summary["segments_market_type"]
    ]
    verdict_reasons = "; ".join(summary["verdict_reasons"]) or "Todas las puertas prerregistradas se cumplieron."
    strict = summary["temporal_boundary_audit"]["base_after_purge"]
    overlap_text = ", ".join(
        f"{row['date']} ({row['splits']})" for row in summary["temporal_boundary_audit"]["overlaps"]
    )
    exploratory = summary["exploratory_sensitivity_audit"]
    first_run = summary.get("first_full_run_timing", {})
    first_run_line = (
        f"- Primera ejecución completa, incluida la descarga: {first_run['total_seconds']:.2f} s "
        f"({first_run['total_seconds']/60:.2f} min).\n"
        if first_run.get("total_seconds") is not None else ""
    )
    return f"""# BACKTEST AUTOMÁTICO REALISTA DE CLIMA — V003

Generado: {summary['generated_at']}

## Veredicto

**{summary['verdict']}** — {verdict_reasons}

El escenario primario usa el precio histórico oficial observado más tres centavos,
comisión taker y capital limitado. Es un replay `L1_PRICE_PROXY`: no equivale a un
fill probado porque Polymarket no entrega el libro L2 histórico para esos instantes.

## Resultado primario — BASE_3C

- Operaciones: {primary['trades']} de {primary['signals_before_capital']} señales.
- Ganadas/perdidas: {primary['wins']}/{primary['losses']}.
- Win rate: {primary['win_rate']:.2%}.
- PnL neto: {primary['net_pnl_usdc']:.2f} USDC.
- ROI sobre coste: {primary['roi_on_cost']:.2%}.
- Profit factor: {primary['profit_factor'] if primary['profit_factor'] is not None else 'n/a'}.
- Drawdown máximo: {primary['max_drawdown_usdc']:.2f} USDC.
- Capital final desde 1,000 USDC: {primary['ending_capital_usdc']:.2f} USDC.
- PnL esperado por las probabilidades del modelo: {primary['expected_pnl_from_model_usdc']:.2f} USDC.
- PnL realmente observado: {primary['net_pnl_usdc']:.2f} USDC.
- Bootstrap diario 95% del PnL total: [{primary['daily_block_bootstrap_total_pnl_lower_95']:.2f}, {primary['daily_block_bootstrap_total_pnl_upper_95']:.2f}] USDC.

## Estrés de ejecución

| Escenario | Trades | Win rate | PnL con capital | ROI | PnL todas las señales | Profit factor | Drawdown |
|---|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(scenario_lines)}

Los escenarios anteriores pueden escoger señales diferentes porque el coste cambia
la puerta de edge. Para aislar únicamente el coste, se mantuvieron fijas las 130
operaciones del escenario base:

| Coste aplicado a la misma cohorte | Trades | PnL USDC | ROI |
|---|---:|---:|---:|
{chr(10).join(fixed_lines)}

## Resultado por tipo de mercado

| Tipo | Trades | Win rate | PnL USDC | ROI |
|---|---:|---:|---:|---:|
{chr(10).join(type_lines)}

## Auditoría de frontera temporal

Se detectaron fechas presentes en dos particiones: {overlap_text}. El resultado
prerregistrado conserva la partición original; como prueba de robustez se eliminó
del TEST la fecha 2026-08-07 compartida con VALIDATION:

- Operaciones estrictas: {strict['trades']}.
- PnL estricto: {strict['net_pnl_usdc']:.2f} USDC.
- ROI estricto: {strict['roi_on_cost']:.2%}.
- Veredicto estricto: {summary['temporal_boundary_audit']['verdict_after_purge']}.

La corrección mejora el PnL al retirar tres pérdidas, pero no revierte la conclusión.

## Sensibilidad exploratoria

Se evaluaron {exploratory['configurations']} combinaciones poshoc de probabilidad y
edge. Configuraciones que superaron todas las puertas confirmatorias: {exploratory['evidence_passes']}.
El mejor PnL exploratorio fue {exploratory['best_pnl_usdc']:.2f} USDC con
probabilidad mínima {exploratory['best_probability_min']:.2f} y edge mínimo
{exploratory['best_net_edge_min']:.2f}; su profit factor fue
{exploratory['best_profit_factor']:.3f} y su bootstrap inferior
{exploratory['best_bootstrap_lower_95']:.2f} USDC, por lo que tampoco constituye
evidencia confirmatoria.

## Calidad predictiva

- Predicciones D+1 elegibles: {coverage['eligible_candidates']}.
- Con precio dentro de cinco minutos: {coverage['covered_candidates']} ({coverage['price_coverage']:.2%}).
- Accuracy de la cubeta superior en la muestra cubierta: {probability['top_bucket_accuracy']:.2%}.
- Brier binario del modelo para la cubeta elegida: {probability['binary_model_brier']:.4f}.
- Brier del precio histórico para esa misma cubeta: {probability['binary_market_reference_brier']:.4f}.
- Mejora Brier del modelo sobre el precio (positivo favorece al modelo): {probability['binary_brier_improvement_vs_market']:.4f}.

## Integridad temporal

- TRAIN: 2025-12-30 a 2026-07-03.
- VALIDATION: 2026-07-03 a 2026-08-07.
- TEST usado aquí: 2026-08-07 a 2026-09-01.
- La predicción es D+1 y la referencia se toma a las 15:00 locales del día anterior.
- Retraso mediano del punto histórico: {coverage['median_price_delay_seconds']:.0f} segundos.
- No se usó volumen final como filtro de entrada, porque habría introducido datos futuros.

## Lo que este resultado sí y no demuestra

Sí compara una regla congelada contra precios históricos timestamped, resolución,
fees, tick, capital, concurrencia y tres costes de ejecución. No demuestra que una
orden real hubiera tenido spread, profundidad o fill suficientes. Por ello ningún
resultado de esta V003 autoriza dinero real por sí solo; debe sobrevivir el forward
V002 con libros capturados en vivo.

El conjunto TEST meteorológico ya se había examinado en V001. La regla económica
V003 fue congelada antes de descargar estos precios, pero el estudio no es una
réplica ciega integral. La sensibilidad incluida es exploratoria y no cambia el
veredicto primario.

## Tiempo del proceso

{first_run_line}- Reejecución reproducible usando caché: {timing['total_seconds']:.2f} s ({timing['total_seconds']/60:.2f} min).
- Verificación de hashes: {timing['verify_inputs_seconds']:.2f} s.
- Construcción del universo: {timing['build_candidates_seconds']:.2f} s.
- Lectura de contratos: {timing['contract_metadata_seconds']:.2f} s.
- Descarga/reuso de precios: {timing['price_collection_seconds']:.2f} s.
- Evaluación y reportes: {timing['evaluation_seconds']:.2f} s.
"""


def evaluate(
    db: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    contracts: dict[str, dict[str, Any]],
    output: Path,
    timing: dict[str, float],
    counters: dict[str, int],
    collection: dict[str, Any],
    input_integrity: dict[str, Any],
    overlaps: list[dict[str, Any]],
    first_full_run_timing: dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    covered, audit = prepare_replay_rows(db, candidates, contracts)
    scenario_metrics: dict[str, Any] = {}
    scenario_trades: dict[str, list[dict[str, Any]]] = {}
    scenario_prepared: dict[str, list[dict[str, Any]]] = {}
    for name, shift in SCENARIOS.items():
        prepared = scenario_candidates(covered, shift)
        scenario_prepared[name] = prepared
        trades, skipped, capital = simulate_capital(prepared)
        result = metrics(trades, skipped, capital)
        all_signals = [hypothetical_trade(row) for row in prepared if row["signal"]]
        unconstrained_capital = {
            "ending_capital_usdc": PRIMARY["initial_capital_usdc"] + sum(float(row["pnl_usdc"]) for row in all_signals),
            "max_drawdown_usdc": 0.0,
        }
        result["unconstrained"] = metrics(all_signals, [], unconstrained_capital)
        result["execution_shift"] = shift
        scenario_metrics[name] = result
        scenario_trades[name] = trades
        write_csv(output / f"trades_{name.lower()}.csv", trades)
        write_csv(output / f"signals_{name.lower()}.csv", all_signals)

    primary_slugs = {row["slug"] for row in scenario_trades["BASE_3C"]}
    fixed_primary_cohort_stress: dict[str, Any] = {}
    for name, prepared in scenario_prepared.items():
        fixed = [hypothetical_trade(row) for row in prepared if row["slug"] in primary_slugs]
        fixed_capital = {
            "ending_capital_usdc": PRIMARY["initial_capital_usdc"] + sum(float(row["pnl_usdc"]) for row in fixed),
            "max_drawdown_usdc": 0.0,
        }
        fixed_primary_cohort_stress[name] = metrics(fixed, [], fixed_capital)

    sensitivity = []
    for probability_min in (0.25, 0.30, 0.35, 0.40):
        for edge_min in (0.03, 0.05, 0.07, 0.10):
            prepared = scenario_candidates(
                covered, SCENARIOS["BASE_3C"],
                probability_min=probability_min, net_edge_min=edge_min,
            )
            trades, skipped, capital = simulate_capital(prepared)
            row = metrics(trades, skipped, capital)
            row_verdict, row_reasons = evidence_verdict(row)
            sensitivity.append({
                "probability_min": probability_min,
                "net_edge_min": edge_min,
                "exploratory_verdict": row_verdict,
                "exploratory_reasons": "; ".join(row_reasons),
                **row,
            })

    primary = scenario_metrics["BASE_3C"]
    overlap_test_dates = {
        row["date"] for row in overlaps
        if "TEST" in row["splits"] and "VALIDATION" in row["splits"]
    }
    strict_prepared = [
        row for row in scenario_prepared["BASE_3C"]
        if row["market_date"] not in overlap_test_dates
    ]
    strict_trades, strict_skipped, strict_capital = simulate_capital(strict_prepared)
    strict_metrics = metrics(strict_trades, strict_skipped, strict_capital)
    strict_verdict, strict_reasons = evidence_verdict(strict_metrics)
    best_sensitivity = max(sensitivity, key=lambda row: float(row["net_pnl_usdc"]))
    sensitivity_audit = {
        "configurations": len(sensitivity),
        "evidence_passes": sum(row["exploratory_verdict"] == "EVIDENCE_PASS" for row in sensitivity),
        "best_pnl_usdc": best_sensitivity["net_pnl_usdc"],
        "best_probability_min": best_sensitivity["probability_min"],
        "best_net_edge_min": best_sensitivity["net_edge_min"],
        "best_profit_factor": best_sensitivity["profit_factor"],
        "best_bootstrap_lower_95": best_sensitivity["daily_block_bootstrap_total_pnl_lower_95"],
    }
    segments_market_type = segment_rows(scenario_trades["BASE_3C"], "market_type")
    segments_station = segment_rows(scenario_trades["BASE_3C"], "station_id")
    verdict, verdict_reasons = evidence_verdict(primary)
    delays = [int(row["price_delay_seconds"]) for row in covered]
    coverage = {
        "eligible_candidates": len(candidates),
        "covered_candidates": len(covered),
        "price_coverage": len(covered) / len(candidates) if candidates else 0.0,
        "excluded_candidates": len(candidates) - len(covered),
        "audit_reasons": dict(Counter(reason for row in audit for reason in str(row["audit_reasons"]).split(",") if reason)),
        "median_price_delay_seconds": statistics.median(delays) if delays else None,
        "p95_price_delay_seconds": sorted(delays)[int(0.95 * (len(delays) - 1))] if delays else None,
    }
    timing["evaluation_seconds"] = time.perf_counter() - started
    summary = {
        "schema": SCHEMA,
        "generated_at": iso_utc(),
        "evidence_grade": "L1_PRICE_PROXY",
        "preregistration": str(PREREGISTRATION),
        "primary_scenario": "BASE_3C",
        "primary_rules": PRIMARY,
        "verdict": verdict,
        "verdict_reasons": verdict_reasons,
        "input_integrity": input_integrity,
        "first_full_run_timing": first_full_run_timing,
        "candidate_build": counters,
        "collection": collection,
        "coverage": coverage,
        "probability_metrics": probability_metrics(covered),
        "scenarios": scenario_metrics,
        "fixed_primary_cohort_stress": fixed_primary_cohort_stress,
        "segments_market_type": segments_market_type,
        "segments_station": segments_station,
        "sensitivity": sensitivity,
        "exploratory_sensitivity_audit": sensitivity_audit,
        "temporal_boundary_audit": {
            "overlaps": overlaps,
            "test_dates_purged": sorted(overlap_test_dates),
            "base_after_purge": strict_metrics,
            "verdict_after_purge": strict_verdict,
            "verdict_reasons_after_purge": strict_reasons,
        },
        "limitations": [
            "Historical prices are timestamped L1 reference values, not executable historical asks.",
            "No historical spread, depth, queue position or partial fills are available.",
            "Meteorological TEST outcomes had already been inspected in V001.",
            "Sensitivity results are post-hoc exploratory.",
        ],
        "timing": timing,
    }
    write_csv(output / "candidate_audit.csv", audit)
    write_csv(output / "scenario_metrics.csv", [
        {
            "scenario": name,
            **{key: value for key, value in row.items() if key != "unconstrained"},
            **{f"unconstrained_{key}": value for key, value in row["unconstrained"].items()},
        }
        for name, row in scenario_metrics.items()
    ])
    write_csv(output / "sensitivity_exploratory.csv", sensitivity)
    write_csv(output / "calibration.csv", summary["probability_metrics"].get("calibration", []))
    write_csv(output / "daily_primary.csv", daily_rows(scenario_trades["BASE_3C"]))
    write_csv(output / "segments_market_type.csv", segments_market_type)
    write_csv(output / "segments_station.csv", segments_station)
    return summary


def build_manifest(output: Path, files: list[Path]) -> None:
    write_json(output / "manifest.json", {
        "schema": SCHEMA,
        "generated_at": iso_utc(),
        "files": [
            {"path": path.name, "bytes": path.stat().st_size, "sha256": sha256_path(path)}
            for path in files if path.exists()
        ],
    })


def run(research: Path, output: Path, workers: int, max_markets: int | None = None) -> dict[str, Any]:
    total_started = time.perf_counter()
    output.mkdir(parents=True, exist_ok=True)
    timing: dict[str, float] = {}

    started = time.perf_counter()
    integrity = verify_inputs(research)
    timing["verify_inputs_seconds"] = time.perf_counter() - started

    started = time.perf_counter()
    candidates, counters = build_candidates(research)
    overlaps = temporal_split_overlaps(research)
    if max_markets is not None:
        candidates = candidates[:max_markets]
        counters["debug_max_markets"] = max_markets
    timing["build_candidates_seconds"] = time.perf_counter() - started

    started = time.perf_counter()
    contracts = load_contract_metadata(research, output, candidates)
    timing["contract_metadata_seconds"] = time.perf_counter() - started

    with init_cache(output / "price_history_cache.db") as db:
        collection = collect_histories(db, candidates, workers)
        timing["price_collection_seconds"] = collection["wall_seconds"]
        timing_path = output / "first_full_run_timing.json"
        first_full_run_timing = json.loads(timing_path.read_text(encoding="utf-8")) if timing_path.exists() else {}
        summary = evaluate(
            db, candidates, contracts, output, timing, counters, collection, integrity,
            overlaps, first_full_run_timing,
        )

    timing["total_seconds"] = time.perf_counter() - total_started
    summary["timing"] = timing
    summary_path = output / "backtest_summary.json"
    report_path = output / "BACKTEST_AUTOMATICO_REALISTA.md"
    write_json(summary_path, summary)
    report_path.write_text(render_report(summary), encoding="utf-8")
    build_manifest(output, [
        summary_path, report_path, output / "candidate_audit.csv", output / "scenario_metrics.csv",
        output / "sensitivity_exploratory.csv", output / "calibration.csv", output / "daily_primary.csv",
        output / "segments_market_type.csv", output / "segments_station.csv",
        output / "trades_favorable_1c.csv", output / "trades_base_3c.csv", output / "trades_severe_5c.csv",
        output / "signals_favorable_1c.csv", output / "signals_base_3c.csv", output / "signals_severe_5c.csv",
        output / "contract_metadata.json", output / "price_history_cache.db", output / "first_full_run_timing.json",
    ])
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest climático automático con replay de precios históricos")
    parser.add_argument("--research", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--max-markets", type=int)
    args = parser.parse_args(argv)
    summary = run(args.research, args.output, args.workers, args.max_markets)
    primary = summary["scenarios"]["BASE_3C"]
    print(json.dumps({
        "verdict": summary["verdict"],
        "evidence_grade": summary["evidence_grade"],
        "covered": summary["coverage"]["covered_candidates"],
        "trades": primary["trades"],
        "pnl_usdc": primary["net_pnl_usdc"],
        "roi": primary["roi_on_cost"],
        "total_seconds": summary["timing"]["total_seconds"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
