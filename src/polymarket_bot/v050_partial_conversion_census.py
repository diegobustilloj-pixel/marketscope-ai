from __future__ import annotations

import asyncio
import json
import math
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v045_census import (
    CLOB_BASE_URL,
    GAMMA_EVENTS_URL,
    _http_json,
    executable_buy_cost,
)
from polymarket_bot.v047_ws_census import WsCollector, collect_initial_books
from polymarket_bot.v049_conversion_census import (
    collect_onchain_conversion_parameters,
    filter_linked_event,
    validate_linked_market,
)
from polymarket_bot.v050_contract import VARIANT, load_and_verify_preregistration


RESULT_SCHEMA = "result_v050_neg_risk_partial_conversion_cross_market_census_1"


class V050CensusError(RuntimeError):
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


def executable_sell_revenue(
    bids: Sequence[Mapping[str, Any]],
    *,
    shares: float,
    fee_rate: float,
    fee_exponent: float,
    reserve_tick: float = 0.0,
) -> dict[str, Any] | None:
    if not math.isfinite(shares) or shares <= 0.0:
        return None
    levels: list[tuple[float, float]] = []
    for level in bids:
        price = _number(level.get("price"))
        size = _number(level.get("size"))
        if price is None or size is None or not 0.0 < price < 1.0 or size <= 0.0:
            return None
        levels.append((price, size))
    levels.sort(key=lambda item: item[0], reverse=True)
    remaining = float(shares)
    gross = 0.0
    fee = 0.0
    fills: list[dict[str, float]] = []
    for price, available in levels:
        take = min(remaining, available)
        if take <= 0.0:
            continue
        adjusted = max(0.0001, price - float(reserve_tick))
        level_fee = take * fee_rate * (adjusted * (1.0 - adjusted)) ** fee_exponent
        proceeds = take * adjusted
        gross += proceeds
        fee += level_fee
        fills.append(
            {
                "price": adjusted,
                "shares": take,
                "gross_proceeds": proceeds,
                "fee": level_fee,
            }
        )
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return None
    net = gross - fee
    return {
        "shares": shares,
        "gross_proceeds": gross,
        "fee": fee,
        "net_revenue": net,
        "average_net_revenue_per_share": net / shares,
        "fills": fills,
    }


