from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import urllib.parse
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

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
from polymarket_bot.v047_contract import VARIANT, load_and_verify_preregistration


RESULT_SCHEMA = "result_v047_ws_neg_risk_economic_census_1"


class V047CensusError(RuntimeError):
    pass


HttpJson = Callable[[str, str, Any | None], Any]
WsCollector = Callable[[str, Sequence[str], Mapping[str, Any]], Awaitable[dict[str, Any]]]


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


def _normalize_book(item: Mapping[str, Any], received_ms: int) -> dict[str, Any] | None:
    event_type = str(item.get("event_type") or item.get("type") or "")
    if event_type != "book":
        return None
    payload: Mapping[str, Any] = item
    if isinstance(item.get("payload"), Mapping):
        payload = item["payload"]
    token = str(
        payload.get("asset_id")
        or payload.get("assetId")
        or payload.get("token_id")
        or payload.get("tokenId")
        or ""
    )
    condition = str(payload.get("market") or "")
    source_ms = _timestamp(payload.get("timestamp"))
    official_hash = _normalized_hash(payload.get("hash"))
    asks = payload.get("asks")
    bids = payload.get("bids")
    minimum_order = _number(payload.get("min_order_size", payload.get("minOrderSize")))
    tick = _number(payload.get("tick_size", payload.get("tickSize")))
    neg_risk_value = payload.get("neg_risk", payload.get("negRisk"))
    neg_risk = neg_risk_value if isinstance(neg_risk_value, bool) else None
    if not token or not condition or source_ms is None or not official_hash:
        return None
    if not isinstance(asks, list) or not isinstance(bids, list):
        return None
    canonical = json.dumps(
        {"asks": asks, "bids": bids},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "token_id": token,
        "condition_id": condition,
        "source_timestamp_ms": source_ms,
        "received_timestamp_ms": int(received_ms),
        "official_hash": official_hash,
        "levels_sha256": hashlib.sha256(canonical).hexdigest(),
        "asks": asks,
        "bids": bids,
        "minimum_order_size": minimum_order,
        "tick_size": tick,
        "neg_risk": neg_risk,
    }


