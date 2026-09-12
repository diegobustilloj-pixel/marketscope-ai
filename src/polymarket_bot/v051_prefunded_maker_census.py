from __future__ import annotations

import asyncio
import json
import math
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v045_census import (
    CLOB_BASE_URL,
    GAMMA_EVENTS_URL,
    _clob_market_values,
    _http_json,
)
from polymarket_bot.v047_ws_census import WsCollector, collect_initial_books
from polymarket_bot.v050_partial_conversion_census import executable_sell_revenue
from polymarket_bot.v051_contract import VARIANT, load_and_verify_preregistration


RESULT_SCHEMA = "result_v051_prefunded_maker_single_fill_hedge_census_1"


class V051CensusError(RuntimeError):
    pass


HttpJson = Callable[[str, str, Any | None], Any]


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


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _normalized_hash(value: Any) -> str:
    result = str(value or "").strip().lower()
    return result[2:] if result.startswith("0x") else result


def _spread(values: Sequence[int | None]) -> int | None:
    if not values or any(value is None for value in values):
        return None
    usable = [int(value) for value in values if value is not None]
    return max(usable) - min(usable)


def _gamma_fee(market: Mapping[str, Any]) -> tuple[float, float, bool] | None:
    if not _boolean(market.get("feesEnabled")):
        return 0.0, 1.0, True
    schedule = market.get("feeSchedule")
    if not isinstance(schedule, Mapping):
        return None
    rate = _number(schedule.get("rate"))
    exponent = _number(schedule.get("exponent"))
    taker_only = _boolean(schedule.get("takerOnly"))
    if rate is None or exponent is None or rate < 0.0 or exponent <= 0.0 or not taker_only:
        return None
    return rate, exponent, taker_only


