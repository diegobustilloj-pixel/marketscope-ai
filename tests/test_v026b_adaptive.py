import math
import unittest

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v026b_runner import (
    checkpoint_decision,
    load_and_verify_prereg,
    mean_pnl_upper_bound,
    poisson_mean_upper_bound,
)


def _resolved_down(*, cost: float, won: bool, index: int) -> dict:
    return {
        "condition_id": f"down-{index}",
        "market_start_ms": index,
        "favorite_side": "Down",
        "label": "Down" if won else "Up",
        "entry_cost": cost,
        "label_verified": 1,
    }


class V026BAdaptiveTests(unittest.TestCase):
    def test_frozen_preregistration_and_code_hashes_verify(self):
        payload = load_and_verify_prereg(
            ROOT / "data/prereg_v026b_adaptive_checkpoints.json"
        )
        self.assertFalse(payload["stopping"]["early_success_allowed"])
        self.assertTrue(payload["stopping"]["early_stop_for_futility_only"])

    def test_poisson_upper_bound_for_zero_matches_exact_solution(self):
        self.assertAlmostEqual(
            poisson_mean_upper_bound(0), -math.log(0.05), places=8
        )

    def test_zero_down_at_four_hours_does_not_freeze_too_early(self):
        result = checkpoint_decision(
            checkpoint_hour=4,
            captured_down=0,
            captured_first_half_down=0,
            resolved_down_rows=[],
            technical_passed=True,
            safety_passed=True,
        )
        self.assertEqual(result["decision"], "CONTINUE")
        self.assertGreater(
            result["frequency"]["projected_24h_upper_down_trades"], 15
        )

    def test_zero_down_at_eight_hours_freezes_frequency(self):
        result = checkpoint_decision(
            checkpoint_hour=8,
            captured_down=0,
            captured_first_half_down=0,
            resolved_down_rows=[],
            technical_passed=True,
            safety_passed=True,
        )
        self.assertEqual(result["decision"], "FREEZE_FREQUENCY_FUTILITY")

    def test_two_down_at_eight_hours_retains_statistical_possibility(self):
        result = checkpoint_decision(
            checkpoint_hour=8,
            captured_down=2,
            captured_first_half_down=2,
            resolved_down_rows=[],
            technical_passed=True,
            safety_passed=True,
        )
        self.assertEqual(result["decision"], "CONTINUE")

    def test_closed_first_half_below_five_freezes_at_twelve_hours(self):
        result = checkpoint_decision(
            checkpoint_hour=12,
            captured_down=9,
            captured_first_half_down=4,
            resolved_down_rows=[],
            technical_passed=True,
            safety_passed=True,
        )
        self.assertEqual(result["decision"], "FREEZE_FIRST_HALF_FREQUENCY")

    def test_eight_clear_losses_freeze_negative_futility(self):
        rows = [
            _resolved_down(cost=0.70 + index / 1000, won=False, index=index)
            for index in range(8)
        ]
        self.assertLess(mean_pnl_upper_bound(rows), 0)
        result = checkpoint_decision(
            checkpoint_hour=8,
            captured_down=8,
            captured_first_half_down=8,
            resolved_down_rows=rows,
            technical_passed=True,
            safety_passed=True,
        )
        self.assertEqual(result["decision"], "FREEZE_NEGATIVE_FUTILITY")

    def test_technical_failure_requests_retry_before_freezing(self):
        result = checkpoint_decision(
            checkpoint_hour=8,
            captured_down=8,
            captured_first_half_down=8,
            resolved_down_rows=[],
            technical_passed=False,
            safety_passed=True,
        )
        self.assertEqual(result["decision"], "RETRY_TECHNICAL")

    def test_safety_failure_has_priority(self):
        result = checkpoint_decision(
            checkpoint_hour=4,
            captured_down=10,
            captured_first_half_down=10,
            resolved_down_rows=[],
            technical_passed=False,
            safety_passed=False,
        )
        self.assertEqual(result["decision"], "FREEZE_SAFETY")


if __name__ == "__main__":
    unittest.main()