async def collect_initial_books(
    endpoint: str,
    token_ids: Sequence[str],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    expected = {str(token) for token in token_ids}
    if not expected:
        raise V047CensusError("No hay tokens V0.47 para suscribir")
    transport = contract["transport"]
    started = time.monotonic()
    books: dict[str, dict[str, Any]] = {}
    malformed_messages = 0
    heartbeat_task: asyncio.Task[Any] | None = None
    connected_at_ms: int | None = None
    try:
        async with connect(
            endpoint,
            ping_interval=None,
            open_timeout=12,
            close_timeout=5,
            max_size=8 * 1024 * 1024,
            max_queue=4096,
            user_agent_header="PolyMarkerQuantBot-V0.47-read-only-census/1.0",
            proxy=None,
        ) as websocket:
            connected_at_ms = time.time_ns() // 1_000_000
            await websocket.send(
                json.dumps(
                    {"assets_ids": sorted(expected), "type": "market"},
                    separators=(",", ":"),
                )
            )

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(float(transport["heartbeat_seconds"]))
                    await websocket.send(str(transport["heartbeat_text"]))

            heartbeat_task = asyncio.create_task(heartbeat())
            deadline = time.monotonic() + float(transport["initial_snapshot_timeout_seconds"])
            while set(books) != expected:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                received_ms = time.time_ns() // 1_000_000
                text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                if text == "PONG":
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    malformed_messages += 1
                    continue
                items = payload if isinstance(payload, list) else [payload]
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    book = _normalize_book(item, received_ms)
                    if book is not None and book["token_id"] in expected and book["token_id"] not in books:
                        books[str(book["token_id"])] = book
    except Exception as exc:
        raise V047CensusError(f"Fallo WebSocket V0.47: {type(exc).__name__}: {exc}") from exc
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
    return {
        "connected_at_ms": connected_at_ms,
        "finished_at_ms": time.time_ns() // 1_000_000,
        "elapsed_seconds": time.monotonic() - started,
        "expected_tokens": len(expected),
        "received_tokens": len(books),
        "missing_tokens": sorted(expected - set(books)),
        "malformed_messages": malformed_messages,
        "books": books,
    }


def _spread(values: Sequence[int | None]) -> int | None:
    if not values or any(value is None for value in values):
        return None
    usable = [int(value) for value in values if value is not None]
    return max(usable) - min(usable)


def evaluate_ws_event(
    event: Mapping[str, Any],
    books: Mapping[str, Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    shares = float(contract["hypothetical_position"]["shares_per_leg"])
    receive_values: list[int | None] = []
    source_values: list[int | None] = []
    observed_total = 0.0
    conservative_total = 0.0
    legs: list[dict[str, Any]] = []
    for market in event["markets"]:
        token = str(market["yes_token_id"])
        book = books.get(token)
        if not isinstance(book, Mapping):
            return None, "WEBSOCKET_BOOK_MISSING"
        if str(book.get("condition_id") or "").lower() != str(market["condition_id"]).lower():
            return None, "WEBSOCKET_CONDITION_ID_MISMATCH"
        if book.get("neg_risk") is not True:
            return None, "WEBSOCKET_NEG_RISK_NOT_TRUE"
        if not _normalized_hash(book.get("official_hash")):
            return None, "WEBSOCKET_HASH_MISSING"
        ws_minimum = _number(book.get("minimum_order_size"))
        ws_tick = _number(book.get("tick_size"))
        if ws_minimum is None or abs(ws_minimum - float(market["minimum_order_size"])) > 1e-12:
            return None, "WEBSOCKET_MINIMUM_ORDER_MISMATCH"
        if ws_tick is None or abs(ws_tick - float(market["minimum_tick_size"])) > 1e-12:
            return None, "WEBSOCKET_TICK_SIZE_MISMATCH"
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
        receive_values.append(_timestamp(book.get("received_timestamp_ms")))
        source_values.append(_timestamp(book.get("source_timestamp_ms")))
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
                "source_timestamp_ms": book["source_timestamp_ms"],
                "received_timestamp_ms": book["received_timestamp_ms"],
                "official_hash": _normalized_hash(book["official_hash"]),
                "levels_sha256": book.get("levels_sha256"),
                "observed": observed,
                "conservative": conservative,
            }
        )
    receive_spread = _spread(receive_values)
    source_spread = _spread(source_values)
    maximum_receive = int(contract["transport"]["maximum_local_receive_spread_ms_per_event"])
    if receive_spread is None or receive_spread > maximum_receive:
        return None, "WEBSOCKET_RECEIVE_SPREAD_EXCEEDED"
    payout = float(contract["hypothetical_position"]["payout_if_exactly_one_yes_per_share"])
    observed_edge = payout - observed_total
    conservative_edge = payout - conservative_total
    threshold = float(contract["cost_model"]["minimum_conservative_conditional_edge_per_share"])
    candidate = observed_edge > 0.0 and conservative_edge >= threshold
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
        "cost_candidate": candidate,
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
            "verdict": "FAIL_CENSUS_INSUFFICIENT_WS_BOOKS",
            "next_step": "RESOLVE_WS_COVERAGE_BEFORE_ECONOMIC_CONCLUSION",
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
        "verdict": "REJECT_CURRENT_WS_BUY_ALL_YES_FEASIBILITY",
        "next_step": "DO_NOT_LAUNCH_PERSISTENCE_OBSERVER_FOR_THIS_HYPOTHESIS",
        "economic_snapshot_conclusion_allowed": True,
        "required_evaluated_events": required,
        "coverage": coverage,
    }


async def census_v047_async(
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
        raise V047CensusError("Gamma no devolvio la lista esperada")
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
        validated_markets: list[dict[str, Any]] = []
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
            validated_markets.append(validated)
        if failed is not None:
            validation_rejections[failed] += 1
        else:
            validated_events.append({**event, "markets": validated_markets})
    tokens = [
        str(market["yes_token_id"])
        for event in validated_events
        for market in event["markets"]
    ]
    websocket = await ws_collector(str(contract["transport"]["endpoint"]), tokens, contract)
    books = websocket.get("books", {})
    if not isinstance(books, Mapping):
        raise V047CensusError("Coleccion WebSocket sin libros")
    evaluation_rejections: Counter[str] = Counter()
    evaluated: list[dict[str, Any]] = []
    for event in validated_events:
        record, reason = evaluate_ws_event(event, books, contract)
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
        "websocket": {key: value for key, value in websocket.items() if key != "books"},
        "census": {
            **classification,
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
            "network_calls": "PUBLIC_READ_ONLY_GAMMA_CLOB_INFO_AND_MARKET_WEBSOCKET",
            "orders_created": 0,
            "paper_orders": 0,
            "wallet_required": False,
            "authentication_used": False,
            "real_money": "BLOQUEADO",
        },
    }
    _write_atomic(result_file, payload)
    return payload


def census_v047(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(census_v047_async(**kwargs))


__all__ = [
    "RESULT_SCHEMA",
    "V047CensusError",
    "census_v047",
    "census_v047_async",
    "classify_census",
    "collect_initial_books",
    "evaluate_ws_event",
]
