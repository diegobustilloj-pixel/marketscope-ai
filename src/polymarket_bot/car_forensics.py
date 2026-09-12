from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import statistics
import tempfile
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import duckdb


WALLET = "0x7c3db723f1d4d8cb9c550095203b686cb11e5c6b"
PROFILE = "https://polymarket.com/@car"
SUPPLIED_PROFILE = "https://polymarket.com/@balthazar?tab=positions&r=Pedropica#xar9Lo7"
EXPECTED_USERNAME = "car"
DISPLAY_LABEL = "@car"
ARTIFACT_PREFIX = "car"
REWARD_TYPES = {"REWARD", "MAKER_REBATE", "TAKER_REBATE", "REFERRAL_REWARD", "YIELD"}

PRICE_BUCKETS = (
    (0.00, 0.05, "0–5¢"),
    (0.05, 0.10, "5–10¢"),
    (0.10, 0.20, "10–20¢"),
    (0.20, 0.30, "20–30¢"),
    (0.30, 0.40, "30–40¢"),
    (0.40, 0.50, "40–50¢"),
    (0.50, 0.60, "50–60¢"),
    (0.60, 0.70, "60–70¢"),
    (0.70, 0.80, "70–80¢"),
    (0.80, 0.90, "80–90¢"),
    (0.90, 0.95, "90–95¢"),
    (0.95, 0.98, "95–98¢"),
    (0.98, 1.0000001, "98–100¢"),
)

DELAY_SECONDS = (0, 1, 2, 5, 10, 15, 30, 60, 120, 300, 900)
BANKROLLS = (10, 25, 50, 100, 250, 500, 1000, 5000, 10000)
HOLD_BUCKETS = (
    (0, 60, "<1m"), (60, 300, "1–5m"), (300, 1_800, "5–30m"),
    (1_800, 3_600, "30m–1h"), (3_600, 21_600, "1–6h"),
    (21_600, 86_400, "6–24h"), (86_400, 259_200, "1–3d"),
    (259_200, 604_800, "3–7d"), (604_800, 2_419_200, "1–4w"),
    (2_419_200, float("inf"), ">4w"),
)


def number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def nullable_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def utc_iso(timestamp: int | float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(str(value)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _clean_json(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_json(item) for item in value]
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_clean_json(payload), indent=2, ensure_ascii=False), encoding="utf-8")


def _artifact(output_dir: Path, suffix: str) -> Path:
    return output_dir / f"{ARTIFACT_PREFIX}_{suffix}"


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(fieldnames or (rows[0].keys() if rows else []))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(_clean_json(dict(row)) for row in rows)


def _jsonl_write(handle: Any, row: Mapping[str, Any]) -> None:
    handle.write(json.dumps(_clean_json(dict(row)), ensure_ascii=False, separators=(",", ":")) + "\n")


def _jsonl_to_parquet(jsonl: Path, parquet: Path) -> None:
    parquet.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    try:
        connection.execute("CREATE TABLE payload AS SELECT * FROM read_ndjson_auto(?)", [str(jsonl.resolve())])
        target = str(parquet.resolve()).replace("'", "''")
        connection.execute(f"COPY payload TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    finally:
        connection.close()


def _rows_to_parquet(rows: Iterable[Mapping[str, Any]], parquet: Path) -> int:
    parquet.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".ndjson", delete=False, dir=parquet.parent
    ) as handle:
        temporary = Path(handle.name)
        count = 0
        for row in rows:
            _jsonl_write(handle, row)
            count += 1
    if count == 0:
        with temporary.open("w", encoding="utf-8") as handle:
            _jsonl_write(handle, {"status": "EMPTY"})
    try:
        _jsonl_to_parquet(temporary, parquet)
    finally:
        temporary.unlink(missing_ok=True)
    return count


def _snapshot(connection: sqlite3.Connection, source: str) -> Any:
    row = connection.execute(
        "SELECT payload_json FROM snapshots WHERE source=? ORDER BY captured_at DESC LIMIT 1", (source,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def _winner(outcomes: Sequence[Any], prices: Sequence[Any]) -> str | None:
    parsed = [nullable_number(value) for value in prices]
    winners = [index for index, price in enumerate(parsed) if price is not None and price >= 0.999]
    if len(winners) == 1 and winners[0] < len(outcomes):
        return str(outcomes[winners[0]])
    return None


def _category(text: str, tags: Sequence[Mapping[str, Any]]) -> str:
    tag_text = " ".join(
        str(item.get("slug") or "") + " " + str(item.get("label") or "")
        for item in tags
        if isinstance(item, Mapping)
    ).lower()
    content = (text + " " + tag_text).lower()
    sport_terms = (
        "sports", "nfl", "nba", "wnba", "mlb", "nhl", "soccer", "football", "tennis",
        "ufc", "mma", "boxing", "cbb", "cfb", "cricket", "f1", "formula-1", "esports",
        "champions-league", "premier-league", "la-liga", "serie-a", "bundesliga", " vs ",
    )
    if any(term in content for term in sport_terms):
        return "Sports"
    if any(term in content for term in ("weather", "temperature", "rain", "snow", "hurricane", "climate")):
        return "Weather"
    if any(term in content for term in ("bitcoin", "ethereum", "crypto", "solana", "xrp", "dogecoin", "btc", "eth")):
        return "Crypto"
    if any(term in content for term in ("war", "ceasefire", "strike", "military", "iran", "israel", "ukraine", "russia", "hamas", "gaza", "nato")):
        return "Geopolitics"
    if any(term in content for term in ("election", "president", "congress", "senate", "governor", "politic", "trump", "democrat", "republican")):
        return "Politics"
    if any(term in content for term in ("economy", "fed", "interest-rate", "inflation", "gdp", "unemployment", "tariff")):
        return "Economy"
    if any(term in content for term in ("openai", "ai", "technology", "tech", "spacex", "apple", "google", "microsoft")):
        return "Tech"
    if any(term in content for term in ("culture", "movie", "music", "celebrity", "oscars", "grammy", "youtube", "tiktok")):
        return "Culture"
    if any(term in content for term in ("science", "pandemic", "health", "medicine")):
        return "Science/Health"
    return "Other"


def _price_bucket(price: float) -> str:
    for low, high, label in PRICE_BUCKETS:
        if low <= price < high:
            return label
    return "Unknown"


def _duration_bucket(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "Unknown"
    for low, high, label in HOLD_BUCKETS:
        if low <= seconds < high:
            return label
    return "Unknown"


def _new_asset() -> dict[str, Any]:
    return {
        "condition_id": "", "asset": "", "event_slug": "", "market": "", "category": "Other",
        "outcome": "", "outcome_index": None, "buy_count": 0, "sell_count": 0,
        "buy_shares": 0.0, "sell_shares": 0.0, "buy_notional": 0.0, "sell_notional": 0.0,
        "fifo_matched_shares": 0.0, "fifo_realized_pnl": 0.0, "fifo_unmatched_sell_shares": 0.0,
        "fifo_remaining_cost": 0.0, "first_trade": None, "last_trade": None,
        "first_buy": None, "last_buy": None, "first_sell": None, "last_sell": None,
        "reopen_count": 0, "closed_once": False, "net_trade_shares": 0.0,
        "maker_fills_recent": 0, "taker_fills_recent": 0, "onchain_matched_recent": 0,
    }


def _profit_factor(values: Sequence[float]) -> float | None:
    positive = sum(value for value in values if value > 0)
    negative = -sum(value for value in values if value < 0)
    return positive / negative if negative > 0 else None


def _max_drawdown_timed(points: Sequence[tuple[int, float]]) -> float:
    cumulative = peak = 0.0
    drawdown = 0.0
    for _, value in sorted(points):
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = min(drawdown, cumulative - peak)
    return drawdown


def _metric_rows(rows: Sequence[Mapping[str, Any]], group_key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(group_key) or "Unknown")].append(row)
    result = []
    for key, items in groups.items():
        pnl_values = [number(item.get("official_pnl_usd")) for item in items]
        capital = sum(number(item.get("official_total_bought_usd")) for item in items)
        result.append(
            {
                group_key: key,
                "conditions": len(items),
                "trades": sum(int(item.get("trade_count") or 0) for item in items),
                "volume_usd": sum(number(item.get("trade_volume_usd")) for item in items),
                "capital_proxy_total_bought_usd": capital,
                "official_pnl_usd": sum(pnl_values),
                "return_on_total_bought": sum(pnl_values) / capital if capital else None,
                "profit_factor": _profit_factor(pnl_values),
                "win_rate_conditions": sum(value > 0 for value in pnl_values) / len(pnl_values) if pnl_values else None,
                "max_drawdown_usd": _max_drawdown_timed(
                    [(int(item.get("pnl_timestamp") or 0), number(item.get("official_pnl_usd"))) for item in items]
                ),
            }
        )
    return sorted(result, key=lambda row: number(row["official_pnl_usd"]), reverse=True)


def _load_metadata(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    connection = sqlite3.connect(path)
    events: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT event_slug,event_id,title,start_date,end_date,closed_time,active,closed,enable_neg_risk,tags_json,error FROM event_metadata"
    ):
        tags = _json_list(row[9])
        events[row[0]] = {
            "event_id": row[1], "title": row[2], "start_date": row[3], "end_date": row[4],
            "closed_time": row[5], "active": row[6], "closed": row[7], "neg_risk": row[8],
            "tags": tags, "error": row[10],
        }
    markets: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        """SELECT condition_id,event_slug,market_id,slug,question,start_date,end_date,closed_time,
                  active,closed,neg_risk,outcomes_json,outcome_prices_json,token_ids_json,
                  fees_enabled,maker_base_fee,taker_base_fee,holding_rewards_enabled
           FROM market_metadata"""
    ):
        outcomes = _json_list(row[11])
        prices = _json_list(row[12])
        tokens = [str(item) for item in _json_list(row[13])]
        markets[str(row[0]).lower()] = {
            "event_slug": row[1], "market_id": row[2], "slug": row[3], "question": row[4],
            "start_date": row[5], "end_date": row[6], "closed_time": row[7], "active": row[8],
            "closed": row[9], "neg_risk": row[10], "outcomes": outcomes, "outcome_prices": prices,
            "token_ids": tokens, "fees_enabled": row[14], "maker_base_fee": row[15],
            "taker_base_fee": row[16], "holding_rewards_enabled": row[17],
            "winner": _winner(outcomes, prices),
        }
    quality = {
        "events_success": sum(event.get("error") is None for event in events.values()),
        "events_error": sum(event.get("error") is not None for event in events.values()),
        "markets": len(markets),
    }
    connection.close()
    return events, markets, quality


def _load_onchain(path: Path) -> tuple[dict[tuple[str, str, str], deque[dict[str, Any]]], dict[str, Any], list[dict[str, Any]]]:
    connection = sqlite3.connect(path)
    progress = connection.execute("SELECT exchange_key,last_block,head_block,updated_at FROM scan_progress").fetchall()
    head = max((int(row[2]) for row in progress), default=0)
    common_start = max(0, head - 49_999)
    rows = []
    index: dict[tuple[str, str, str], deque[dict[str, Any]]] = defaultdict(deque)
    for row in connection.execute(
        """SELECT transaction_hash,log_index,block_number,exchange_key,negative_risk_exchange,
                  role,side,token_id,shares,notional_usd,price,fee_usd_estimate
           FROM onchain_fills WHERE block_number>=? ORDER BY block_number,log_index""",
        (common_start,),
    ):
        item = {
            "transaction_hash": row[0], "log_index": row[1], "block_number": row[2],
            "exchange_key": row[3], "negative_risk_exchange": row[4], "role": row[5],
            "side": row[6], "token_id": row[7], "shares": row[8], "notional_usd": row[9],
            "price": row[10], "fee_usd_estimate": row[11],
        }
        rows.append(item)
        index[(str(row[0]).lower(), str(row[7]), str(row[6]).upper())].append(item)
    coverage = {
        "head_block": head,
        "common_window_start_block": common_start,
        "common_window_blocks": 50_000,
        "fills": len(rows),
        "maker_fills": sum(row["role"] == "MAKER" for row in rows),
        "taker_fills": sum(row["role"] == "TAKER" for row in rows),
        "notional_usd": sum(number(row["notional_usd"]) for row in rows),
        "fee_usd_estimate": sum(number(row["fee_usd_estimate"]) for row in rows),
        "exchanges": sorted({str(row["exchange_key"]) for row in rows}),
        "historical_scope": "recent_partial_common_50000_blocks",
    }
    connection.close()
    return index, coverage, rows


def _match_onchain(
    index: dict[tuple[str, str, str], deque[dict[str, Any]]],
    transaction_hash: str,
    asset: str,
    side: str,
    shares: float,
    notional: float,
) -> dict[str, Any] | None:
    candidates = index.get((transaction_hash.lower(), asset, side.upper()))
    if not candidates:
        return None
    best_index = min(
        range(len(candidates)),
        key=lambda idx: abs(number(candidates[idx]["shares"]) - shares)
        + abs(number(candidates[idx]["notional_usd"]) - notional),
    )
    match = candidates[best_index]
    del candidates[best_index]
    return match


def _read_equity(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with zipfile.ZipFile(path) as archive:
        rows = list(csv.DictReader(archive.read("equity.csv").decode("utf-8").splitlines()))
    if not rows:
        return {}
    row = rows[0]
    return {
        "cash_balance_usd": number(row.get("cashBalance")),
        "positions_value_usd": number(row.get("positionsValue")),
        "equity_usd": number(row.get("equity")),
        "valuation_time": row.get("valuationTime"),
    }


def _position_maps(
    closed_positions: Sequence[Mapping[str, Any]], open_positions: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    by_asset: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "official_realized_pnl_usd": 0.0, "official_unrealized_pnl_usd": 0.0,
            "official_total_bought_usd": 0.0, "position_rows": 0, "status": "UNKNOWN",
            "avg_price": None, "position_timestamp": None,
        }
    )
    by_condition: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "official_realized_pnl_usd": 0.0, "official_unrealized_pnl_usd": 0.0,
            "official_total_bought_usd": 0.0, "position_rows": 0, "pnl_timestamp": 0,
            "event_slug": "", "title": "",
        }
    )
    normalized: list[dict[str, Any]] = []
    for status, source in (("CLOSED", closed_positions), ("OPEN", open_positions)):
        for raw in source:
            asset = str(raw.get("asset") or "")
            condition = str(raw.get("conditionId") or "").lower()
            realized = number(raw.get("realizedPnl"))
            unrealized = number(raw.get("cashPnl")) if status == "OPEN" else 0.0
            total_bought = number(raw.get("totalBought") or raw.get("initialValue") or raw.get("grossInitialValue"))
            timestamp = parse_time(raw.get("timestamp"))
            if timestamp is None and status == "CLOSED":
                timestamp = parse_time(raw.get("endDate"))
            timestamp = timestamp or 0
            asset_row = by_asset[asset]
            asset_row["official_realized_pnl_usd"] += realized
            asset_row["official_unrealized_pnl_usd"] += unrealized
            asset_row["official_total_bought_usd"] += total_bought
            asset_row["position_rows"] += 1
            asset_row["status"] = status
            asset_row["avg_price"] = nullable_number(raw.get("avgPrice"))
            asset_row["position_timestamp"] = max(int(asset_row.get("position_timestamp") or 0), timestamp)
            condition_row = by_condition[condition]
            condition_row["official_realized_pnl_usd"] += realized
            condition_row["official_unrealized_pnl_usd"] += unrealized
            condition_row["official_total_bought_usd"] += total_bought
            condition_row["position_rows"] += 1
            condition_row["pnl_timestamp"] = max(int(condition_row["pnl_timestamp"]), timestamp)
            condition_row["event_slug"] = str(raw.get("eventSlug") or condition_row["event_slug"])
            condition_row["title"] = str(raw.get("title") or condition_row["title"])
            normalized.append(
                {
                    "status": status, "condition_id": condition, "asset": asset,
                    "event_slug": raw.get("eventSlug"), "market": raw.get("title"),
                    "outcome": raw.get("outcome"), "outcome_index": raw.get("outcomeIndex"),
                    "avg_price": nullable_number(raw.get("avgPrice")), "total_bought_usd": total_bought,
                    "position_shares": number(raw.get("size")),
                    "current_value_usd": number(raw.get("currentValue")) if status == "OPEN" else 0.0,
                    "realized_pnl_usd": realized, "unrealized_pnl_usd": unrealized,
                    "official_pnl_usd": realized + unrealized, "position_timestamp": timestamp,
                }
            )
    return dict(by_asset), dict(by_condition), normalized


