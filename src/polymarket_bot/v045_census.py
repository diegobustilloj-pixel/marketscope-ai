from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v045_contract import VARIANT, load_and_verify_preregistration


RESULT_SCHEMA = "result_v045_neg_risk_structural_census_1"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BASE_URL = "https://clob.polymarket.com"


class V045CensusError(RuntimeError):
    pass


def classify_census(
    *, eligible_events: int, evaluated_events: int, cost_candidates: int
) -> dict[str, Any]:
    minimum_evaluated = max(3, math.ceil(0.5 * eligible_events)) if eligible_events else 1
    coverage = evaluated_events / eligible_events if eligible_events else 0.0
    if evaluated_events < minimum_evaluated:
        return {
            "verdict": "FAIL_CENSUS_INSUFFICIENT_COMPARABLE_BOOKS",
            "next_step": "REVIEW_BATCH_BOOK_TIMESTAMP_SEMANTICS_BEFORE_ANY_NEW_CENSUS",
            "economic_family_conclusion_allowed": False,
            "minimum_evaluated_events": minimum_evaluated,
            "coverage": coverage,
        }
    if cost_candidates:
        return {
            "verdict": "REQUIRE_MANUAL_SEMANTIC_REVIEW_AND_FRESH_PERSISTENCE_OBSERVER",
            "next_step": "MANUALLY_VERIFY_EXHAUSTIVENESS_THEN_PREREGISTER_READ_ONLY_PERSISTENCE_OBSERVER",
            "economic_family_conclusion_allowed": False,
            "minimum_evaluated_events": minimum_evaluated,
            "coverage": coverage,
        }
    return {
        "verdict": "REJECT_NEG_RISK_BUY_ALL_YES_TOP100_SNAPSHOT",
        "next_step": "DO_NOT_LAUNCH_OBSERVER_FOR_THIS_HYPOTHESIS",
        "economic_family_conclusion_allowed": True,
        "minimum_evaluated_events": minimum_evaluated,
        "coverage": coverage,
    }


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _http_json(
    method: str,
    url: str,
    body: Any | None = None,
    *,
    timeout: float = 30.0,
) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "PolyMarkerQuantBot-V0.45-read-only-census/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # urllib has several transport exception families
        raise V045CensusError(f"Fallo endpoint publico {url}: {exc}") from exc


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _float(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise V045CensusError(f"Campo numerico invalido: {field}") from exc
    if not math.isfinite(number):
        raise V045CensusError(f"Campo numerico no finito: {field}")
    return number


def _gamma_fee(market: Mapping[str, Any]) -> tuple[float, float, bool] | None:
    enabled = _boolean(market.get("feesEnabled"))
    schedule = market.get("feeSchedule")
    if not enabled:
        return (0.0, 1.0, True)
    if not isinstance(schedule, Mapping):
        return None
    try:
        rate = _float(schedule.get("rate"), field="feeSchedule.rate")
        exponent = _float(schedule.get("exponent"), field="feeSchedule.exponent")
    except V045CensusError:
        return None
    taker_only = _boolean(schedule.get("takerOnly"))
    if rate < 0.0 or exponent <= 0.0 or not taker_only:
        return None
    return (rate, exponent, taker_only)


def filter_event(
    event: Mapping[str, Any], contract: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    filters = contract["event_filters"]
    if not _boolean(event.get("active")) or _boolean(event.get("closed")):
        return None, "EVENT_NOT_OPEN"
    if not _boolean(event.get("negRisk")):
        return None, "EVENT_NOT_NEG_RISK"
    if _boolean(event.get("negRiskAugmented")):
        return None, "EVENT_AUGMENTED"
    markets = event.get("markets")
    if not isinstance(markets, list):
        return None, "MARKETS_MISSING"
    if not int(filters["minimum_markets"]) <= len(markets) <= int(filters["maximum_markets"]):
        return None, "MARKET_COUNT_OUT_OF_SCOPE"
    normalized: list[dict[str, Any]] = []
    condition_ids: set[str] = set()
    yes_token_ids: set[str] = set()
    for market in markets:
        if not isinstance(market, Mapping):
            return None, "MARKET_INVALID"
        if not _boolean(market.get("active")) or _boolean(market.get("closed")):
            return None, "PARTIAL_EVENT_MARKET_NOT_OPEN"
        if not _boolean(market.get("acceptingOrders")):
            return None, "MARKET_NOT_ACCEPTING_ORDERS"
        if not _boolean(market.get("enableOrderBook")):
            return None, "ORDER_BOOK_DISABLED"
        if not _boolean(market.get("negRisk")):
            return None, "MARKET_NOT_NEG_RISK"
        if _boolean(market.get("negRiskOther")):
            return None, "NEG_RISK_OTHER_NOT_ALLOWED"
        outcomes = _json_value(market.get("outcomes"))
        tokens = _json_value(market.get("clobTokenIds"))
        if outcomes != filters["required_binary_outcomes"]:
            return None, "OUTCOMES_NOT_YES_NO"
        if not isinstance(tokens, list) or len(tokens) != 2:
            return None, "TOKEN_IDS_INVALID"
        condition_id = str(market.get("conditionId") or "")
        yes_token = str(tokens[0])
        if not condition_id or not yes_token:
            return None, "IDENTIFIER_MISSING"
        if condition_id in condition_ids or yes_token in yes_token_ids:
            return None, "DUPLICATE_IDENTIFIER"
        gamma_fee = _gamma_fee(market)
        if gamma_fee is None:
            return None, "GAMMA_FEE_INVALID"
        condition_ids.add(condition_id)
        yes_token_ids.add(yes_token)
        normalized.append(
            {
                "condition_id": condition_id,
                "market_id": str(market.get("id") or ""),
                "question": str(market.get("question") or ""),
                "slug": str(market.get("slug") or ""),
                "yes_token_id": yes_token,
                "gamma_fee_rate": gamma_fee[0],
                "gamma_fee_exponent": gamma_fee[1],
            }
        )
    return {
        "event_id": str(event.get("id") or ""),
        "slug": str(event.get("slug") or ""),
        "title": str(event.get("title") or ""),
        "market_count": len(normalized),
        "volume24hr": float(event.get("volume24hr") or 0.0),
        "markets": normalized,
    }, None


def _clob_market_values(
    info: Mapping[str, Any],
) -> tuple[float, float, bool | None, float, float, bool]:
    minimum_order = _float(
        info.get("mos", info.get("minimum_order_size", info.get("minimumOrderSize"))),
        field="minimum_order_size",
    )
    tick = _float(
        info.get("mts", info.get("minimum_tick_size", info.get("minimumTickSize"))),
        field="minimum_tick_size",
    )
    neg_risk_value = info.get("neg_risk", info.get("negRisk"))
    neg_risk = None if neg_risk_value is None else _boolean(neg_risk_value)
    fee_info = info.get("fd") or info.get("feeSchedule")
    fees_enabled = _boolean(info.get("fees_enabled", info.get("feesEnabled")))
    if not fees_enabled and not isinstance(fee_info, Mapping):
        return minimum_order, tick, neg_risk, 0.0, 1.0, True
    if not isinstance(fee_info, Mapping):
        raise V045CensusError("CLOB fd ausente para mercado con comision")
    rate = _float(fee_info.get("r", fee_info.get("rate")), field="fd.r")
    exponent = _float(fee_info.get("e", fee_info.get("exponent")), field="fd.e")
    taker_only = _boolean(fee_info.get("to", fee_info.get("takerOnly")))
    return minimum_order, tick, neg_risk, rate, exponent, taker_only


def validate_clob_market(
    market: Mapping[str, Any], info: Mapping[str, Any], shares: float
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        minimum_order, tick, neg_risk, rate, exponent, taker_only = _clob_market_values(info)
    except V045CensusError:
        return None, "CLOB_MARKET_INFO_INVALID"
    reported_condition = str(info.get("condition_id", info.get("conditionId", "")))
    if reported_condition and reported_condition.lower() != str(market["condition_id"]).lower():
        return None, "CONDITION_ID_MISMATCH"
    if neg_risk is False:
        return None, "CLOB_NEG_RISK_MISMATCH"
    if minimum_order <= 0.0 or minimum_order > shares:
        return None, "MINIMUM_ORDER_EXCEEDS_POSITION"
    if tick not in {0.1, 0.01, 0.001, 0.0001}:
        return None, "TICK_SIZE_INVALID"
    if not taker_only or rate < 0.0 or exponent <= 0.0:
        return None, "CLOB_FEE_INVALID"
    if abs(rate - float(market["gamma_fee_rate"])) > 1e-12:
        return None, "FEE_RATE_MISMATCH"
    if abs(exponent - float(market["gamma_fee_exponent"])) > 1e-12:
        return None, "FEE_EXPONENT_MISMATCH"
    tokens = info.get("t", info.get("tokens"))
    if isinstance(tokens, list):
        yes_ids = {
            str(token.get("t", token.get("token_id", token.get("tokenId", ""))))
            for token in tokens
            if isinstance(token, Mapping)
            and str(token.get("o", token.get("outcome", ""))).lower() == "yes"
        }
        if yes_ids and str(market["yes_token_id"]) not in yes_ids:
            return None, "YES_TOKEN_MISMATCH"
    return {
        **dict(market),
        "minimum_order_size": minimum_order,
        "minimum_tick_size": tick,
        "fee_rate": rate,
        "fee_exponent": exponent,
        "fee_taker_only": taker_only,
    }, None


def executable_buy_cost(
    asks: Sequence[Mapping[str, Any]],
    *,
    shares: float,
    fee_rate: float,
    fee_exponent: float,
    reserve_tick: float = 0.0,
) -> dict[str, Any] | None:
    levels: list[tuple[float, float]] = []
    for level in asks:
        try:
            price = _float(level.get("price"), field="book.price")
            size = _float(level.get("size"), field="book.size")
        except V045CensusError:
            return None
        if not 0.0 < price < 1.0 or size <= 0.0:
            return None
        levels.append((price, size))
    levels.sort(key=lambda item: item[0])
    remaining = float(shares)
    cash = 0.0
    fee = 0.0
    fills: list[dict[str, float]] = []
    for price, available in levels:
        take = min(remaining, available)
        if take <= 0.0:
            continue
        adjusted = min(0.9999, price + float(reserve_tick))
        level_fee = take * fee_rate * (adjusted * (1.0 - adjusted)) ** fee_exponent
        cash += take * adjusted
        fee += level_fee
        fills.append({"price": adjusted, "shares": take, "fee": level_fee})
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return None
    return {
        "shares": shares,
        "cash": cash,
        "fee": fee,
        "total_cost": cash + fee,
        "average_total_cost_per_share": (cash + fee) / shares,
        "fills": fills,
    }


def _book_timestamp_ms(book: Mapping[str, Any]) -> int | None:
    value = book.get("timestamp")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def evaluate_event_books(
    event: Mapping[str, Any],
    books: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    book_map = {
        str(book.get("asset_id", book.get("assetId", ""))): book
        for book in books
        if isinstance(book, Mapping)
    }
    timestamps = [_book_timestamp_ms(book) for book in book_map.values()]
    if any(value is None for value in timestamps):
        return None, "BOOK_TIMESTAMP_MISSING"
    spread = max(int(value) for value in timestamps) - min(int(value) for value in timestamps)
    maximum_spread = int(contract["cost_model"]["maximum_book_timestamp_spread_ms"])
    if spread > maximum_spread:
        return None, "BOOK_TIMESTAMP_SPREAD_EXCEEDED"
    shares = float(contract["hypothetical_position"]["shares_per_leg"])
    observed_total = 0.0
    conservative_total = 0.0
    legs: list[dict[str, Any]] = []
    for market in event["markets"]:
        token = str(market["yes_token_id"])
        book = book_map.get(token)
        if book is None:
            return None, "BOOK_MISSING"
        asks = book.get("asks")
        if not isinstance(asks, list):
            return None, "ASKS_INVALID"
        observed = executable_buy_cost(
            asks,
            shares=shares,
            fee_rate=float(market["fee_rate"]),
            fee_exponent=float(market["fee_exponent"]),
        )
        conservative = executable_buy_cost(
            asks,
            shares=shares,
            fee_rate=float(market["fee_rate"]),
            fee_exponent=float(market["fee_exponent"]),
            reserve_tick=float(market["minimum_tick_size"]),
        )
        if observed is None or conservative is None:
            return None, "INSUFFICIENT_ASK_DEPTH"
        observed_total += float(observed["average_total_cost_per_share"])
        conservative_total += float(conservative["average_total_cost_per_share"])
        legs.append(
            {
                "condition_id": market["condition_id"],
                "question": market["question"],
                "yes_token_id": token,
                "tick_size": market["minimum_tick_size"],
                "fee_rate": market["fee_rate"],
                "fee_exponent": market["fee_exponent"],
                "observed": observed,
                "conservative": conservative,
                "book_timestamp_ms": _book_timestamp_ms(book),
                "book_hash": str(book.get("hash") or ""),
            }
        )
    payout = float(contract["hypothetical_position"]["payout_if_exactly_one_yes_per_share"])
    observed_edge = payout - observed_total
    conservative_edge = payout - conservative_total
    threshold = float(contract["cost_model"]["minimum_conservative_conditional_edge_per_share"])
    return {
        "event_id": event["event_id"],
        "slug": event["slug"],
        "title": event["title"],
        "market_count": event["market_count"],
        "volume24hr": event["volume24hr"],
        "book_timestamp_spread_ms": spread,
        "observed_total_cost_per_share": observed_total,
        "observed_conditional_edge_per_share": observed_edge,
        "conservative_total_cost_per_share": conservative_total,
        "conservative_conditional_edge_per_share": conservative_edge,
        "cost_candidate": observed_edge > 0.0 and conservative_edge >= threshold,
        "semantic_exhaustiveness_verified": False,
        "executable_atomically": False,
        "legs": legs,
    }, None


def census_v045(
    *,
    prereg_path: str | Path,
    result_path: str | Path,
    project_root: str | Path = ROOT,
    http_json: Callable[[str, str, Any | None], Any] = _http_json,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    result_file = Path(result_path).resolve()
    prereg = load_and_verify_preregistration(prereg_file, project_root=root)
    if result_file.is_file():
        return json.loads(result_file.read_text(encoding="utf-8"))
    contract = prereg["economic_contract"]
    sample = contract["event_sample"]
    query = urllib.parse.urlencode(
        {
            "active": str(sample["active"]).lower(),
            "closed": str(sample["closed"]).lower(),
            "limit": sample["limit"],
            "order": sample["order"],
            "ascending": str(sample["ascending"]).lower(),
        }
    )
    started = time.monotonic()
    raw_events = http_json("GET", f"{GAMMA_EVENTS_URL}?{query}", None)
    if not isinstance(raw_events, list):
        raise V045CensusError("Gamma no devolvio una lista de eventos")
    filter_rejections: Counter[str] = Counter()
    eligible: list[dict[str, Any]] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            filter_rejections["EVENT_INVALID"] += 1
            continue
        event, reason = filter_event(raw, contract)
        if event is None:
            filter_rejections[str(reason)] += 1
        else:
            eligible.append(event)
    evaluated: list[dict[str, Any]] = []
    runtime_rejections: Counter[str] = Counter()
    for event in eligible:
        validated_markets: list[dict[str, Any]] = []
        failed_reason: str | None = None
        for market in event["markets"]:
            condition = urllib.parse.quote(str(market["condition_id"]), safe="")
            info = http_json("GET", f"{CLOB_BASE_URL}/clob-markets/{condition}", None)
            if not isinstance(info, Mapping):
                failed_reason = "CLOB_MARKET_INFO_INVALID"
                break
            validated, reason = validate_clob_market(
                market,
                info,
                float(contract["hypothetical_position"]["shares_per_leg"]),
            )
            if validated is None:
                failed_reason = reason
                break
            validated_markets.append(validated)
        if failed_reason is not None:
            runtime_rejections[str(failed_reason)] += 1
            continue
        validated_event = {**event, "markets": validated_markets}
        request_books = [{"token_id": market["yes_token_id"]} for market in validated_markets]
        raw_books = http_json("POST", f"{CLOB_BASE_URL}/books", request_books)
        if not isinstance(raw_books, list):
            runtime_rejections["BOOK_RESPONSE_INVALID"] += 1
            continue
        record, reason = evaluate_event_books(validated_event, raw_books, contract)
        if record is None:
            runtime_rejections[str(reason)] += 1
            continue
        evaluated.append(record)
    evaluated.sort(key=lambda item: float(item["conservative_conditional_edge_per_share"]), reverse=True)
    candidates = [item for item in evaluated if bool(item["cost_candidate"])]
    classification = classify_census(
        eligible_events=len(eligible),
        evaluated_events=len(evaluated),
        cost_candidates=len(candidates),
    )
    try:
        prereg_reference = str(prereg_file.relative_to(root)).replace("\\", "/")
    except ValueError:
        prereg_reference = str(prereg_file)
    payload = {
        "schema": RESULT_SCHEMA,
        "variant": VARIANT,
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": classification["verdict"],
        "preregistration": {
            "relative_path": prereg_reference,
            "sha256": sha256_file(prereg_file),
            "verified_before_network_census": True,
        },
        "census": {
            "events_received": len(raw_events),
            "events_filter_eligible": len(eligible),
            "events_evaluated_with_full_books": len(evaluated),
            "cost_candidates": len(candidates),
            "filter_rejections": dict(sorted(filter_rejections.items())),
            "runtime_rejections": dict(sorted(runtime_rejections.items())),
            "minimum_evaluated_events_for_family_conclusion": classification["minimum_evaluated_events"],
            "comparable_coverage": classification["coverage"],
            "elapsed_seconds": time.monotonic() - started,
            "events": evaluated,
        },
        "interpretation": {
            "conditional_edge_only": True,
            "semantic_exhaustiveness_verified_for_candidates": False,
            "atomic_multileg_execution_available": False,
            "performance_validation": False,
            "economic_family_conclusion_allowed": classification["economic_family_conclusion_allowed"],
            "next_step": classification["next_step"],
            "automatic_followup_launched": False,
        },
        "safety": {
            "network_calls": "PUBLIC_READ_ONLY_GAMMA_AND_CLOB",
            "orders_created": 0,
            "paper_orders": 0,
            "wallet_required": False,
            "authentication_used": False,
            "real_money": "BLOQUEADO",
        },
    }
    _write_atomic(result_file, payload)
    return payload


__all__ = [
    "RESULT_SCHEMA",
    "V045CensusError",
    "census_v045",
    "classify_census",
    "evaluate_event_books",
    "executable_buy_cost",
    "filter_event",
    "validate_clob_market",
]
