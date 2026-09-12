import unittest

from polymarket_bot.v027_design import candidate_profile
from polymarket_bot.v027_strategy import (
    DOWN_MID_COST_ID,
    UP_LOW_VOL_ID,
    arm_matches,
    frozen_arm_config,
    validate_arm_config,
)


def _row(side: str, cost: float, volatility: float, won: bool, hour: float) -> dict:
    return {
        "condition_id": f"{side}-{cost}-{hour}",
        "market_start_ms": int(hour * 3_600_000),
        "favorite_side": side,
        "favorite_probability": cost - 0.02,
        "label": side if won else ("Down" if side == "Up" else "Up"),
        "entry_cost": cost,
        "volatility_regime_ratio": volatility,
        "elapsed_hours": hour,
        "elapsed_block": f"h{int(hour // 4) * 4:02d}_to_h{min(24, int(hour // 4) * 4 + 4):02d}",
    }


class V027DesignTests(unittest.TestCase):
    def test_exact_arm_contract_and_boundaries(self):
        arms = {item["id"]: item for item in frozen_arm_config()}
        validate_arm_config(list(arms.values()))
        self.assertTrue(
            arm_matches(_row("Up", 0.70, 0.749, True, 1), arms[UP_LOW_VOL_ID])
        )
        self.assertFalse(
            arm_matches(_row("Up", 0.70, 0.75, True, 1), arms[UP_LOW_VOL_ID])
        )
        self.assertTrue(
            arm_matches(_row("Down", 0.70, 1.0, True, 1), arms[DOWN_MID_COST_ID])
        )
        self.assertFalse(
            arm_matches(_row("Down", 0.80, 1.0, True, 1), arms[DOWN_MID_COST_ID])
        )

    def test_candidate_profile_requires_fresh_validation(self):
        rows = [
            _row("Up", 0.65, 0.50, True, 1),
            _row("Up", 0.65, 0.50, True, 5),
            _row("Up", 0.65, 0.50, False, 13),
            _row("Down", 0.75, 1.00, True, 17),
        ]
        profile = candidate_profile(rows, candidate_id=UP_LOW_VOL_ID)
        self.assertTrue(profile["observed_only_not_validated"])
        self.assertEqual(profile["metrics"]["trades"], 3)
        self.assertEqual(profile["first_half_metrics"]["trades"], 2)
        self.assertEqual(profile["second_half_metrics"]["trades"], 1)


if __name__ == "__main__":
    unittest.main()