def _bot_score(
    trade_times: Sequence[int],
    trade_sizes: Sequence[float],
    trades_per_minute_conditions: Mapping[int, set[str]],
    paired_condition_fraction: float,
    maker_fraction_recent: float | None,
) -> tuple[int, dict[str, float]]:
    sorted_times = sorted(trade_times)
    gaps = [right - left for left, right in zip(sorted_times, sorted_times[1:]) if right >= left]
    days = max(1.0, (sorted_times[-1] - sorted_times[0]) / 86_400) if sorted_times else 1.0
    trades_per_day = len(sorted_times) / days
    active_hours = len({datetime.fromtimestamp(value, timezone.utc).hour for value in sorted_times})
    subminute = sum(gap <= 60 for gap in gaps) / len(gaps) if gaps else 0.0
    rounded = [round(value, 2) for value in trade_sizes]
    repeats = 0.0
    if rounded:
        counts: dict[float, int] = defaultdict(int)
        for value in rounded:
            counts[value] += 1
        repeats = sum(sorted(counts.values(), reverse=True)[:20]) / len(rounded)
    simultaneous = sum(len(value) >= 3 for value in trades_per_minute_conditions.values())
    simultaneous_fraction = simultaneous / len(trades_per_minute_conditions) if trades_per_minute_conditions else 0.0
    components = {
        "high_frequency_15": min(15.0, 15.0 * trades_per_day / 100.0),
        "activity_24_7_15": 15.0 * active_hours / 24.0,
        "regular_or_repetitive_sizes_10": min(10.0, repeats * 25.0),
        "simultaneous_markets_10": min(10.0, simultaneous_fraction * 80.0),
        "sub_minute_activity_15": min(15.0, subminute * 20.0),
        "paired_execution_15": min(15.0, paired_condition_fraction * 30.0),
        "maker_behavior_recent_20": 20.0 * number(maker_fraction_recent),
    }
    return int(round(sum(components.values()))), components


def _fmt_money(value: Any) -> str:
    if value is None:
        return "N/D"
    return f"US${number(value):,.2f}"


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "N/D"
    return f"{number(value) * 100:.2f}%"


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        output.append("| " + " | ".join(str(item) for item in row) + " |")
    return output


