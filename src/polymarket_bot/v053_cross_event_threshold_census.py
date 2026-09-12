from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import urllib.parse
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v045_census import CLOB_BASE_URL, _http_json
from polymarket_bot.v047_ws_census import WsCollector, collect_initial_books
from polymarket_bot.v051_prefunded_maker_census import (
    HttpJson,
    _timestamp,
    collect_rest_metadata,
    normalize_standard_market,
)
from polymarket_bot.v051b_prefunded_maker_census import (
    validate_corrected_standard_clob_market,
)
from polymarket_bot.v052_linked_threshold_census import (
    PRICE_FIELDS,
    evaluate_relationship,
)
from polymarket_bot.v053_contract import (
    SEMANTIC_SCHEMA,
    VARIANT,
    load_and_verify_discovery_preregistration,
    load_and_verify_economic_lock,
)


RESULT_SCHEMA = "result_v053_cross_event_threshold_taker_floor_census_1"


class V053CensusError(RuntimeError):
    pass


_NUMBER_PATTERN = (
    r"(?:US\$|\$|€|£)?\s*-?"
    r"(?:\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:\.\d+)?"
    r"(?:\s*[kKmMbB](?![A-Za-z]))?"
    r"(?:\s*(?:%|°[CFcf]|[A-Z]{3}(?![A-Za-z])))?"
)
_NUMBER_FULL = re.compile(rf"^\s*(?P<token>{_NUMBER_PATTERN})\s*$")
_NUMBER_FIND = re.compile(_NUMBER_PATTERN)


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _parse_numeric_token(value: Any) -> tuple[float, str] | None:
    text = str(value or "").strip()
    match = _NUMBER_FULL.fullmatch(text)
    if match is None:
        return None
    token = match.group("token").replace(" ", "")
    currency = ""
    for prefix in ("US$", "$", "€", "£"):
        if token.upper().startswith(prefix.upper()):
            currency = prefix.upper()
            token = token[len(prefix) :]
            break
    unit = ""
    unit_match = re.search(r"(%|°[CFcf]|[A-Za-z]{3})$", token)
    if unit_match is not None:
        unit = unit_match.group(1).upper()
        token = token[: unit_match.start()]
    multiplier = 1.0
    if token and token[-1:].lower() in {"k", "m", "b"}:
        multiplier = {"k": 1e3, "m": 1e6, "b": 1e9}[token[-1].lower()]
        token = token[:-1]
    try:
        number = float(token.replace(",", "")) * multiplier
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return number, f"{currency}|{unit}"


def parse_cross_event_threshold_question(
    question: str, comparator_phrases: Sequence[str]
) -> dict[str, Any] | None:
    text = " ".join(str(question or "").strip().split())
    if not text.endswith("?") or not text.lower().startswith("will "):
        return None
    for phrase in comparator_phrases:
        pattern = re.compile(
            rf"^(?P<prefix>will .+? {re.escape(phrase)} )"
            rf"(?P<threshold>{_NUMBER_PATTERN})"
            rf"(?P<suffix>(?: .+)?)\?$",
            flags=re.IGNORECASE,
        )
        match = pattern.fullmatch(text)
        if match is None:
            continue
        parsed = _parse_numeric_token(match.group("threshold"))
        if parsed is None:
            return None
        prefix = _normalize_text(match.group("prefix"))
        suffix = _normalize_text(match.group("suffix"))
        return {
            "comparator": phrase,
            "threshold": parsed[0],
            "unit_signature": parsed[1],
            "threshold_text": match.group("threshold").strip(),
            "question_skeleton": (
                f"{prefix} <threshold>{f' {suffix}' if suffix else ''}?"
            ),
        }
    return None