def normalize_standard_market(
    event: Mapping[str, Any], market: Mapping[str, Any], contract: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    if not _boolean(event.get("active")) or _boolean(event.get("closed")):
        return None, "EVENT_NOT_OPEN"
    if not _boolean(market.get("active")) or _boolean(market.get("closed")):
        return None, "MARKET_NOT_OPEN"
    if not _boolean(market.get("acceptingOrders")):
        return None, "MARKET_NOT_ACCEPTING_ORDERS"
    if not _boolean(market.get("enableOrderBook")):
        return None, "ORDER_BOOK_DISABLED"
    if market.get("negRisk") is None:
        return None, "NEG_RISK_FLAG_MISSING"
    if _boolean(market.get("negRisk")):
        return None, "NEG_RISK_MARKET_EXCLUDED"
    outcomes = _json_value(market.get("outcomes"))
    tokens = _json_value(market.get("clobTokenIds"))
    if outcomes != contract["market_filters"]["required_binary_outcomes"]:
        return None, "OUTCOMES_NOT_YES_NO"
    if not isinstance(tokens, list) or len(tokens) != 2:
        return None, "TOKEN_IDS_INVALID"
    condition = str(market.get("conditionId") or "")
    yes_token = str(tokens[0] or "")
    no_token = str(tokens[1] or "")
    if not condition or not yes_token or not no_token or yes_token == no_token:
        return None, "IDENTIFIER_MISSING_OR_DUPLICATE"
    fee = _gamma_fee(market)
    if fee is None:
        return None, "GAMMA_FEE_INVALID"
    return {
        "event_id": str(event.get("id") or ""),
        "event_slug": str(event.get("slug") or ""),
        "event_title": str(event.get("title") or ""),
        "event_volume24hr": float(event.get("volume24hr") or 0.0),
        "market_id": str(market.get("id") or ""),
        "condition_id": condition,
        "slug": str(market.get("slug") or ""),
        "question": str(market.get("question") or ""),
        "yes_token_id": yes_token,
        "no_token_id": no_token,
        "gamma_fee_rate": fee[0],
        "gamma_fee_exponent": fee[1],
    }, None


def validate_standard_clob_market(
    market: Mapping[str, Any], info: Mapping[str, Any], shares: float
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        minimum, tick, neg_risk, rate, exponent, taker_only = _clob_market_values(info)
    except Exception:
        return None, "CLOB_MARKET_INFO_INVALID"
    reported_condition = str(info.get("condition_id", info.get("conditionId", "")))
    if reported_condition and reported_condition.lower() != str(market["condition_id"]).lower():
        return None, "CONDITION_ID_MISMATCH"
    if neg_risk is not False:
        return None, "CLOB_NEG_RISK_NOT_EXPLICITLY_FALSE"
    if minimum <= 0.0 or minimum > shares:
        return None, "MINIMUM_ORDER_EXCEEDS_POSITION"
    if tick not in {0.1, 0.01, 0.001, 0.0001}:
        return None, "TICK_SIZE_INVALID"
    if rate < 0.0 or exponent <= 0.0 or not taker_only:
        return None, "CLOB_FEE_INVALID"
    if abs(rate - float(market["gamma_fee_rate"])) > 1e-12:
        return None, "FEE_RATE_MISMATCH"
    if abs(exponent - float(market["gamma_fee_exponent"])) > 1e-12:
        return None, "FEE_EXPONENT_MISMATCH"
    tokens = info.get("t", info.get("tokens"))
    if not isinstance(tokens, list):
        return None, "CLOB_OUTCOME_TOKENS_MISSING"
    yes_ids: set[str] = set()
    no_ids: set[str] = set()
    for token in tokens:
        if not isinstance(token, Mapping):
            continue
        token_id = str(token.get("t", token.get("token_id", token.get("tokenId", ""))))
        outcome = str(token.get("o", token.get("outcome", ""))).strip().lower()
        if outcome == "yes" and token_id:
            yes_ids.add(token_id)
        elif outcome == "no" and token_id:
            no_ids.add(token_id)
    if yes_ids != {str(market["yes_token_id"])}:
        return None, "CLOB_YES_TOKEN_MISMATCH"
    if no_ids != {str(market["no_token_id"])}:
        return None, "CLOB_NO_TOKEN_MISMATCH"
    return {
        **dict(market),
        "minimum_order_size": minimum,
        "minimum_tick_size": tick,
        "fee_rate": rate,
        "fee_exponent": exponent,
        "fee_taker_only": taker_only,
    }, None


def collect_rest_metadata(
    markets: Sequence[Mapping[str, Any]], http_json: HttpJson
) -> dict[str, Any]:
    tokens = [
        str(token)
        for market in markets
        for token in (market["yes_token_id"], market["no_token_id"])
    ]
    started_ms = time.time_ns() // 1_000_000
    response = (
        http_json("POST", f"{CLOB_BASE_URL}/books", [{"token_id": token} for token in tokens])
        if tokens
        else []
    )
    received_ms = time.time_ns() // 1_000_000
    records: dict[str, dict[str, Any]] = {}
    if isinstance(response, list):
        for item in response:
            if not isinstance(item, Mapping):
                continue
            token = str(item.get("asset_id") or item.get("assetId") or "")
            if token not in tokens:
                continue
            records[token] = {
                "condition_id": str(item.get("market") or ""),
                "source_timestamp_ms": _timestamp(item.get("timestamp")),
                "official_hash": _normalized_hash(item.get("hash")),
                "minimum_order_size": _number(
                    item.get("min_order_size", item.get("minOrderSize"))
                ),
                "tick_size": _number(item.get("tick_size", item.get("tickSize"))),
                "neg_risk": item.get("neg_risk", item.get("negRisk")),
            }
    expected = set(tokens)
    return {
        "request_started_ms": started_ms,
        "response_received_ms": received_ms,
        "request_elapsed_ms": received_ms - started_ms,
        "expected_tokens": len(expected),
        "received_tokens": len(records),
        "missing_tokens": sorted(expected - set(records)),
        "records": records,
    }


def _levels(
    raw: Any, *, reverse: bool
) -> list[tuple[float, float]] | None:
    if not isinstance(raw, list):
        return None
    parsed: list[tuple[float, float]] = []
    for level in raw:
        if not isinstance(level, Mapping):
            return None
        price = _number(level.get("price"))
        size = _number(level.get("size"))
        if price is None or size is None or not 0.0 < price < 1.0 or size <= 0.0:
            return None
        parsed.append((price, size))
    parsed.sort(key=lambda item: item[0], reverse=reverse)
    return parsed


def _validate_token_snapshot(
    *,
    market: Mapping[str, Any],
    token: str,
    metadata: Mapping[str, Mapping[str, Any]],
    books: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[Mapping[str, Any], Mapping[str, Any]] | None, str | None]:
    meta = metadata.get(token)
    book = books.get(token)
    if not isinstance(meta, Mapping):
        return None, "REST_METADATA_MISSING"
    if not isinstance(book, Mapping):
        return None, "WEBSOCKET_BOOK_MISSING"
    expected = str(market["condition_id"]).lower()
    if str(meta.get("condition_id") or "").lower() != expected:
        return None, "REST_METADATA_CONDITION_MISMATCH"
    if str(book.get("condition_id") or "").lower() != expected:
        return None, "WEBSOCKET_CONDITION_MISMATCH"
    if meta.get("neg_risk") is not False:
        return None, "REST_METADATA_NEG_RISK_NOT_EXPLICITLY_FALSE"
    minimum = _number(meta.get("minimum_order_size"))
    tick = _number(meta.get("tick_size"))
    if minimum is None or abs(minimum - float(market["minimum_order_size"])) > 1e-12:
        return None, "REST_METADATA_MINIMUM_ORDER_MISMATCH"
    if tick is None or abs(tick - float(market["minimum_tick_size"])) > 1e-12:
        return None, "REST_METADATA_TICK_SIZE_MISMATCH"
    if not _normalized_hash(meta.get("official_hash")):
        return None, "REST_METADATA_HASH_MISSING"
    if not _normalized_hash(book.get("official_hash")):
        return None, "WEBSOCKET_HASH_MISSING"
    if _levels(book.get("asks"), reverse=False) is None:
        return None, "WEBSOCKET_ASKS_INVALID"
    if _levels(book.get("bids"), reverse=True) is None:
        return None, "WEBSOCKET_BIDS_INVALID"
    return (meta, book), None


def _direction(
    *,
    maker_side: str,
    maker_book: Mapping[str, Any],
    hedge_book: Mapping[str, Any],
    market: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    shares = float(contract["prefunded_inventory"]["complete_sets"])
    asks = _levels(maker_book.get("asks"), reverse=False)
    own_bids = _levels(maker_book.get("bids"), reverse=True)
    hedge_bids_raw = hedge_book.get("bids")
    if asks is None or own_bids is None or not isinstance(hedge_bids_raw, list):
        return {
            "maker_side": maker_side,
            "hedge_side": "NO" if maker_side == "YES" else "YES",
            "status": "NON_EXECUTABLE_INVALID_BOOK",
            "cost_candidate_before_gas": False,
        }
    if not asks:
        return {
            "maker_side": maker_side,
            "hedge_side": "NO" if maker_side == "YES" else "YES",
            "status": "NON_EXECUTABLE_NO_MAKER_ASK_REFERENCE",
            "cost_candidate_before_gas": False,
        }
    maker_price = asks[0][0]
    best_bid = own_bids[0][0] if own_bids else None
    if best_bid is not None and maker_price <= best_bid + 1e-12:
        return {
            "maker_side": maker_side,
            "hedge_side": "NO" if maker_side == "YES" else "YES",
            "status": "NON_EXECUTABLE_MAKER_QUOTE_WOULD_BE_MARKETABLE",
            "maker_quote_price": maker_price,
            "current_best_bid": best_bid,
            "cost_candidate_before_gas": False,
        }
    observed = executable_sell_revenue(
        hedge_bids_raw,
        shares=shares,
        fee_rate=float(market["fee_rate"]),
        fee_exponent=float(market["fee_exponent"]),
    )
    conservative = executable_sell_revenue(
        hedge_bids_raw,
        shares=shares,
        fee_rate=float(market["fee_rate"]),
        fee_exponent=float(market["fee_exponent"]),
        reserve_tick=(
            int(contract["cost_model"]["conservative_hedge_price_reserve_ticks"])
            * float(market["minimum_tick_size"])
        ),
    )
    if observed is None or conservative is None:
        return {
            "maker_side": maker_side,
            "hedge_side": "NO" if maker_side == "YES" else "YES",
            "status": "NON_EXECUTABLE_INSUFFICIENT_HEDGE_BID_DEPTH",
            "maker_quote_price": maker_price,
            "current_best_bid": best_bid,
            "maker_queue_ahead_shares": sum(size for price, size in asks if price == maker_price),
            "cost_candidate_before_gas": False,
        }
    maker_proceeds = maker_price * shares
    inventory_cost = shares * float(
        contract["prefunded_inventory"]["collateral_per_complete_set"]
    )
    observed_pnl = maker_proceeds + float(observed["net_revenue"]) - inventory_cost
    conservative_pnl = (
        maker_proceeds + float(conservative["net_revenue"]) - inventory_cost
    )
    observed_edge = observed_pnl / shares
    conservative_edge = conservative_pnl / shares
    threshold = float(contract["cost_model"]["minimum_conservative_edge_per_complete_set"])
    candidate = observed_edge > 0.0 and conservative_edge >= threshold
    return {
        "maker_side": maker_side,
        "hedge_side": "NO" if maker_side == "YES" else "YES",
        "status": "ECONOMICALLY_EVALUATED",
        "maker_quote_price": maker_price,
        "current_best_bid": best_bid,
        "maker_queue_ahead_shares": sum(size for price, size in asks if price == maker_price),
        "maker_fee": 0.0,
        "maker_rebate": 0.0,
        "inventory_complete_sets": shares,
        "inventory_cost": inventory_cost,
        "maker_proceeds": maker_proceeds,
        "observed_hedge": observed,
        "observed_edge_per_complete_set_before_gas": observed_edge,
        "observed_pnl_at_5_complete_sets_before_gas": observed_pnl,
        "conservative_hedge": conservative,
        "conservative_edge_per_complete_set_before_gas": conservative_edge,
        "conservative_pnl_at_5_complete_sets_before_gas": conservative_pnl,
        "cost_candidate_before_gas": candidate,
    }


def evaluate_market(
    market: Mapping[str, Any],
    metadata: Mapping[str, Mapping[str, Any]],
    books: Mapping[str, Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    token_sources: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    received_times: list[int | None] = []
    source_times: list[int | None] = []
    for side, token_field in (("YES", "yes_token_id"), ("NO", "no_token_id")):
        token = str(market[token_field])
        source, reason = _validate_token_snapshot(
            market=market,
            token=token,
            metadata=metadata,
            books=books,
        )
        if source is None:
            return None, f"{side}_{reason}"
        token_sources[side] = source
        received_times.append(_timestamp(source[1].get("received_timestamp_ms")))
        source_times.append(_timestamp(source[1].get("source_timestamp_ms")))
    receive_spread = _spread(received_times)
    source_spread = _spread(source_times)
    if receive_spread is None or receive_spread > int(
        contract["transport"]["maximum_local_receive_spread_ms_per_market"]
    ):
        return None, "WEBSOCKET_RECEIVE_SPREAD_EXCEEDED"
    yes_meta, yes_book = token_sources["YES"]
    no_meta, no_book = token_sources["NO"]
    directions = [
        _direction(
            maker_side="YES",
            maker_book=yes_book,
            hedge_book=no_book,
            market=market,
            contract=contract,
        ),
        _direction(
            maker_side="NO",
            maker_book=no_book,
            hedge_book=yes_book,
            market=market,
            contract=contract,
        ),
    ]
    status_counts = Counter(str(item["status"]) for item in directions)
    candidates = sum(bool(item["cost_candidate_before_gas"]) for item in directions)
    economic = [item for item in directions if item["status"] == "ECONOMICALLY_EVALUATED"]
    best = (
        max(
            economic,
            key=lambda item: float(item["conservative_edge_per_complete_set_before_gas"]),
        )
        if economic
        else None
    )
    return {
        "event_id": market["event_id"],
        "event_slug": market["event_slug"],
        "event_title": market["event_title"],
        "event_volume24hr": market["event_volume24hr"],
        "market_id": market["market_id"],
        "condition_id": market["condition_id"],
        "slug": market["slug"],
        "question": market["question"],
        "yes_token_id": market["yes_token_id"],
        "no_token_id": market["no_token_id"],
        "minimum_order_size": market["minimum_order_size"],
        "tick_size": market["minimum_tick_size"],
        "fee_rate": market["fee_rate"],
        "fee_exponent": market["fee_exponent"],
        "websocket_receive_spread_ms": receive_spread,
        "websocket_source_spread_ms": source_spread,
        "yes_rest_hash": _normalized_hash(yes_meta.get("official_hash")),
        "yes_websocket_hash": _normalized_hash(yes_book.get("official_hash")),
        "no_rest_hash": _normalized_hash(no_meta.get("official_hash")),
        "no_websocket_hash": _normalized_hash(no_book.get("official_hash")),
        "directions_screened": 2,
        "directions_economically_evaluated": len(economic),
        "directions_non_executable": 2 - len(economic),
        "direction_status_counts": dict(sorted(status_counts.items())),
        "cost_candidates_before_gas": candidates,
        "best_direction": best,
        "directions": directions,
    }, None


def classify_census(
    *,
    selected_markets: int,
    synchronized_markets: int,
    cost_candidates: int,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    gate = contract["evidence_gate"]
    required = max(
        int(gate["minimum_synchronized_markets"]),
        math.ceil(float(gate["minimum_synchronized_fraction_of_selected"]) * selected_markets),
    )
    coverage = synchronized_markets / selected_markets if selected_markets else 0.0
    if synchronized_markets < required:
        return {
            "verdict": "FAIL_CENSUS_INSUFFICIENT_PREFUNDED_MAKER_BOOKS",
            "next_step": "RESOLVE_STANDARD_MARKET_DUAL_TOKEN_BOOK_COVERAGE",
            "conditional_fill_conclusion_allowed": False,
            "required_synchronized_markets": required,
            "coverage": coverage,
        }
    if cost_candidates:
        return {
            "verdict": "REQUIRE_FRESH_PERSISTENCE_QUEUE_LATENCY_GAS_STUDY",
            "next_step": "PREREGISTER_ONLY_FROZEN_CANDIDATES_WITH_QUEUE_AND_ADVERSE_SELECTION_CONTROLS",
            "conditional_fill_conclusion_allowed": True,
            "required_synchronized_markets": required,
            "coverage": coverage,
        }
    return {
        "verdict": "REJECT_PREFUNDED_MAKER_SINGLE_FILL_HEDGE_FEASIBILITY",
        "next_step": "DO_NOT_LAUNCH_OBSERVER_FOR_PREFUNDED_MAKER_SINGLE_FILL_HEDGE",
        "conditional_fill_conclusion_allowed": True,
        "required_synchronized_markets": required,
        "coverage": coverage,
    }


async def census_v051_async(
    *,
    prereg_path: str | Path,
    result_path: str | Path,
    project_root: str | Path = ROOT,
    http_json: HttpJson = _http_json,
    ws_collector: WsCollector = collect_initial_books,
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
        raise V051CensusError("Gamma no devolvio la lista esperada")
    filter_rejections: Counter[str] = Counter()
    selected: list[dict[str, Any]] = []
    seen_conditions: set[str] = set()
    seen_tokens: set[str] = set()
    markets_received = 0
    for raw_event in raw_events:
        if not isinstance(raw_event, Mapping):
            filter_rejections["EVENT_INVALID"] += 1
            continue
        raw_markets = raw_event.get("markets")
        if not isinstance(raw_markets, list):
            filter_rejections["MARKETS_MISSING"] += 1
            continue
        for raw_market in raw_markets:
            markets_received += 1
            if not isinstance(raw_market, Mapping):
                filter_rejections["MARKET_INVALID"] += 1
                continue
            market, reason = normalize_standard_market(raw_event, raw_market, contract)
            if market is None:
                filter_rejections[str(reason)] += 1
                continue
            condition = str(market["condition_id"])
            tokens = {str(market["yes_token_id"]), str(market["no_token_id"])}
            if condition in seen_conditions or seen_tokens.intersection(tokens):
                filter_rejections["DUPLICATE_CONDITION_OR_TOKEN"] += 1
                continue
            if len(selected) < int(sample["maximum_eligible_markets"]):
                selected.append(market)
                seen_conditions.add(condition)
                seen_tokens.update(tokens)
    validated: list[dict[str, Any]] = []
    validation_rejections: Counter[str] = Counter()
    shares = float(contract["prefunded_inventory"]["complete_sets"])
    for market in selected:
        condition = urllib.parse.quote(str(market["condition_id"]), safe="")
        info = http_json("GET", f"{CLOB_BASE_URL}/clob-markets/{condition}", None)
        if not isinstance(info, Mapping):
            validation_rejections["CLOB_MARKET_INFO_INVALID"] += 1
            continue
        result, reason = validate_standard_clob_market(market, info, shares)
        if result is None:
            validation_rejections[str(reason)] += 1
        else:
            validated.append(result)
    rest_metadata = collect_rest_metadata(validated, http_json)
    tokens = [
        str(token)
        for market in validated
        for token in (market["yes_token_id"], market["no_token_id"])
    ]
    if tokens:
        websocket = await ws_collector(str(contract["transport"]["endpoint"]), tokens, contract)
    else:
        now_ms = time.time_ns() // 1_000_000
        websocket = {
            "connected_at_ms": now_ms,
            "finished_at_ms": now_ms,
            "elapsed_seconds": 0.0,
            "expected_tokens": 0,
            "received_tokens": 0,
            "missing_tokens": [],
            "malformed_messages": 0,
            "books": {},
        }
    connected_at = _timestamp(websocket.get("connected_at_ms"))
    metadata_received = _timestamp(rest_metadata.get("response_received_ms"))
    metadata_to_ws_ms = (
        connected_at - metadata_received
        if connected_at is not None and metadata_received is not None
        else None
    )
    books = websocket.get("books", {})
    metadata_records = rest_metadata.get("records", {})
    if not isinstance(books, Mapping) or not isinstance(metadata_records, Mapping):
        raise V051CensusError("Fuentes V0.51 sin mapas de libros")
    evaluation_rejections: Counter[str] = Counter()
    evaluated: list[dict[str, Any]] = []
    if (
        metadata_to_ws_ms is None
        or metadata_to_ws_ms < 0
        or metadata_to_ws_ms > int(contract["metadata"]["maximum_rest_response_to_ws_connect_ms"])
    ):
        evaluation_rejections["REST_METADATA_TO_WS_WINDOW_EXCEEDED"] = len(validated)
    else:
        for market in validated:
            result, reason = evaluate_market(market, metadata_records, books, contract)
            if result is None:
                evaluation_rejections[str(reason)] += 1
            else:
                evaluated.append(result)
    evaluated.sort(
        key=lambda item: (
            float(item["best_direction"]["conservative_edge_per_complete_set_before_gas"])
            if item["best_direction"] is not None
            else float("-inf")
        ),
        reverse=True,
    )
    total_screened = sum(int(item["directions_screened"]) for item in evaluated)
    total_economic = sum(int(item["directions_economically_evaluated"]) for item in evaluated)
    total_non_executable = sum(int(item["directions_non_executable"]) for item in evaluated)
    total_candidates = sum(int(item["cost_candidates_before_gas"]) for item in evaluated)
    classification = classify_census(
        selected_markets=len(selected),
        synchronized_markets=len(evaluated),
        cost_candidates=total_candidates,
        contract=contract,
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
        "sample": {
            "events_received": len(raw_events),
            "markets_received": markets_received,
            "markets_selected": len(selected),
            "markets_clob_validated": len(validated),
            "tokens_subscribed": len(set(tokens)),
            "filter_rejections": dict(sorted(filter_rejections.items())),
            "validation_rejections": dict(sorted(validation_rejections.items())),
        },
        "rest_metadata": {key: value for key, value in rest_metadata.items() if key != "records"},
        "websocket": {key: value for key, value in websocket.items() if key != "books"},
        "census": {
            **classification,
            "metadata_response_to_ws_connect_ms": metadata_to_ws_ms,
            "markets_synchronized": len(evaluated),
            "directions_screened": total_screened,
            "directions_economically_evaluated": total_economic,
            "directions_non_executable": total_non_executable,
            "cost_candidates_before_gas": total_candidates,
            "evaluation_rejections": dict(sorted(evaluation_rejections.items())),
            "elapsed_seconds": time.monotonic() - started,
            "markets": evaluated,
        },
        "interpretation": {
            "prefunded_complete_set_removes_directional_residual_after_hedge": True,
            "conditional_maker_fill_feasibility_only": True,
            "fill_probability_or_queue_priority_proven": False,
            "partial_maker_fill_below_minimum_hedge_size_solved": False,
            "adverse_selection_beyond_two_ticks_proven": False,
            "gas_included": False,
            "nonpositive_edge_cannot_be_rescued_by_nonnegative_gas": True,
            "performance_validation": False,
            "automatic_followup_launched": False,
            "next_step": classification["next_step"],
        },
        "safety": {
            "network_calls": "PUBLIC_READ_ONLY_GAMMA_CLOB_AND_MARKET_WEBSOCKET",
            "orders_created": 0,
            "paper_orders": 0,
            "transactions_created": 0,
            "wallet_required": False,
            "authentication_used": False,
            "real_money": "BLOQUEADO",
        },
    }
    _write_atomic(result_file, payload)
    return payload


def census_v051(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(census_v051_async(**kwargs))


__all__ = [
    "RESULT_SCHEMA",
    "V051CensusError",
    "census_v051",
    "census_v051_async",
    "classify_census",
    "collect_rest_metadata",
    "evaluate_market",
    "normalize_standard_market",
    "validate_standard_clob_market",
]