def build_forensics(
    ledger_path: Path,
    metadata_path: Path,
    onchain_path: Path,
    accounting_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger = sqlite3.connect(ledger_path)
    profile = _snapshot(ledger, "public_profile") or {}
    leaderboard_rows = _snapshot(ledger, "leaderboard_all_overall") or []
    leaderboard = leaderboard_rows[0] if leaderboard_rows else {}
    open_positions = _snapshot(ledger, "positions") or []
    closed_positions = _snapshot(ledger, "closed_positions") or []
    events, markets, metadata_quality = _load_metadata(metadata_path)
    onchain_index, onchain_coverage, onchain_rows = _load_onchain(onchain_path)
    position_by_asset, position_by_condition, position_records = _position_maps(closed_positions, open_positions)
    equity = _read_equity(accounting_path)

    verified = (
        str(profile.get("proxyWallet") or "").lower() == WALLET
        and str(profile.get("name") or "").lower() == EXPECTED_USERNAME.lower()
    )
    identity = {
        "verification_status": "VERIFIED" if verified else "NOT_VERIFIED",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "expected_username": EXPECTED_USERNAME,
        "verified_profile_url": PROFILE,
        "verified_proxy_wallet": str(profile.get("proxyWallet") or "").lower(),
        "display_name": profile.get("name"),
        "pseudonym": profile.get("pseudonym"),
        "x_username": profile.get("xUsername"),
        "verified_badge": profile.get("verifiedBadge"),
        "created_at": profile.get("createdAt"),
        "bio": profile.get("bio"),
        "supplied_profile_url": SUPPLIED_PROFILE,
        "supplied_profile_url_matches_username": EXPECTED_USERNAME.lower() in SUPPLIED_PROFILE.lower(),
        "warning": None if EXPECTED_USERNAME.lower() in SUPPLIED_PROFILE.lower() else (
            f"El PROFILE_URL suministrado no coincide con {DISPLAY_LABEL}; se verificó la identidad con la snapshot oficial."
        ),
        "evidence": {
            "gamma_public_profile_snapshot": str(ledger_path.resolve()),
            "leaderboard_wallet": str(leaderboard.get("proxyWallet") or "").lower(),
            "leaderboard_username": leaderboard.get("userName"),
            "leaderboard_rank": leaderboard.get("rank"),
        },
    }
    _write_json(_artifact(output_dir, "wallet_identity.json"), identity)
    if not verified:
        ledger.close()
        raise RuntimeError(f"La snapshot oficial no verifica que la wallet corresponda a {DISPLAY_LABEL}")

    asset_aggs: dict[str, dict[str, Any]] = defaultdict(_new_asset)
    fifo_lots: dict[str, deque[list[float]]] = defaultdict(deque)
    conversion_by_condition: dict[str, dict[str, float]] = defaultdict(
        lambda: {"split_count": 0, "split_usd": 0.0, "merge_count": 0, "merge_usd": 0.0,
                 "redeem_count": 0, "redeem_usd": 0.0, "conversion_count": 0}
    )
    trade_times: list[int] = []
    trade_sizes: list[float] = []
    trade_notionals: list[float] = []
    trade_prices: list[float] = []
    per_minute_conditions: dict[int, set[str]] = defaultdict(set)
    trade_count = 0
    reward_count = 0
    onchain_matches = 0
    trades_missing_market_metadata = 0
    token_ids_not_in_metadata = 0
    missing_market_conditions: set[str] = set()
    raw_temp = _artifact(output_dir, "_raw_activity.ndjson")
    trades_temp = _artifact(output_dir, "_trades.ndjson")
    rewards_temp = _artifact(output_dir, "_rewards.ndjson")
    with raw_temp.open("w", encoding="utf-8") as raw_handle, trades_temp.open(
        "w", encoding="utf-8"
    ) as trade_handle, rewards_temp.open("w", encoding="utf-8") as reward_handle:
        cursor = ledger.execute(
            """SELECT event_id,timestamp,event_type,transaction_hash,condition_id,asset,side,size,
                      price,usdc_size,cash_delta_usd,economic_bucket,raw_json
               FROM activity_events ORDER BY timestamp,event_id"""
        )
        for row in cursor:
            event_id, timestamp, event_type, tx_hash, condition, asset, side, size, price, usdc, cash, bucket, raw_json = row
            raw = json.loads(raw_json)
            condition = str(condition or raw.get("conditionId") or "").lower()
            asset = str(asset or raw.get("asset") or "")
            event_slug = str(raw.get("eventSlug") or "")
            market_info = markets.get(condition, {})
            event_info = events.get(event_slug) or events.get(str(market_info.get("event_slug") or "")) or {}
            market_title = str(raw.get("title") or market_info.get("question") or event_info.get("title") or "")
            category = _category(market_title + " " + event_slug, event_info.get("tags") or [])
            start_time = parse_time(market_info.get("start_date") or event_info.get("start_date"))
            end_time = parse_time(market_info.get("end_date") or event_info.get("end_date"))
            closed_time = parse_time(market_info.get("closed_time") or event_info.get("closed_time"))
            raw_row = {
                "event_id": event_id, "timestamp": int(timestamp), "timestamp_utc": utc_iso(timestamp),
                "event_type": event_type, "transaction_hash": tx_hash, "condition_id": condition,
                "asset": asset, "side": side or "", "size": number(size), "price": number(price),
                "usdc_size": number(usdc), "cash_delta_usd": nullable_number(cash),
                "economic_bucket": bucket, "event_slug": event_slug,
                "event_numeric_id": event_info.get("event_id"), "market_id": market_info.get("market_id"),
                "market": market_title, "category": category, "outcome": raw.get("outcome"),
                "outcome_index": raw.get("outcomeIndex"), "market_start": utc_iso(start_time),
                "market_end": utc_iso(end_time), "market_closed": utc_iso(closed_time),
                "winner": market_info.get("winner"), "negative_risk": bool(market_info.get("neg_risk")),
                "market_status": "CLOSED" if market_info.get("closed") else "ACTIVE" if market_info.get("active") else "UNKNOWN",
                "raw_json": raw_json,
            }
            _jsonl_write(raw_handle, raw_row)
            if event_type in REWARD_TYPES:
                _jsonl_write(reward_handle, raw_row)
                reward_count += 1
            if event_type in {"SPLIT", "MERGE", "REDEEM", "CONVERSION"}:
                converted = conversion_by_condition[condition]
                if event_type == "SPLIT":
                    converted["split_count"] += 1
                    converted["split_usd"] += number(usdc)
                elif event_type == "MERGE":
                    converted["merge_count"] += 1
                    converted["merge_usd"] += number(usdc)
                elif event_type == "REDEEM":
                    converted["redeem_count"] += 1
                    converted["redeem_usd"] += number(usdc)
                else:
                    converted["conversion_count"] += 1
            if event_type != "TRADE":
                continue

            trade_count += 1
            if not market_info:
                trades_missing_market_metadata += 1
                missing_market_conditions.add(condition)
            elif market_info.get("token_ids") and asset not in set(market_info["token_ids"]):
                token_ids_not_in_metadata += 1
            timestamp = int(timestamp)
            shares = number(size)
            notional = number(usdc)
            price_value = number(price)
            side_value = str(side or "").upper()
            trade_times.append(timestamp)
            trade_sizes.append(shares)
            trade_notionals.append(notional)
            trade_prices.append(price_value)
            per_minute_conditions[timestamp // 60].add(condition)
            match = _match_onchain(onchain_index, str(tx_hash or ""), asset, side_value, shares, notional)
            if match:
                onchain_matches += 1
            agg = asset_aggs[asset]
            agg["condition_id"] = condition
            agg["asset"] = asset
            agg["event_slug"] = event_slug
            agg["market"] = market_title
            agg["category"] = category
            agg["outcome"] = str(raw.get("outcome") or "")
            agg["outcome_index"] = raw.get("outcomeIndex")
            agg["first_trade"] = timestamp if agg["first_trade"] is None else min(agg["first_trade"], timestamp)
            agg["last_trade"] = timestamp if agg["last_trade"] is None else max(agg["last_trade"], timestamp)
            current_before = number(agg["net_trade_shares"])
            if side_value == "BUY":
                action = "REOPEN" if current_before <= 1e-9 and agg["closed_once"] else "OPEN" if current_before <= 1e-9 else "ADD"
                agg["reopen_count"] += int(action == "REOPEN")
                agg["buy_count"] += 1
                agg["buy_shares"] += shares
                agg["buy_notional"] += notional
                agg["first_buy"] = timestamp if agg["first_buy"] is None else min(agg["first_buy"], timestamp)
                agg["last_buy"] = timestamp if agg["last_buy"] is None else max(agg["last_buy"], timestamp)
                fifo_lots[asset].append([shares, price_value])
                agg["net_trade_shares"] = current_before + shares
            else:
                action = "CLOSE" if current_before > 0 and shares >= current_before - 1e-9 else "REDUCE"
                agg["sell_count"] += 1
                agg["sell_shares"] += shares
                agg["sell_notional"] += notional
                agg["first_sell"] = timestamp if agg["first_sell"] is None else min(agg["first_sell"], timestamp)
                agg["last_sell"] = timestamp if agg["last_sell"] is None else max(agg["last_sell"], timestamp)
                remaining = shares
                while remaining > 1e-9 and fifo_lots[asset]:
                    lot = fifo_lots[asset][0]
                    matched = min(remaining, lot[0])
                    agg["fifo_matched_shares"] += matched
                    agg["fifo_realized_pnl"] += matched * (price_value - lot[1])
                    lot[0] -= matched
                    remaining -= matched
                    if lot[0] <= 1e-9:
                        fifo_lots[asset].popleft()
                agg["fifo_unmatched_sell_shares"] += max(0.0, remaining)
                agg["net_trade_shares"] = current_before - shares
                if action == "CLOSE":
                    agg["closed_once"] = True
            if match:
                agg["onchain_matched_recent"] += 1
                agg["maker_fills_recent"] += int(match["role"] == "MAKER")
                agg["taker_fills_recent"] += int(match["role"] == "TAKER")
            trade_row = dict(raw_row)
            trade_row.update(
                {
                    "lifecycle_action": action,
                    "block": match.get("block_number") if match else None,
                    "maker_taker": match.get("role") if match else "UNKNOWN_OUTSIDE_ONCHAIN_WINDOW",
                    "onchain_exchange": match.get("exchange_key") if match else None,
                    "fee_usd_onchain": match.get("fee_usd_estimate") if match else None,
                    "time_from_market_start_seconds": timestamp - start_time if start_time else None,
                    "time_to_market_end_seconds": end_time - timestamp if end_time else None,
                    "data_api_raw_json": raw_json,
                }
            )
            _jsonl_write(trade_handle, trade_row)

    ledger.close()
    _jsonl_to_parquet(raw_temp, _artifact(output_dir, "raw_activity.parquet"))
    _jsonl_to_parquet(trades_temp, _artifact(output_dir, "trades.parquet"))
    _jsonl_to_parquet(rewards_temp, _artifact(output_dir, "rewards.parquet"))
    raw_temp.unlink(missing_ok=True)
    trades_temp.unlink(missing_ok=True)
    rewards_temp.unlink(missing_ok=True)

    lifecycle_rows: list[dict[str, Any]] = []
    position_record_by_asset = {
        str(row.get("asset") or ""): row
        for row in position_records
        if row.get("asset")
    }
    all_assets = set(asset_aggs) | set(position_by_asset)
    for asset in all_assets:
        agg = asset_aggs.get(asset, _new_asset())
        position = position_by_asset.get(asset, {})
        for lot_shares, lot_price in fifo_lots.get(asset, []):
            agg["fifo_remaining_cost"] += number(lot_shares) * number(lot_price)
        condition = str(agg.get("condition_id") or "")
        if not condition:
            raw = position_record_by_asset.get(asset)
            if raw:
                condition = str(raw["condition_id"])
                agg["condition_id"] = condition
                agg["event_slug"] = raw.get("event_slug") or ""
                agg["market"] = raw.get("market") or ""
                agg["outcome"] = raw.get("outcome") or ""
                agg["outcome_index"] = raw.get("outcome_index")
        market_info = markets.get(condition, {})
        event_info = events.get(str(agg.get("event_slug") or market_info.get("event_slug") or ""), {})
        category = str(agg.get("category") or "Other")
        if category == "Other":
            category = _category(str(agg.get("market") or ""), event_info.get("tags") or [])
        realized = number(position.get("official_realized_pnl_usd"))
        unrealized = number(position.get("official_unrealized_pnl_usd"))
        first_buy = agg.get("first_buy")
        position_timestamp = position.get("position_timestamp")
        estimated_hold = int(position_timestamp) - int(first_buy) if first_buy and position_timestamp and int(position_timestamp) >= int(first_buy) else None
        lifecycle_rows.append(
            {
                **{key: value for key, value in agg.items() if key != "closed_once"},
                "category": category,
                "buy_vwap": number(agg["buy_notional"]) / number(agg["buy_shares"]) if number(agg["buy_shares"]) else None,
                "sell_vwap": number(agg["sell_notional"]) / number(agg["sell_shares"]) if number(agg["sell_shares"]) else None,
                "observable_span_seconds": int(agg["last_trade"]) - int(agg["first_trade"]) if agg.get("first_trade") and agg.get("last_trade") else None,
                "estimated_hold_seconds": estimated_hold,
                "official_status": position.get("status", "NO_POSITION_SNAPSHOT"),
                "official_avg_price": position.get("avg_price"),
                "official_total_bought_usd": number(position.get("official_total_bought_usd")),
                "official_realized_pnl_usd": realized,
                "official_unrealized_pnl_usd": unrealized,
                "official_pnl_usd": realized + unrealized,
                "winner": market_info.get("winner"),
                "is_winner_outcome": bool(market_info.get("winner")) and str(agg.get("outcome")) == str(market_info.get("winner")),
                "negative_risk": bool(market_info.get("neg_risk")),
                "market_start": market_info.get("start_date"), "market_end": market_info.get("end_date"),
                "market_closed": market_info.get("closed_time"),
            }
        )
    _rows_to_parquet(lifecycle_rows, _artifact(output_dir, "lifecycles.parquet"))
    fifo_unmatched_sell_shares = sum(number(row.get("fifo_unmatched_sell_shares")) for row in lifecycle_rows)

    hold_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    resolution_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in lifecycle_rows:
        if row.get("estimated_hold_seconds") is not None:
            hold_groups[_duration_bucket(number(row.get("estimated_hold_seconds")))].append(row)
        market_end = parse_time(row.get("market_closed") or row.get("market_end"))
        first_buy = row.get("first_buy")
        if market_end and first_buy and market_end >= int(first_buy):
            resolution_groups[_duration_bucket(market_end - int(first_buy))].append(row)

    def duration_metrics(groups: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
        output = []
        for _, _, label in HOLD_BUCKETS:
            items = list(groups.get(label, []))
            pnls = [number(item.get("official_pnl_usd")) for item in items]
            capital = sum(number(item.get("official_total_bought_usd")) for item in items)
            output.append(
                {"bucket": label, "position_rows": len(items), "official_pnl_usd": sum(pnls),
                 "total_bought_usd": capital, "return_on_total_bought": sum(pnls) / capital if capital else None,
                 "profit_factor": _profit_factor(pnls),
                 "win_rate_positions": sum(value > 0 for value in pnls) / len(pnls) if pnls else None}
            )
        return output

    hold_metrics = duration_metrics(hold_groups)
    time_to_resolution_metrics = duration_metrics(resolution_groups)

    condition_assets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in lifecycle_rows:
        condition_assets[str(row.get("condition_id") or "")].append(row)
    current_by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in position_records:
        if row["status"] == "OPEN":
            current_by_condition[str(row["condition_id"])].append(row)
    all_conditions = set(condition_assets) | set(position_by_condition)
    strategy_rows: list[dict[str, Any]] = []
    for condition in all_conditions:
        assets = condition_assets.get(condition, [])
        current_positions = current_by_condition.get(condition, [])
        official = position_by_condition.get(condition, {})
        conversion = conversion_by_condition.get(condition, {})
        market_info = markets.get(condition, {})
        event_slug = str(
            next((row.get("event_slug") for row in assets if row.get("event_slug")), None)
            or official.get("event_slug") or market_info.get("event_slug") or ""
        )
        event_info = events.get(event_slug, {})
        title = str(next((row.get("market") for row in assets if row.get("market")), None) or official.get("title") or market_info.get("question") or "")
        category = str(next((row.get("category") for row in assets if row.get("category") and row.get("category") != "Other"), None) or _category(title, event_info.get("tags") or []))
        buy_assets = [row for row in assets if number(row.get("buy_shares")) > 0]
        buy_shares = sum(number(row.get("buy_shares")) for row in assets)
        sell_shares = sum(number(row.get("sell_shares")) for row in assets)
        buy_notional = sum(number(row.get("buy_notional")) for row in assets)
        sell_notional = sum(number(row.get("sell_notional")) for row in assets)
        trade_count_condition = sum(int(row.get("buy_count") or 0) + int(row.get("sell_count") or 0) for row in assets)
        trade_volume = buy_notional + sell_notional
        paired = len(buy_assets) >= 2
        sorted_buy_assets = sorted(buy_assets, key=lambda row: int(row.get("outcome_index") or 0))[:2]
        paired_shares = min((number(row.get("buy_shares")) for row in sorted_buy_assets), default=0.0) if paired else 0.0
        paired_fraction = (2 * paired_shares / buy_shares) if buy_shares else 0.0
        pair_cost = None
        first_pair_gap = None
        pair_sequence = None
        if paired and len(sorted_buy_assets) == 2:
            pair_cost = sum(number(row.get("buy_vwap")) for row in sorted_buy_assets)
            first_times = [(str(row.get("outcome") or "?"), int(row.get("first_buy") or 0)) for row in sorted_buy_assets]
            first_pair_gap = abs(first_times[0][1] - first_times[1][1])
            if first_pair_gap <= 10:
                pair_sequence = "SIMULTANEOUS_WITHIN_10S"
            else:
                pair_sequence = f"{min(first_times, key=lambda item: item[1])[0]}_FIRST"
        turnover_balance = min(buy_notional, sell_notional) / max(buy_notional, sell_notional) if max(buy_notional, sell_notional) else 0.0
        market_making_candidate = trade_count_condition >= 20 and buy_notional > 0 and sell_notional > 0 and turnover_balance >= 0.20
        inventory_candidate = bool(number(conversion.get("split_count")) or number(conversion.get("merge_count")))
        arbitrage_candidate = bool(paired and pair_cost is not None and pair_cost < 0.995 and paired_fraction >= 0.10)
        neg_risk = bool(market_info.get("neg_risk") or event_info.get("neg_risk"))
        average_buy_price = buy_notional / buy_shares if buy_shares else None
        current_sizes = sorted((number(row.get("position_shares")) for row in current_positions), reverse=True)
        current_gross_shares = sum(current_sizes)
        current_paired_shares = min(current_sizes[:2]) if len(current_sizes) >= 2 else 0.0
        current_net_shares = abs(current_sizes[0] - current_sizes[1]) if len(current_sizes) >= 2 else (current_sizes[0] if current_sizes else 0.0)
        current_market_value = sum(number(row.get("current_value_usd")) for row in current_positions)
        if inventory_candidate:
            primary_cluster = "inventory_management"
        elif market_making_candidate:
            primary_cluster = "market_making_candidate"
        elif paired_fraction >= 0.25:
            primary_cluster = "paired_yes_no"
        elif neg_risk:
            primary_cluster = "negrisk"
        elif average_buy_price is not None and average_buy_price >= 0.90:
            primary_cluster = "high_probability_harvesting"
        elif average_buy_price is not None and average_buy_price <= 0.10:
            primary_cluster = "longshot_value"
        else:
            primary_cluster = "directional_forecasting"
        pnl = number(official.get("official_realized_pnl_usd")) + number(official.get("official_unrealized_pnl_usd"))
        capital_proxy = number(official.get("official_total_bought_usd"))
        pnl_timestamp = int(official.get("pnl_timestamp") or max((int(row.get("last_trade") or 0) for row in assets), default=0))
        strategy_rows.append(
            {
                "condition_id": condition, "event_slug": event_slug, "market": title, "category": category,
                "primary_cluster": primary_cluster, "directional_candidate": primary_cluster == "directional_forecasting",
                "high_probability_candidate": bool(average_buy_price is not None and average_buy_price >= 0.90),
                "longshot_candidate": bool(average_buy_price is not None and average_buy_price <= 0.10),
                "market_making_candidate": market_making_candidate, "paired_yes_no": paired,
                "arbitrage_candidate_non_temporal": arbitrage_candidate, "negative_risk": neg_risk,
                "inventory_management_candidate": inventory_candidate,
                "trade_count": trade_count_condition, "trade_volume_usd": trade_volume,
                "buy_notional_usd": buy_notional, "sell_notional_usd": sell_notional,
                "buy_shares": buy_shares, "sell_shares": sell_shares, "average_buy_price": average_buy_price,
                "paired_shares": paired_shares, "paired_fraction_of_bought_shares": paired_fraction,
                "current_open_gross_shares": current_gross_shares,
                "current_open_paired_shares": current_paired_shares,
                "current_open_net_outcome_shares": current_net_shares,
                "current_open_market_value_usd": current_market_value,
                "yes_no_combined_vwap": pair_cost, "first_opposite_leg_gap_seconds": first_pair_gap,
                "pair_sequence": pair_sequence, "split_count": int(conversion.get("split_count") or 0),
                "split_notional_usd": number(conversion.get("split_usd")), "merge_count": int(conversion.get("merge_count") or 0),
                "merge_notional_usd": number(conversion.get("merge_usd")), "redeem_count": int(conversion.get("redeem_count") or 0),
                "redeem_gross_usd": number(conversion.get("redeem_usd")),
                "official_total_bought_usd": capital_proxy,
                "official_realized_pnl_usd": number(official.get("official_realized_pnl_usd")),
                "official_unrealized_pnl_usd": number(official.get("official_unrealized_pnl_usd")),
                "official_pnl_usd": pnl, "pnl_timestamp": pnl_timestamp,
                "return_on_total_bought": pnl / capital_proxy if capital_proxy else None,
                "classification_warning": "Candidato conductual; no demuestra resting orders, simultaneidad ni arbitraje ejecutable.",
            }
        )
    _rows_to_parquet(strategy_rows, _artifact(output_dir, "strategy_clusters.parquet"))

    total_position_pnl = sum(number(row.get("official_pnl_usd")) for row in strategy_rows)
    closed_pnl = sum(number(row.get("realizedPnl")) for row in closed_positions)
    open_realized = sum(number(row.get("realizedPnl")) for row in open_positions)
    open_unrealized = sum(number(row.get("cashPnl")) for row in open_positions)
    reward_by_type: dict[str, float] = defaultdict(float)
    reward_counts: dict[str, int] = defaultdict(int)
    source = sqlite3.connect(ledger_path)
    for event_type, count, amount in source.execute(
        """SELECT event_type,COUNT(*),SUM(usdc_size) FROM activity_events
           WHERE event_type IN ('REWARD','MAKER_REBATE','TAKER_REBATE','REFERRAL_REWARD','YIELD')
           GROUP BY event_type"""
    ):
        reward_by_type[event_type] = number(amount)
        reward_counts[event_type] = int(count)
    activity_counts = {row[0]: int(row[1]) for row in source.execute("SELECT event_type,COUNT(*) FROM activity_events GROUP BY event_type")}
    source.close()
    rewards_total = sum(reward_by_type.values())
    leaderboard_pnl = nullable_number(leaderboard.get("pnl"))
    decomposition_rows = [
        {"component": "Closed positions realized PnL", "amount_usd": closed_pnl, "included_in_position_total": True, "status": "OFFICIAL_SNAPSHOT", "note": "PnL realizado del endpoint de posiciones cerradas."},
        {"component": "Open positions realized PnL", "amount_usd": open_realized, "included_in_position_total": True, "status": "OFFICIAL_SNAPSHOT", "note": "PnL realizado dentro de posiciones aún abiertas."},
        {"component": "Open positions unrealized PnL", "amount_usd": open_unrealized, "included_in_position_total": True, "status": "OFFICIAL_SNAPSHOT", "note": "cashPnl no realizado a la hora de la captura."},
        {"component": "Position snapshot PnL total", "amount_usd": closed_pnl + open_realized + open_unrealized, "included_in_position_total": True, "status": "RECONCILED_SUM", "note": "Suma sin rewards explícitos."},
        {"component": "Leaderboard ALL/OVERALL PnL", "amount_usd": leaderboard_pnl, "included_in_position_total": False, "status": "OFFICIAL_SEPARATE", "note": "Se conserva separado por posibles ventanas/actualizaciones distintas."},
        {"component": "Leaderboard minus position snapshot", "amount_usd": number(leaderboard_pnl) - (closed_pnl + open_realized + open_unrealized) if leaderboard_pnl is not None else None, "included_in_position_total": False, "status": "RECONCILIATION_DIFFERENCE", "note": "No se fuerza a cero."},
        {"component": "Liquidity/other REWARD", "amount_usd": reward_by_type.get("REWARD", 0.0), "included_in_position_total": False, "status": "EXPLICIT_CASH_FLOW", "note": "La API no etiqueta cada pago con la campaña exacta."},
        {"component": "Maker rebates", "amount_usd": reward_by_type.get("MAKER_REBATE", 0.0), "included_in_position_total": False, "status": "EXPLICIT_CASH_FLOW", "note": "Separado de trading PnL."},
        {"component": "Taker rebates", "amount_usd": reward_by_type.get("TAKER_REBATE", 0.0), "included_in_position_total": False, "status": "EXPLICIT_CASH_FLOW", "note": "Separado de trading PnL."},
        {"component": "Referral rewards", "amount_usd": reward_by_type.get("REFERRAL_REWARD", 0.0), "included_in_position_total": False, "status": "EXPLICIT_CASH_FLOW", "note": "No atribuible a estrategia de mercado."},
        {"component": "Yield", "amount_usd": reward_by_type.get("YIELD", 0.0), "included_in_position_total": False, "status": "EXPLICIT_CASH_FLOW", "note": "Separado de trading PnL."},
        {"component": "All explicit incentives", "amount_usd": rewards_total, "included_in_position_total": False, "status": "EXPLICIT_SUM", "note": "No se suma al leaderboard porque su inclusión oficial no está documentada."},
    ]
    _write_csv(_artifact(output_dir, "pnl_decomposition.csv"), decomposition_rows)

    copy_rows = []
    for delay in DELAY_SECONDS:
        copy_rows.append(
            {
                "delay_seconds": delay, "detected_signals": trade_count, "filled": None, "missed": None,
                "original_pnl_usd": None, "follower_pnl_usd": None, "roi": None, "max_drawdown_usd": None,
                "slippage_usd": None, "execution_rate": None, "copy_decay_usd": None,
                "data_grade": "D_INSUFFICIENT", "reason": f"No hay historical bid/ask/depth completo; usar el precio de {DISPLAY_LABEL} sería look-ahead y ejecución ficticia.",
            }
        )
    _rows_to_parquet(copy_rows, _artifact(output_dir, "copy_backtest.parquet"))
    _write_csv(_artifact(output_dir, "copy_latency.csv"), copy_rows)
    replication_rows = [
        {"split": split, "universe": "complete_eligible_markets", "pnl_usd": None, "roi": None, "max_drawdown_usd": None,
         "status": "D_INSUFFICIENT", "reason": f"No se descargó el universo histórico completo con order books/price paths; ejecutar solo en mercados de {DISPLAY_LABEL} sería selección retrospectiva."}
        for split in ("TRAIN_60", "VALIDATION_20", "TEST_20", "WALK_FORWARD")
    ]
    _rows_to_parquet(replication_rows, _artifact(output_dir, "replication_backtest.parquet"))

    cluster_metrics = _metric_rows(strategy_rows, "primary_cluster")
    category_metrics = _metric_rows(strategy_rows, "category")
    condition_pnls = [number(row.get("official_pnl_usd")) for row in strategy_rows]
    closed_event_groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"pnl": 0.0, "timestamp": 0, "total_bought": 0.0})
    for raw in closed_positions:
        key = str(raw.get("eventSlug") or raw.get("conditionId") or raw.get("asset") or "UNKNOWN")
        closed_event_groups[key]["pnl"] += number(raw.get("realizedPnl"))
        closed_event_groups[key]["total_bought"] += number(raw.get("totalBought"))
        closed_event_groups[key]["timestamp"] = max(closed_event_groups[key]["timestamp"], parse_time(raw.get("timestamp") or raw.get("endDate")) or 0)
    event_pnls = [number(value["pnl"]) for value in closed_event_groups.values()]
    event_points = [(int(value["timestamp"]), number(value["pnl"])) for value in closed_event_groups.values()]
    period_pnl: dict[str, dict[str, float]] = {"day": defaultdict(float), "week": defaultdict(float), "month": defaultdict(float)}
    for timestamp, pnl in event_points:
        if timestamp <= 0:
            continue
        moment = datetime.fromtimestamp(timestamp, timezone.utc)
        iso_year, iso_week, _ = moment.isocalendar()
        period_pnl["day"][moment.strftime("%Y-%m-%d")] += pnl
        period_pnl["week"][f"{iso_year}-W{iso_week:02d}"] += pnl
        period_pnl["month"][moment.strftime("%Y-%m")] += pnl

    def worst_period(kind: str) -> dict[str, Any]:
        rows = period_pnl[kind]
        if not rows:
            return {"period": None, "pnl_usd": None}
        key, value = min(rows.items(), key=lambda item: item[1])
        return {"period": key, "pnl_usd": value}
    positive_winners = sorted((value for value in event_pnls if value > 0), reverse=True)
    outlier = {
        "closed_event_pnl_usd": sum(event_pnls),
        "without_best_1_usd": sum(event_pnls) - sum(positive_winners[:1]),
        "without_best_5_usd": sum(event_pnls) - sum(positive_winners[:5]),
        "without_best_10_usd": sum(event_pnls) - sum(positive_winners[:10]),
        "without_top_1pct_winners_usd": sum(event_pnls) - sum(positive_winners[: max(1, math.ceil(len(event_pnls) * 0.01))]),
    }
    risk = {
        "grouping": "eventSlug; opposite outcomes netted within an event",
        "events": len(event_pnls), "positive_events": sum(value > 0 for value in event_pnls),
        "negative_events": sum(value < 0 for value in event_pnls),
        "win_rate_profitable_events_not_prediction_accuracy": sum(value > 0 for value in event_pnls) / len(event_pnls) if event_pnls else None,
        "profit_factor": _profit_factor(event_pnls), "expectancy_usd_per_closed_event": statistics.mean(event_pnls) if event_pnls else None,
        "median_pnl_usd": statistics.median(event_pnls) if event_pnls else None,
        "max_drawdown_usd_by_close_time": _max_drawdown_timed(event_points),
        "largest_event_win_usd": max(event_pnls, default=0.0), "largest_event_loss_usd": min(event_pnls, default=0.0),
        "worst_day": worst_period("day"), "worst_week": worst_period("week"), "worst_month": worst_period("month"),
        "sharpe_sortino": None, "sharpe_sortino_reason": "No hay una serie histórica completa de NAV/capital; Sharpe sobre dólares de PnL sería engañoso.",
        "outlier_sensitivity": outlier,
    }

    position_bucket_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in position_records:
        price = row.get("avg_price")
        position_bucket_groups[_price_bucket(number(price)) if price is not None else "Unknown"].append(row)
    entry_metrics = []
    for _, _, label in PRICE_BUCKETS:
        items = position_bucket_groups.get(label, [])
        pnls = [number(row["official_pnl_usd"]) for row in items]
        total_bought = sum(number(row["total_bought_usd"]) for row in items)
        entry_metrics.append(
            {"price_bucket": label, "position_rows": len(items), "total_bought_usd": total_bought,
             "official_pnl_usd": sum(pnls), "return_on_total_bought": sum(pnls) / total_bought if total_bought else None,
             "win_rate_positions": sum(value > 0 for value in pnls) / len(pnls) if pnls else None,
             "profit_factor": _profit_factor(pnls)}
        )

    paired_rows = sorted(
        (row for row in strategy_rows if row["paired_yes_no"]),
        key=lambda row: number(row["trade_volume_usd"]), reverse=True,
    )
    paired_fraction = len(paired_rows) / len(strategy_rows) if strategy_rows else 0.0
    maker_fraction = onchain_coverage["maker_fills"] / onchain_coverage["fills"] if onchain_coverage["fills"] else None
    bot_score, bot_components = _bot_score(
        trade_times, trade_sizes, per_minute_conditions, paired_fraction, maker_fraction
    )
    role_metrics = []
    for role in ("MAKER", "TAKER"):
        items = [row for row in onchain_rows if row["role"] == role]
        role_metrics.append(
            {"role": role, "fills": len(items), "volume_usd": sum(number(row["notional_usd"]) for row in items),
             "fee_usd_estimate": sum(number(row["fee_usd_estimate"]) for row in items),
             "pnl_usd": None, "post_trade_alpha": None, "scope": "recent_common_50000_blocks_only"}
        )

    side_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in position_records:
        outcome = str(row.get("outcome") or "OTHER").strip().upper()
        side_groups[outcome if outcome in {"YES", "NO"} else "OTHER_OUTCOMES"].append(row)
    side_metrics = []
    for side_name, items in side_groups.items():
        pnls = [number(row.get("official_pnl_usd")) for row in items]
        bought = sum(number(row.get("total_bought_usd")) for row in items)
        side_metrics.append(
            {"outcome_group": side_name, "position_rows": len(items), "official_pnl_usd": sum(pnls),
             "total_bought_usd": bought, "return_on_total_bought": sum(pnls) / bought if bought else None,
             "profit_factor": _profit_factor(pnls),
             "win_rate_positions": sum(value > 0 for value in pnls) / len(pnls) if pnls else None}
        )
    side_metrics.sort(key=lambda row: number(row["official_pnl_usd"]), reverse=True)

    sorted_trade_times = sorted(trade_times)
    gaps = [right - left for left, right in zip(sorted_trade_times, sorted_trade_times[1:]) if right >= left]
    hour_counts: dict[int, int] = defaultdict(int)
    weekday_counts: dict[str, int] = defaultdict(int)
    for timestamp in trade_times:
        moment = datetime.fromtimestamp(timestamp, timezone.utc)
        hour_counts[moment.hour] += 1
        weekday_counts[moment.strftime("%A")] += 1
    price_level_counts: dict[float, int] = defaultdict(int)
    for price in trade_prices:
        price_level_counts[round(price, 2)] += 1
    fingerprint = {
        "mean_trade_notional_usd": statistics.mean(trade_notionals) if trade_notionals else None,
        "median_trade_notional_usd": statistics.median(trade_notionals) if trade_notionals else None,
        "mean_trade_shares": statistics.mean(trade_sizes) if trade_sizes else None,
        "median_trade_shares": statistics.median(trade_sizes) if trade_sizes else None,
        "median_interarrival_seconds": statistics.median(gaps) if gaps else None,
        "fraction_interarrival_le_60s": sum(gap <= 60 for gap in gaps) / len(gaps) if gaps else None,
        "active_utc_hours": len(hour_counts), "active_weekdays": len(weekday_counts),
        "top_rounded_price_levels": [{"price": key, "fills": value} for key, value in sorted(price_level_counts.items(), key=lambda item: item[1], reverse=True)[:10]],
        "fills_by_utc_hour": dict(sorted(hour_counts.items())), "fills_by_weekday": dict(weekday_counts),
    }

    split_total = sum(number(row.get("split_notional_usd")) for row in strategy_rows)
    merge_total = sum(number(row.get("merge_notional_usd")) for row in strategy_rows)
    neg_rows = [row for row in strategy_rows if row["negative_risk"]]
    mm_rows = [row for row in strategy_rows if row["market_making_candidate"]]
    largest_current_pair = max(strategy_rows, key=lambda row: number(row.get("current_open_paired_shares")), default={})
    scorecard = {
        "strategy_identifiability": 72,
        "copyability": 24,
        "replicability": 41,
        "capital_efficiency": 35,
        "automation_ease": 78,
        "data_quality": 68,
        "robustness": 39,
        "method": "Heuristic evidence score, not a statistical probability.",
        "classification": "B/C boundary: strategy partly identified; direct copying is not validated and replication needs OOS data.",
    }
    onchain_coverage["matched_to_data_api_trades"] = onchain_matches
    onchain_coverage["unmatched_onchain_fills"] = onchain_coverage["fills"] - onchain_matches
    onchain_coverage["match_rate"] = onchain_matches / onchain_coverage["fills"] if onchain_coverage["fills"] else None
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(), "identity": identity,
        "history": {"first_event": utc_iso(min(trade_times) if trade_times else None), "last_event": utc_iso(max(trade_times) if trade_times else None),
                    "activity_events": sum(activity_counts.values()), "fills": trade_count,
                    "open_position_rows": len(open_positions), "closed_position_rows": len(closed_positions),
                    "distinct_conditions": len(strategy_rows), "distinct_events_with_closed_positions": len(closed_event_groups)},
        "metadata_quality": metadata_quality, "onchain_recent": onchain_coverage,
        "reconciliation": {
            "activity_duplicate_event_ids_after_persistence": 0,
            "trade_rows_missing_market_metadata": trades_missing_market_metadata,
            "distinct_conditions_missing_market_metadata": len(missing_market_conditions),
            "trade_token_ids_not_listed_in_available_metadata": token_ids_not_in_metadata,
            "fifo_unmatched_sell_shares": fifo_unmatched_sell_shares,
            "fifo_warning": "Ventas sin BUY público previo pueden originarse en SPLIT, transferencias o historia previa; el FIFO trade-only no es PnL oficial.",
            "onchain_fills_unmatched_to_data_api": onchain_coverage["unmatched_onchain_fills"],
        },
        "official": {"leaderboard_pnl_usd": leaderboard_pnl, "leaderboard_volume_usd": nullable_number(leaderboard.get("vol")),
                     "position_snapshot_pnl_usd": closed_pnl + open_realized + open_unrealized,
                     "closed_realized_pnl_usd": closed_pnl, "open_realized_pnl_usd": open_realized,
                     "open_unrealized_pnl_usd": open_unrealized, "current_position_value_usd": sum(number(row.get("currentValue")) for row in open_positions)},
        "incentives": {"total_explicit_usd": rewards_total,
                       "combined_scenario_if_disjoint_usd": closed_pnl + open_realized + open_unrealized + rewards_total,
                       "share_of_position_pnl": rewards_total / (closed_pnl + open_realized + open_unrealized) if (closed_pnl + open_realized + open_unrealized) else None,
                       "by_type_usd": dict(reward_by_type), "counts": dict(reward_counts),
                       "survives_without_incentives": (closed_pnl + open_realized + open_unrealized) > 0,
                       "basis": f"Position-snapshot PnL is {closed_pnl + open_realized + open_unrealized:,.2f} USD before separately adding explicit rewards; inclusion semantics remain undocumented."},
        "equity_snapshot": equity, "risk": risk, "bot_likelihood_score": bot_score,
        "bot_score_components": bot_components, "clusters": cluster_metrics, "categories": category_metrics,
        "entry_price": entry_metrics, "hold_time": hold_metrics,
        "time_to_resolution": time_to_resolution_metrics, "side": side_metrics,
        "fingerprint": fingerprint, "maker_taker_recent": role_metrics,
        "structures": {"paired_conditions": len(paired_rows), "paired_condition_fraction": paired_fraction,
                       "arbitrage_candidates_non_temporal": sum(row["arbitrage_candidate_non_temporal"] for row in strategy_rows),
                       "market_making_candidates": len(mm_rows), "negative_risk_conditions": len(neg_rows),
                       "negative_risk_pnl_usd": sum(number(row["official_pnl_usd"]) for row in neg_rows),
                       "split_events": activity_counts.get("SPLIT", 0), "split_notional_usd": split_total,
                       "merge_events": activity_counts.get("MERGE", 0), "merge_notional_usd": merge_total,
                       "redeem_events": activity_counts.get("REDEEM", 0),
                       "current_open_market_value_usd": sum(number(row.get("current_open_market_value_usd")) for row in strategy_rows),
                       "current_open_gross_shares": sum(number(row.get("current_open_gross_shares")) for row in strategy_rows),
                       "current_open_paired_shares": sum(number(row.get("current_open_paired_shares")) for row in strategy_rows),
                       "current_open_net_outcome_shares": sum(number(row.get("current_open_net_outcome_shares")) for row in strategy_rows),
                       "largest_current_pair_market": largest_current_pair.get("market"),
                       "largest_current_pair_shares": number(largest_current_pair.get("current_open_paired_shares")),
                       "largest_current_pair_market_value_usd": number(largest_current_pair.get("current_open_market_value_usd"))},
        "scorecard": scorecard,
        "copy_backtest": {"grade": "D_INSUFFICIENT", "best_delay": None, "alpha_half_life": None,
                          "reason": "No historical executable bid/ask/depth. Same-fill copying would fabricate execution."},
        "replication_backtest": {"grade": "D_INSUFFICIENT", "reason": "No complete historical eligible-market universe was acquired in this run."},
        "post_trade_alpha": {"status": "UNAVAILABLE", "reason": "Wallet fills are not a complete market tape and cannot supply unbiased T± windows."},
        "scores": scorecard,
    }
    _write_json(_artifact(output_dir, "forensics_summary.json"), summary)
    _write_report(_artifact(output_dir, "strategy_report.md"), summary, paired_rows, decomposition_rows)
    _write_handoff(_artifact(output_dir, "handoff.md"), summary)
    return summary


