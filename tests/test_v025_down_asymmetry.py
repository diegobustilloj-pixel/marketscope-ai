import unittest

from polymarket_bot.v025_audit import sequence_metrics
from polymarket_bot.v025_strategy import (
    PRIMARY_ARM_ID,
    arm_matches,
    eligible_arm_ids,
    frozen_arm_config,
    validate_arm_config,
)


class V025DownAsymmetryTests(unittest.TestCase):
    def test_primary_and_controls_have_exact_direction_roles(self):
        arms = {item["id"]: item for item in frozen_arm_config()}
        down = {"favorite_side": "Down", "entry_cost": 0.70}
        up = {"favorite_side": "Up", "entry_cost": 0.70}
        self.assertTrue(arm_matches(down, arms[PRIMARY_ARM_ID]))
        self.assertFalse(arm_matches(up, arms[PRIMARY_ARM_ID]))
        self.assertIn(PRIMARY_ARM_ID, eligible_arm_ids(down, list(arms.values())))
        self.assertNotIn(PRIMARY_ARM_ID, eligible_arm_ids(up, list(arms.values())))
        self.assertIn("favorite_all_cap_090_control", eligible_arm_ids(down, list(arms.values())))
        self.assertIn("favorite_all_cap_090_control", eligible_arm_ids(up, list(arms.values())))

    def test_cost_boundaries_are_frozen(self):
        primary = frozen_arm_config()[0]
        self.assertFalse(
            arm_matches({"favorite_side": "Down", "entry_cost": 0.50}, primary)
        )
        self.assertTrue(
            arm_matches({"favorite_side": "Down", "entry_cost": 0.90}, primary)
        )
        self.assertFalse(
            arm_matches({"favorite_side": "Down", "entry_cost": 0.90001}, primary)
        )

    def test_arm_contract_rejects_changes(self):
        config = frozen_arm_config()
        validate_arm_config(config)
        config[0]["favorite_side"] = "Any"
        with self.assertRaisesRegex(Exception, "Parametros V0.25 incompatibles"):
            validate_arm_config(config)

    def test_sequence_metrics_use_first_and_last_five(self):
        rows = []
        for index in range(10):
            rows.append(
                {
                    "market_start_ms": index,
                    "favorite_side": "Down",
                    "label": "Down" if index < 6 else "Up",
                    "entry_cost": 0.60,
                }
            )
        metrics = sequence_metrics(rows)
        self.assertEqual(metrics["trades"], 10)
        self.assertEqual(metrics["wins"], 6)
        self.assertAlmostEqual(metrics["net_pnl_per_share_sequence"], 0.0)
        self.assertAlmostEqual(
            metrics["first_five"]["net_pnl_per_share_sequence"], 2.0
        )
        self.assertAlmostEqual(
            metrics["last_five"]["net_pnl_per_share_sequence"], -2.0
        )
        self.assertAlmostEqual(metrics["net_pnl_at_5_shares"], 0.0)


if __name__ == "__main__":
    unittest.main()
