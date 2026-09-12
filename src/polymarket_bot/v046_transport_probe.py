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
from polymarket_bot.v045_census import CLOB_BASE_URL, GAMMA_EVENTS_URL, _http_json, filter_event
from polymarket_bot.v045_contract import frozen_contract as v045_frozen_contract
from polymarket_bot.v046_contract import VARIANT, load_and_verify_preregistration


RESULT_SCHEMA = "result_v046_rest_websocket_transport_probe_1"


class V046ProbeError(RuntimeError):
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


def _normalized_hash(value: Any) -> str:
    result = str(value or "").strip().lower()
    return result[2:] if result.startswith("0x") else result


def _timestamp_ms(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _normalize_ws_book(item: Mapping[str, Any], received_ms: int) -> dict[str, Any] | None:
    payload: Mapping[str, Any] = item
    event_type = str(item.get("event_type") or item.get("type") or "")
    if event_type != "book":
        return None
    nested = item.get("payload")
    if isinstance(nested, Mapping):
        payload = nested
    token = str(
        payload.get("asset_id")
        or payload.get("assetId")
        or payload.get("token_id")
        or payload.get("tokenId")
        or ""
    )
    timestamp = _timestamp_ms(payload.get("timestamp"))
    official_hash = _normalized_hash(payload.get("hash"))
    asks = payload.get("asks")
    bids = payload.get("bids")
    if not token or timestamp is None or not official_hash:
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
        "condition_id": str(payload.get("market") or ""),
        "source_timestamp_ms": timestamp,
        "received_timestamp_ms": int(received_ms),
        "official_hash": official_hash,
        "levels_sha256": hashlib.sha256(canonical).hexdigest(),
        "ask_levels": len(asks),
        "bid_levels": len(bids),
    }


