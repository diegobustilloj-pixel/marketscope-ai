from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


MINIMUM_ENTRY_COST = 0.50
MAXIMUM_ENTRY_COST = 0.90
PAPER_SHARES = 5.0
PRIMARY_ARM_ID = "favorite_down_cap_090"

FROZEN_ARMS: tuple[dict[str, Any], ...] = (
    {
        "id": PRIMARY_ARM_ID,
        "role": "primary_candidate",
        "favorite_side": "Down",
        "minimum_entry_cost": MINIMUM_ENTRY_COST,
        "minimum_entry_cost_inclusive": False,
        "maximum_entry_cost": MAXIMUM_ENTRY_COST,
        "maximum_entry_cost_inclusive": True,
    },
    {
        "id": "favorite_up_cap_090_control",
        "role": "direction_control",
        "favorite_side": "Up",
        "minimum_entry_cost": MINIMUM_ENTRY_COST,
        "minimum_entry_cost_inclusive": False,
        "maximum_entry_cost": MAXIMUM_ENTRY_COST,
        "maximum_entry_cost_inclusive": True,
    },
    {
        "id": "favorite_all_cap_090_control",
        "role": "broad_control",
        "favorite_side": "Any",
        "minimum_entry_cost": MINIMUM_ENTRY_COST,
        "minimum_entry_cost_inclusive": False,
        "maximum_entry_cost": MAXIMUM_ENTRY_COST,
        "maximum_entry_cost_inclusive": True,
    },
)


class V025StrategyError(ValueError):
    pass


def frozen_arm_config() -> list[dict[str, Any]]:
    return [dict(item) for item in FROZEN_ARMS]


def validate_arm_config(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(FROZEN_ARMS):
        raise V025StrategyError("V0.25 requiere exactamente tres brazos")
    expected = {str(item["id"]): item for item in FROZEN_ARMS}
    normalized: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict) or str(raw.get("id")) not in expected:
            raise V025StrategyError("Brazo V0.25 desconocido")
        if raw != expected[str(raw["id"])]:
            raise V025StrategyError(
                f"Parametros V0.25 incompatibles: {raw.get('id')}"
            )
        normalized.append(dict(raw))
    if {str(item["id"]) for item in normalized} != set(expected):
        raise V025StrategyError("Brazos V0.25 duplicados o ausentes")
    return normalized


def arm_matches(record: Mapping[str, Any], arm: Mapping[str, Any]) -> bool:
    favorite_side = str(record.get("favorite_side"))
    required_side = str(arm["favorite_side"])
    if required_side != "Any" and favorite_side != required_side:
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
    return minimum_ok and maximum_ok


def eligible_arm_ids(
    record: Mapping[str, Any], arms: Sequence[Mapping[str, Any]]
) -> list[str]:
    return [str(arm["id"]) for arm in arms if arm_matches(record, arm)]


__all__ = [
    "FROZEN_ARMS",
    "MAXIMUM_ENTRY_COST",
    "MINIMUM_ENTRY_COST",
    "PAPER_SHARES",
    "PRIMARY_ARM_ID",
    "V025StrategyError",
    "arm_matches",
    "eligible_arm_ids",
    "frozen_arm_config",
    "validate_arm_config",
]
