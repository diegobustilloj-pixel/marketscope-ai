import unittest

from polymarket_bot.v026_audit import evaluate_v026
from polymarket_bot.v026_strategy import (
    ALL_CONTROL_ID,
    PRIMARY_ARM_ID,
    UP_CONTROL_ID,
    arm_matches,
    frozen_arm_config,
    validate_arm_config,
)


def _row(side: str, cost: float, won: bool, timestamp: int, index: int) -> dict:
    return {
        "condition_id": f"{side}-{index}",
        "market_start_ms": timestamp,
        "favorite_side": side,
        "label": side if won else ("Up" if side == "Down" else "Down"),
        "entry_cost": cost,
        "favorite_probability": cost - 0.02,
        "label_verified": 1,
    }


def _arms(down: list[dict], up: list[dict]) -> dict[str, list[dict]]:
    return {
        PRIMARY_ARM_ID: down,
        UP_CONTROL_ID: up,
        ALL_CONTROL_ID: sorted(
            [*down, *up], key=lambda row: int(row["market_start_ms"])
        ),
    }


class V026ReplicationTests(unittest.TestCase):
    def test_signal_contract_is_same_direction_and_cost_cap_as_v025(self):
        arms = {item["id"]: item for item in frozen_arm_config()}
        validate_arm_config(list(arms.values()))
        self.assertFalse(
            arm_matches({"favorite_side": "Down", "entry_cost": 0.50}, arms[PRIMARY_ARM_ID])
        )
        self.assertTrue(
            arm_matches({"favorite_side": "Down", "entry_cost": 0.90}, arms[PRIMARY_ARM_ID])
        )
        self.assertFalse(
            arm_matches({"favorite_side": "Up", "entry_cost": 0.70}, arms[PRIMARY_ARM_ID])
        )

    def test_economic_replication_without_confidence_continues_paper(self):
        down = [
            _row("Down", 0.71, index % 4 != 3, index, index)
            for index in range(16)
        ]
        up = [
            _row("Up", 0.76, index % 4 != 3, index, index)
            for index in range(16)
        ]
        result = evaluate_v026(_arms(down, up), midpoint_ms=8)
        self.assertTrue(result["frequency_passed"])
        self.assertTrue(result["economic_replication_passed"])
        self.assertFalse(result["full_statistical_passed"])

    def test_strong_replication_can_pass_statistical_gates(self):
        down = [_row("Down", 0.55, True, index, index) for index in range(20)]
        up = [
            _row("Up", 0.55, index % 2 == 0, index, index)
            for index in range(20)
        ]
        result = evaluate_v026(_arms(down, up), midpoint_ms=10)
        self.assertTrue(result["economic_replication_passed"])
        self.assertTrue(result["full_statistical_passed"])

    def test_less_than_fifteen_down_fails_frequency(self):
        down = [_row("Down", 0.60, True, index, index) for index in range(14)]
        up = [_row("Up", 0.65, False, index, index) for index in range(14)]
        result = evaluate_v026(_arms(down, up), midpoint_ms=7)
        self.assertFalse(result["frequency_passed"])
        self.assertFalse(result["economic_replication_passed"])

    def test_negative_second_half_cannot_be_rescued(self):
        down = [
            _row("Down", 0.60, index < 8, index, index)
            for index in range(16)
        ]
        up = [_row("Up", 0.65, index % 2 == 0, index, index) for index in range(16)]
        result = evaluate_v026(_arms(down, up), midpoint_ms=8)
        self.assertTrue(result["frequency_passed"])
        self.assertFalse(
            result["gates"]["economic_replication"][
                "positive_second_half_pnl_passed"
            ]
        )
        self.assertFalse(result["economic_replication_passed"])


if __name__ == "__main__":
    unittest.main()