async def collect_initial_websocket_books(
    endpoint: str,
    token_ids: Sequence[str],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    expected = {str(token) for token in token_ids}
    if not expected:
        raise V046ProbeError("No hay tokens para el probe WebSocket")
    websocket_contract = contract["websocket"]
    timeout_seconds = float(websocket_contract["initial_snapshot_timeout_seconds"])
    started_monotonic = time.monotonic()
    connected_at_ms: int | None = None
    books: dict[str, dict[str, Any]] = {}
    malformed_messages = 0
    heartbeat_task: asyncio.Task[Any] | None = None
    try:
        async with connect(
            endpoint,
            ping_interval=None,
            open_timeout=12,
            close_timeout=5,
            max_size=8 * 1024 * 1024,
            max_queue=4096,
            user_agent_header="PolyMarkerQuantBot-V0.46-read-only-probe/1.0",
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
                    await asyncio.sleep(float(websocket_contract["heartbeat_seconds"]))
                    await websocket.send(str(websocket_contract["heartbeat_text"]))

            heartbeat_task = asyncio.create_task(heartbeat())
            deadline = time.monotonic() + timeout_seconds
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
                    book = _normalize_ws_book(item, received_ms)
                    if book is None:
                        continue
                    token = str(book["token_id"])
                    if token in expected and token not in books:
                        books[token] = book
    except Exception as exc:
        raise V046ProbeError(f"Fallo WebSocket publico: {type(exc).__name__}: {exc}") from exc
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
    return {
        "connected_at_ms": connected_at_ms,
        "finished_at_ms": time.time_ns() // 1_000_000,
        "elapsed_seconds": time.monotonic() - started_monotonic,
        "expected_tokens": len(expected),
        "received_tokens": len(books),
        "missing_tokens": sorted(expected - set(books)),
        "malformed_messages": malformed_messages,
        "books": books,
    }


def _rest_batch(
    event: Mapping[str, Any], http_json: HttpJson
) -> dict[str, Any]:
    tokens = [str(market["yes_token_id"]) for market in event["markets"]]
    before_ms = time.time_ns() // 1_000_000
    response = http_json(
        "POST",
        f"{CLOB_BASE_URL}/books",
        [{"token_id": token} for token in tokens],
    )
    after_ms = time.time_ns() // 1_000_000
    if not isinstance(response, list):
        return {
            "request_started_ms": before_ms,
            "response_received_ms": after_ms,
            "valid": False,
            "books": {},
        }
    books: dict[str, dict[str, Any]] = {}
    for item in response:
        if not isinstance(item, Mapping):
            continue
        token = str(item.get("asset_id") or item.get("assetId") or "")
        timestamp = _timestamp_ms(item.get("timestamp"))
        official_hash = _normalized_hash(item.get("hash"))
        if token in tokens and timestamp is not None and official_hash:
            books[token] = {
                "source_timestamp_ms": timestamp,
                "official_hash": official_hash,
                "ask_levels": len(item.get("asks", [])) if isinstance(item.get("asks"), list) else None,
                "bid_levels": len(item.get("bids", [])) if isinstance(item.get("bids"), list) else None,
            }
    return {
        "request_started_ms": before_ms,
        "response_received_ms": after_ms,
        "request_elapsed_ms": after_ms - before_ms,
        "valid": set(books) == set(tokens),
        "books": books,
    }


def _spread(values: Sequence[int | None]) -> int | None:
    usable = [int(value) for value in values if value is not None]
    if len(usable) != len(values) or not usable:
        return None
    return max(usable) - min(usable)


def evaluate_event_transport(
    event: Mapping[str, Any],
    rest_before: Mapping[str, Any],
    websocket: Mapping[str, Any],
    rest_after: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    tokens = [str(market["yes_token_id"]) for market in event["markets"]]
    ws_books = websocket.get("books", {})
    before_books = rest_before.get("books", {})
    after_books = rest_after.get("books", {})
    token_records: list[dict[str, Any]] = []
    for token in tokens:
        before = before_books.get(token) if isinstance(before_books, Mapping) else None
        ws = ws_books.get(token) if isinstance(ws_books, Mapping) else None
        after = after_books.get(token) if isinstance(after_books, Mapping) else None
        before_hash = _normalized_hash(before.get("official_hash")) if isinstance(before, Mapping) else ""
        ws_hash = _normalized_hash(ws.get("official_hash")) if isinstance(ws, Mapping) else ""
        after_hash = _normalized_hash(after.get("official_hash")) if isinstance(after, Mapping) else ""
        hash_continuity = bool(ws_hash and ws_hash in {before_hash, after_hash})
        token_records.append(
            {
                "token_id": token,
                "rest_before_timestamp_ms": before.get("source_timestamp_ms") if isinstance(before, Mapping) else None,
                "websocket_source_timestamp_ms": ws.get("source_timestamp_ms") if isinstance(ws, Mapping) else None,
                "websocket_received_timestamp_ms": ws.get("received_timestamp_ms") if isinstance(ws, Mapping) else None,
                "rest_after_timestamp_ms": after.get("source_timestamp_ms") if isinstance(after, Mapping) else None,
                "rest_before_hash": before_hash,
                "websocket_hash": ws_hash,
                "rest_after_hash": after_hash,
                "hash_continuity": hash_continuity,
            }
        )
    ws_receive_spread = _spread([item["websocket_received_timestamp_ms"] for item in token_records])
    ws_source_spread = _spread([item["websocket_source_timestamp_ms"] for item in token_records])
    rest_before_spread = _spread([item["rest_before_timestamp_ms"] for item in token_records])
    rest_after_spread = _spread([item["rest_after_timestamp_ms"] for item in token_records])
    maximum_receive = int(contract["websocket"]["maximum_local_receive_spread_ms_per_event"])
    all_hashes = all(bool(item["hash_continuity"]) for item in token_records)
    complete = (
        bool(rest_before.get("valid"))
        and bool(rest_after.get("valid"))
        and all(item["websocket_received_timestamp_ms"] is not None for item in token_records)
        and ws_receive_spread is not None
        and ws_receive_spread <= maximum_receive
        and all_hashes
    )
    return {
        "event_id": event["event_id"],
        "slug": event["slug"],
        "title": event["title"],
        "market_count": event["market_count"],
        "rest_before_source_spread_ms": rest_before_spread,
        "websocket_source_spread_ms": ws_source_spread,
        "websocket_receive_spread_ms": ws_receive_spread,
        "rest_after_source_spread_ms": rest_after_spread,
        "all_hashes_continuous": all_hashes,
        "transport_complete": complete,
        "source_spread_exceeds_receive_gate": bool(
            ws_source_spread is not None
            and ws_source_spread > maximum_receive
            and ws_receive_spread is not None
            and ws_receive_spread <= maximum_receive
        ),
        "tokens": token_records,
    }


def classify_probe(events: Sequence[Mapping[str, Any]], eligible_count: int, contract: Mapping[str, Any]) -> dict[str, Any]:
    complete = [event for event in events if bool(event.get("transport_complete"))]
    gate = contract["evidence_gate"]
    required = max(
        int(gate["minimum_complete_events"]),
        math.ceil(float(gate["minimum_complete_fraction_of_eligible"]) * eligible_count),
    )
    if len(complete) >= required:
        verdict = "PASS_WS_RECEIVE_WINDOW_AND_HASH_CONTINUITY"
        next_step = "PREREGISTER_V047_WS_RECEIVE_WINDOW_ECONOMIC_CENSUS"
        transport_validated = True
    else:
        verdict = "FAIL_TRANSPORT_PROBE_INSUFFICIENT_COMPLETE_EVENTS"
        next_step = "DO_NOT_REPEAT_ECONOMIC_CENSUS_UNTIL_TRANSPORT_IS_RESOLVED"
        transport_validated = False
    source_divergence = sum(bool(event.get("source_spread_exceeds_receive_gate")) for event in complete)
    if transport_validated and source_divergence:
        semantics = "SOURCE_TIMESTAMPS_DO_NOT_MEASURE_CROSS_TOKEN_RECEIVE_WINDOW"
    elif transport_validated:
        semantics = "SOURCE_AND_RECEIVE_SPREADS_ALIGNED_IN_THIS_PROBE"
    else:
        semantics = "UNRESOLVED"
    return {
        "verdict": verdict,
        "next_step": next_step,
        "transport_validated": transport_validated,
        "eligible_events": eligible_count,
        "required_complete_events": required,
        "complete_events": len(complete),
        "complete_fraction": len(complete) / eligible_count if eligible_count else 0.0,
        "source_timestamp_semantics": semantics,
        "complete_events_with_source_receive_divergence": source_divergence,
    }


async def probe_v046_async(
    *,
    prereg_path: str | Path,
    result_path: str | Path,
    project_root: str | Path = ROOT,
    http_json: HttpJson = _http_json,
    ws_collector: WsCollector = collect_initial_websocket_books,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    result_file = Path(result_path).resolve()
    prereg = load_and_verify_preregistration(prereg_file, project_root=root)
    if result_file.is_file():
        return json.loads(result_file.read_text(encoding="utf-8"))
    contract = prereg["technical_contract"]
    query = urllib.parse.urlencode(
        {
            "active": "true",
            "closed": "false",
            "limit": 100,
            "order": "volume24hr",
            "ascending": "false",
        }
    )
    started = time.monotonic()
    raw_events = http_json("GET", f"{GAMMA_EVENTS_URL}?{query}", None)
    if not isinstance(raw_events, list):
        raise V046ProbeError("Gamma no devolvio la lista esperada")
    rejections: Counter[str] = Counter()
    eligible: list[dict[str, Any]] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            rejections["EVENT_INVALID"] += 1
            continue
        event, reason = filter_event(raw, v045_frozen_contract())
        if event is None:
            rejections[str(reason)] += 1
        elif len(eligible) < int(contract["event_selection"]["maximum_eligible_events"]):
            eligible.append(event)
    rest_before = {str(event["event_id"]): _rest_batch(event, http_json) for event in eligible}
    all_tokens = [
        str(market["yes_token_id"])
        for event in eligible
        for market in event["markets"]
    ]
    websocket = await ws_collector(str(contract["websocket"]["endpoint"]), all_tokens, contract)
    rest_after = {str(event["event_id"]): _rest_batch(event, http_json) for event in eligible}
    evaluated = [
        evaluate_event_transport(
            event,
            rest_before[str(event["event_id"])],
            websocket,
            rest_after[str(event["event_id"])],
            contract,
        )
        for event in eligible
    ]
    classification = classify_probe(evaluated, len(eligible), contract)
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
            "verified_before_network_probe": True,
        },
        "sample": {
            "events_received": len(raw_events),
            "events_eligible": len(eligible),
            "tokens_subscribed": len(set(all_tokens)),
            "filter_rejections": dict(sorted(rejections.items())),
        },
        "websocket": {
            key: value for key, value in websocket.items() if key != "books"
        },
        "transport": {
            **classification,
            "events": evaluated,
            "elapsed_seconds": time.monotonic() - started,
        },
        "interpretation": {
            "economic_result": False,
            "v045_family_rejected": False,
            "automatic_followup_launched": False,
            "next_step": classification["next_step"],
        },
        "safety": {
            "network_calls": "PUBLIC_READ_ONLY_GAMMA_CLOB_REST_AND_MARKET_WEBSOCKET",
            "orders_created": 0,
            "paper_orders": 0,
            "wallet_required": False,
            "authentication_used": False,
            "real_money": "BLOQUEADO",
        },
    }
    _write_atomic(result_file, payload)
    return payload


def probe_v046(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(probe_v046_async(**kwargs))


__all__ = [
    "RESULT_SCHEMA",
    "V046ProbeError",
    "classify_probe",
    "collect_initial_websocket_books",
    "evaluate_event_transport",
    "probe_v046",
    "probe_v046_async",
]
