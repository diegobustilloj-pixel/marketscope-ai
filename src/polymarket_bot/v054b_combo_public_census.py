from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v045_census import _http_json
from polymarket_bot.v054_combo_public_census import (
    HttpJson,
    classify_census,
    collect_combo_catalog,
)
from polymarket_bot.v054b_contract import VARIANT, load_and_verify_preregistration


RESULT_SCHEMA = "result_v054b_public_combo_binary_labels_observability_census_1"


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def normalize_combo_market(
    raw: Mapping[str, Any], contract: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    for field in contract["market_schema"]["required_fields"]:
        if field not in raw:
            return None, f"REQUIRED_FIELD_MISSING_{str(field).upper()}"
    market_id = str(raw.get("id") or "").strip()
    condition_id = str(raw.get("condition_id") or "").strip().lower()
    if not market_id:
        return None, "MARKET_ID_INVALID"
    if re.fullmatch(r"0x[0-9a-f]{64}", condition_id) is None:
        return None, "CONDITION_ID_INVALID"
    position_ids_raw = raw.get("position_ids")
    if (
        not isinstance(position_ids_raw, Sequence)
        or isinstance(position_ids_raw, (str, bytes))
        or len(position_ids_raw) != 2
    ):
        return None, "POSITION_IDS_INVALID"
    position_ids = [str(value).strip() for value in position_ids_raw]
    if any(not value.isdigit() for value in position_ids) or len(set(position_ids)) != 2:
        return None, "POSITION_IDS_INVALID"
    outcomes_raw = raw.get("outcomes")
    if (
        not isinstance(outcomes_raw, Sequence)
        or isinstance(outcomes_raw, (str, bytes))
        or len(outcomes_raw) != 2
    ):
        return None, "OUTCOME_LABELS_INVALID"
    outcomes = [str(value).strip() for value in outcomes_raw]
    if any(not value for value in outcomes) or len(set(outcomes)) != 2:
        return None, "OUTCOME_LABELS_INVALID"
    prices_raw = raw.get("outcome_prices")
    if (
        not isinstance(prices_raw, Sequence)
        or isinstance(prices_raw, (str, bytes))
        or len(prices_raw) != 2
    ):
        return None, "OUTCOME_PRICES_INVALID"
    prices = [_finite_float(value) for value in prices_raw]
    if any(value is None or value < 0.0 or value > 1.0 for value in prices):
        return None, "OUTCOME_PRICES_INVALID"
    slug = str(raw.get("slug") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not slug or not title:
        return None, "IDENTITY_TEXT_INVALID"
    volume = _finite_float(raw.get("volume"))
    if volume is None or volume < 0.0:
        return None, "VOLUME_INVALID"
    tags_raw = raw.get("tags")
    if not isinstance(tags_raw, Sequence) or isinstance(tags_raw, (str, bytes)):
        return None, "TAGS_INVALID"
    tags = [str(value).strip() for value in tags_raw]
    if any(not value for value in tags):
        return None, "TAGS_INVALID"
    forbidden = sorted(
        set(raw).intersection(contract["economic_observability"]["forbidden_catalog_quote_fields"])
    )
    return {
        "id": market_id,
        "condition_id": condition_id,
        "position_ids": position_ids,
        "slug": slug,
        "title": title,
        "outcomes": outcomes,
        "outcome_prices": [float(value) for value in prices],
        "outcome_price_sum": float(sum(value for value in prices if value is not None)),
        "volume": volume,
        "tags": tags,
        "unexpected_executable_quote_fields": forbidden,
    }, None


def census_v054b(
    *,
    prereg_path: str | Path,
    result_path: str | Path,
    project_root: str | Path = ROOT,
    http_json: HttpJson = _http_json,
) -> dict[str, Any]:
    started = time.monotonic()
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    result_file = Path(result_path).resolve()
    prereg = load_and_verify_preregistration(prereg_file, project_root=root)
    contract = prereg["contract"]
    records, transport = collect_combo_catalog(http_json, contract)
    rejections: Counter[str] = Counter()
    valid: list[dict[str, Any]] = []
    seen_conditions: set[str] = set()
    for raw in records:
        normalized, reason = normalize_combo_market(raw, contract)
        if normalized is None:
            rejections[str(reason)] += 1
            continue
        condition_id = normalized["condition_id"]
        if condition_id in seen_conditions:
            rejections["DUPLICATE_CONDITION_ID"] += 1
            continue
        seen_conditions.add(condition_id)
        valid.append(normalized)
    quote_schema_records = sum(
        bool(record["unexpected_executable_quote_fields"]) for record in valid
    )
    classification = classify_census(
        received=len(records),
        valid=len(valid),
        quote_schema_records=quote_schema_records,
        contract=contract,
    )
    price_sums = [record["outcome_price_sum"] for record in valid]
    label_shapes = Counter(
        "YES_NO" if record["outcomes"] == ["Yes", "No"] else "OTHER_BINARY_LABELS"
        for record in valid
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
            "verified_before_network_call": True,
        },
        "catalog": {
            **transport,
            "valid_markets": len(valid),
            "invalid_or_duplicate_markets": len(records) - len(valid),
            "validation_rejections": dict(sorted(rejections.items())),
            "outcome_label_classes": dict(sorted(label_shapes.items())),
            "markets_with_unexpected_executable_quote_fields": quote_schema_records,
            "unique_condition_ids": len(seen_conditions),
            "unique_position_ids": len({token for market in valid for token in market["position_ids"]}),
            "tag_counts": dict(sorted(Counter(tag for market in valid for tag in market["tags"]).items())),
            "indicative_leg_price_sum": {
                "minimum": min(price_sums) if price_sums else None,
                "maximum": max(price_sums) if price_sums else None,
                "mean": sum(price_sums) / len(price_sums) if price_sums else None,
            },
            "markets": valid,
        },
        "observability": {
            **classification,
            "public_catalog_observed": bool(records),
            "executable_combo_bid_or_ask_observed": quote_schema_records > 0,
            "active_rfq_request_observed": False,
            "rfq_trade_observed": False,
            "profitability_measured": False,
            "pnl_measured": False,
            "candidate_count": None,
            "elapsed_seconds": time.monotonic() - started,
        },
        "interpretation": {
            "native_combo_mechanism_exists": bool(valid),
            "arbitrary_binary_outcome_labels_preserved": True,
            "outcome_prices_are_indicative_leg_prices": True,
            "leg_price_sum_is_not_combo_edge": True,
            "authenticated_quoter_gateway_required_for_live_rfq_requests": True,
            "signed_order_required_to_submit_maker_quote": True,
            "no_economic_inference_from_unobservable_quote": True,
            "automatic_followup_launched": False,
            "next_step": classification["next_step"],
        },
        "safety": {
            "network_calls": "PUBLIC_COMBO_CATALOG_GET_ONLY",
            "orders_created": 0,
            "paper_orders": 0,
            "transactions_created": 0,
            "wallet_required": False,
            "authentication_used": False,
            "websocket_used": False,
            "real_money": "BLOQUEADO",
        },
    }
    _write_atomic(result_file, payload)
    return payload


__all__ = ["RESULT_SCHEMA", "census_v054b", "normalize_combo_market"]
