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
    _http_json,
    executable_buy_cost,
    filter_event,
    validate_clob_market,
)
from polymarket_bot.v045_contract import frozen_contract as v045_frozen_contract
from polymarket_bot.v047_ws_census import WsCollector, collect_initial_books
from polymarket_bot.v048_contract import VARIANT, load_and_verify_preregistration


RESULT_SCHEMA = "result_v048_rest_metadata_ws_economic_census_1"


class V048CensusError(RuntimeError):
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


def collect_rest_metadata(
    events: Sequence[Mapping[str, Any]], http_json: HttpJson
) -> dict[str, Any]:
    tokens = [
        str(market["yes_token_id"])
        for event in events
        for market in event["markets"]
    ]
    started_ms = time.time_ns() // 1_000_000
    response = http_json(
        "POST",
        f"{CLOB_BASE_URL}/books",
        [{"token_id": token} for token in tokens],
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
    metadata: Mapping[str, Mapping[str, Any]],
    books: Mapping[str, Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    shares = float(contract["hypothetical_position"]["shares_per_leg"])
    received_times: list[int | None] = []
    source_times: list[int | None] = []
    observed_total = 0.0
    conservative_total = 0.0
    legs: list[dict[str, Any]] = []
    for market in event["markets"]:
        token = str(market["yes_token_id"])
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
                "question": market["question"],
                "yes_token_id": token,
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
        "websocket_receive_spread_ms": receive_spread,
        "websocket_source_spread_ms": source_spread,
        "observed_total_cost_per_share": observed_total,
        "observed_conditional_edge_per_share": observed_edge,
        "observed_conditional_pnl_at_5_shares": observed_edge * shares,
        "conservative_total_cost_per_share": conservative_total,
        "conservative_conditional_edge_per_share": conservative_edge,
        "conservative_conditional_pnl_at_5_shares": conservative_edge * shares,
        "cost_candidate": observed_edge > 0.0 and conservative_edge >= threshold,
        "semantic_exhaustiveness_verified": False,
        "executable_atomically": False,
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
            "verdict": "FAIL_CENSUS_INSUFFICIENT_CORRECTED_WS_BOOKS",
            "next_step": "RESOLVE_CORRECTED_WS_COVERAGE_BEFORE_ECONOMIC_CONCLUSION",
            "economic_snapshot_conclusion_allowed": False,
            "required_evaluated_events": required,
            "coverage": coverage,
        }
    if cost_candidates:
        return {
            "verdict": "REQUIRE_MANUAL_EXHAUSTIVENESS_REVIEW_AND_FRESH_PERSISTENCE_OBSERVER",
            "next_step": "MANUALLY_VERIFY_EXHAUSTIVENESS_BEFORE_PREREGISTERED_PERSISTENCE_TEST",
            "economic_snapshot_conclusion_allowed": False,
            "required_evaluated_events": required,
            "coverage": coverage,
        }
    return {
        "verdict": "REJECT_CURRENT_CORRECTED_WS_BUY_ALL_YES_FEASIBILITY",
        "next_step": "DO_NOT_LAUNCH_PERSISTENCE_OBSERVER_FOR_THIS_HYPOTHESIS",
        "economic_snapshot_conclusion_allowed": True,
        "required_evaluated_events": required,
        "coverage": coverage,
    }


async def census_v048_async(
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
        raise V048CensusError("Gamma no devolvio la lista esperada")
    filter_rejections: Counter[str] = Counter()
    selected: list[dict[str, Any]] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            filter_rejections["EVENT_INVALID"] += 1
            continue
        event, reason = filter_event(raw, v045_frozen_contract())
        if event is None:
            filter_rejections[str(reason)] += 1
        elif len(selected) < int(sample["maximum_eligible_events"]):
            selected.append(event)
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
            validated, reason = validate_clob_market(
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
    rest_metadata = collect_rest_metadata(validated_events, http_json)
    tokens = [
        str(market["yes_token_id"])
        for event in validated_events
        for market in event["markets"]
    ]
    websocket = await ws_collector(str(contract["transport"]["endpoint"]), tokens, contract)
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
        raise V048CensusError("Fuentes V0.48 sin mapas de libros")
    evaluation_rejections: Counter[str] = Counter()
    evaluated: list[dict[str, Any]] = []
    if metadata_to_ws_ms is None or metadata_to_ws_ms < 0 or metadata_to_ws_ms > int(contract["metadata"]["maximum_rest_response_to_ws_connect_ms"]):
        evaluation_rejections["REST_METADATA_TO_WS_WINDOW_EXCEEDED"] = len(validated_events)
    else:
        for event in validated_events:
            record, reason = evaluate_event(event, metadata_records, books, contract)
            if record is None:
                evaluation_rejections[str(reason)] += 1
            else:
                evaluated.append(record)
    evaluated.sort(key=lambda item: float(item["conservative_conditional_edge_per_share"]), reverse=True)
    candidates = [event for event in evaluated if bool(event["cost_candidate"])]
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
            "tokens_subscribed": len(set(tokens)),
            "filter_rejections": dict(sorted(filter_rejections.items())),
            "validation_rejections": dict(sorted(validation_rejections.items())),
        },
        "rest_metadata": {
            key: value for key, value in rest_metadata.items() if key != "records"
        },
        "websocket": {key: value for key, value in websocket.items() if key != "books"},
        "census": {
            **classification,
            "metadata_response_to_ws_connect_ms": metadata_to_ws_ms,
            "events_evaluated": len(evaluated),
            "cost_candidates": len(candidates),
            "evaluation_rejections": dict(sorted(evaluation_rejections.items())),
            "elapsed_seconds": time.monotonic() - started,
            "events": evaluated,
        },
        "interpretation": {
            "conditional_edge_only": True,
            "performance_validation": False,
            "semantic_exhaustiveness_verified_for_candidates": False,
            "atomic_multileg_execution_available": False,
            "automatic_followup_launched": False,
            "next_step": classification["next_step"],
        },
        "safety": {
            "network_calls": "PUBLIC_READ_ONLY_GAMMA_CLOB_INFO_REST_BOOK_METADATA_AND_MARKET_WEBSOCKET",
            "orders_created": 0,
            "paper_orders": 0,
            "wallet_required": False,
            "authentication_used": False,
            "real_money": "BLOQUEADO",
        },
    }
    _write_atomic(result_file, payload)
    return payload


def census_v048(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(census_v048_async(**kwargs))


__all__ = [
    "RESULT_SCHEMA",
    "V048CensusError",
    "census_v048",
    "census_v048_async",
    "classify_census",
    "collect_rest_metadata",
    "evaluate_event",
]
