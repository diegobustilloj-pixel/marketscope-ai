from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


UP_LOW_VOL_ID = "favorite_up_low_vol_lt_075"
DOWN_MID_COST_ID = "favorite_down_cost_070_080"
UP_BROAD_CONTROL_ID = "favorite_up_cost_050_090_control"
DOWN_BROAD_CONTROL_ID = "favorite_down_cost_050_090_control"

FROZEN_ARMS: tuple[dict[str, Any], ...] = (
    {
        "id": UP_LOW_VOL_ID,
        "role": "co_primary_candidate",
        "selectable": True,
        "favorite_side": "Up",
        "minimum_entry_cost": 0.50,
        "minimum_entry_cost_inclusive": False,
        "maximum_entry_cost": 0.90,
        "maximum_entry_cost_inclusive": True,
        "maximum_volatility_regime_ratio": 0.75,
        "maximum_volatility_regime_ratio_inclusive": False,
    },
    {
        "id": DOWN_MID_COST_ID,
        "role": "co_primary_candidate",
        "selectable": True,
        "favorite_side": "Down",
        "minimum_entry_cost": 0.70,
        "minimum_entry_cost_inclusive": True,
        "maximum_entry_cost": 0.80,
        "maximum_entry_cost_inclusive": False,
        "maximum_volatility_regime_ratio": None,
        "maximum_volatility_regime_ratio_inclusive": None,
    },
    {
        "id": UP_BROAD_CONTROL_ID,
        "role": "non_selectable_parent_control",
        "selectable": False,
        "favorite_side": "Up",
        "minimum_entry_cost": 0.50,
        "minimum_entry_cost_inclusive": False,
        "maximum_entry_cost": 0.90,
        "maximum_entry_cost_inclusive": True,
        "maximum_volatility_regime_ratio": None,
        "maximum_volatility_regime_ratio_inclusive": None,
    },
    {
        "id": DOWN_BROAD_CONTROL_ID,
        "role": "non_selectable_parent_control",
        "selectable": False,
        "favorite_side": "Down",
        "minimum_entry_cost": 0.50,
        "minimum_entry_cost_inclusive": False,
        "maximum_entry_cost": 0.90,
        "maximum_entry_cost_inclusive": True,
        "maximum_volatility_regime_ratio": None,
        "maximum_volatility_regime_ratio_inclusive": None,
    },
)


class V027StrategyError(ValueError):
    pass


def frozen_arm_config() -> list[dict[str, Any]]:
    return [dict(item) for item in FROZEN_ARMS]


def validate_arm_config(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(FROZEN_ARMS):
        raise V027StrategyError("V0.27 requiere exactamente cuatro brazos")
    expected = {str(item["id"]): item for item in FROZEN_ARMS}
    normalized: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict) or str(raw.get("id")) not in expected:
            raise V027StrategyError("Brazo V0.27 desconocido")
        if raw != expected[str(raw["id"])]:
            raise V027StrategyError(f"Parametros V0.27 incompatibles: {raw.get('id')}")
        normalized.append(dict(raw))
    if {str(item["id"]) for item in normalized} != set(expected):
        raise V027StrategyError("Brazos V0.27 duplicados o ausentes")
    return normalized


def arm_matches(record: Mapping[str, Any], arm: Mapping[str, Any]) -> bool:
    if str(record.get("favorite_side")) != str(arm["favorite_side"]):
        return False
    cost = float(record["entry_cost"])
    minimum = float(arm["minimum_entry_cost"])
    maximum = float(arm["maximum_entry_cost"])
    minimum_ok = (
        cost >= minimum if arm["minimum_entry_cost_inclusive"] else cost > minimum
    )
    maximum_ok = (
        cost <= maximum if arm["maximum_entry_cost_inclusive"] else cost < maximum
    )
    if not minimum_ok or not maximum_ok:
        return False
    volatility_cap = arm.get("maximum_volatility_regime_ratio")
    if volatility_cap is None:
        return True
    volatility = record.get("volatility_regime_ratio")
    if volatility is None:
        return False
    return (
        float(volatility) <= float(volatility_cap)
        if arm["maximum_volatility_regime_ratio_inclusive"]
        else float(volatility) < float(volatility_cap)
    )


def eligible_arm_ids(
    record: Mapping[str, Any], arms: Sequence[Mapping[str, Any]]
) -> list[str]:
    return [str(arm["id"]) for arm in arms if arm_matches(record, arm)]


__all__ = [
    "DOWN_BROAD_CONTROL_ID",
    "DOWN_MID_COST_ID",
    "FROZEN_ARMS",
    "UP_BROAD_CONTROL_ID",
    "UP_LOW_VOL_ID",
    "V027StrategyError",
    "arm_matches",
    "eligible_arm_ids",
    "frozen_arm_config",
    "validate_arm_config",
]
