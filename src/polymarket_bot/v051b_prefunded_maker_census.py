from __future__ import annotations

import asyncio
import json
import math
import time
import urllib.parse
from collections import Counter
from collections.abc import Mapping
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
from polymarket_bot.v051_prefunded_maker_census import (
    HttpJson,
    V051CensusError,
    _timestamp,
    classify_census,
    collect_rest_metadata,
    evaluate_market,
    normalize_standard_market,
)
from polymarket_bot.v051b_contract import VARIANT, load_and_verify_preregistration


RESULT_SCHEMA = "result_v051b_prefunded_maker_transport_correction_census_1"


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def validate_corrected_standard_clob_market(
    market: Mapping[str, Any], info: Mapping[str, Any], shares: float
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        minimum, tick, neg_risk, rate, exponent, taker_only = _clob_market_values(info)
    except Exception:
        return None, "CLOB_MARKET_INFO_INVALID"
    reported_condition = str(info.get("condition_id", info.get("conditionId", "")))
    if reported_condition and reported_condition.lower() != str(market["condition_id"]).lower():
        return None, "CONDITION_ID_MISMATCH"
    if neg_risk is True:
        return None, "CLOB_NEG_RISK_TRUE"
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
        "clob_neg_risk_field_state": "ABSENT" if neg_risk is None else "FALSE",
    }, None


async def census_v051b_async(
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
    clob_neg_risk_states: Counter[str] = Counter()
    shares = float(contract["prefunded_inventory"]["complete_sets"])
    for market in selected:
        condition = urllib.parse.quote(str(market["condition_id"]), safe="")
        info = http_json("GET", f"{CLOB_BASE_URL}/clob-markets/{condition}", None)
        if not isinstance(info, Mapping):
            validation_rejections["CLOB_MARKET_INFO_INVALID"] += 1
            continue
        result, reason = validate_corrected_standard_clob_market(market, info, shares)
        if result is None:
            validation_rejections[str(reason)] += 1
        else:
            validated.append(result)
            clob_neg_risk_states[str(result["clob_neg_risk_field_state"])] += 1
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
        raise V051CensusError("Fuentes V0.51b sin mapas de libros")
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
                result["clob_neg_risk_field_state"] = market["clob_neg_risk_field_state"]
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
        "transport_correction": {
            "economic_rules_changed_from_v051": False,
            "clob_neg_risk_field_states": dict(sorted(clob_neg_risk_states.items())),
            "gamma_false_required_before_selection": True,
            "rest_false_required_for_both_tokens_before_evaluation": True,
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
            "prefunded_complete_set_removes_directional_residual_after_full_hedge": True,
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


def census_v051b(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(census_v051b_async(**kwargs))


__all__ = [
    "RESULT_SCHEMA",
    "census_v051b",
    "census_v051b_async",
    "validate_corrected_standard_clob_market",
]
