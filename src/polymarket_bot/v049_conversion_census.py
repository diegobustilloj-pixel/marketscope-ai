from __future__ import annotations

import asyncio
import hashlib
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
    _http_json,
    executable_buy_cost,
    filter_event,
    validate_clob_market,
)
from polymarket_bot.v045_contract import frozen_contract as v045_frozen_contract
from polymarket_bot.v047_ws_census import WsCollector, collect_initial_books
from polymarket_bot.v049_contract import VARIANT, load_and_verify_preregistration


RESULT_SCHEMA = "result_v049_neg_risk_buy_all_no_conversion_census_1"


class V049CensusError(RuntimeError):
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


def _hex32(value: Any) -> str | None:
    candidate = str(value or "").strip().lower()
    if candidate.startswith("0x"):
        candidate = candidate[2:]
    if len(candidate) != 64:
        return None
    try:
        int(candidate, 16)
    except ValueError:
        return None
    return "0x" + candidate


def _normalized_hash(value: Any) -> str:
    result = str(value or "").strip().lower()
    return result[2:] if result.startswith("0x") else result


def filter_linked_event(
    raw_event: Mapping[str, Any], contract: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    event, reason = filter_event(raw_event, v045_frozen_contract())
    if event is None:
        return None, reason
    market_id = _hex32(raw_event.get("negRiskMarketID"))
    if market_id is None or not market_id.endswith("00"):
        return None, "NEG_RISK_MARKET_ID_INVALID"
    raw_markets = raw_event.get("markets")
    if not isinstance(raw_markets, list):
        return None, "MARKETS_MISSING"
    by_condition = {
        str(item.get("conditionId") or "").lower(): item
        for item in raw_markets
        if isinstance(item, Mapping)
    }
    enriched: list[dict[str, Any]] = []
    question_indices: set[int] = set()
    no_tokens: set[str] = set()
    for market in event["markets"]:
        raw_market = by_condition.get(str(market["condition_id"]).lower())
        if not isinstance(raw_market, Mapping):
            return None, "RAW_MARKET_CONDITION_MISSING"
        if _hex32(raw_market.get("negRiskMarketID")) != market_id:
            return None, "NEG_RISK_MARKET_ID_MISMATCH"
        outcomes = _json_value(raw_market.get("outcomes"))
        tokens = _json_value(raw_market.get("clobTokenIds"))
        if outcomes != contract["event_filters"]["required_binary_outcomes"]:
            return None, "OUTCOMES_NOT_YES_NO"
        if not isinstance(tokens, list) or len(tokens) != 2:
            return None, "TOKEN_IDS_INVALID"
        no_token = str(tokens[1] or "")
        if not no_token or no_token in no_tokens:
            return None, "NO_TOKEN_INVALID_OR_DUPLICATE"
        question_id = _hex32(raw_market.get("questionID"))
        if question_id is None or question_id[:-2] != market_id[:-2]:
            return None, "QUESTION_ID_NOT_LINKED_TO_MARKET"
        question_index = int(question_id[-2:], 16)
        if question_index in question_indices:
            return None, "QUESTION_INDEX_DUPLICATE"
        question_indices.add(question_index)
        no_tokens.add(no_token)
        enriched.append(
            {
                **market,
                "no_token_id": no_token,
                "question_id": question_id,
                "question_index": question_index,
            }
        )
    if question_indices != set(range(len(enriched))):
        return None, "QUESTION_INDICES_NOT_CONTIGUOUS"
    enriched.sort(key=lambda item: int(item["question_index"]))
    return {
        **event,
        "neg_risk_market_id": market_id,
        "markets": enriched,
    }, None


def validate_linked_market(
    market: Mapping[str, Any], info: Mapping[str, Any], shares: float
) -> tuple[dict[str, Any] | None, str | None]:
    validated, reason = validate_clob_market(market, info, shares)
    if validated is None:
        return None, reason
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
    return {**validated, "no_token_id": market["no_token_id"], "question_id": market["question_id"], "question_index": market["question_index"]}, None


def _rpc_hex_int(value: Any) -> int | None:
    candidate = str(value or "")
    try:
        result = int(candidate, 16)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def collect_onchain_conversion_parameters(
    events: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
    rpc_json: HttpJson,
) -> dict[str, Any]:
    settings = contract["onchain_conversion"]
    endpoints = [str(item) for item in settings["rpc_endpoints"]]
    required = int(settings["minimum_agreeing_endpoints"])
    errors: dict[str, str] = {}
    heads: dict[str, int] = {}
    for endpoint in endpoints:
        try:
            response = rpc_json(
                "POST",
                endpoint,
                {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
            )
            if not isinstance(response, Mapping):
                raise ValueError("respuesta no es objeto")
            head = _rpc_hex_int(response.get("result"))
            if head is None:
                raise ValueError("blockNumber invalido")
            heads[endpoint] = head
        except Exception as exc:  # public RPC transports vary
            errors[endpoint] = f"HEAD:{type(exc).__name__}:{exc}"
    if len(heads) < required:
        return {
            "valid": False,
            "rejection": "INSUFFICIENT_RPC_HEADS",
            "adapter_address": settings["adapter_address"],
            "endpoints_required": required,
            "endpoint_heads": heads,
            "endpoint_errors": errors,
            "records": {},
        }
    reference_block = min(heads.values())
    block_hex = hex(reference_block)
    market_ids = sorted({str(event["neg_risk_market_id"]) for event in events})
    successful: dict[str, dict[str, Any]] = {}
    for endpoint in heads:
        requests: list[dict[str, Any]] = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getCode",
                "params": [settings["adapter_address"], block_hex],
            }
        ]
        request_index: dict[int, tuple[str, str]] = {}
        next_id = 2
        for market_id in market_ids:
            argument = market_id[2:]
            for field, selector in (
                ("fee_bips", settings["get_fee_bips_selector"]),
                ("question_count", settings["get_question_count_selector"]),
            ):
                request_index[next_id] = (market_id, field)
                requests.append(
                    {
                        "jsonrpc": "2.0",
                        "id": next_id,
                        "method": "eth_call",
                        "params": [
                            {"to": settings["adapter_address"], "data": selector + argument},
                            block_hex,
                        ],
                    }
                )
                next_id += 1
        try:
            response = rpc_json("POST", endpoint, requests)
            if not isinstance(response, list):
                raise ValueError("batch RPC no es lista")
            by_id = {
                int(item.get("id")): item
                for item in response
                if isinstance(item, Mapping) and item.get("id") is not None
            }
            code_response = by_id.get(1)
            code = str(code_response.get("result") if code_response else "")
            if not code.startswith("0x") or len(code) <= 2:
                raise ValueError("codigo del adaptador ausente")
            records: dict[str, dict[str, int]] = {market_id: {} for market_id in market_ids}
            for request_id, (market_id, field) in request_index.items():
                item = by_id.get(request_id)
                if not isinstance(item, Mapping) or item.get("error") is not None:
                    raise ValueError(f"eth_call invalido id={request_id}")
                value = _rpc_hex_int(item.get("result"))
                if value is None:
                    raise ValueError(f"resultado hexadecimal invalido id={request_id}")
                records[market_id][field] = value
            successful[endpoint] = {
                "code_sha256": hashlib.sha256(bytes.fromhex(code[2:])).hexdigest(),
                "records": records,
            }
        except Exception as exc:
            errors[endpoint] = f"BATCH:{type(exc).__name__}:{exc}"
    if len(successful) < required:
        return {
            "valid": False,
            "rejection": "INSUFFICIENT_RPC_BATCHES",
            "adapter_address": settings["adapter_address"],
            "reference_block_number": reference_block,
            "reference_block_hex": block_hex,
            "endpoints_required": required,
            "endpoint_heads": heads,
            "endpoint_errors": errors,
            "successful_endpoints": sorted(successful),
            "records": {},
        }
    code_hashes = {record["code_sha256"] for record in successful.values()}
    if len(code_hashes) != 1:
        rejection = "RPC_ADAPTER_CODE_DISAGREEMENT"
        records_out: dict[str, Any] = {}
    else:
        rejection = None
        records_out = {}
        for market_id in market_ids:
            fee_values = {
                endpoint_record["records"][market_id]["fee_bips"]
                for endpoint_record in successful.values()
            }
            count_values = {
                endpoint_record["records"][market_id]["question_count"]
                for endpoint_record in successful.values()
            }
            if len(fee_values) != 1 or len(count_values) != 1:
                rejection = "RPC_CONVERSION_PARAMETER_DISAGREEMENT"
                records_out = {}
                break
            fee_bips = next(iter(fee_values))
            question_count = next(iter(count_values))
            if not 0 <= fee_bips <= int(settings["fee_denominator"]):
                rejection = "ONCHAIN_FEE_BIPS_INVALID"
                records_out = {}
                break
            if not 2 <= question_count <= 255:
                rejection = "ONCHAIN_QUESTION_COUNT_INVALID"
                records_out = {}
                break
            records_out[market_id] = {
                "fee_bips": fee_bips,
                "question_count": question_count,
            }
    return {
        "valid": rejection is None,
        "rejection": rejection,
        "adapter_address": settings["adapter_address"],
        "adapter_code_sha256": next(iter(code_hashes)) if len(code_hashes) == 1 else None,
        "reference_block_number": reference_block,
        "reference_block_hex": block_hex,
        "endpoints_required": required,
        "endpoint_heads": heads,
        "endpoint_errors": errors,
        "successful_endpoints": sorted(successful),
        "records": records_out,
    }