def collect_rest_metadata(
    events: Sequence[Mapping[str, Any]], http_json: HttpJson
) -> dict[str, Any]:
    tokens = [
        str(token)
        for event in events
        for market in event["markets"]
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


def enumerate_partial_subsets(market_count: int) -> list[tuple[int, ...]]:
    if market_count < 3:
        return []
    return [
        subset
        for size in range(1, market_count)
        for subset in combinations(range(market_count), size)
    ]


def _validate_snapshot_token(
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
    expected_condition = str(market["condition_id"]).lower()
    if str(meta.get("condition_id") or "").lower() != expected_condition:
        return None, "REST_METADATA_CONDITION_MISMATCH"
    if str(book.get("condition_id") or "").lower() != expected_condition:
        return None, "WEBSOCKET_CONDITION_MISMATCH"
    if meta.get("neg_risk") is not True:
        return None, "REST_METADATA_NEG_RISK_NOT_TRUE"
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
    return (meta, book), None


def evaluate_event(
    event: Mapping[str, Any],
    onchain: Mapping[str, Any],
    metadata: Mapping[str, Mapping[str, Any]],
    books: Mapping[str, Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    market_count = int(event["market_count"])
    if int(onchain.get("question_count", -1)) != market_count:
        return None, "ONCHAIN_QUESTION_COUNT_MISMATCH"
    shares = float(contract["hypothetical_position"]["input_shares_per_no_leg"])
    settings = contract["onchain_conversion"]
    scale = int(settings["token_unit_scale"])
    denominator = int(settings["fee_denominator"])
    amount_units = int(round(shares * scale))
    if abs(amount_units / scale - shares) > 1e-12:
        return None, "POSITION_NOT_REPRESENTABLE_IN_TOKEN_UNITS"
    fee_bips = int(onchain["fee_bips"])
    fee_units = amount_units * fee_bips // denominator
    amount_out_units = amount_units - fee_units
    amount_out = amount_out_units / scale

    received_times: list[int | None] = []
    source_times: list[int | None] = []
    legs: list[dict[str, Any]] = []
    for market in event["markets"]:
        token_sources: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
        for side, token_field in (("yes", "yes_token_id"), ("no", "no_token_id")):
            token = str(market[token_field])
            source, reason = _validate_snapshot_token(
                market=market,
                token=token,
                metadata=metadata,
                books=books,
            )
            if source is None:
                return None, f"{side.upper()}_{reason}"
            token_sources[side] = source
            received_times.append(_timestamp(source[1].get("received_timestamp_ms")))
            source_times.append(_timestamp(source[1].get("source_timestamp_ms")))
        yes_meta, yes_book = token_sources["yes"]
        no_meta, no_book = token_sources["no"]
        asks = no_book.get("asks")
        bids = yes_book.get("bids")
        if not isinstance(asks, list):
            return None, "NO_WEBSOCKET_ASKS_INVALID"
        if not isinstance(bids, list):
            return None, "YES_WEBSOCKET_BIDS_INVALID"
        observed_buy = executable_buy_cost(
            asks,
            shares=shares,
            fee_rate=float(market["fee_rate"]),
            fee_exponent=float(market["fee_exponent"]),
        )
        conservative_buy = executable_buy_cost(
            asks,
            shares=shares,
            fee_rate=float(market["fee_rate"]),
            fee_exponent=float(market["fee_exponent"]),
            reserve_tick=float(market["minimum_tick_size"]),
        )
        output_meets_minimum = amount_out + 1e-12 >= float(market["minimum_order_size"])
        observed_sale = (
            executable_sell_revenue(
                bids,
                shares=amount_out,
                fee_rate=float(market["fee_rate"]),
                fee_exponent=float(market["fee_exponent"]),
            )
            if output_meets_minimum
            else None
        )
        conservative_sale = (
            executable_sell_revenue(
                bids,
                shares=amount_out,
                fee_rate=float(market["fee_rate"]),
                fee_exponent=float(market["fee_exponent"]),
                reserve_tick=float(market["minimum_tick_size"]),
            )
            if output_meets_minimum
            else None
        )
        legs.append(
            {
                "condition_id": market["condition_id"],
                "question_id": market["question_id"],
                "question_index": market["question_index"],
                "question": market["question"],
                "yes_token_id": market["yes_token_id"],
                "no_token_id": market["no_token_id"],
                "fee_rate": market["fee_rate"],
                "fee_exponent": market["fee_exponent"],
                "tick_size": market["minimum_tick_size"],
                "minimum_order_size": market["minimum_order_size"],
                "yes_output_meets_minimum_order": output_meets_minimum,
                "yes_rest_hash": _normalized_hash(yes_meta.get("official_hash")),
                "yes_websocket_hash": _normalized_hash(yes_book.get("official_hash")),
                "yes_levels_sha256": yes_book.get("levels_sha256"),
                "no_rest_hash": _normalized_hash(no_meta.get("official_hash")),
                "no_websocket_hash": _normalized_hash(no_book.get("official_hash")),
                "no_levels_sha256": no_book.get("levels_sha256"),
                "observed_no_buy": observed_buy,
                "conservative_no_buy": conservative_buy,
                "observed_yes_sale": observed_sale,
                "conservative_yes_sale": conservative_sale,
            }
        )
    receive_spread = _spread(received_times)
    source_spread = _spread(source_times)
    if receive_spread is None or receive_spread > int(
        contract["transport"]["maximum_local_receive_spread_ms_per_event"]
    ):
        return None, "WEBSOCKET_RECEIVE_SPREAD_EXCEEDED"

    threshold = float(contract["cost_model"]["minimum_conservative_edge_per_input_share"])
    subset_records: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    economic_records: list[dict[str, Any]] = []
    candidate_count = 0
    all_indices = set(range(market_count))
    for subset in enumerate_partial_subsets(market_count):
        complement = tuple(sorted(all_indices - set(subset)))
        index_set = sum(1 << index for index in subset)
        unavailable_reason: str | None = None
        if any(legs[index]["observed_no_buy"] is None for index in subset):
            unavailable_reason = "NON_EXECUTABLE_INSUFFICIENT_NO_ASK_DEPTH"
        elif any(not legs[index]["yes_output_meets_minimum_order"] for index in complement):
            unavailable_reason = "NON_EXECUTABLE_YES_OUTPUT_BELOW_MINIMUM_ORDER"
        elif any(legs[index]["observed_yes_sale"] is None for index in complement):
            unavailable_reason = "NON_EXECUTABLE_INSUFFICIENT_YES_BID_DEPTH"
        if unavailable_reason is not None:
            status_counts[unavailable_reason] += 1
            subset_records.append(
                {
                    "no_indices": list(subset),
                    "yes_indices": list(complement),
                    "index_set": index_set,
                    "status": unavailable_reason,
                    "cost_candidate_before_gas": False,
                }
            )
            continue
        multiplier = len(subset) - 1
        collateral_total = multiplier * amount_out
        observed_buy_total = sum(
            float(legs[index]["observed_no_buy"]["total_cost"]) for index in subset
        )
        conservative_buy_total = sum(
            float(legs[index]["conservative_no_buy"]["total_cost"]) for index in subset
        )
        observed_sale_total = sum(
            float(legs[index]["observed_yes_sale"]["net_revenue"]) for index in complement
        )
        conservative_sale_total = sum(
            float(legs[index]["conservative_yes_sale"]["net_revenue"])
            for index in complement
        )
        observed_edge = (collateral_total + observed_sale_total - observed_buy_total) / shares
        conservative_edge = (
            collateral_total + conservative_sale_total - conservative_buy_total
        ) / shares
        candidate = observed_edge > 0.0 and conservative_edge >= threshold
        if candidate:
            candidate_count += 1
        record = {
            "no_indices": list(subset),
            "yes_indices": list(complement),
            "index_set": index_set,
            "status": "ECONOMICALLY_EVALUATED",
            "conversion_collateral_multiplier": multiplier,
            "conversion_collateral_total_at_input_shares": collateral_total,
            "observed_no_buy_total": observed_buy_total,
            "observed_yes_sale_net_total": observed_sale_total,
            "observed_edge_per_input_share_before_gas": observed_edge,
            "observed_pnl_at_input_shares_before_gas": observed_edge * shares,
            "conservative_no_buy_total": conservative_buy_total,
            "conservative_yes_sale_net_total": conservative_sale_total,
            "conservative_edge_per_input_share_before_gas": conservative_edge,
            "conservative_pnl_at_input_shares_before_gas": conservative_edge * shares,
            "cost_candidate_before_gas": candidate,
        }
        status_counts["ECONOMICALLY_EVALUATED"] += 1
        subset_records.append(record)
        economic_records.append(record)
    best = (
        max(
            economic_records,
            key=lambda item: float(item["conservative_edge_per_input_share_before_gas"]),
        )
        if economic_records
        else None
    )
    return {
        "event_id": event["event_id"],
        "slug": event["slug"],
        "title": event["title"],
        "neg_risk_market_id": event["neg_risk_market_id"],
        "market_count": market_count,
        "volume24hr": event["volume24hr"],
        "websocket_receive_spread_ms": receive_spread,
        "websocket_source_spread_ms": source_spread,
        "onchain_fee_bips": fee_bips,
        "onchain_question_count": onchain["question_count"],
        "input_shares_per_no_leg": shares,
        "conversion_fee_units_per_output": fee_units,
        "conversion_amount_out_shares": amount_out,
        "subsets_screened": len(subset_records),
        "subsets_economically_evaluated": len(economic_records),
        "subsets_non_executable": len(subset_records) - len(economic_records),
        "subset_status_counts": dict(sorted(status_counts.items())),
        "cost_candidates_before_gas": candidate_count,
        "best_economic_subset": best,
        "gas_included": False,
        "semantic_parser_required": False,
        "atomic_clob_and_onchain_execution_available": False,
        "legs": legs,
        "subsets": subset_records,
    }, None


def classify_census(
    *,
    selected_events: int,
    synchronized_events: int,
    cost_candidates: int,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    gate = contract["evidence_gate"]
    required = max(
        int(gate["minimum_synchronized_events"]),
        math.ceil(float(gate["minimum_synchronized_fraction_of_selected"]) * selected_events),
    )
    coverage = synchronized_events / selected_events if selected_events else 0.0
    if synchronized_events < required:
        return {
            "verdict": "FAIL_CENSUS_INSUFFICIENT_PARTIAL_CONVERSION_BOOKS",
            "next_step": "RESOLVE_ONCHAIN_OR_DUAL_TOKEN_BOOK_COVERAGE_BEFORE_ECONOMIC_CONCLUSION",
            "structural_snapshot_conclusion_allowed": False,
            "required_synchronized_events": required,
            "coverage": coverage,
        }
    if cost_candidates:
        return {
            "verdict": "REQUIRE_FRESH_PERSISTENCE_GAS_AND_SEQUENTIAL_EXECUTION_STUDY",
            "next_step": "PREREGISTER_PERSISTENCE_GAS_AND_SEQUENTIAL_EXECUTION_STUDY_FOR_FROZEN_SUBSETS",
            "structural_snapshot_conclusion_allowed": True,
            "required_synchronized_events": required,
            "coverage": coverage,
        }
    return {
        "verdict": "REJECT_PARTIAL_CONVERSION_CROSS_MARKET_FEASIBILITY",
        "next_step": "DO_NOT_LAUNCH_OBSERVER_FOR_PARTIAL_CONVERSION_HYPOTHESIS",
        "structural_snapshot_conclusion_allowed": True,
        "required_synchronized_events": required,
        "coverage": coverage,
    }


async def census_v050_async(
    *,
    prereg_path: str | Path,
    result_path: str | Path,
    project_root: str | Path = ROOT,
    http_json: HttpJson = _http_json,
    rpc_json: HttpJson | None = None,
    ws_collector: WsCollector = collect_initial_books,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    result_file = Path(result_path).resolve()
    prereg = load_and_verify_preregistration(prereg_file, project_root=root)
    if result_file.is_file():
        return json.loads(result_file.read_text(encoding="utf-8"))
    rpc = rpc_json or http_json
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
        raise V050CensusError("Gamma no devolvio la lista esperada")
    filter_rejections: Counter[str] = Counter()
    selected: list[dict[str, Any]] = []
    selected_market_ids: set[str] = set()
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            filter_rejections["EVENT_INVALID"] += 1
            continue
        event, reason = filter_linked_event(raw, contract)
        if event is None:
            filter_rejections[str(reason)] += 1
        elif event["neg_risk_market_id"] in selected_market_ids:
            filter_rejections["DUPLICATE_NEG_RISK_MARKET_ID"] += 1
        elif len(selected) < int(sample["maximum_eligible_events"]):
            selected.append(event)
            selected_market_ids.add(str(event["neg_risk_market_id"]))
    validated_events: list[dict[str, Any]] = []
    validation_rejections: Counter[str] = Counter()
    for event in selected:
        markets: list[dict[str, Any]] = []
        failed: str | None = None
        for market in event["markets"]:
            condition = urllib.parse.quote(str(market["condition_id"]), safe="")
            info = http_json("GET", f"{CLOB_BASE_URL}/clob-markets/{condition}", None)
            if not isinstance(info, Mapping):
                failed = "CLOB_MARKET_INFO_INVALID"
                break
            validated, reason = validate_linked_market(
                market,
                info,
                float(contract["hypothetical_position"]["input_shares_per_no_leg"]),
            )
            if validated is None:
                failed = str(reason)
                break
            markets.append(validated)
        if failed is not None:
            validation_rejections[failed] += 1
        else:
            validated_events.append({**event, "markets": markets})
    onchain = collect_onchain_conversion_parameters(validated_events, contract, rpc)
    onchain_records = onchain.get("records", {})
    onchain_validated: list[dict[str, Any]] = []
    onchain_rejections: Counter[str] = Counter()
    if not onchain.get("valid") or not isinstance(onchain_records, Mapping):
        onchain_rejections[str(onchain.get("rejection") or "ONCHAIN_PARAMETERS_INVALID")] = len(
            validated_events
        )
    else:
        for event in validated_events:
            record = onchain_records.get(str(event["neg_risk_market_id"]))
            if not isinstance(record, Mapping):
                onchain_rejections["ONCHAIN_EVENT_RECORD_MISSING"] += 1
            elif int(record.get("question_count", -1)) != int(event["market_count"]):
                onchain_rejections["ONCHAIN_QUESTION_COUNT_MISMATCH"] += 1
            else:
                onchain_validated.append(event)
    rest_metadata = collect_rest_metadata(onchain_validated, http_json)
    tokens = [
        str(token)
        for event in onchain_validated
        for market in event["markets"]
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
        raise V050CensusError("Fuentes V0.50 sin mapas de libros")
    evaluation_rejections: Counter[str] = Counter()
    evaluated: list[dict[str, Any]] = []
    if (
        metadata_to_ws_ms is None
        or metadata_to_ws_ms < 0
        or metadata_to_ws_ms > int(contract["metadata"]["maximum_rest_response_to_ws_connect_ms"])
    ):
        evaluation_rejections["REST_METADATA_TO_WS_WINDOW_EXCEEDED"] = len(onchain_validated)
    else:
        for event in onchain_validated:
            record = onchain_records[str(event["neg_risk_market_id"])]
            result, reason = evaluate_event(event, record, metadata_records, books, contract)
            if result is None:
                evaluation_rejections[str(reason)] += 1
            else:
                evaluated.append(result)
    evaluated.sort(
        key=lambda item: (
            float(item["best_economic_subset"]["conservative_edge_per_input_share_before_gas"])
            if item["best_economic_subset"] is not None
            else float("-inf")
        ),
        reverse=True,
    )
    total_screened = sum(int(event["subsets_screened"]) for event in evaluated)
    total_economic = sum(int(event["subsets_economically_evaluated"]) for event in evaluated)
    total_non_executable = sum(int(event["subsets_non_executable"]) for event in evaluated)
    total_candidates = sum(int(event["cost_candidates_before_gas"]) for event in evaluated)
    classification = classify_census(
        selected_events=len(selected),
        synchronized_events=len(evaluated),
        cost_candidates=total_candidates,
        contract=contract,
    )
    try:
        prereg_reference = str(prereg_file.relative_to(root)).replace("\\", "/")
    except ValueError:
        prereg_reference = str(prereg_file)
    onchain_public = {key: value for key, value in onchain.items() if key != "records"}
    onchain_public["events_with_parameters"] = (
        len(onchain_records) if isinstance(onchain_records, Mapping) else 0
    )
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
            "events_selected": len(selected),
            "events_clob_validated": len(validated_events),
            "events_onchain_validated": len(onchain_validated),
            "tokens_subscribed": len(set(tokens)),
            "filter_rejections": dict(sorted(filter_rejections.items())),
            "validation_rejections": dict(sorted(validation_rejections.items())),
            "onchain_rejections": dict(sorted(onchain_rejections.items())),
        },
        "onchain_conversion": onchain_public,
        "rest_metadata": {key: value for key, value in rest_metadata.items() if key != "records"},
        "websocket": {key: value for key, value in websocket.items() if key != "books"},
        "census": {
            **classification,
            "metadata_response_to_ws_connect_ms": metadata_to_ws_ms,
            "events_synchronized": len(evaluated),
            "subsets_screened": total_screened,
            "subsets_economically_evaluated": total_economic,
            "subsets_non_executable": total_non_executable,
            "cost_candidates_before_gas": total_candidates,
            "evaluation_rejections": dict(sorted(evaluation_rejections.items())),
            "elapsed_seconds": time.monotonic() - started,
            "events": evaluated,
        },
        "interpretation": {
            "conversion_property_structural": True,
            "all_nonempty_proper_subsets_enumerated": True,
            "semantic_parser_required": False,
            "gas_included": False,
            "nonpositive_edge_cannot_be_rescued_by_nonnegative_gas": True,
            "performance_validation": False,
            "atomic_clob_and_onchain_execution_available": False,
            "automatic_followup_launched": False,
            "next_step": classification["next_step"],
        },
        "safety": {
            "network_calls": "PUBLIC_READ_ONLY_GAMMA_CLOB_POLYGON_RPC_AND_MARKET_WEBSOCKET",
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


def census_v050(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(census_v050_async(**kwargs))


__all__ = [
    "RESULT_SCHEMA",
    "V050CensusError",
    "census_v050",
    "census_v050_async",
    "classify_census",
    "collect_rest_metadata",
    "enumerate_partial_subsets",
    "evaluate_event",
    "executable_sell_revenue",
]
