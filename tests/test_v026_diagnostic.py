import unittest

from polymarket_bot.v026_diagnostic import (
    approximate_sample_size_for_positive_lcb,
    cost_matched_direction_contrast,
    direction_contrast,
    favorite_cap_rows,
)


def _row(side: str, cost: float, won: bool, index: int) -> dict:
    return {
        "condition_id": str(index),
        "market_start_ms": index,
        "favorite_side": side,
        "label": side if won else ("Up" if side == "Down" else "Down"),
        "entry_cost": cost,
        "favorite_probability": cost - 0.02,
    }


class V026DiagnosticTests(unittest.TestCase):
    def test_sample_size_planning_is_explicitly_approximate(self):
        self.assertEqual(
            approximate_sample_size_for_positive_lcb(
                mean_pnl_per_share=0.10,
                pnl_standard_deviation=0.50,
            ),
            68,
        )
        self.assertIsNone(
            approximate_sample_size_for_positive_lcb(
                mean_pnl_per_share=0.0,
                pnl_standard_deviation=0.50,
            )
        )

    def test_direction_contrast_decomposes_win_rate_and_entry_cost(self):
        down = [_row("Down", 0.60, True, 1), _row("Down", 0.60, False, 2)]
        up = [_row("Up", 0.70, True, 3), _row("Up", 0.70, False, 4)]
        result = direction_contrast(down, up)
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["down_minus_up_mean_pnl"], 0.10)
        self.assertAlmostEqual(
            result["decomposition"]["down_minus_up_win_rate"], 0.0
        )
        self.assertAlmostEqual(
            result["decomposition"]["up_minus_down_average_entry_cost"], 0.10
        )
        self.assertAlmostEqual(
            result["decomposition"]["reconstructed_down_minus_up_mean_pnl"],
            result["down_minus_up_mean_pnl"],
        )

    def test_cost_matching_uses_only_bands_with_both_sides(self):
        rows = [
            _row("Down", 0.55, True, 1),
            _row("Up", 0.55, False, 2),
            _row("Down", 0.65, True, 3),
        ]
        result = cost_matched_direction_contrast(rows)
        self.assertEqual(result["matched_weight"], 1)
        self.assertAlmostEqual(result["matched_down_minus_up_mean_pnl"], 1.0)
        self.assertFalse(result["bands"]["0.60_to_0.70"]["available"])

    def test_favorite_cap_keeps_original_boundaries(self):
        rows = [
            _row("Down", 0.50, True, 1),
            _row("Down", 0.50001, True, 2),
            _row("Up", 0.90, True, 3),
            _row("Up", 0.90001, True, 4),
        ]
        eligible = favorite_cap_rows(rows)
        self.assertEqual([row["condition_id"] for row in eligible], ["2", "3"])


if __name__ == "__main__":
    unittest.main()