def cross_event_description_skeleton(
    description: str, *, threshold: float
) -> tuple[str, int] | None:
    text = " ".join(str(description or "").strip().split())
    if not text:
        return None
    parts: list[str] = []
    cursor = 0
    replacements = 0
    for match in _NUMBER_FIND.finditer(text):
        parsed = _parse_numeric_token(match.group(0))
        if parsed is None or not math.isclose(
            parsed[0], threshold, rel_tol=0.0, abs_tol=1e-9
        ):
            continue
        parts.append(text[cursor : match.start()])
        parts.append("<threshold>")
        cursor = match.end()
        replacements += 1
    if replacements == 0:
        return None
    parts.append(text[cursor:])
    return _normalize_text("".join(parts)), replacements


def normalize_cross_event_market(
    event: Mapping[str, Any], market: Mapping[str, Any], contract: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    base, reason = normalize_standard_market(event, market, contract)
    if base is None:
        return None, reason
    semantic = contract["semantic_discovery"]
    parsed = parse_cross_event_threshold_question(
        str(market.get("question") or ""),
        semantic["allowed_exact_comparator_phrases"],
    )
    if parsed is None:
        return None, "QUESTION_TEMPLATE_NOT_EXACT"
    description = str(market.get("description") or "").strip()
    description_result = cross_event_description_skeleton(
        description, threshold=float(parsed["threshold"])
    )
    if description_result is None:
        return None, "DESCRIPTION_THRESHOLD_NOT_PROVEN"
    resolution_source = str(
        market.get("resolutionSource") or event.get("resolutionSource") or ""
    ).strip()
    if not resolution_source:
        return None, "RESOLUTION_SOURCE_MISSING"
    end_date = str(market.get("endDate") or event.get("endDate") or "").strip()
    if not end_date:
        return None, "END_DATE_MISSING"
    if not str(base.get("event_id") or ""):
        return None, "EVENT_ID_MISSING"
    return {
        **base,
        "market_group": "CROSS_EVENT_EXACT_RULE",
        "group_item_title": str(market.get("groupItemTitle") or ""),
        "threshold": float(parsed["threshold"]),
        "question_threshold_text": parsed["threshold_text"],
        "comparator": parsed["comparator"],
        "unit_signature": parsed["unit_signature"],
        "question_skeleton": parsed["question_skeleton"],
        "description": description,
        "description_skeleton": description_result[0],
        "description_threshold_mentions": description_result[1],
        "resolution_source": resolution_source,
        "end_date": end_date,
        "market_type": str(market.get("marketType") or ""),
        "format_type": str(market.get("formatType") or ""),
    }, None


def _semantic_group_key(market: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        str(market["comparator"]),
        str(market["unit_signature"]),
        str(market["question_skeleton"]),
        str(market["description_skeleton"]),
        _normalize_text(market["resolution_source"]),
        str(market["end_date"]),
        str(market["market_type"]),
        str(market["format_type"]),
    )


def _frozen_market(market: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "event_id",
        "event_slug",
        "event_title",
        "market_id",
        "condition_id",
        "slug",
        "question",
        "yes_token_id",
        "no_token_id",
        "gamma_fee_rate",
        "gamma_fee_exponent",
        "market_group",
        "group_item_title",
        "threshold",
        "question_threshold_text",
        "comparator",
        "unit_signature",
        "question_skeleton",
        "description",
        "description_skeleton",
        "description_threshold_mentions",
        "resolution_source",
        "end_date",
        "market_type",
        "format_type",
    )
    return {field: market[field] for field in fields}


def build_cross_event_relationships(
    raw_events: Sequence[Any], contract: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rejections: Counter[str] = Counter()
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    markets_received = 0
    eligible_markets = 0
    for event_position, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, Mapping):
            rejections["EVENT_INVALID"] += 1
            continue
        markets = raw_event.get("markets")
        if not isinstance(markets, list):
            rejections["MARKETS_MISSING"] += 1
            continue
        for market_position, raw_market in enumerate(markets):
            markets_received += 1
            if not isinstance(raw_market, Mapping):
                rejections["MARKET_INVALID"] += 1
                continue
            normalized, reason = normalize_cross_event_market(
                raw_event, raw_market, contract
            )
            if normalized is None:
                rejections[str(reason)] += 1
                continue
            normalized["_event_position"] = event_position
            normalized["_market_position"] = market_position
            groups.setdefault(_semantic_group_key(normalized), []).append(normalized)
            eligible_markets += 1
    relationships: list[dict[str, Any]] = []
    duplicate_groups = 0
    same_event_pairs_excluded = 0
    for markets in groups.values():
        by_threshold: dict[float, list[dict[str, Any]]] = {}
        for market in markets:
            by_threshold.setdefault(float(market["threshold"]), []).append(market)
        if any(len(items) != 1 for items in by_threshold.values()):
            duplicate_groups += 1
            rejections["SEMANTIC_GROUP_DUPLICATE_THRESHOLD"] += len(markets)
            continue
        ordered = [by_threshold[value][0] for value in sorted(by_threshold)]
        if len(ordered) < 2:
            rejections["SEMANTIC_GROUP_SINGLETON"] += len(ordered)
            continue
        for lower, upper in zip(ordered, ordered[1:]):
            if str(lower["event_id"]) == str(upper["event_id"]):
                same_event_pairs_excluded += 1
                rejections["SAME_EVENT_RELATION_EXCLUDED"] += 1
                continue
            canonical = {
                "lower_event_id": lower["event_id"],
                "upper_event_id": upper["event_id"],
                "lower_condition_id": lower["condition_id"],
                "upper_condition_id": upper["condition_id"],
                "lower_threshold": lower["threshold"],
                "upper_threshold": upper["threshold"],
                "question_skeleton": lower["question_skeleton"],
                "description_skeleton": lower["description_skeleton"],
                "resolution_source": lower["resolution_source"],
                "end_date": lower["end_date"],
            }
            digest = hashlib.sha256(
                json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            relationships.append(
                {
                    "relationship_id": digest[:20],
                    "logical_relation": "CROSS_EVENT_UPPER_YES_IMPLIES_LOWER_YES",
                    "portfolio": ["BUY_LOWER_YES", "BUY_UPPER_NO"],
                    "payout_floor_per_bundle_share": 1.0,
                    "lower": _frozen_market(lower),
                    "upper": _frozen_market(upper),
                }
            )
    maximum = int(contract["semantic_discovery"]["maximum_adjacent_relationships"])
    relationships = relationships[:maximum]
    return relationships, {
        "events_received": len(raw_events),
        "markets_received": markets_received,
        "semantic_markets_eligible": eligible_markets,
        "semantic_groups": len(groups),
        "duplicate_threshold_groups_rejected": duplicate_groups,
        "same_event_pairs_excluded": same_event_pairs_excluded,
        "adjacent_cross_event_relationships_selected": len(relationships),
        "filter_rejections": dict(sorted(rejections.items())),
    }


def collect_keyset_events(
    http_json: HttpJson, contract: Mapping[str, Any]
) -> tuple[list[Any], dict[str, Any]]:
    semantic = contract["semantic_discovery"]
    query_values = semantic["query"]
    endpoint = str(semantic["endpoint"])
    maximum_pages = int(semantic["maximum_pages"])
    maximum_events = int(semantic["maximum_events"])
    events: list[Any] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages: list[dict[str, Any]] = []
    pagination_complete = False
    for page_index in range(maximum_pages):
        parameters: dict[str, Any] = {
            "closed": str(query_values["closed"]).lower(),
            "limit": query_values["limit"],
            "order": query_values["order"],
            "ascending": str(query_values["ascending"]).lower(),
        }
        if cursor is not None:
            parameters["after_cursor"] = cursor
        url = f"{endpoint}?{urllib.parse.urlencode(parameters)}"
        response = http_json("GET", url, None)
        if not isinstance(response, Mapping) or not isinstance(response.get("events"), list):
            raise V053CensusError("Gamma keyset no devolvio events y next_cursor")
        page_events = response["events"]
        room = maximum_events - len(events)
        events.extend(page_events[:room])
        next_cursor_raw = response.get("next_cursor")
        next_cursor = str(next_cursor_raw).strip() if next_cursor_raw is not None else ""
        pages.append(
            {
                "page_index": page_index,
                "events_received": len(page_events),
                "events_retained": min(len(page_events), room),
                "next_cursor_present": bool(next_cursor),
            }
        )
        if not next_cursor:
            pagination_complete = True
            break
        if next_cursor in seen_cursors or next_cursor == cursor:
            raise V053CensusError("Gamma keyset repitio cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        if len(events) >= maximum_events:
            break
    return events, {
        "pages_requested": len(pages),
        "pages": pages,
        "pagination_complete": pagination_complete,
        "capped_by_maximum_events_or_pages": not pagination_complete,
        "cursor_values_persisted": False,
    }


def discover_cross_event_relationships(
    *,
    prereg_path: str | Path,
    output_path: str | Path,
    project_root: str | Path = ROOT,
    http_json: HttpJson = _http_json,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    output_file = Path(output_path).resolve()
    prereg = load_and_verify_discovery_preregistration(prereg_file, project_root=root)
    if output_file.is_file():
        return json.loads(output_file.read_text(encoding="utf-8"))
    raw_events, pagination = collect_keyset_events(http_json, prereg["contract"])
    relationships, diagnostics = build_cross_event_relationships(
        raw_events, prereg["contract"]
    )
    try:
        prereg_reference = str(prereg_file.relative_to(root)).replace("\\", "/")
    except ValueError:
        prereg_reference = str(prereg_file)
    canonical = json.dumps(
        relationships, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload = {
        "schema": SEMANTIC_SCHEMA,
        "variant": VARIANT,
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "FROZEN_BEFORE_ANY_CLOB_CALL",
        "preregistration": {
            "relative_path": prereg_reference,
            "sha256": sha256_file(prereg_file),
            "verified_before_gamma_discovery": True,
        },
        "pagination": pagination,
        "discovery": {
            **diagnostics,
            "endpoint_class": "PUBLIC_GAMMA_KEYSET_METADATA_ONLY",
            "price_fields_ignored": sorted(PRICE_FIELDS),
            "prices_used_for_filtering_or_selection": False,
            "clob_calls_made": 0,
            "relationship_payload_sha256": hashlib.sha256(canonical).hexdigest(),
        },
        "logical_contract": prereg["contract"]["logical_contract"],
        "relationships": relationships,
        "safety": {
            "network_calls": "PUBLIC_READ_ONLY_GAMMA_KEYSET_METADATA_ONLY",
            "orders_created": 0,
            "paper_orders": 0,
            "transactions_created": 0,
            "wallet_required": False,
            "authentication_used": False,
            "real_money": "BLOQUEADO",
        },
    }
    _write_atomic(output_file, payload)
    return payload


def classify_census(
    *,
    selected_relationships: int,
    evaluated_relationships: int,
    candidates: int,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    if selected_relationships == 0:
        return {
            "verdict": "CLOSE_STANDARD_THRESHOLD_RELATION_FAMILY_NO_SEMANTIC_PAIRS",
            "next_step": "DO_NOT_RELAX_THRESHOLD_SEMANTICS_OR_ACCESS_CLOB_POSTHOC",
            "economic_conclusion_allowed": False,
            "required_evaluated_relationships": 0,
            "coverage": 0.0,
        }
    gate = contract["evidence_gate"]
    required = max(
        int(gate["minimum_evaluated_relationships"]),
        math.ceil(float(gate["minimum_evaluated_fraction_of_selected"]) * selected_relationships),
    )
    coverage = evaluated_relationships / selected_relationships
    if evaluated_relationships < required:
        return {
            "verdict": "FAIL_CENSUS_INSUFFICIENT_CROSS_EVENT_THRESHOLD_BOOKS",
            "next_step": "RESOLVE_FROZEN_CROSS_EVENT_BOOK_COVERAGE_WITHOUT_CHANGING_SEMANTICS",
            "economic_conclusion_allowed": False,
            "required_evaluated_relationships": required,
            "coverage": coverage,
        }
    if candidates:
        return {
            "verdict": "REQUIRE_MANUAL_RULE_REVIEW_AND_FRESH_PERSISTENCE_OBSERVER",
            "next_step": "MANUALLY_VERIFY_EACH_CROSS_EVENT_RULE_THEN_PREREGISTER_NON_ATOMIC_PERSISTENCE_RISK",
            "economic_conclusion_allowed": False,
            "required_evaluated_relationships": required,
            "coverage": coverage,
        }
    return {
        "verdict": "REJECT_CROSS_EVENT_THRESHOLD_TAKER_FLOOR_ARBITRAGE_SNAPSHOT",
        "next_step": "DO_NOT_LAUNCH_OBSERVER_FOR_THIS_FROZEN_CROSS_EVENT_SAMPLE",
        "economic_conclusion_allowed": True,
        "required_evaluated_relationships": required,
        "coverage": coverage,
    }


async def census_v053_async(
    *,
    economic_lock_path: str | Path,
    result_path: str | Path,
    project_root: str | Path = ROOT,
    http_json: HttpJson = _http_json,
    ws_collector: WsCollector = collect_initial_books,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    lock_file = Path(economic_lock_path).resolve()
    result_file = Path(result_path).resolve()
    lock, semantic = load_and_verify_economic_lock(lock_file, project_root=root)
    if result_file.is_file():
        return json.loads(result_file.read_text(encoding="utf-8"))
    contract = lock["contract"]
    relationships = semantic.get("relationships", [])
    if not isinstance(relationships, list):
        raise V053CensusError("Relaciones V0.53 invalidas")
    started = time.monotonic()
    unique_markets: dict[str, dict[str, Any]] = {}
    for relationship in relationships:
        if not isinstance(relationship, Mapping):
            continue
        for side in ("lower", "upper"):
            market = relationship.get(side)
            if isinstance(market, Mapping):
                unique_markets.setdefault(str(market.get("condition_id") or ""), dict(market))
    validated: dict[str, dict[str, Any]] = {}
    validation_rejections: Counter[str] = Counter()
    shares = float(contract["hypothetical_execution"]["shares_per_leg"])
    for condition, market in unique_markets.items():
        escaped = urllib.parse.quote(condition, safe="")
        info = http_json("GET", f"{CLOB_BASE_URL}/clob-markets/{escaped}", None)
        if not isinstance(info, Mapping):
            validation_rejections["CLOB_MARKET_INFO_INVALID"] += 1
            continue
        checked, reason = validate_corrected_standard_clob_market(market, info, shares)
        if checked is None:
            validation_rejections[str(reason)] += 1
            continue
        validated[condition] = checked
    validated_markets = list(validated.values())
    if validated_markets:
        rest_metadata = collect_rest_metadata(validated_markets, http_json)
        tokens = sorted(
            {
                str(token)
                for market in validated_markets
                for token in (market["yes_token_id"], market["no_token_id"])
            }
        )
        websocket = await ws_collector(str(contract["transport"]["endpoint"]), tokens, contract)
    else:
        now_ms = time.time_ns() // 1_000_000
        tokens = []
        rest_metadata = {
            "request_started_ms": now_ms,
            "response_received_ms": now_ms,
            "request_elapsed_ms": 0,
            "expected_tokens": 0,
            "received_tokens": 0,
            "missing_tokens": [],
            "records": {},
        }
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
    rest_received = _timestamp(rest_metadata.get("response_received_ms"))
    rest_to_ws_ms = (
        connected_at - rest_received
        if connected_at is not None and rest_received is not None
        else None
    )
    records = rest_metadata.get("records", {})
    books = websocket.get("books", {})
    if not isinstance(records, Mapping) or not isinstance(books, Mapping):
        raise V053CensusError("Fuentes V0.53 sin mapas de libros")
    evaluated: list[dict[str, Any]] = []
    evaluation_rejections: Counter[str] = Counter()
    if relationships and (
        rest_to_ws_ms is None
        or rest_to_ws_ms < 0
        or rest_to_ws_ms > int(contract["metadata"]["maximum_rest_response_to_ws_connect_ms"])
    ):
        evaluation_rejections["REST_METADATA_TO_WS_WINDOW_EXCEEDED"] = len(relationships)
    else:
        for relationship in relationships:
            if not isinstance(relationship, Mapping):
                evaluation_rejections["RELATIONSHIP_INVALID"] += 1
                continue
            result, reason = evaluate_relationship(
                relationship, validated, records, books, contract
            )
            if result is None:
                evaluation_rejections[str(reason)] += 1
            else:
                evaluated.append(result)
    evaluated.sort(
        key=lambda item: float(item["conservative_edge_per_bundle_share_before_gas"]),
        reverse=True,
    )
    candidates = sum(bool(item["cost_candidate_before_gas"]) for item in evaluated)
    classification = classify_census(
        selected_relationships=len(relationships),
        evaluated_relationships=len(evaluated),
        candidates=candidates,
        contract=contract,
    )
    try:
        lock_reference = str(lock_file.relative_to(root)).replace("\\", "/")
    except ValueError:
        lock_reference = str(lock_file)
    payload = {
        "schema": RESULT_SCHEMA,
        "variant": VARIANT,
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": classification["verdict"],
        "economic_lock": {
            "relative_path": lock_reference,
            "sha256": sha256_file(lock_file),
            "verified_before_any_clob_call": True,
            "semantic_relationships_sha256": lock["semantic_relationships"]["sha256"],
        },
        "sample": {
            "semantic_relationships_frozen": len(relationships),
            "unique_markets": len(unique_markets),
            "markets_clob_validated": len(validated),
            "tokens_subscribed": len(tokens),
            "validation_rejections": dict(sorted(validation_rejections.items())),
        },
        "rest_metadata": {key: value for key, value in rest_metadata.items() if key != "records"},
        "websocket": {key: value for key, value in websocket.items() if key != "books"},
        "census": {
            **classification,
            "metadata_response_to_ws_connect_ms": rest_to_ws_ms,
            "relationships_evaluated": len(evaluated),
            "relationships_non_executable": len(relationships) - len(evaluated),
            "cost_candidates_before_gas": candidates,
            "evaluation_rejections": dict(sorted(evaluation_rejections.items())),
            "elapsed_seconds": time.monotonic() - started,
            "relationships": evaluated,
        },
        "interpretation": {
            "different_event_ids_required_and_verified_semantically": True,
            "semantic_relationships_selected_before_clob_prices": True,
            "guaranteed_payout_floor_assuming_frozen_rules_are_correct": True,
            "both_legs_taker_and_fees_included": True,
            "atomic_multileg_execution_available": False,
            "manual_rule_review_completed": False,
            "gas_included": False,
            "nonpositive_edge_cannot_be_rescued_by_nonnegative_gas": True,
            "performance_validation": False,
            "automatic_followup_launched": False,
            "next_step": classification["next_step"],
        },
        "safety": {
            "network_calls": (
                "NONE_AFTER_SEMANTIC_ZERO_RELATION_BRANCH"
                if not relationships
                else "PUBLIC_READ_ONLY_CLOB_REST_AND_MARKET_WEBSOCKET"
            ),
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


def census_v053(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(census_v053_async(**kwargs))


__all__ = [
    "RESULT_SCHEMA",
    "V053CensusError",
    "build_cross_event_relationships",
    "census_v053",
    "census_v053_async",
    "classify_census",
    "collect_keyset_events",
    "discover_cross_event_relationships",
    "normalize_cross_event_market",
    "parse_cross_event_threshold_question",
    "cross_event_description_skeleton",
]
