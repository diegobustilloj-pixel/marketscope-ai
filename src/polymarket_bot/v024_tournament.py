from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from polymarket_bot.phase4 import _taker_cost_per_share


FEE_RATE = 0.07
SLIPPAGE_PER_SHARE = 0.005
PAPER_SHARES = 5.0

FROZEN_STRATEGIES: tuple[dict[str, Any], ...] = (
    {
        "id": "agreement_cap_090",
        "require_model_agreement": True,
        "minimum_entry_cost": 0.50,
        "minimum_entry_cost_inclusive": False,
        "maximum_entry_cost": 0.90,
        "maximum_entry_cost_inclusive": True,
    },
    {
        "id": "favorite_band_060_080",
        "require_model_agreement": False,
        "minimum_entry_cost": 0.60,
        "minimum_entry_cost_inclusive": True,
        "maximum_entry_cost": 0.80,
        "maximum_entry_cost_inclusive": False,
    },
    {
        "id": "favorite_cap_090",
        "require_model_agreement": False,
        "minimum_entry_cost": 0.50,
        "minimum_entry_cost_inclusive": False,
        "maximum_entry_cost": 0.90,
        "maximum_entry_cost_inclusive": True,
    },
)


class V024TournamentError(ValueError):
    pass


def frozen_strategy_config() -> list[dict[str, Any]]:
    return [dict(item) for item in FROZEN_STRATEGIES]


def validate_strategy_config(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(FROZEN_STRATEGIES):
        raise V024TournamentError("V0.24 requiere exactamente tres estrategias")
    normalized: list[dict[str, Any]] = []
    expected_by_id = {item["id"]: item for item in FROZEN_STRATEGIES}
    for raw in value:
        if not isinstance(raw, dict) or raw.get("id") not in expected_by_id:
            raise V024TournamentError("Estrategia V0.24 desconocida")
        expected = expected_by_id[str(raw["id"])]
        if raw != expected:
            raise V024TournamentError(
                f"Parametros V0.24 incompatibles: {raw.get('id')}"
            )
        normalized.append(dict(raw))
    if {item["id"] for item in normalized} != set(expected_by_id):
        raise V024TournamentError("Estrategias V0.24 duplicadas o ausentes")
    return normalized


def market_record(
    *,
    feature_json: str | Mapping[str, Any],
    model_probability_up: float | None,
    label: str | None = None,
    market_start_ms: int | None = None,
    condition_id: str | None = None,
) -> dict[str, Any] | None:
    feature = (
        json.loads(feature_json) if isinstance(feature_json, str) else dict(feature_json)
    )
    implied = feature.get("implied_up_mid_probability")
    if implied is None:
        return None
    implied_value = float(implied)
    if implied_value == 0.5:
        return None
    favorite_side = "Up" if implied_value > 0.5 else "Down"
    ask_key = "up_best_ask" if favorite_side == "Up" else "down_best_ask"
    ask = feature.get(ask_key)
    if ask is None:
        return None
    entry_cost, fill_price, fee = _taker_cost_per_share(
        float(ask), fee_rate=FEE_RATE, slippage_per_share=SLIPPAGE_PER_SHARE
    )
    model_side: str | None
    if model_probability_up is None or float(model_probability_up) == 0.5:
        model_side = None
    else:
        model_side = "Up" if float(model_probability_up) > 0.5 else "Down"
    return {
        "condition_id": condition_id,
        "market_start_ms": market_start_ms,
        "label": label,
        "favorite_side": favorite_side,
        "model_side": model_side,
        "model_agrees": model_side == favorite_side,
        "implied_up_mid_probability": implied_value,
        "entry_cost": entry_cost,
        "fill_price": fill_price,
        "fee": fee,
    }


def strategy_matches(record: Mapping[str, Any], strategy: Mapping[str, Any]) -> bool:
    if strategy["require_model_agreement"] and not bool(record["model_agrees"]):
        return False
    cost = float(record["entry_cost"])
    minimum = float(strategy["minimum_entry_cost"])
    maximum = float(strategy["maximum_entry_cost"])
    minimum_ok = (
        cost >= minimum if strategy["minimum_entry_cost_inclusive"] else cost > minimum
    )
    maximum_ok = (
        cost <= maximum if strategy["maximum_entry_cost_inclusive"] else cost < maximum
    )
    return minimum_ok and maximum_ok


def eligible_strategy_ids(
    record: Mapping[str, Any], strategies: Sequence[Mapping[str, Any]]
) -> list[str]:
    return [
        str(strategy["id"])
        for strategy in strategies
        if strategy_matches(record, strategy)
    ]


__all__ = [
    "FEE_RATE",
    "FROZEN_STRATEGIES",
    "PAPER_SHARES",
    "SLIPPAGE_PER_SHARE",
    "V024TournamentError",
    "eligible_strategy_ids",
    "frozen_strategy_config",
    "market_record",
    "strategy_matches",
    "validate_strategy_config",
]
