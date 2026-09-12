from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


PRIMARY_ID = "favorite_up_low_vol_twap_abs_lt_5bps"
PARENT_CONTROL_ID = "favorite_up_low_vol_no_twap_distance_control"

FROZEN_ARMS: tuple[dict[str, Any], ...] = (
    {
        "id": PRIMARY_ID,
        "role": "single_primary_candidate",
        "selectable": True,
        "favorite_side": "Up",
        "minimum_entry_cost": 0.50,
        "minimum_entry_cost_inclusive": False,
        "maximum_entry_cost": 0.90,
        "maximum_entry_cost_inclusive": True,
        "maximum_volatility_regime_ratio": 0.75,
        "maximum_volatility_regime_ratio_inclusive": False,
        "maximum_abs_twap_distance_to_open_bps": 5.0,
        "maximum_abs_twap_distance_inclusive": False,
        "required_resolution_twap_window_s": 60,
    },
    {
        "id": PARENT_CONTROL_ID,
        "role": "non_selectable_parent_control",
        "selectable": False,
        "favorite_side": "Up",
        "minimum_entry_cost": 0.50,
        "minimum_entry_cost_inclusive": False,
        "maximum_entry_cost": 0.90,
        "maximum_entry_cost_inclusive": True,
        "maximum_volatility_regime_ratio": 0.75,
        "maximum_volatility_regime_ratio_inclusive": False,
        "maximum_abs_twap_distance_to_open_bps": None,
        "maximum_abs_twap_distance_inclusive": None,
        "required_resolution_twap_window_s": 60,
    },
)


class V028StrategyError(ValueError):
    pass


def frozen_arm_config() -> list[dict[str, Any]]:
    return [dict(item) for item in FROZEN_ARMS]


def validate_arm_config(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(FROZEN_ARMS):
        raise V028StrategyError("V0.28 requiere exactamente dos brazos")
    expected = {str(item["id"]): item for item in FROZEN_ARMS}
    normalized: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict) or str(raw.get("id")) not in expected:
            raise V028StrategyError("Brazo V0.28 desconocido")
        if raw != expected[str(raw["id"])]:
            raise V028StrategyError(f"Parametros V0.28 incompatibles: {raw.get('id')}")
        normalized.append(dict(raw))
    if {str(item["id"]) for item in normalized} != set(expected):
        raise V028StrategyError("Brazos V0.28 duplicados o ausentes")
    return normalized


def arm_matches(record: Mapping[str, Any], arm: Mapping[str, Any]) -> bool:
    if str(record.get("favorite_side")) != str(arm["favorite_side"]):
        return False
    if record.get("resolution_twap_window_s") is None or int(
        record["resolution_twap_window_s"]
    ) != int(arm["required_resolution_twap_window_s"]):
        return False
    cost = record.get("entry_cost")
    volatility = record.get("volatility_regime_ratio")
    if cost is None or volatility is None:
        return False
    cost_value = float(cost)
    minimum = float(arm["minimum_entry_cost"])
    maximum = float(arm["maximum_entry_cost"])
    minimum_ok = (
        cost_value >= minimum
        if arm["minimum_entry_cost_inclusive"]
        else cost_value > minimum
    )
    maximum_ok = (
        cost_value <= maximum
        if arm["maximum_entry_cost_inclusive"]
        else cost_value < maximum
    )
    if not minimum_ok or not maximum_ok:
        return False
    volatility_cap = float(arm["maximum_volatility_regime_ratio"])
    volatility_ok = (
        float(volatility) <= volatility_cap
        if arm["maximum_volatility_regime_ratio_inclusive"]
        else float(volatility) < volatility_cap
    )
    if not volatility_ok:
        return False
    distance_cap = arm.get("maximum_abs_twap_distance_to_open_bps")
    if distance_cap is None:
        return True
    distance = record.get("twap_distance_to_open_bps")
    if distance is None:
        return False
    absolute_distance = abs(float(distance))
    return (
        absolute_distance <= float(distance_cap)
        if arm["maximum_abs_twap_distance_inclusive"]
        else absolute_distance < float(distance_cap)
    )


def eligible_arm_ids(
    record: Mapping[str, Any], arms: Sequence[Mapping[str, Any]]
) -> list[str]:
    return [str(arm["id"]) for arm in arms if arm_matches(record, arm)]


__all__ = [
    "FROZEN_ARMS",
    "PARENT_CONTROL_ID",
    "PRIMARY_ID",
    "V028StrategyError",
    "arm_matches",
    "eligible_arm_ids",
    "frozen_arm_config",
    "validate_arm_config",
]