def collect_rest_metadata(
    events: Sequence[Mapping[str, Any]], http_json: HttpJson
) -> dict[str, Any]:
    tokens = [
        str(market["no_token_id"])
        for event in events
        for market in event["markets"]
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
                "minimum_order_size": _number(item.get("min_order_size", item.get("minOrderSize"))),
                "tick_size": _number(item.get("tick_size", item.get("tickSize"))),
                "neg_risk": item.get("neg_risk", item.get("negRisk")),
            }
    return {
        "request_started_ms": started_ms,
        "response_received_ms": received_ms,
        "request_elapsed_ms": received_ms - started_ms,
        "expected_tokens": len(set(tokens)),
        "received_tokens": len(records),
        "missing_tokens": sorted(set(tokens) - set(records)),
        "records": records,
    }


def _spread(values: Sequence[int | None]) -> int | None:
    if not values or any(value is None for value in values):
        return None
    usable = [int(value) for value in values if value is not None]
    return max(usable) - min(usable)


def evaluate_event(
    event: Mapping[str, Any],
    onchain: Mapping[str, Any],
    metadata: Mapping[str, Mapping[str, Any]],
    books: Mapping[str, Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if int(onchain.get("question_count", -1)) != int(event["market_count"]):
        return None, "ONCHAIN_QUESTION_COUNT_MISMATCH"
    shares = float(contract["hypothetical_position"]["shares_per_leg"])
    received_times: list[int | None] = []
    source_times: list[int | None] = []
    observed_total = 0.0
    conservative_total = 0.0
    legs: list[dict[str, Any]] = []
    for market in event["markets"]:
        token = str(market["no_token_id"])
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
        rest_hash = _normalized_hash(meta.get("official_hash"))
        ws_hash = _normalized_hash(book.get("official_hash"))
        if not rest_hash:
            return None, "REST_METADATA_HASH_MISSING"
        if not ws_hash:
            return None, "WEBSOCKET_HASH_MISSING"
        asks = book.get("asks")
        if not isinstance(asks, list):
            return None, "WEBSOCKET_ASKS_INVALID"
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
            return None, "INSUFFICIENT_WEBSOCKET_ASK_DEPTH"
        received_times.append(_timestamp(book.get("received_timestamp_ms")))
        source_times.append(_timestamp(book.get("source_timestamp_ms")))
        observed_total += float(observed["average_total_cost_per_share"])
        conservative_total += float(conservative["average_total_cost_per_share"])
        legs.append(
            {
                "condition_id": market["condition_id"],
                "question_id": market["question_id"],
                "question_index": market["question_index"],
                "question": market["question"],
                "no_token_id": token,
                "fee_rate": market["fee_rate"],
                "fee_exponent": market["fee_exponent"],
                "tick_size": market["minimum_tick_size"],
                "rest_metadata_timestamp_ms": meta.get("source_timestamp_ms"),
                "websocket_source_timestamp_ms": book.get("source_timestamp_ms"),
                "websocket_received_timestamp_ms": book.get("received_timestamp_ms"),
                "rest_hash": rest_hash,
                "websocket_hash": ws_hash,
                "rest_websocket_hash_equal": rest_hash == ws_hash,
                "levels_sha256": book.get("levels_sha256"),
                "observed": observed,
                "conservative": conservative,
            }
        )
    receive_spread = _spread(received_times)
    source_spread = _spread(source_times)
    if receive_spread is None or receive_spread > int(contract["transport"]["maximum_local_receive_spread_ms_per_event"]):
        return None, "WEBSOCKET_RECEIVE_SPREAD_EXCEEDED"
    settings = contract["onchain_conversion"]
    scale = int(settings["token_unit_scale"])
    denominator = int(settings["fee_denominator"])
    amount_units = int(round(shares * scale))
    if abs(amount_units / scale - shares) > 1e-12:
        return None, "POSITION_NOT_REPRESENTABLE_IN_TOKEN_UNITS"
    fee_bips = int(onchain["fee_bips"])
    fee_units = amount_units * fee_bips // denominator
    amount_out_units = amount_units - fee_units
    multiplier = int(event["market_count"]) - 1
    conversion_collateral_total = multiplier * amount_out_units / scale
    conversion_collateral_per_bundle_share = conversion_collateral_total / shares
    conversion_fee_total = multiplier * fee_units / scale
    observed_edge = conversion_collateral_per_bundle_share - observed_total
    conservative_edge = conversion_collateral_per_bundle_share - conservative_total
    threshold = float(contract["cost_model"]["minimum_conservative_edge_per_bundle_share"])
    candidate = observed_edge > 0.0 and conservative_edge >= threshold
    return {
        "event_id": event["event_id"],
        "slug": event["slug"],
        "title": event["title"],
        "neg_risk_market_id": event["neg_risk_market_id"],
        "market_count": event["market_count"],
        "volume24hr": event["volume24hr"],
        "websocket_receive_spread_ms": receive_spread,
        "websocket_source_spread_ms": source_spread,
        "onchain_fee_bips": fee_bips,
        "onchain_question_count": onchain["question_count"],
        "conversion_collateral_multiplier": multiplier,
        "conversion_fee_total_at_5_shares": conversion_fee_total,
        "conversion_collateral_total_at_5_shares": conversion_collateral_total,
        "conversion_collateral_per_bundle_share": conversion_collateral_per_bundle_share,
        "observed_total_cost_per_bundle_share": observed_total,
        "observed_edge_per_bundle_share_before_gas": observed_edge,
        "observed_pnl_at_5_shares_before_gas": observed_edge * shares,
        "conservative_total_cost_per_bundle_share": conservative_total,
        "conservative_edge_per_bundle_share_before_gas": conservative_edge,
        "conservative_pnl_at_5_shares_before_gas": conservative_edge * shares,
        "cost_candidate_before_gas": candidate,
        "gas_included": False,
        "semantic_exhaustiveness_required": False,
        "atomic_multileg_order_execution_available": False,
        "legs": legs,
    }, None


def classify_census(
    *, selected_events: int, evaluated_events: int, cost_candidates: int, contract: Mapping[str, Any]
) -> dict[str, Any]:
    gate = contract["evidence_gate"]
    required = max(
        int(gate["minimum_evaluated_events"]),
        math.ceil(float(gate["minimum_evaluated_fraction_of_selected"]) * selected_events),
    )
    coverage = evaluated_events / selected_events if selected_events else 0.0
    if evaluated_events < required:
        return {
            "verdict": "FAIL_CENSUS_INSUFFICIENT_BUY_ALL_NO_BOOKS",
            "next_step": "RESOLVE_ONCHAIN_OR_BOOK_COVERAGE_BEFORE_ECONOMIC_CONCLUSION",
            "structural_snapshot_conclusion_allowed": False,
            "required_evaluated_events": required,
            "coverage": coverage,
        }
    if cost_candidates:
        return {
            "verdict": "REQUIRE_FRESH_PERSISTENCE_GAS_AND_SEQUENTIAL_EXECUTION_STUDY",
            "next_step": "PREREGISTER_PERSISTENCE_GAS_AND_SEQUENTIAL_EXECUTION_STUDY",
            "structural_snapshot_conclusion_allowed": True,
            "required_evaluated_events": required,
            "coverage": coverage,
        }
    return {
        "verdict": "REJECT_BUY_ALL_NO_CONVERSION_FEASIBILITY",
        "next_step": "DO_NOT_LAUNCH_OBSERVER_FOR_BUY_ALL_NO_CONVERSION_HYPOTHESIS",
        "structural_snapshot_conclusion_allowed": True,
        "required_evaluated_events": required,
        "coverage": coverage,
    }


async def census_v049_async(
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
        raise V049CensusError("Gamma no devolvio la lista esperada")
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
                float(contract["hypothetical_position"]["shares_per_leg"]),
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
        onchain_rejections[str(onchain.get("rejection") or "ONCHAIN_PARAMETERS_INVALID")] = len(validated_events)
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
        str(market["no_token_id"])
        for event in onchain_validated
        for market in event["markets"]
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
        raise V049CensusError("Fuentes V0.49 sin mapas de libros")
    evaluation_rejections: Counter[str] = Counter()
    evaluated: list[dict[str, Any]] = []
    if metadata_to_ws_ms is None or metadata_to_ws_ms < 0 or metadata_to_ws_ms > int(contract["metadata"]["maximum_rest_response_to_ws_connect_ms"]):
        evaluation_rejections["REST_METADATA_TO_WS_WINDOW_EXCEEDED"] = len(onchain_validated)
    else:
        for event in onchain_validated:
            record = onchain_records[str(event["neg_risk_market_id"])]
            result, reason = evaluate_event(event, record, metadata_records, books, contract)
            if result is None:
                evaluation_rejections[str(reason)] += 1
            else:
                evaluated.append(result)
    evaluated.sort(key=lambda item: float(item["conservative_edge_per_bundle_share_before_gas"]), reverse=True)
    candidates = [event for event in evaluated if bool(event["cost_candidate_before_gas"])]
    classification = classify_census(
        selected_events=len(selected),
        evaluated_events=len(evaluated),
        cost_candidates=len(candidates),
        contract=contract,
    )
    try:
        prereg_reference = str(prereg_file.relative_to(root)).replace("\\", "/")
    except ValueError:
        prereg_reference = str(prereg_file)
    onchain_public = {key: value for key, value in onchain.items() if key != "records"}
    onchain_public["events_with_parameters"] = len(onchain_records) if isinstance(onchain_records, Mapping) else 0
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
            "events_evaluated": len(evaluated),
            "cost_candidates_before_gas": len(candidates),
            "evaluation_rejections": dict(sorted(evaluation_rejections.items())),
            "elapsed_seconds": time.monotonic() - started,
            "events": evaluated,
        },
        "interpretation": {
            "conversion_property_structural": True,
            "semantic_exhaustiveness_required": False,
            "gas_included": False,
            "nonpositive_edge_cannot_be_rescued_by_nonnegative_gas": True,
            "performance_validation": False,
            "atomic_multileg_order_execution_available": False,
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


def census_v049(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(census_v049_async(**kwargs))


__all__ = [
    "RESULT_SCHEMA",
    "V049CensusError",
    "census_v049",
    "census_v049_async",
    "classify_census",
    "collect_onchain_conversion_parameters",
    "collect_rest_metadata",
    "evaluate_event",
    "filter_linked_event",
    "validate_linked_market",
]
