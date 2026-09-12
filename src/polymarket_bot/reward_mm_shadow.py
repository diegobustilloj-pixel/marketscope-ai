from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover
    import fcntl


SCHEMA_VERSION = "polymarket_reward_mm_shadow_v001"
CODE_VERSION = "0.1.0"
USER_AGENT = "PolyMarkerQuantBot-RewardMMShadow/0.1 (read-only)"
CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DEFAULT_ROOT = Path("data/reward_mm_shadow_v001")
DISTANCES_CENTS = (0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0)
SINGLE_SIDE_DIVISOR = 3.0


class RewardShadowError(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, database: Path) -> None:
        self.path = database.resolve().with_suffix(database.suffix + ".lock")
        self.handle: Any | None = None

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RewardShadowError("Ya existe un scanner de recompensas usando esta base") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.handle is None:
            return
        self.handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def number(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        try:
            result = datetime.fromisoformat(text + "T00:00:00+00:00")
        except ValueError:
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def http_json(
    url: str,
    *,
    method: str = "GET",
    body: Any | None = None,
    timeout: float = 30.0,
    attempts: int = 3,
) -> Any:
    encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code != 429:
                break
            time.sleep(min(8.0, 1.0 * (2**attempt)))
    raise RewardShadowError(f"Fallo API publica {url}: {last}")


def paginate_rewards(
    endpoint: str,
    *,
    page_size: int = 500,
    extra_query: Mapping[str, Any] | None = None,
    keep_conditions: set[str] | None = None,
    stop_after_empty_rewards: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    seen: set[str] = set()
    pages = 0
    while True:
        query: dict[str, Any] = {"page_size": page_size, **dict(extra_query or {})}
        if cursor:
            query["next_cursor"] = cursor
        url = f"{CLOB}{endpoint}?{urllib.parse.urlencode(query)}"
        payload = http_json(url)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
            raise RewardShadowError(f"Respuesta paginada invalida: {endpoint}")
        page = [dict(item) for item in payload["data"] if isinstance(item, Mapping)]
        kept = [
            item
            for item in page
            if keep_conditions is None or str(item.get("condition_id") or "") in keep_conditions
        ]
        rows.extend(kept)
        pages += 1
        print(
            f"{endpoint}: pagina {pages}, recibidos {len(page)}, relevantes acumulados {len(rows)}",
            file=sys.stderr,
            flush=True,
        )
        next_cursor = str(payload.get("next_cursor") or "")
        no_reward_rows = stop_after_empty_rewards and not any(
            isinstance(item.get("rewards_config"), list) and item.get("rewards_config") for item in page
        )
        found_all = keep_conditions is not None and keep_conditions.issubset(
            {str(item.get("condition_id") or "") for item in rows}
        )
        if not page or no_reward_rows or found_all or not next_cursor or next_cursor in {"LTE=", "LTE", "null", "None"}:
            break
        if next_cursor in seen:
            raise RewardShadowError(f"Cursor repetido en {endpoint}: {next_cursor}")
        seen.add(next_cursor)
        cursor = next_cursor
        if pages >= 500:
            raise RewardShadowError(f"Guard de paginacion alcanzado en {endpoint}")
    return rows, pages


def order_score(max_spread_cents: float, distance_cents: float, size: float) -> float:
    """Base Q contribution for b=1: S(v,s)*size."""
    if (
        max_spread_cents <= 0
        or size <= 0
        or distance_cents < 0
        or distance_cents >= max_spread_cents - 1e-9
    ):
        return 0.0
    return ((max_spread_cents - distance_cents) / max_spread_cents) ** 2 * size


def qmin_score(q_one: float, q_two: float, midpoint: float, divisor: float = SINGLE_SIDE_DIVISOR) -> float:
    """Official two-side/single-side transformation before market normalization."""
    if midpoint < 0.10 or midpoint > 0.90:
        return min(q_one, q_two)
    return max(min(q_one, q_two), max(q_one / divisor, q_two / divisor))


def floor_tick(value: float, tick: float) -> float:
    if tick <= 0:
        raise ValueError("tick debe ser positivo")
    clean_value = Decimal(str(round(value, 12)))
    result = (clean_value / Decimal(str(tick))).to_integral_value(rounding=ROUND_FLOOR)
    return float(result * Decimal(str(tick)))


def levels(book: Mapping[str, Any], side: str) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for row in book.get(side, []) if isinstance(book.get(side), list) else []:
        if not isinstance(row, Mapping):
            continue
        price, size = number(row.get("price")), number(row.get("size"))
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            result.append((price, size))
    return sorted(result, reverse=(side == "bids"))


def best_prices(book: Mapping[str, Any]) -> tuple[float | None, float | None]:
    bids, asks = levels(book, "bids"), levels(book, "asks")
    return (bids[0][0] if bids else None, asks[0][0] if asks else None)


def midpoint_from_book(
    book: Mapping[str, Any], fallback: float | None, minimum_size: float = 0.0
) -> tuple[float | None, str]:
    eligible_bids = [(price, size) for price, size in levels(book, "bids") if size >= minimum_size]
    eligible_asks = [(price, size) for price, size in levels(book, "asks") if size >= minimum_size]
    bid = eligible_bids[0][0] if eligible_bids else None
    ask = eligible_asks[0][0] if eligible_asks else None
    if bid is not None and ask is not None and bid <= ask:
        return (bid + ask) / 2.0, "CLOB_SIZE_FILTERED_LEVEL_PROXY"
    if fallback is not None and 0 < fallback < 1:
        return fallback, "REWARDS_API_TOKEN_PRICE_FALLBACK"
    return None, "UNAVAILABLE"


def competition_metrics(
    yes_book: Mapping[str, Any],
    no_book: Mapping[str, Any],
    *,
    yes_midpoint: float,
    max_spread_cents: float,
    minimum_size: float = 0.0,
) -> dict[str, float]:
    q_one = 0.0
    q_two = 0.0
    depths = {0.5: 0.0, 1.0: 0.0, 2.0: 0.0, max_spread_cents: 0.0}
    no_midpoint = 1.0 - yes_midpoint
    for book, midpoint, bid_bucket, ask_bucket in (
        (yes_book, yes_midpoint, "one", "two"),
        (no_book, no_midpoint, "two", "one"),
    ):
        for side in ("bids", "asks"):
            for price, size in levels(book, side):
                if size < minimum_size:
                    continue
                distance = abs(price - midpoint) * 100.0
                score = order_score(max_spread_cents, distance, size)
                target = bid_bucket if side == "bids" else ask_bucket
                if target == "one":
                    q_one += score
                else:
                    q_two += score
                for threshold in depths:
                    if distance <= threshold + 1e-12:
                        depths[threshold] += size
    return {
        "aggregate_q_one": q_one,
        "aggregate_q_two": q_two,
        "aggregate_qmin_proxy": qmin_score(q_one, q_two, yes_midpoint),
        "depth_05c": depths.get(0.5, 0.0),
        "depth_1c": depths.get(1.0, 0.0),
        "depth_2c": depths.get(2.0, 0.0),
        "depth_max_spread": depths.get(max_spread_cents, 0.0),
    }


def proposed_quote(
    *,
    yes_midpoint: float,
    desired_distance_cents: float,
    tick: float,
    size: float,
    max_spread_cents: float,
    yes_book: Mapping[str, Any],
    no_book: Mapping[str, Any],
    competitor_q_proxy: float,
    daily_reward: float,
    active_hours_remaining: float,
) -> dict[str, Any] | None:
    yes_target = yes_midpoint - desired_distance_cents / 100.0
    no_target = (1.0 - yes_midpoint) - desired_distance_cents / 100.0
    yes_price, no_price = floor_tick(yes_target, tick), floor_tick(no_target, tick)
    _, yes_ask = best_prices(yes_book)
    _, no_ask = best_prices(no_book)
    if yes_ask is not None:
        yes_price = min(yes_price, floor_tick(yes_ask - tick, tick))
    if no_ask is not None:
        no_price = min(no_price, floor_tick(no_ask - tick, tick))
    if yes_price <= 0 or no_price <= 0:
        return None
    yes_distance = (yes_midpoint - yes_price) * 100.0
    no_distance = ((1.0 - yes_midpoint) - no_price) * 100.0
    q_one = order_score(max_spread_cents, yes_distance, size)
    q_two = order_score(max_spread_cents, no_distance, size)
    qmin = qmin_score(q_one, q_two, yes_midpoint)
    capital = size * (yes_price + no_price)
    denominator = competitor_q_proxy + qmin
    share_proxy = qmin / denominator if denominator > 0 else 0.0
    gross_daily = daily_reward * share_proxy
    estimate_hours = min(24.0, max(0.0, active_hours_remaining))
    gross_remaining = gross_daily * estimate_hours / 24.0
    return {
        "desired_distance_cents": desired_distance_cents,
        "yes_bid": yes_price,
        "no_bid": no_price,
        "yes_distance_cents": yes_distance,
        "no_distance_cents": no_distance,
        "size_shares_each": size,
        "q_one": q_one,
        "q_two": q_two,
        "qmin_exact_ours": qmin,
        "competitor_q_aggregate_proxy": competitor_q_proxy,
        "gross_share_public_proxy": share_proxy,
        "capital_required_usdc": capital,
        "gross_reward_daily_proxy_usdc": gross_daily,
        "gross_reward_remaining_proxy_usdc": gross_remaining,
        "gross_reward_per_100_per_day": gross_daily / capital * 100.0 if capital > 0 else None,
        "gross_reward_per_capital_hour": gross_daily / capital / 24.0 if capital > 0 else None,
        "single_side_qmin_comparison": qmin_score(q_one, 0.0, yes_midpoint),
        "decision": "SHADOW_ONLY" if q_one > 1e-12 and q_two > 1e-12 else "NO_TRADE_NONQUALIFYING_TWO_SIDE",
        "net_expected_pnl": None,
    }


def market_category(question: str, slug: str) -> str:
    text = f"{question} {slug}".lower()
    groups = (
        ("Crypto", ("bitcoin", "btc", "ethereum", "eth", "solana", "sol ", "xrp", "crypto", "hyperliquid", "hype")),
        ("Sports", (" vs ", "nba", "nfl", "mlb", "nhl", "ufc", "soccer", "football", "tennis")),
        ("Politics", ("election", "president", "senate", "congress", "trump", "democrat", "republican")),
        ("Weather", ("temperature", "rain", "snow", "hurricane", "weather")),
        ("Economy/Finance", ("fed", "interest rate", "inflation", "gdp", "stock", "s&p", "nasdaq")),
        ("Culture", ("movie", "album", "oscar", "grammy", "youtube", "tweet", "post")),
    )
    return next((name for name, words in groups if any(word in text for word in words)), "Other")


def reward_horizon_hours(row: Mapping[str, Any], observed: datetime) -> float:
    candidates = [parse_datetime(row.get("end_date"))]
    configs = row.get("rewards_config")
    if isinstance(configs, list):
        candidates.extend(parse_datetime(item.get("end_date")) for item in configs if isinstance(item, Mapping))
    finite = [value for value in candidates if value is not None and value.year < 2400]
    if not finite:
        return 24.0
    return max(0.0, (min(finite) - observed).total_seconds() / 3600.0)


def fetch_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    market_id = str(row.get("market_id") or "")
    condition_id = str(row.get("condition_id") or "")
    gamma: Mapping[str, Any] = {}
    clob: Mapping[str, Any] = {}
    errors: list[str] = []
    if market_id:
        try:
            value = http_json(f"{GAMMA}/markets/{urllib.parse.quote(market_id)}")
            gamma = value if isinstance(value, Mapping) else {}
        except RewardShadowError as exc:
            errors.append(f"gamma:{exc}")
    if condition_id:
        try:
            value = http_json(f"{CLOB}/clob-markets/{urllib.parse.quote(condition_id)}")
            clob = value if isinstance(value, Mapping) else {}
        except RewardShadowError as exc:
            errors.append(f"clob:{exc}")
    return {"gamma": dict(gamma), "clob": dict(clob), "errors": errors}


def fetch_books(tokens: Sequence[str], batch_size: int = 100) -> tuple[dict[str, dict[str, Any]], list[str]]:
    result: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for start in range(0, len(tokens), batch_size):
        batch = tokens[start : start + batch_size]
        try:
            payload = http_json(
                f"{CLOB}/books",
                method="POST",
                body=[{"token_id": token} for token in batch],
                timeout=60.0,
            )
            if not isinstance(payload, list):
                raise RewardShadowError("Respuesta /books no es lista")
            for book in payload:
                if not isinstance(book, Mapping):
                    continue
                token = str(book.get("asset_id") or book.get("token_id") or "")
                if token:
                    result[token] = dict(book)
        except RewardShadowError as exc:
            errors.append(f"books[{start}:{start + len(batch)}]:{exc}")
    return result, errors


DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cycles(
 cycle_id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL, finished_at TEXT,
 status TEXT NOT NULL, current_rows INTEGER DEFAULT 0, multi_rows INTEGER DEFAULT 0,
 mapped_rows INTEGER DEFAULT 0, book_rows INTEGER DEFAULT 0, proposal_rows INTEGER DEFAULT 0,
 current_pages INTEGER DEFAULT 0, multi_pages INTEGER DEFAULT 0, errors_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reward_markets(
 cycle_id INTEGER NOT NULL, condition_id TEXT NOT NULL, market_id TEXT, event_id TEXT,
 question TEXT, market_slug TEXT, event_slug TEXT, category TEXT, category_source TEXT,
 created_at TEXT, end_date TEXT, volume_24hr REAL, volume_total REAL, liquidity REAL,
 market_competitiveness REAL, spread_api REAL, rewards_min_size REAL, rewards_max_spread_cents REAL,
 native_daily_rate REAL, sponsored_daily_rate REAL, total_daily_rate REAL,
 reward_start_date TEXT, reward_end_date TEXT, rewards_config_json TEXT,
 yes_token_id TEXT, yes_outcome TEXT, yes_api_price REAL,
 no_token_id TEXT, no_outcome TEXT, no_api_price REAL,
 tick_size REAL, minimum_order_size REAL, neg_risk INTEGER, seconds_delay REAL,
 fees_enabled INTEGER, fee_rate REAL, fee_exponent REAL, maker_rebate_rate REAL,
 holding_rewards_enabled INTEGER, resolution_source TEXT, accepting_orders INTEGER,
 data_status TEXT NOT NULL, raw_multi_json TEXT NOT NULL, raw_current_json TEXT,
 raw_gamma_json TEXT, raw_clob_json TEXT,
 PRIMARY KEY(cycle_id, condition_id)
);
CREATE TABLE IF NOT EXISTS books(
 cycle_id INTEGER NOT NULL, condition_id TEXT NOT NULL, token_id TEXT NOT NULL, outcome TEXT,
 book_timestamp TEXT, best_bid REAL, best_ask REAL, spread REAL, tick_size REAL,
 minimum_order_size REAL, total_bid_size REAL, total_ask_size REAL, raw_json TEXT NOT NULL,
 PRIMARY KEY(cycle_id, token_id)
);
CREATE TABLE IF NOT EXISTS competition(
 cycle_id INTEGER NOT NULL, condition_id TEXT NOT NULL, midpoint REAL, midpoint_source TEXT,
 aggregate_q_one REAL, aggregate_q_two REAL, aggregate_qmin_proxy REAL,
 depth_05c REAL, depth_1c REAL, depth_2c REAL, depth_max_spread REAL,
 limitation TEXT NOT NULL, PRIMARY KEY(cycle_id, condition_id)
);
CREATE TABLE IF NOT EXISTS proposals(
 cycle_id INTEGER NOT NULL, condition_id TEXT NOT NULL, distance_cents REAL NOT NULL,
 yes_bid REAL, no_bid REAL, size_shares_each REAL, q_one REAL, q_two REAL,
 qmin_exact_ours REAL, competitor_q_aggregate_proxy REAL, gross_share_public_proxy REAL,
 capital_required_usdc REAL, gross_reward_daily_proxy_usdc REAL,
 gross_reward_remaining_proxy_usdc REAL, gross_reward_per_100_per_day REAL,
 gross_reward_per_capital_hour REAL, single_side_qmin_comparison REAL,
 decision TEXT NOT NULL, net_expected_pnl REAL,
 PRIMARY KEY(cycle_id, condition_id, distance_cents)
);
"""


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(DDL)
    connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)", (SCHEMA_VERSION,))
    connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('code_version',?)", (CODE_VERSION,))
    connection.commit()
    return connection


def extract_fee(gamma: Mapping[str, Any], clob: Mapping[str, Any]) -> tuple[bool, float | None, float | None, float | None]:
    enabled = boolean(gamma.get("feesEnabled"))
    schedule = gamma.get("feeSchedule") if isinstance(gamma.get("feeSchedule"), Mapping) else {}
    fd = clob.get("fd") if isinstance(clob.get("fd"), Mapping) else {}
    rate = number(fd.get("r"), number(schedule.get("rate")))
    exponent = number(fd.get("e"), number(schedule.get("exponent")))
    rebate = number(schedule.get("rebateRate"))
    return enabled, rate, exponent, rebate


def normalize_market(
    row: Mapping[str, Any], current: Mapping[str, Any] | None, metadata: Mapping[str, Any]
) -> dict[str, Any]:
    gamma = metadata.get("gamma") if isinstance(metadata.get("gamma"), Mapping) else {}
    clob = metadata.get("clob") if isinstance(metadata.get("clob"), Mapping) else {}
    tokens = row.get("tokens") if isinstance(row.get("tokens"), list) else []
    parsed_tokens = [item for item in tokens if isinstance(item, Mapping)]
    if len(parsed_tokens) != 2:
        parsed_tokens = [{}, {}]
    current = current or {}
    configs = row.get("rewards_config") if isinstance(row.get("rewards_config"), list) else []
    starts = [str(item.get("start_date")) for item in configs if isinstance(item, Mapping) and item.get("start_date")]
    ends = [str(item.get("end_date")) for item in configs if isinstance(item, Mapping) and item.get("end_date")]
    total_rate = number(current.get("total_daily_rate"), sum(number(item.get("rate_per_day"), 0.0) or 0.0 for item in configs)) or 0.0
    native_rate = number(current.get("native_daily_rate"))
    tick = number(clob.get("mts"), number(gamma.get("orderPriceMinTickSize"), 0.01)) or 0.01
    minimum = number(clob.get("mos"), number(gamma.get("orderMinSize"), 0.0)) or 0.0
    fees_enabled, fee_rate, fee_exponent, maker_rebate = extract_fee(gamma, clob)
    question = str(row.get("question") or gamma.get("question") or "")
    slug = str(row.get("market_slug") or gamma.get("slug") or "")
    return {
        "condition_id": str(row.get("condition_id") or gamma.get("conditionId") or ""),
        "market_id": str(row.get("market_id") or gamma.get("id") or ""),
        "event_id": str(row.get("event_id") or ""),
        "question": question,
        "market_slug": slug,
        "event_slug": str(row.get("event_slug") or ""),
        "category": market_category(question, slug),
        "category_source": "KEYWORD_HEURISTIC_NOT_OFFICIAL",
        "created_at": row.get("created_at") or gamma.get("createdAt"),
        "end_date": row.get("end_date") or gamma.get("endDate"),
        "volume_24hr": number(row.get("volume_24hr"), number(gamma.get("volume24hr"))),
        "volume_total": number(gamma.get("volumeNum")),
        "liquidity": number(gamma.get("liquidityNum")),
        "market_competitiveness": number(row.get("market_competitiveness")),
        "spread_api": number(row.get("spread"), number(gamma.get("spread"))),
        "rewards_min_size": number(row.get("rewards_min_size"), number(current.get("rewards_min_size"))),
        "rewards_max_spread_cents": number(row.get("rewards_max_spread"), number(current.get("rewards_max_spread"))),
        "native_daily_rate": native_rate,
        "sponsored_daily_rate": max(0.0, total_rate - native_rate) if native_rate is not None else None,
        "total_daily_rate": total_rate,
        "reward_start_date": min(starts) if starts else None,
        "reward_end_date": min(ends) if ends else None,
        "rewards_config_json": json.dumps(configs, ensure_ascii=False, sort_keys=True),
        "yes_token_id": str(parsed_tokens[0].get("token_id") or ""),
        "yes_outcome": str(parsed_tokens[0].get("outcome") or ""),
        "yes_api_price": number(parsed_tokens[0].get("price")),
        "no_token_id": str(parsed_tokens[1].get("token_id") or ""),
        "no_outcome": str(parsed_tokens[1].get("outcome") or ""),
        "no_api_price": number(parsed_tokens[1].get("price")),
        "tick_size": tick,
        "minimum_order_size": minimum,
        "neg_risk": int(boolean(gamma.get("negRisk"))),
        "seconds_delay": number(gamma.get("secondsDelay")),
        "fees_enabled": int(fees_enabled),
        "fee_rate": fee_rate,
        "fee_exponent": fee_exponent,
        "maker_rebate_rate": maker_rebate,
        "holding_rewards_enabled": int(boolean(gamma.get("holdingRewardsEnabled"))),
        "resolution_source": gamma.get("resolutionSource"),
        "accepting_orders": int(boolean(gamma.get("acceptingOrders", clob.get("ao", True)))),
        "data_status": str(row.get("_catalog_status") or ("OK" if not metadata.get("errors") else "PARTIAL_METADATA")),
        "raw_multi_json": json.dumps(dict(row), ensure_ascii=False, sort_keys=True),
        "raw_current_json": json.dumps(dict(current), ensure_ascii=False, sort_keys=True) if current else None,
        "raw_gamma_json": json.dumps(dict(gamma), ensure_ascii=False, sort_keys=True) if gamma else None,
        "raw_clob_json": json.dumps(dict(clob), ensure_ascii=False, sort_keys=True) if clob else None,
    }


MARKET_COLUMNS = (
    "condition_id", "market_id", "event_id", "question", "market_slug", "event_slug", "category",
    "category_source", "created_at", "end_date", "volume_24hr", "volume_total", "liquidity",
    "market_competitiveness", "spread_api", "rewards_min_size", "rewards_max_spread_cents",
    "native_daily_rate", "sponsored_daily_rate", "total_daily_rate", "reward_start_date",
    "reward_end_date", "rewards_config_json", "yes_token_id", "yes_outcome", "yes_api_price",
    "no_token_id", "no_outcome", "no_api_price", "tick_size", "minimum_order_size", "neg_risk",
    "seconds_delay", "fees_enabled", "fee_rate", "fee_exponent", "maker_rebate_rate",
    "holding_rewards_enabled", "resolution_source", "accepting_orders", "data_status",
    "raw_multi_json", "raw_current_json", "raw_gamma_json", "raw_clob_json",
)


def insert_market(connection: sqlite3.Connection, cycle_id: int, row: Mapping[str, Any]) -> None:
    columns = ",".join(MARKET_COLUMNS)
    placeholders = ",".join("?" for _ in MARKET_COLUMNS)
    connection.execute(
        f"INSERT INTO reward_markets(cycle_id,{columns}) VALUES(?,{placeholders})",
        (cycle_id, *(row.get(key) for key in MARKET_COLUMNS)),
    )


def run_cycle(
    connection: sqlite3.Connection,
    *,
    output_root: Path,
    workers: int = 16,
    book_batch_size: int = 100,
    metadata_refresh_seconds: float = 3600.0,
    catalog_refresh_seconds: float = 3600.0,
    scan_markets: int = 200,
    fast_only: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    observed_dt = utc_now()
    observed_at = iso(observed_dt)
    cursor = connection.execute(
        "INSERT INTO cycles(observed_at,status,errors_json) VALUES(?,?,?)",
        (observed_at, "RUNNING", "[]"),
    )
    cycle_id = int(cursor.lastrowid)
    connection.commit()
    errors: list[str] = []
    try:
        previous = connection.execute(
            "SELECT cycle_id,observed_at FROM cycles WHERE cycle_id<? AND status LIKE 'COMPLETE%' ORDER BY cycle_id DESC LIMIT 1",
            (cycle_id,),
        ).fetchone()
        catalog_previous = connection.execute(
            """SELECT cycle_id,observed_at FROM cycles
               WHERE cycle_id<? AND status LIKE 'COMPLETE%' AND current_pages>0 AND multi_rows>1000
               ORDER BY cycle_id DESC LIMIT 1""",
            (cycle_id,),
        ).fetchone()
        if fast_only:
            if not catalog_previous:
                raise RewardShadowError("FAST_ONLY_REQUIERE_UN_CENSO_COMPLETO_PREVIO")
            cached_current: dict[str, dict[str, Any]] = {}
            for condition, raw_current in connection.execute(
                "SELECT condition_id,raw_current_json FROM reward_markets WHERE cycle_id=?",
                (catalog_previous[0],),
            ):
                if not raw_current:
                    continue
                try:
                    value = json.loads(raw_current)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, Mapping):
                    cached_current[str(condition)] = dict(value)
            current_map = cached_current
            current_rows = list(current_map.values())
            current_pages = 0
        else:
            current_rows, current_pages = paginate_rewards("/rewards/markets/current")
            current_map = {str(row.get("condition_id") or ""): row for row in current_rows}
        multi_rows: list[dict[str, Any]] = []
        previous_at = parse_datetime(previous[1]) if previous else None
        previous_age = (observed_dt - previous_at).total_seconds() if previous_at else math.inf
        catalog_at = parse_datetime(catalog_previous[1]) if catalog_previous else None
        catalog_age = (observed_dt - catalog_at).total_seconds() if catalog_at else math.inf
        if fast_only:
            multi_pages = 0
        elif catalog_previous and catalog_age <= catalog_refresh_seconds:
            for condition, raw_multi in connection.execute(
                "SELECT condition_id,raw_multi_json FROM reward_markets WHERE cycle_id=?",
                (catalog_previous[0],),
            ):
                if str(condition) not in current_map or not raw_multi:
                    continue
                try:
                    value = json.loads(raw_multi)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, Mapping):
                    multi_rows.append(dict(value))
            multi_pages = 0
            print(f"Catalogo multi: {len(multi_rows)} filas reutilizadas", file=sys.stderr, flush=True)
        else:
            multi_rows, multi_pages = paginate_rewards(
                "/rewards/markets/multi",
                extra_query={"order_by": "rate_per_day", "position": "DESC"},
                keep_conditions=set(current_map),
                stop_after_empty_rewards=True,
            )
        mapped_conditions = {
            str(row.get("condition_id") or "") for row in multi_rows if not row.get("_catalog_status")
        }
        fast_url = f"{CLOB}/rewards/markets/multi?" + urllib.parse.urlencode(
            {"page_size": 500, "order_by": "rate_per_day", "position": "DESC"}
        )
        fast_payload = http_json(fast_url)
        fast_rows = [
            dict(row)
            for row in (fast_payload.get("data", []) if isinstance(fast_payload, Mapping) else [])
            if isinstance(row, Mapping) and isinstance(row.get("rewards_config"), list) and row.get("rewards_config")
        ]
        catalog = {str(row.get("condition_id") or ""): row for row in multi_rows}
        for row in fast_rows:
            condition = str(row.get("condition_id") or "")
            if condition:
                catalog[condition] = row
        mapped_conditions = {
            condition for condition, row in catalog.items() if not row.get("_catalog_status")
        }
        if not fast_only:
            for condition in sorted(set(current_map) - mapped_conditions):
                current = current_map[condition]
                catalog[condition] = {
                    "condition_id": condition,
                    "rewards_config": current.get("rewards_config", []),
                    "rewards_max_spread": current.get("rewards_max_spread"),
                    "rewards_min_size": current.get("rewards_min_size"),
                    "_catalog_status": "CURRENT_ONLY_NOT_MAPPED_DURING_CYCLE",
                }
        multi_rows = list(catalog.values())
        if limit is not None:
            multi_rows = multi_rows[:limit]

        def coarse_priority(item: Mapping[str, Any]) -> tuple[float, str]:
            current = current_map.get(str(item.get("condition_id") or ""), {})
            reward = number(current.get("total_daily_rate"))
            if reward is None:
                configs = item.get("rewards_config") if isinstance(item.get("rewards_config"), list) else []
                reward = sum(number(config.get("rate_per_day"), 0.0) or 0.0 for config in configs if isinstance(config, Mapping))
            minimum = number(item.get("rewards_min_size"), number(current.get("rewards_min_size"), 0.0)) or 0.0
            return float(reward or 0.0) / max(1.0, minimum), str(item.get("created_at") or "")

        scan_rows = sorted(multi_rows, key=coarse_priority, reverse=True)[: max(0, scan_markets)]
        if fast_only:
            multi_rows = scan_rows
        scan_conditions = {str(row.get("condition_id") or "") for row in scan_rows}

        metadata: dict[str, dict[str, Any]] = {}
        if previous:
            if previous_age <= metadata_refresh_seconds:
                for condition, raw_gamma, raw_clob in connection.execute(
                    "SELECT condition_id,raw_gamma_json,raw_clob_json FROM reward_markets WHERE cycle_id=?",
                    (previous[0],),
                ):
                    try:
                        metadata[str(condition)] = {
                            "gamma": json.loads(raw_gamma) if raw_gamma else {},
                            "clob": json.loads(raw_clob) if raw_clob else {},
                            "errors": [],
                        }
                    except json.JSONDecodeError:
                        pass
        metadata = {condition: value for condition, value in metadata.items() if condition in scan_conditions}
        uncached = [row for row in scan_rows if str(row.get("condition_id") or "") not in metadata]
        print(
            f"Metadatos: {len(metadata)} en cache, {len(uncached)} por consultar",
            file=sys.stderr,
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(fetch_metadata, row): str(row.get("condition_id") or "") for row in uncached}
            for future in as_completed(futures):
                condition = futures[future]
                try:
                    metadata[condition] = future.result()
                except Exception as exc:
                    metadata[condition] = {"gamma": {}, "clob": {}, "errors": [str(exc)]}
                    errors.append(f"metadata:{condition}:{exc}")

        markets: list[dict[str, Any]] = []
        for raw in multi_rows:
            condition = str(raw.get("condition_id") or "")
            market = normalize_market(raw, current_map.get(condition), metadata.get(condition, {}))
            markets.append(market)
            insert_market(connection, cycle_id, market)
        stored_mapped_conditions = {
            market["condition_id"]
            for market in markets
            if market["data_status"] != "CURRENT_ONLY_NOT_MAPPED_DURING_CYCLE"
        }

        all_tokens = list(dict.fromkeys(
            token
            for market in markets
            if market["condition_id"] in scan_conditions
            for token in (market["yes_token_id"], market["no_token_id"])
            if token
        ))
        books, book_errors = fetch_books(all_tokens, book_batch_size)
        print(f"Libros: {len(books)} de {len(all_tokens)} outcomes", file=sys.stderr, flush=True)
        errors.extend(book_errors)
        if len(books) != len(all_tokens):
            errors.append(f"MISSING_BOOKS:{len(all_tokens) - len(books)}_OF_{len(all_tokens)}")
        proposal_rows: list[dict[str, Any]] = []
        for market in markets:
            condition = market["condition_id"]
            if condition not in scan_conditions:
                continue
            yes_book = books.get(market["yes_token_id"], {})
            no_book = books.get(market["no_token_id"], {})
            for token, outcome, book in (
                (market["yes_token_id"], market["yes_outcome"], yes_book),
                (market["no_token_id"], market["no_outcome"], no_book),
            ):
                if not token or not book:
                    continue
                bid, ask = best_prices(book)
                connection.execute(
                    """INSERT INTO books VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        cycle_id, condition, token, outcome, str(book.get("timestamp") or ""), bid, ask,
                        ask - bid if bid is not None and ask is not None else None,
                        number(book.get("tick_size")), number(book.get("min_order_size")),
                        sum(size for _, size in levels(book, "bids")),
                        sum(size for _, size in levels(book, "asks")),
                        json.dumps(book, ensure_ascii=False, sort_keys=True),
                    ),
                )
            if not yes_book or not no_book or not market["accepting_orders"]:
                continue
            max_spread = market["rewards_max_spread_cents"]
            reward_min = market["rewards_min_size"]
            midpoint, midpoint_source = midpoint_from_book(
                yes_book, market["yes_api_price"], float(reward_min or 0.0)
            )
            if midpoint is None or max_spread is None or max_spread <= 0 or reward_min is None or reward_min <= 0:
                continue
            competition = competition_metrics(
                yes_book,
                no_book,
                yes_midpoint=midpoint,
                max_spread_cents=max_spread,
                minimum_size=float(reward_min),
            )
            connection.execute(
                """INSERT INTO competition VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cycle_id, condition, midpoint, midpoint_source, competition["aggregate_q_one"],
                    competition["aggregate_q_two"], competition["aggregate_qmin_proxy"],
                    competition["depth_05c"], competition["depth_1c"], competition["depth_2c"],
                    competition["depth_max_spread"],
                    "PUBLIC_AGGREGATE_PROXY: maker identity, mirrored-order identity, per-order minimum-size eligibility and official sampled normalization are unavailable",
                ),
            )
            live_minimum = max(
                float(market["minimum_order_size"] or 0.0),
                float(number(yes_book.get("min_order_size"), 0.0) or 0.0),
                float(number(no_book.get("min_order_size"), 0.0) or 0.0),
            )
            book_ticks = [
                float(value)
                for value in (number(yes_book.get("tick_size")), number(no_book.get("tick_size")))
                if value is not None and value > 0
            ]
            live_tick = max(book_ticks) if book_ticks else float(market["tick_size"] or 0.01)
            connection.execute(
                "UPDATE reward_markets SET tick_size=?,minimum_order_size=? WHERE cycle_id=? AND condition_id=?",
                (live_tick, live_minimum, cycle_id, condition),
            )
            size = max(float(reward_min), live_minimum)
            tick = live_tick
            horizon = reward_horizon_hours(json.loads(market["raw_multi_json"]), utc_now())
            if horizon <= 0:
                continue
            for distance in DISTANCES_CENTS:
                if distance > max_spread + 1e-12:
                    continue
                proposal = proposed_quote(
                    yes_midpoint=midpoint,
                    desired_distance_cents=distance,
                    tick=tick,
                    size=size,
                    max_spread_cents=max_spread,
                    yes_book=yes_book,
                    no_book=no_book,
                    competitor_q_proxy=competition["aggregate_qmin_proxy"],
                    daily_reward=float(market["total_daily_rate"] or 0.0),
                    active_hours_remaining=horizon,
                )
                if proposal is None:
                    continue
                connection.execute(
                    """INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        cycle_id, condition, distance, proposal["yes_bid"], proposal["no_bid"],
                        proposal["size_shares_each"], proposal["q_one"], proposal["q_two"],
                        proposal["qmin_exact_ours"], proposal["competitor_q_aggregate_proxy"],
                        proposal["gross_share_public_proxy"], proposal["capital_required_usdc"],
                        proposal["gross_reward_daily_proxy_usdc"], proposal["gross_reward_remaining_proxy_usdc"],
                        proposal["gross_reward_per_100_per_day"], proposal["gross_reward_per_capital_hour"],
                        proposal["single_side_qmin_comparison"], proposal["decision"], None,
                    ),
                )
                proposal_rows.append({"condition_id": condition, "question": market["question"], "category": market["category"], **proposal})

        connection.execute(
            """UPDATE cycles SET finished_at=?,status=?,current_rows=?,multi_rows=?,mapped_rows=?,book_rows=?,
               proposal_rows=?,current_pages=?,multi_pages=?,errors_json=? WHERE cycle_id=?""",
            (
                iso(), "COMPLETE" if not errors else "COMPLETE_WITH_WARNINGS", len(current_rows), len(multi_rows),
                len(set(current_map).intersection(stored_mapped_conditions)), len(books),
                len(proposal_rows), current_pages, multi_pages, json.dumps(errors, ensure_ascii=False), cycle_id,
            ),
        )
        connection.commit()
        output_root.mkdir(parents=True, exist_ok=True)
        export_cycle(connection, cycle_id, output_root)
        report = build_report(connection, cycle_id)
        (output_root / "POLYMARKET_REWARDS_FINAL_RESEARCH_REPORT.md").write_text(report, encoding="utf-8")
        summary = {
            "schema": SCHEMA_VERSION,
            "cycle_id": cycle_id,
            "observed_at": observed_at,
            "current_reward_configurations": len(current_rows),
            "active_reward_markets": len(multi_rows),
            "markets_selected_for_live_books": len(scan_conditions),
            "mapped_current_to_multi": len(set(current_map).intersection(stored_mapped_conditions)),
            "books_received": len(books),
            "proposals": len(proposal_rows),
            "warnings": len(errors),
            "mode": "FAST_LANE" if fast_only else "FULL_CENSUS",
            "decision": "MORE_DATA_REQUIRED_SHADOW_ONLY",
        }
        (output_root / "latest_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return summary
    except Exception as exc:
        connection.execute(
            "UPDATE cycles SET finished_at=?,status='FAILED',errors_json=? WHERE cycle_id=?",
            (iso(), json.dumps(errors + [str(exc)], ensure_ascii=False), cycle_id),
        )
        connection.commit()
        raise


def export_cycle(connection: sqlite3.Connection, cycle_id: int, output_root: Path) -> None:
    query = """
      SELECT p.condition_id,m.question,m.category,m.total_daily_rate,m.native_daily_rate,m.sponsored_daily_rate,
             m.rewards_max_spread_cents,m.rewards_min_size,c.midpoint,c.midpoint_source,
             p.distance_cents,p.yes_bid,p.no_bid,p.size_shares_each,p.qmin_exact_ours,
             p.competitor_q_aggregate_proxy,p.gross_share_public_proxy,p.capital_required_usdc,
             p.gross_reward_daily_proxy_usdc,p.gross_reward_remaining_proxy_usdc,
             p.gross_reward_per_100_per_day,p.gross_reward_per_capital_hour,p.decision,p.net_expected_pnl
      FROM proposals p JOIN reward_markets m USING(cycle_id,condition_id)
      JOIN competition c USING(cycle_id,condition_id)
      WHERE p.cycle_id=?
      ORDER BY CASE WHEN p.decision='SHADOW_ONLY' THEN 0 ELSE 1 END,
               p.gross_reward_per_capital_hour DESC
    """
    cursor = connection.execute(query, (cycle_id,))
    rows = cursor.fetchall()
    path = output_root / "latest_ranked_opportunities.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow([item[0] for item in cursor.description])
        writer.writerows(rows)


def build_report(connection: sqlite3.Connection, cycle_id: int) -> str:
    cycle = connection.execute("SELECT * FROM cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
    live_markets = int(cycle[5]) if cycle else 0
    master = connection.execute(
        """SELECT current_rows,multi_rows FROM cycles
           WHERE cycle_id<=? AND status LIKE 'COMPLETE%' AND current_pages>0 AND multi_rows>1000
           ORDER BY cycle_id DESC LIMIT 1""",
        (cycle_id,),
    ).fetchone()
    markets = int(master[0]) if master else live_markets
    history = connection.execute(
        """SELECT MIN(observed_at),MAX(finished_at),COUNT(*),COALESCE(SUM(book_rows),0)
           FROM cycles WHERE cycle_id<=? AND status LIKE 'COMPLETE%'""",
        (cycle_id,),
    ).fetchone()
    first_observed, last_finished, reward_snapshots, books = history
    categories = connection.execute(
        "SELECT GROUP_CONCAT(DISTINCT category) FROM reward_markets WHERE cycle_id=?", (cycle_id,)
    ).fetchone()[0] or "N/A"
    book_markets = connection.execute(
        "SELECT COUNT(DISTINCT condition_id) FROM books WHERE cycle_id=?", (cycle_id,)
    ).fetchone()[0] or 0
    previous_cycle = connection.execute(
        "SELECT MAX(cycle_id) FROM cycles WHERE cycle_id<? AND status LIKE 'COMPLETE%'",
        (cycle_id,),
    ).fetchone()[0]
    shadow_stability = "Aun no existe un segundo ciclo comparable."
    if previous_cycle:
        common, mean_mid, max_mid = connection.execute(
            """SELECT COUNT(*),AVG(ABS(a.midpoint-b.midpoint)),MAX(ABS(a.midpoint-b.midpoint))
               FROM competition a JOIN competition b USING(condition_id)
               WHERE a.cycle_id=? AND b.cycle_id=?""",
            (previous_cycle, cycle_id),
        ).fetchone()
        new_count = connection.execute(
            """SELECT COUNT(*) FROM (
                 SELECT condition_id FROM reward_markets WHERE cycle_id=?
                 EXCEPT SELECT condition_id FROM reward_markets WHERE cycle_id=?
               )""",
            (cycle_id, previous_cycle),
        ).fetchone()[0]
        share_count, mean_share, max_share = connection.execute(
            """WITH a AS (
                 SELECT condition_id,MAX(gross_share_public_proxy) s FROM proposals
                 WHERE cycle_id=? AND decision='SHADOW_ONLY' GROUP BY condition_id
               ), b AS (
                 SELECT condition_id,MAX(gross_share_public_proxy) s FROM proposals
                 WHERE cycle_id=? AND decision='SHADOW_ONLY' GROUP BY condition_id
               ) SELECT COUNT(*),AVG(ABS(a.s-b.s)),MAX(ABS(a.s-b.s)) FROM a JOIN b USING(condition_id)""",
            (previous_cycle, cycle_id),
        ).fetchone()
        shadow_stability = (
            f"Entre los dos ultimos ciclos, {new_count} de {live_markets} mercados cambiaron en la via rapida; "
            f"{common} fueron comparables. Su midpoint se movio {float(mean_mid or 0) * 100:.2f} centavos "
            f"en promedio y {float(max_mid or 0) * 100:.2f} como maximo. En {share_count} mercados con propuesta "
            f"comparable, el share proxy cambio {float(mean_share or 0) * 100:.2f} puntos porcentuales en promedio "
            f"y {float(max_share or 0) * 100:.2f} como maximo. Esto demuestra inestabilidad, no PnL."
        )
    top = connection.execute(
        """SELECT m.question,m.category,m.total_daily_rate,p.distance_cents,p.yes_bid,p.no_bid,
                  p.capital_required_usdc,p.gross_reward_daily_proxy_usdc,p.gross_reward_per_capital_hour,
                  p.gross_share_public_proxy,p.gross_reward_remaining_proxy_usdc,
                  p.qmin_exact_ours,p.competitor_q_aggregate_proxy
           FROM proposals p JOIN reward_markets m USING(cycle_id,condition_id)
           WHERE p.cycle_id=? AND p.decision='SHADOW_ONLY'
           ORDER BY p.gross_reward_per_capital_hour DESC LIMIT 1""",
        (cycle_id,),
    ).fetchone()
    if top:
        candidate = str(top[0])
        category = str(top[1])
        daily = f"${float(top[2]):,.6f}"
        distance = f"{float(top[3]):.3f} centavos por lado"
        quotes = f"YES {float(top[4]):.4f} / NO {float(top[5]):.4f}"
        capital = f"${float(top[6]):,.2f}"
        gross = f"${float(top[7]):,.6f}/dia (proxy bruto, no beneficio)"
        share = f"{float(top[9]) * 100:.4f}% proxy publico"
        remaining = f"${float(top[10]):,.6f} hasta el cierre o las proximas 24h, lo que ocurra primero (proxy bruto)"
        score_comparison = f"Q propio {float(top[11]):,.6f} vs Q agregado publico {float(top[12]):,.6f}"
    else:
        candidate = category = daily = distance = quotes = capital = gross = share = remaining = score_comparison = "No disponible"
    observed = str(cycle[1]) if cycle else "N/A"
    return f"""# POLYMARKET REWARDS — FINAL RESEARCH REPORT

## DATA COVERAGE

Reward markets analyzed: {markets} configuraciones del ultimo censo current completo; el ciclo actual analiza {live_markets} mercados prioritarios con libros.

Categories: {categories}. Clasificacion heuristica, no etiqueta oficial.

Historical period: {first_observed} a {last_finished}; todavia insuficiente para un backtest.

Orderbook snapshots: {books} libros de outcome correspondientes a {book_markets} mercados de la via rapida; el censo de configuraciones si cubre todo el universo.

Reward snapshots: {reward_snapshots} ciclos completos; el ciclo actual contiene {live_markets} mercados en su via de analisis.

Trades: 0. No se simulan fills sin cinta y posicion de cola.

Completeness: censo de configuraciones paginado sin sampling. Los libros se toman en una via rapida limitada por ciclo porque el universo supera 15 mil mercados; faltantes y metadata parcial quedan marcados, no imputados.

Confidence: 35/100 para ranking bruto; 0/100 para Net PnL.

---

# HOW POLYMARKET REWARDS ACTUALLY WORK

Liquidity Rewards: score por orden S(v,s)=((v-s)/v)^2*b y luego se multiplica por size. En precios centrales se permite score unilateral penalizado por divisor 3; debajo de 0.10 o encima de 0.90 se exige liquidez bilateral. El midpoint usa niveles filtrados por minimum size. Polymarket normaliza el Qmin de cada maker en muestras periodicas. El multiplicador in-game no aparece en la respuesta publica usada y no se inventa; el scanner exporta score base con b=1, que es exacto para mercados sin ese boost y no debe llamarse score oficial final en otros casos.

Maker Rebates: dependen solo de volumen maker ejecutado y del fee-equivalent generado; la tasa se recupera de feeSchedule.rebateRate. Sin fills reales no hay rebate atribuible.

Sponsored Rewards: se separan como total_daily_rate - native_daily_rate, usando la configuracion vigente de la API.

Holding Rewards: se registra holdingRewardsEnabled; no se mezcla con Liquidity Rewards.

---

# BEST CATEGORY

INCONCLUSIVE. El candidato de eficiencia bruta actual pertenece a {category}, pero no hay markouts ni fills para estimar retorno neto.

---

# BEST MARKET

{candidate} (solo candidato del scanner, no recomendacion de entrada).

Reward/day: {daily} configurado; en mercados cortos se prorratea por tiempo restante.

Competition: participacion estimada {share}; {score_comparison}. No es la normalizacion oficial por maker.

Capital: {capital}.

Expected reward: {gross}; {remaining}.

Expected maker PnL: No estimable sin fills, rebates, spread realizado y markouts.

Expected net return: No estimable; NO TRADE.

---

# BEST MARKET CHARACTERISTICS

Configuracion activa, ambos libros disponibles, tick y minimo leidos dinamicamente, profundidad calificable baja y horizonte suficiente. Debe excluirse si el libro esta stale, el evento entra en alta velocidad informativa o no puede verificarse scoring.

---

# BEST TIME TO PROVIDE LIQUIDITY

INCONCLUSIVE hasta acumular snapshots por minuto y segmentarlos por hora UTC, fase de mercado y proximidad al evento.

---

# BEST QUOTE DISTANCE

El mayor proxy bruto actual usa {distance}, cotizando {quotes}. No es distancia optima neta: acercarse aumenta score y tambien seleccion adversa.

---

# TWO-SIDED VS SINGLE-SIDED

Winner: two-sided para elegibilidad robusta, no para rentabilidad demostrada.

Reason: evita la penalizacion central y es obligatorio en extremos; exige mas capital y puede dejar inventario unilateral tras un fill.

---

# BEST CAPITAL SIZE

Solo el minimo dinamico por mercado durante shadow. No escalar antes de medir curva marginal, fills, payout minimo y saturacion.

---

# BEST STRATEGY

Shadow two-sided prefunded con quotes post-only hipoteticas a varias distancias, sin enviar ordenes. Seleccionar por reward bruto/capital-hora solo para recolectar evidencia, nunca como señal de trading.

---

# BACKTEST

Days: 0.

Markets: {markets} en un snapshot, no backtest historico.

Rewards: No observados; solo configuracion y proxy.

Maker Rebates: No observados.

Spread PnL: No observado.

Adverse Selection: No medible.

Inventory PnL: No medible.

NET PNL: No medible.

ROI: No medible.

Max DD: No medible.

---

# OUT-OF-SAMPLE

No disponible. Requiere separar cronologicamente datos event-driven recolectados en train/validation/test.

---

# SHADOW FORWARD

Implementado en modo read-only. El censo completo pagina ambos endpoints; la via rapida reutiliza el catalogo, toma libros frescos, recalcula score y persiste propuestas. {shadow_stability} Se requieren al menos 7 dias y multiples ciclos de vida; 30 dias es preferible.

---

# PNL DECOMPOSITION

Liquidity Rewards: No realizado.

Maker Rebates: No realizado.

Spread: No realizado.

Inventory: No realizado.

Adverse Selection: No medido.

Costs: No medidos.

TOTAL: INCONCLUSIVE.

---

# CAPITAL EFFICIENCY

PnL per $100: No medible; el CSV contiene solo reward bruto proxy por $100.

PnL per $1,000: No medible.

PnL per Capital Hour: No medible.

Peak Capital: No asignado; shadow usa capital hipotetico minimo.

---

# RISK

Primary risk: confundir reward bruto estimado con Net PnL.

Inventory risk: un solo fill crea exposicion direccional.

Adverse selection: no medida y potencialmente dominante en crypto corto, deportes live y noticias.

Resolution risk: debe evaluarse por reglas y fuente antes de entrada.

Operational risk: staleness, tick cambiante, queue desconocida y normalizacion oficial no observable publicamente.

---

# SCALABILITY

$100: No validado.

$500: No validado.

$1,000: No validado.

$5,000: No validado.

$10,000: No validado.

$50,000: No validado.

Estimated saturation point: No estimable sin curva marginal de score, queue y fills.

---

# BOT

Should we build it?

MORE DATA REQUIRED.

Bot type:

Scanner y shadow logger read-only. El modulo de ejecucion real queda deliberadamente ausente.

---

# REWARDS SCORECARD

Profitability: 0/100 (no demostrada).

Capital Efficiency: 35/100 (proxy disponible, neto no).

Time Efficiency: 35/100.

Predictability: 20/100.

Automation Ease: 70/100 para datos; no para ejecucion segura.

Scalability: 20/100.

Liquidity: 45/100.

Risk Adjusted Return: 0/100 (no medido).

Data Availability: 45/100 (snapshot publico; maker identity/queue ausentes).

Strategy Robustness: 10/100.

FINAL REWARDS SCORE:

28/100 provisional.

---

# WINNING STRATEGY

Select ONE.

Strategy: Ninguna estrategia real seleccionada; shadow two-sided como protocolo de investigacion.

Market selection: todos los incentivados activos; priorizar para observacion reward bruto/capital-hora y penalizar informacion rapida.

Quote: {quotes}, candidato actual a {distance}; recalculado con tick vigente.

Size: minimo reward/minimo de orden dinamico.

Reprice logic: cada minuto en shadow; retirar hipoteticamente ante cambio de midpoint, reward o libro stale.

Inventory logic: sin fills reales. Futuro limite duro por mercado/evento y skew tras primer fill.

Risk: NO TRADE hasta markouts 1s/5s/15s/30s/1m/5m/15m y reward real.

Capital: {capital} hipotetico para el candidato actual.

Expected reward: {gross}; {remaining}.

Expected total return: INCONCLUSIVE.

Confidence: 20/100.

---

# WHAT WE LEARNED

New findings: el score base propio puede reconstruirse exactamente condicionado al midpoint y b, pero el libro agregado no permite reconstruir la normalizacion por maker ni la elegibilidad de cada orden agregada. El midpoint filtrado por size es material. Rewards diarios enormes en mercados de minutos deben prorratearse; no representan cobro de un dia completo.

Prompt improvements: exigir un periodo minimo de captura, credenciales de solo lectura para order-scoring cuando existan ordenes propias, y criterios preregistrados antes de declarar ganador.

---

# FINAL VERDICT

¿Existe edge proporcionando liquidez?

INCONCLUSIVE.

¿Dónde?

No demostrado. El primer candidato bruto es {candidate}.

¿Cuándo?

No demostrado.

¿Con cuánto capital?

Solo minimo hipotetico durante shadow: {capital} en el candidato mostrado.

¿A qué spread?

No demostrado; candidato bruto {distance}.

¿Two-sided o single-sided?

Two-sided para el experimento; rentabilidad no probada.

¿Cuánto viene de Rewards?

No realizado; proxy del candidato {gross}; {remaining}.

¿Cuánto de Maker Rebates?

Desconocido hasta fills maker reales.

¿Cuánto del Spread?

Desconocido.

¿Cuál es el verdadero Net PnL?

Desconocido; cualquier numero ahora seria inventado.

¿Puede automatizarse?

YES, la captura y el shadow. NO todavia la asignacion con dinero real.

¿Construirías el bot?

NO como ejecutor real; YES como shadow collector ya implementado.

Confidence:

35/100 para esta conclusion prudencial; 0/100 para rentabilidad.

---
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scanner read-only de Liquidity Rewards y market making shadow")
    parser.add_argument("--database", type=Path, default=DEFAULT_ROOT / "reward_mm_shadow.db")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--duration-hours", type=float, default=0.0, help="0 ejecuta un solo ciclo")
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--book-batch-size", type=int, default=100)
    parser.add_argument("--metadata-refresh-seconds", type=float, default=3600.0)
    parser.add_argument("--catalog-refresh-seconds", type=float, default=3600.0)
    parser.add_argument("--scan-markets", type=int, default=200, help="fast lane con libros y metadatos por ciclo")
    parser.add_argument("--fast-only", action="store_true", help="usa el ultimo censo y refresca solo la via rapida")
    parser.add_argument("--limit", type=int, help="solo para pruebas; omitir para censo completo")
    parser.add_argument("--max-database-gb", type=float, default=5.0, help="kill switch de almacenamiento")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.poll_seconds < 10 or args.duration_hours < 0 or args.workers < 1 or args.book_batch_size < 1 or args.scan_markets < 1:
        raise SystemExit("Parametros invalidos")
    if args.max_database_gb <= 0:
        raise SystemExit("--max-database-gb debe ser positivo")
    with ProcessLock(args.database):
        connection = open_database(args.database)
        deadline = time.monotonic() + args.duration_hours * 3600.0
        summaries: list[dict[str, Any]] = []
        try:
            while True:
                if args.database.exists() and args.database.stat().st_size >= args.max_database_gb * 1_000_000_000:
                    raise RewardShadowError("KILL_SWITCH_DATABASE_SIZE_LIMIT")
                started = time.monotonic()
                summary = run_cycle(
                    connection,
                    output_root=args.output_root,
                    workers=args.workers,
                    book_batch_size=args.book_batch_size,
                    metadata_refresh_seconds=args.metadata_refresh_seconds,
                    catalog_refresh_seconds=args.catalog_refresh_seconds,
                    scan_markets=args.scan_markets,
                    fast_only=args.fast_only,
                    limit=args.limit,
                )
                summaries.append(summary)
                print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
                if args.duration_hours <= 0 or time.monotonic() >= deadline:
                    break
                time.sleep(max(0.0, args.poll_seconds - (time.monotonic() - started)))
        except KeyboardInterrupt:
            print("Monitor detenido por el usuario.")
        finally:
            connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