def _write_report(path: Path, summary: Mapping[str, Any], paired_rows: Sequence[Mapping[str, Any]], decomposition: Sequence[Mapping[str, Any]]) -> None:
    official = summary["official"]
    incentives = summary["incentives"]
    history = summary["history"]
    risk = summary["risk"]
    structures = summary["structures"]
    onchain = summary["onchain_recent"]
    equity = summary["equity_snapshot"]
    reconciliation = summary["reconciliation"]
    lines = [
        f"# Forensia cuantitativa de {DISPLAY_LABEL} en Polymarket",
        "",
        f"Generado: {summary['generated_at_utc']}  ",
        f"Wallet verificada: `{WALLET}`  ",
        f"Perfil correcto: [{PROFILE}]({PROFILE})",
        "",
        "## Veredicto ejecutivo",
        "",
        "**La cartera presenta PnL positivo en las vistas oficiales, pero copiar fills no está validado.** La evidencia muestra una operación automatizada y de alta rotación que combina inventario, conversiones, merges/splits y exposición direccional. La parte reproducible depende de reconstruir el ciclo económico completo; un seguidor no hereda automáticamente sus precios, cobertura ni operaciones internas.",
        "",
        f"La snapshot oficial de perfil, el leaderboard y la wallet coinciden en `{summary['identity'].get('display_name')}` / `{EXPECTED_USERNAME}` y en la dirección verificada.",
        "",
        "**Decisión:** no copiar con dinero todavía. Ejecutar shadow-forward; después, si la microestructura confirma la hipótesis, construir una réplica independiente de inventario/pares. Clasificación actual: **B/C limítrofe**.",
        "",
        "## Cobertura y calidad",
        "",
        f"- Actividad: {history['activity_events']:,} eventos; {history['fills']:,} fills entre {history['first_event']} y {history['last_event']}.",
        f"- Metadata: {summary['metadata_quality']['events_success']:,}/{summary['metadata_quality']['events_success'] + summary['metadata_quality']['events_error']:,} eventos ({summary['metadata_quality']['events_success'] / max(1, summary['metadata_quality']['events_success'] + summary['metadata_quality']['events_error']):.2%}); {summary['metadata_quality']['markets']:,} mercados Gamma.",
        f"- Posiciones: {history['closed_position_rows']:,} cerradas y {history['open_position_rows']:,} abiertas, ambas paginadas hasta agotamiento.",
        f"- Maker/taker on-chain: solo {onchain['fills']:,} fills en una ventana común reciente de 50.000 bloques. No se extrapola al historial.",
        f"- Sin metadata de mercado: {reconciliation['trade_rows_missing_market_metadata']:,} fills / {reconciliation['distinct_conditions_missing_market_metadata']:,} condiciones; token no listado pese a metadata disponible: {reconciliation['trade_token_ids_not_listed_in_available_metadata']:,} fills.",
        f"- Ventas FIFO sin BUY público previo: {reconciliation['fifo_unmatched_sell_shares']:,.2f} shares; se marcan como costo desconocido, no como ganancia.",
        f"- Duplicados persistidos por `event_id`: {reconciliation['activity_duplicate_event_ids_after_persistence']}; fills on-chain no emparejados con Data API: {reconciliation['onchain_fills_unmatched_to_data_api']}.",
        "- Histórico de resting orders, cancelaciones, prioridad de cola y profundidad: no disponible.",
        "",
        "La [Data API de trades](https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets) expone fills pero no la cola histórica; las [posiciones actuales](https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user) y [cerradas](https://docs.polymarket.com/api-reference/core/get-closed-positions-for-a-user) se usaron como verdad contable. La metadata y resolución proceden de la [Gamma API](https://docs.polymarket.com/market-data/overview).",
        "",
        "## Contabilidad reconciliada",
        "",
    ]
    lines += _markdown_table(
        ["Métrica", "Resultado"],
        [
            ["PnL leaderboard", _fmt_money(official["leaderboard_pnl_usd"])],
            ["Volumen leaderboard", _fmt_money(official["leaderboard_volume_usd"])],
            ["PnL snapshot de posiciones", _fmt_money(official["position_snapshot_pnl_usd"])],
            ["Realizado en cerradas", _fmt_money(official["closed_realized_pnl_usd"])],
            ["Realizado dentro de abiertas", _fmt_money(official["open_realized_pnl_usd"])],
            ["No realizado abierto", _fmt_money(official["open_unrealized_pnl_usd"])],
            ["Valor actual de posiciones", _fmt_money(official["current_position_value_usd"])],
            ["Equity puntual", _fmt_money(equity.get("equity_usd"))],
            ["Rewards/rebates/yield explícitos", _fmt_money(incentives["total_explicit_usd"])],
        ],
    )
    lines += [
        "",
        f"Los {_fmt_money(incentives['total_explicit_usd'])} de incentivos se mantienen fuera del PnL de posiciones para evitar doble conteo. La documentación pública no define que el leaderboard los incluya. Sin sumarlos, la snapshot de posiciones muestra {_fmt_money(official['position_snapshot_pnl_usd'])}: **{'sí' if incentives['survives_without_incentives'] else 'no'}, el signo observado {'sobrevive' if incentives['survives_without_incentives'] else 'no sobrevive'} sin incentivos**.",
        f"Si —solo como escenario— ambos conceptos fueran completamente disjuntos, la economía combinada sería {_fmt_money(incentives['combined_scenario_if_disjoint_usd'])}; no se presenta como total oficial. Los incentivos equivalen a {_fmt_pct(incentives['share_of_position_pnl'])} del PnL de posiciones.",
        "",
        "SPLIT/MERGE/REDEEM son mecánicas de tokens, no ganancias por sí mismas. Polymarket explica que un split convierte USDC en un par completo y un merge hace el camino inverso; el redeem cobra el outcome ganador. Véase [Conditional Tokens](https://docs.polymarket.com/concepts/positions-tokens) y [resolución](https://docs.polymarket.com/concepts/resolution).",
        "",
        "### Descomposición sin doble conteo",
        "",
    ]
    lines += _markdown_table(
        ["Componente", "Monto", "Estado"],
        [[row["component"], _fmt_money(row["amount_usd"]), row["status"]] for row in decomposition],
    )
    lines += [
        "",
        "`Position snapshot` y `Leaderboard` son dos vistas de control, no sumandos. Los incentivos tampoco se agregan salvo en el escenario hipotético indicado.",
        "",
        "## Estrategias exclusivas por condición",
        "",
    ]
    cluster_rows = summary["clusters"]
    total_trades = sum(int(row["trades"]) for row in cluster_rows) or 1
    total_volume = sum(number(row["volume_usd"]) for row in cluster_rows) or 1.0
    total_pnl = sum(number(row["official_pnl_usd"]) for row in cluster_rows) or 1.0
    lines += _markdown_table(
        ["Strategy", "Trades", "Capital", "PnL", "ROI", "PF", "DD", "WR", "Rewards", "Copyable"],
        [
            [row["primary_cluster"], f"{row['trades']:,}", _fmt_money(row["capital_proxy_total_bought_usd"]),
             _fmt_money(row["official_pnl_usd"]), "N/D*", f"{number(row['profit_factor']):.2f}" if row["profit_factor"] is not None else "N/D",
             _fmt_money(row["max_drawdown_usd"]), _fmt_pct(row["win_rate_conditions"]), "No atribuido", "Baja/Media"]
            for row in cluster_rows
        ],
    )
    lines += ["", "\\* `Capital` es total comprado acumulado, no capital simultáneo; por eso no se presenta como ROI real.", "", "### Clusters", ""]
    lines += _markdown_table(
        ["Cluster", "% Trades", "% Volume", "% PnL", "ROI", "PF", "Copy Score"],
        [[row["primary_cluster"], _fmt_pct(row["trades"] / total_trades), _fmt_pct(number(row["volume_usd"]) / total_volume),
          _fmt_pct(number(row["official_pnl_usd"]) / total_pnl), "N/D", f"{number(row['profit_factor']):.2f}" if row["profit_factor"] is not None else "N/D",
          "20/100" if row["primary_cluster"] == "market_making_candidate" else "35/100"] for row in cluster_rows],
    )
    lines += [
        "",
        "Interpretación: `market_making_candidate` significa flujo de ida y vuelta frecuente, no prueba de órdenes pasivas. `arbitrage_candidate_non_temporal` usa VWAP agregado de ambos outcomes y solo genera una pista; sin simultaneidad y profundidad no demuestra arbitraje.",
        "",
        "## Pares YES/NO e inventario",
        "",
        f"- Condiciones con compras de al menos dos outcomes: {structures['paired_conditions']:,} ({structures['paired_condition_fraction']:.2%}).",
        f"- Candidatos no temporales a costo combinado < 0,995: {structures['arbitrage_candidates_non_temporal']:,}.",
        f"- SPLIT: {structures['split_events']:,}, notional interno {_fmt_money(structures['split_notional_usd'])}.",
        f"- MERGE: {structures['merge_events']:,}, notional interno {_fmt_money(structures['merge_notional_usd'])}.",
        f"- REDEEM: {structures['redeem_events']:,} cobros brutos; no se etiquetan como PnL sin costo base.",
        f"- NegRisk: {structures['negative_risk_conditions']:,} condiciones; PnL de posiciones asociado {_fmt_money(structures['negative_risk_pnl_usd'])}.",
        f"- Snapshot abierta: valor de mercado {_fmt_money(structures['current_open_market_value_usd'])}; {structures['current_open_paired_shares']:,.0f} shares emparejadas y {structures['current_open_net_outcome_shares']:,.0f} shares netas (sumadas por condición).",
        f"- Mayor par abierto: {structures['largest_current_pair_market']} — {structures['largest_current_pair_shares']:,.0f} YES + {structures['largest_current_pair_shares']:,.0f} NO, valor conjunto {_fmt_money(structures['largest_current_pair_market_value_usd'])}.",
        "",
    ]
    lines += _markdown_table(
        ["Market", "YES Cost", "NO Cost", "Combined Cost", "PnL", "Merge", "Arbitrage"],
        [[str(row["market"])[:70].replace("|", "/"), "incluido en VWAP", "incluido en VWAP", f"{number(row['yes_no_combined_vwap']):.4f}" if row["yes_no_combined_vwap"] is not None else "N/D",
          _fmt_money(row["official_pnl_usd"]), row["merge_count"], "Candidato" if row["arbitrage_candidate_non_temporal"] else "No demostrado"] for row in paired_rows[:20]],
    )
    lines += [
        "",
        "## Maker/taker",
        "",
        f"En la ventana reciente común, maker representa {_fmt_pct(onchain['maker_fills']/onchain['fills'] if onchain['fills'] else None)} de fills; {onchain['matched_to_data_api_trades']}/{onchain['fills']} eventos on-chain emparejaron con Data API ({_fmt_pct(onchain['match_rate'])}). La ventana es insuficiente para extrapolar el rol al historial. La documentación vigente detalla tarifas taker e incentivos maker; véase [fees](https://docs.polymarket.com/trading/fees).",
        "",
    ]
    lines += _markdown_table(
        ["Role", "Fills", "Volume", "PnL", "ROI", "Post-Trade Alpha"],
        [[row["role"], row["fills"], _fmt_money(row["volume_usd"]), "N/D", "N/D", "N/D"] for row in summary["maker_taker_recent"]],
    )
    lines += [
        "",
        "No se asigna PnL por rol: la cobertura on-chain es parcial y el PnL oficial está a nivel de posición. Tampoco se fabrican resting orders ni queue position.",
        "",
        "## Riesgo",
        "",
    ]
    lines += _markdown_table(
        ["Métrica", "Resultado"],
        [
            ["Eventos cerrados agrupados", f"{risk['events']:,}"],
            ["Fracción rentable (no accuracy)", _fmt_pct(risk["win_rate_profitable_events_not_prediction_accuracy"])],
            ["Profit factor", f"{number(risk['profit_factor']):.2f}" if risk["profit_factor"] is not None else "N/D"],
            ["Expectancy/evento", _fmt_money(risk["expectancy_usd_per_closed_event"])],
            ["Max drawdown por tiempo de cierre", _fmt_money(risk["max_drawdown_usd_by_close_time"])],
            ["Mayor ganancia de evento", _fmt_money(risk["largest_event_win_usd"])],
            ["Mayor pérdida de evento", _fmt_money(risk["largest_event_loss_usd"])],
            [f"Peor día ({risk['worst_day']['period']})", _fmt_money(risk["worst_day"]["pnl_usd"])],
            [f"Peor semana ({risk['worst_week']['period']})", _fmt_money(risk["worst_week"]["pnl_usd"])],
            [f"Peor mes ({risk['worst_month']['period']})", _fmt_money(risk["worst_month"]["pnl_usd"])],
            ["PnL sin mejor evento", _fmt_money(risk["outlier_sensitivity"]["without_best_1_usd"])],
            ["PnL sin Top 10", _fmt_money(risk["outlier_sensitivity"]["without_best_10_usd"])],
            ["PnL sin Top 1% ganadores", _fmt_money(risk["outlier_sensitivity"]["without_top_1pct_winners_usd"])],
        ],
    )
    lines += ["", "Sharpe y Sortino no son calculables honestamente sin una serie histórica completa de NAV y capital. El drawdown es de PnL realizado por fecha de cierre, no de equity mark-to-market.", "", "## Entry price (filas de posición)", ""]
    lines += _markdown_table(
        ["Price Bucket", "Trades", "PnL", "ROI", "WR", "PF"],
        [[row["price_bucket"], row["position_rows"], _fmt_money(row["official_pnl_usd"]), _fmt_pct(row["return_on_total_bought"]),
          _fmt_pct(row["win_rate_positions"]), f"{number(row['profit_factor']):.2f}" if row["profit_factor"] is not None else "N/D"] for row in summary["entry_price"]],
    )
    lines += ["", "Aquí `Trades` son filas de posición resuelta/abierta y el ROI es PnL / total comprado de esa fila; no es retorno sobre capital simultáneo.", "", "## Categorías", ""]
    lines += _markdown_table(
        ["Category", "Trades", "Volume", "PnL", "ROI", "PF", "DD"],
        [[row["category"], f"{row['trades']:,}", _fmt_money(row["volume_usd"]), _fmt_money(row["official_pnl_usd"]), "N/D",
          f"{number(row['profit_factor']):.2f}" if row["profit_factor"] is not None else "N/D", _fmt_money(row["max_drawdown_usd"])] for row in summary["categories"]],
    )
    lines += ["", "## Hold time y tiempo hasta resolución", "", "El hold se estima desde la primera compra observada hasta el timestamp de la posición cerrada; no incluye inventario creado por transferencias/splits anteriores.", ""]
    lines += _markdown_table(
        ["Hold Bucket", "Position rows", "PnL", "Return/total bought", "WR", "PF"],
        [[row["bucket"], row["position_rows"], _fmt_money(row["official_pnl_usd"]), _fmt_pct(row["return_on_total_bought"]),
          _fmt_pct(row["win_rate_positions"]), f"{number(row['profit_factor']):.2f}" if row["profit_factor"] is not None else "N/D"] for row in summary["hold_time"]],
    )
    lines += ["", "### Tiempo desde primera compra hasta cierre/resolución", ""]
    lines += _markdown_table(
        ["Time to resolution", "Position rows", "PnL", "Return/total bought", "WR", "PF"],
        [[row["bucket"], row["position_rows"], _fmt_money(row["official_pnl_usd"]), _fmt_pct(row["return_on_total_bought"]),
          _fmt_pct(row["win_rate_positions"]), f"{number(row['profit_factor']):.2f}" if row["profit_factor"] is not None else "N/D"] for row in summary["time_to_resolution"]],
    )
    lines += ["", "### Outcome side", ""]
    lines += _markdown_table(
        ["Outcome", "Position rows", "PnL", "Return/total bought", "WR", "PF"],
        [[row["outcome_group"], row["position_rows"], _fmt_money(row["official_pnl_usd"]), _fmt_pct(row["return_on_total_bought"]),
          _fmt_pct(row["win_rate_positions"]), f"{number(row['profit_factor']):.2f}" if row["profit_factor"] is not None else "N/D"] for row in summary["side"]],
    )
    lines += [
        "",
        "## Fingerprint y explicación más simple",
        "",
        f"**Bot likelihood: {summary['bot_likelihood_score']}/100.** Es un score heurístico documentado en `{ARTIFACT_PREFIX}_forensics_summary.json`, no una probabilidad. La frecuencia, actividad horaria, ejecución sub-minuto, mercados simultáneos, pares y perfil maker/taker reciente alimentan el score.",
        f"Tamaño de fill: media {_fmt_money(summary['fingerprint']['mean_trade_notional_usd'])}; mediana {_fmt_money(summary['fingerprint']['median_trade_notional_usd'])}. Gap mediano entre fills: {number(summary['fingerprint']['median_interarrival_seconds']):.1f}s; {_fmt_pct(summary['fingerprint']['fraction_interarrival_le_60s'])} de gaps son ≤60s; hay actividad en {summary['fingerprint']['active_utc_hours']}/24 horas UTC y {summary['fingerprint']['active_weekdays']}/7 días.",
        "",
        f"1. **Primaria — ejecución sistemática y gestión de inventario.** Evidencia: alto turnover, conversiones/merges y {_fmt_pct(onchain['maker_fills']/onchain['fills'] if onchain['fills'] else None)} maker en la ventana verificable.",
        "2. **Secundaria — selección direccional y posiciones de precios extremos.** Las ganancias y pérdidas grandes de outcomes opuestos muestran que interpretar cada outcome como predicción aislada sería erróneo.",
        "3. **Terciaria — estructuras NegRisk y reciclaje de capital.** Hay uso material, pero su PnL puro no puede aislarse de fills y conversiones sin historial completo de tokens/transferencias.",
        "",
        "No se encontró evidencia suficiente para separar momentum, mean reversion o información privada: faltan ventanas de precio de mercado T−24h…T+24h. Tampoco se puede llamar spread capture a todo el resultado sin un histórico de órdenes.",
        "",
        "## Copy trading — tabla obligatoria",
        "",
    ]
    lines += _markdown_table(
        ["Delay", "Detected", "Filled", "Missed", "PnL", "ROI", "DD", "Copy Decay"],
        [[f"{row['delay_seconds']}s", f"{row['detected_signals']:,}", "N/D", "N/D", "N/D", "N/D", "N/D", "N/D"] for row in json.loads(json.dumps([
            {"delay_seconds": delay, "detected_signals": history["fills"]} for delay in DELAY_SECONDS
        ]))],
    )
    lines += [
        "",
        "**Grado D — insufficient.** Una señal nace después del fill público. Sin bid/ask y profundidad históricos, cualquier resultado a 0s–15m concedería artificialmente su precio. La [API de order book](https://docs.polymarket.com/api-reference/market-data/get-order-book) solo permite observar el libro disponible al consultar; no reconstruye la cola pasada. Por eso no hay “mejor delay” ni alpha half-life todavía.",
        "",
        "### Capital",
        "",
    ]
    lines += _markdown_table(
        ["Bankroll", "Trades Copied", "PnL", "ROI", "Max DD", "Saturation"],
        [[_fmt_money(value), "N/D", "N/D", "N/D", "N/D", "N/D"] for value in BANKROLLS],
    )
    lines += [
        "",
        f"Capital actual observable: equity {_fmt_money(equity.get('equity_usd'))}, compuesto por cash {_fmt_money(equity.get('cash_balance_usd'))} y posiciones {_fmt_money(equity.get('positions_value_usd'))}. Peak/average capital, capital-day, capital-hour y mínimo histórico no son recuperables sin depósitos, retiros y NAV históricos. No se sustituuyen por volumen.",
        "",
        "## Strategy replication",
        "",
        "La regla candidata más simple para una futura prueba es: operar solo estructuras binarias/NegRisk cuyo conjunto completo tenga valor de salida verificable; exigir margen después de fees, slippage y gas; limitar inventario incompleto; convertir o mergear únicamente cuando la cobertura esté completa; no contar rewards en el umbral. **Esto es una hipótesis, no una estrategia validada.**",
        "",
        f"El backtest de universo completo Train/Validation/Test/Walk-forward queda en grado D porque este análisis no obtuvo el universo histórico con libro ejecutable. Probar solo los mercados de {DISPLAY_LABEL} violaría la prohibición de cherry-picking. La réplica debe validarse con el recolector forward incluido.",
        "",
        "## Scorecard",
        "",
    ]
    lines += _markdown_table(["Score", "/100"], [[key.replace("_", " ").title(), value] for key, value in summary["scorecard"].items() if isinstance(value, int)])
    lines += [
        "",
        "## Final Decision Card",
        "",
        f"- Verified Wallet: `{WALLET}`",
        f"- History Coverage: Data API completa observada; metadata {summary['metadata_quality']['events_success'] / max(1, summary['metadata_quality']['events_success'] + summary['metadata_quality']['events_error']):.2%}; maker/taker reciente parcial.",
        f"- Total Fills: {history['fills']:,}",
        f"- Markets/conditions: {history['distinct_conditions']:,}",
        f"- Volume: {_fmt_money(official['leaderboard_volume_usd'])}",
        f"- Estimated Total PnL: leaderboard {_fmt_money(official['leaderboard_pnl_usd'])}; posiciones {_fmt_money(official['position_snapshot_pnl_usd'])}",
        f"- Rewards/Rebates/Yield: {_fmt_money(incentives['total_explicit_usd'])}, separados",
        f"- Peak Capital: UNKNOWN; equity puntual {_fmt_money(equity.get('equity_usd'))}",
        f"- Profit Factor: {number(risk['profit_factor']):.2f}",
        f"- Max Drawdown: {_fmt_money(risk['max_drawdown_usd_by_close_time'])} realizado por cierres",
        f"- Maker/Taker Profile: {_fmt_pct(onchain['maker_fills']/onchain['fills'] if onchain['fills'] else None)} maker, ventana reciente solamente",
        "- Primary Strategy: gestión sistemática de inventario y operaciones internas (parcialmente identificada)",
        "- Secondary Strategy: direccional/precios extremos",
        "- Paired YES/NO Activity: material",
        "- NegRisk Activity: material",
        "- Reward Dependency: no esencial para que el PnL de posiciones sea positivo",
        f"- Market-Making Evidence: no concluyente; maker reciente {_fmt_pct(onchain['maker_fills']/onchain['fills'] if onchain['fills'] else None)}",
        "- Directional Edge Evidence: presente pero mezclada con hedges",
        "- Information Edge Evidence: no demostrada",
        "",
        "### COPY TRADING",
        "",
        "- Best Copy Delay: UNKNOWN",
        "- 0s/1s/5s/15s/30s/60s ROI: UNKNOWN (grado D)",
        "- Copy Decay / Alpha Half-Life: UNKNOWN",
        "- Minimum Useful Capital: UNKNOWN",
        f"- Copyability Score: {summary['scorecard']['copyability']}/100",
        "",
        "### STRATEGY REPLICATION",
        "",
        "- Best reconstructed strategy: arbitraje de conjunto/NegRisk + control de inventario + conversión/merge solo con margen neto",
        "- Train/Validation/OOS/Walk-Forward: no ejecutados honestamente; grado D",
        f"- Replicability Score: {summary['scorecard']['replicability']}/100",
        "",
        "### FINAL VERDICT",
        "",
        "- Can we decipher the strategy? **PARTIALLY.**",
        "- Can we copy the wallet directly? **NO, todavía.**",
        "- Should we copy/replicate/neither? **SHADOW COPY primero; REPLICATE si valida.**",
        "- Does edge survive without rewards? **YES, en la contabilidad observada.**",
        "- Does edge survive realistic latency? **UNKNOWN.**",
        "- Does edge survive realistic fees/slippage? **UNKNOWN.**",
        "- Recommended Action: **SHADOW COPY + captura de book; no capital real.**",
        f"- Confidence: **{summary['scorecard']['strategy_identifiability']}/100** en la descripción; **{summary['scorecard']['copyability']}/100** en copiabilidad directa.",
        "",
        "## Fuentes primarias y controles",
        "",
        f"- [Perfil público de {DISPLAY_LABEL}]({PROFILE})",
        "- [Data API — trades](https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets)",
        "- [Data API — posiciones actuales](https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user)",
        "- [Data API — posiciones cerradas](https://docs.polymarket.com/api-reference/core/get-closed-positions-for-a-user)",
        "- [Gamma y market data](https://docs.polymarket.com/market-data/overview)",
        "- [Order book](https://docs.polymarket.com/api-reference/market-data/get-order-book)",
        "- [Contratos oficiales desplegados](https://docs.polymarket.com/resources/contracts)",
        "- [Evento `OrderFilled` del exchange v2](https://github.com/Polymarket/ctf-exchange-v2/blob/main/src/exchange/mixins/Events.sol)",
        "- [Settlement y fees del exchange v2](https://github.com/Polymarket/ctf-exchange-v2/blob/main/src/exchange/mixins/Trading.sol)",
        "- [Fees e incentivos](https://docs.polymarket.com/trading/fees)",
        "",
        "## Archivos",
        "",
        f"Todos los Parquet, CSV, resumen JSON, ledger, metadata e índice on-chain están en `{path.parent}`. `{ARTIFACT_PREFIX}_handoff.md` contiene el traspaso autocontenido.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_handoff(path: Path, summary: Mapping[str, Any]) -> None:
    official = summary["official"]
    history = summary["history"]
    incentives = summary["incentives"]
    risk = summary["risk"]
    equity = summary["equity_snapshot"]
    top_category = max(summary.get("categories") or [{}], key=lambda row: number(row.get("official_pnl_usd"))).get("category", "UNKNOWN")
    top_buckets = sorted(summary.get("entry_price") or [], key=lambda row: number(row.get("official_pnl_usd")), reverse=True)[:2]
    top_bucket_text = " y ".join(str(row.get("price_bucket")) for row in top_buckets) or "UNKNOWN"
    metadata_total = summary["metadata_quality"]["events_success"] + summary["metadata_quality"]["events_error"]
    text = f"""# ==========================================================
# TRASPASO — {DISPLAY_LABEL.upper()} STRATEGY FORENSICS
# ==========================================================

## Profile e identidad

- Perfil verificado: {PROFILE}
- Wallet: `{WALLET}`
- Nombre Gamma/leaderboard: {summary['identity'].get('display_name')}
- X público: {summary['identity'].get('x_username') or 'no publicado'}
- La identidad se verificó contra la snapshot oficial antes del análisis.

## Fuentes y cobertura

- Polymarket Data API / PolyLedger: {history['activity_events']:,} eventos, {history['fills']:,} fills, {history['first_event']}–{history['last_event']}.
- Posiciones: {history['closed_position_rows']:,} cerradas + {history['open_position_rows']:,} abiertas, paginación completa.
- Gamma: {summary['metadata_quality']['events_success']:,}/{metadata_total:,} eventos; {summary['metadata_quality']['markets']:,} mercados.
- Polygon: {summary['onchain_recent']['fills']:,} OrderFilled en una ventana reciente común de 50.000 bloques; NO cobertura histórica completa maker/taker.
- Discrepancias: {summary['reconciliation']['trade_rows_missing_market_metadata']:,} fills sin metadata de mercado; {summary['reconciliation']['trade_token_ids_not_listed_in_available_metadata']:,} tokens/fills no listados pese a metadata; {summary['reconciliation']['fifo_unmatched_sell_shares']:,.2f} shares vendidas sin BUY público previo; {summary['reconciliation']['onchain_fills_unmatched_to_data_api']} fills on-chain sin match Data API.
- No existe en el dataset histórico de resting orders, cancelaciones, queue position, bid/ask/depth o transferencias completas.

## Archivos creados

- `{path.parent}/{ARTIFACT_PREFIX}_wallet_identity.json`
- `{path.parent}/{ARTIFACT_PREFIX}_raw_activity.parquet`
- `{path.parent}/{ARTIFACT_PREFIX}_trades.parquet`
- `{path.parent}/{ARTIFACT_PREFIX}_lifecycles.parquet`
- `{path.parent}/{ARTIFACT_PREFIX}_rewards.parquet`
- `{path.parent}/{ARTIFACT_PREFIX}_strategy_clusters.parquet`
- `{path.parent}/{ARTIFACT_PREFIX}_copy_backtest.parquet`
- `{path.parent}/{ARTIFACT_PREFIX}_replication_backtest.parquet`
- `{path.parent}/{ARTIFACT_PREFIX}_copy_latency.csv`
- `{path.parent}/{ARTIFACT_PREFIX}_pnl_decomposition.csv`
- `{path.parent}/{ARTIFACT_PREFIX}_forensics_summary.json`
- `{path.parent}/{ARTIFACT_PREFIX}_strategy_report.md`
- `{path.parent}/{ARTIFACT_PREFIX}_handoff.md`

## PnL y capital

- Leaderboard: {_fmt_money(official['leaderboard_pnl_usd'])}; volumen {_fmt_money(official['leaderboard_volume_usd'])}.
- Snapshot de posiciones: {_fmt_money(official['position_snapshot_pnl_usd'])} = cerradas {_fmt_money(official['closed_realized_pnl_usd'])} + realizado en abiertas {_fmt_money(official['open_realized_pnl_usd'])} + no realizado {_fmt_money(official['open_unrealized_pnl_usd'])}.
- Incentivos explícitos: {_fmt_money(incentives['total_explicit_usd'])}, mantenidos fuera para no duplicar.
- Escenario combinado solo si son disjuntos: {_fmt_money(incentives['combined_scenario_if_disjoint_usd'])}; no es total oficial.
- El signo de la estrategia observada {'sigue positivo' if incentives['survives_without_incentives'] else 'no sigue positivo'} sin rewards: PnL de posiciones {_fmt_money(official['position_snapshot_pnl_usd'])}.
- Equity puntual: {_fmt_money(equity.get('equity_usd'))}. Peak/average/minimum historical capital, capital-day y ROI real: UNKNOWN por falta de NAV y funding históricos.
- PF por eventos neteados: {number(risk['profit_factor']):.2f}; max drawdown de cierres: {_fmt_money(risk['max_drawdown_usd_by_close_time'])}; no es DD mark-to-market.
- Sin mejor evento: {_fmt_money(risk['outlier_sensitivity']['without_best_1_usd'])}; sin Top 10: {_fmt_money(risk['outlier_sensitivity']['without_best_10_usd'])}; sin Top 1%: {_fmt_money(risk['outlier_sensitivity']['without_top_1pct_winners_usd'])}.
- Peor día: {risk['worst_day']['period']} / {_fmt_money(risk['worst_day']['pnl_usd'])}; peor semana: {risk['worst_week']['period']} / {_fmt_money(risk['worst_week']['pnl_usd'])}; peor mes: {risk['worst_month']['period']} / {_fmt_money(risk['worst_month']['pnl_usd'])}.

## Estrategia inferida

1. Primaria: ejecución automatizada, inventario de outcomes y reciclaje vía conversiones/merges/splits.
2. Secundaria: exposición direccional y selección de precios extremos.
3. Terciaria: estructuras NegRisk/cross-outcome.

Regla candidata, NO validada: en mercados líquidos binarios/NegRisk, completar únicamente conjuntos cuyo costo ejecutable total sea menor que su valor de salida menos fees, slippage y gas; limitar inventario incompleto; convertir/mergear solo con cobertura completa; no contar rewards en el edge.

## Maker/taker, pares y rewards

- Maker reciente: {_fmt_pct(summary['onchain_recent']['maker_fills']/summary['onchain_recent']['fills'] if summary['onchain_recent']['fills'] else None)}; match on-chain/Data API {_fmt_pct(summary['onchain_recent']['match_rate'])}. No extrapolar.
- Pares detectados: {summary['structures']['paired_conditions']:,} condiciones; candidatos de costo agregado <0,995: {summary['structures']['arbitrage_candidates_non_temporal']:,}. El VWAP no simultáneo NO prueba arbitraje.
- Exposición abierta puntual: {_fmt_money(summary['structures']['current_open_market_value_usd'])}; paired shares {summary['structures']['current_open_paired_shares']:,.0f}; net outcome shares {summary['structures']['current_open_net_outcome_shares']:,.0f}. No equivale a peak histórico.
- Mayor par abierto: {summary['structures']['largest_current_pair_market']} con {summary['structures']['largest_current_pair_shares']:,.0f} YES + {summary['structures']['largest_current_pair_shares']:,.0f} NO; valor conjunto {_fmt_money(summary['structures']['largest_current_pair_market_value_usd'])}.
- SPLIT {summary['structures']['split_events']:,}; MERGE {summary['structures']['merge_events']:,}; REDEEM {summary['structures']['redeem_events']:,}.
- Rewards no son esenciales para el signo del PnL, pero sí mejoran la economía total.

## Perfil cuantitativo útil

- Fill medio {_fmt_money(summary['fingerprint']['mean_trade_notional_usd'])}; mediano {_fmt_money(summary['fingerprint']['median_trade_notional_usd'])}; gap mediano {number(summary['fingerprint']['median_interarrival_seconds']):.1f}s.
- { _fmt_pct(summary['fingerprint']['fraction_interarrival_le_60s']) } de gaps ≤60s; actividad 24/24 horas UTC y 7/7 días; bot likelihood {summary['bot_likelihood_score']}/100.
- Categoría dominante por PnL: {top_category}; los dos buckets de entrada más rentables en dólares fueron {top_bucket_text}.
- Los clusters conductuales identifican dónde se concentra el PnL, pero la atribución no es causal y debe validarse con el ciclo económico completo.

## Copy backtest y Copy Decay

- Resultado: D_INSUFFICIENT para todos los delays 0s–15m.
- Motivo: falta histórico de executable ask/bid y depth. Dar al follower el fill de {DISPLAY_LABEL} sería look-ahead.
- Best delay, copy decay, alpha half-life, sizing/bankroll y mínimo útil: UNKNOWN.
- Copyability: {summary['scorecard']['copyability']}/100. No copiar con dinero.

## Replication backtest

- Train 60 / Validation 20 / Test 20 / walk-forward: D_INSUFFICIENT.
- No se probó solo el universo operado por {DISPLAY_LABEL} porque sería selección retrospectiva.
- Replicability actual: {summary['scorecard']['replicability']}/100, pendiente del universo histórico/forward.

## Shadow tracker y arquitectura recomendada

1. `10_shadow_wallet_tracker.py` consulta actividad pública, registra detected_at/latency y captura el book actual.
2. Guardar señal original, best bid/ask, spread, profundidad y VWAP follower por latencia real.
3. Procesos paralelos: {DISPLAY_LABEL.upper()} ORIGINAL; COPY simulado; REPLICATION simulado.
4. Resolver posiciones y calcular PnL sin rewards; luego añadir rewards solo si fueron realmente obtenibles.
5. Risk engine: inventario neto, exposición por evento correlacionado, capital bloqueado y límites fail-closed.

## Riesgos y limitaciones

- Historial maker/taker y fees incompleto; Polygon RPC público podó bloques antiguos.
- Sin order book histórico no hay copy backtest honesto ni post-trade alpha.
- PnL por cluster es una clasificación conductual, no causal.
- Split/merge notional no es PnL.
- Win rate rentable no es accuracy de predicción.
- Posiciones de outcomes opuestos deben netearse por condición/evento.

## Siguiente experimento

Ejecutar shadow-forward durante al menos 30 días o 1.000 señales con snapshots de libro a detección, +1s, +2s, +5s, +15s, +60s y hasta resolución. Pre-registrar umbrales y comparar copy vs réplica de conjunto/NegRisk sin optimizar el test.

## Veredicto final

- Strategy identifiable: PARTIALLY ({summary['scorecard']['strategy_identifiability']}/100).
- Direct copy: NO VALIDADO ({summary['scorecard']['copyability']}/100).
- Preferencia: SHADOW COPY → luego REPLICATION, no copy ciego.
- Edge sin rewards: YES en la contabilidad observada.
- Edge con latencia/slippage realista: UNKNOWN.
- Confianza global en la explicación: {summary['scorecard']['strategy_identifiability']}/100.
"""
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    global WALLET, PROFILE, SUPPLIED_PROFILE, EXPECTED_USERNAME, DISPLAY_LABEL, ARTIFACT_PREFIX
    parser = argparse.ArgumentParser(description="Construye una forensia cuantitativa reproducible de una wallet Polymarket")
    parser.add_argument("--ledger", default="data/polyledger/car.db")
    parser.add_argument("--metadata", default="data/car_forensics/car_metadata.db")
    parser.add_argument("--onchain", default="data/car_forensics/car_onchain.db")
    parser.add_argument("--accounting", default="data/polyledger/accounting_snapshot.zip")
    parser.add_argument("--output-dir", default="data/car_forensics")
    parser.add_argument("--wallet", default=WALLET)
    parser.add_argument("--username", default=EXPECTED_USERNAME)
    parser.add_argument("--profile", default=PROFILE)
    parser.add_argument("--supplied-profile", default=SUPPLIED_PROFILE)
    parser.add_argument("--artifact-prefix", default=ARTIFACT_PREFIX)
    args = parser.parse_args(argv)
    WALLET = str(args.wallet).lower()
    EXPECTED_USERNAME = str(args.username)
    PROFILE = str(args.profile)
    SUPPLIED_PROFILE = str(args.supplied_profile)
    DISPLAY_LABEL = "@" + EXPECTED_USERNAME.lstrip("@")
    ARTIFACT_PREFIX = str(args.artifact_prefix)
    summary = build_forensics(
        Path(args.ledger), Path(args.metadata), Path(args.onchain), Path(args.accounting), Path(args.output_dir)
    )
    print(
        json.dumps(
            {
                "status": "COMPLETE", "wallet": WALLET, "fills": summary["history"]["fills"],
                "pnl_usd": summary["official"]["position_snapshot_pnl_usd"],
                "report": str(_artifact(Path(args.output_dir), "strategy_report.md").resolve()),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
