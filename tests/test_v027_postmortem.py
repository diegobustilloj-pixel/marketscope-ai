import unittest

from polymarket_bot.v027_postmortem import (
    cost_band,
    elapsed_four_hour_block,
    summarize_rows,
    twap_alignment,
    twap_distance_band,
    utc_session,
    volatility_regime,
)


def _row(side: str, cost: float, won: bool, index: int) -> dict:
    return {
        "condition_id": f"{side}-{index}",
        "market_start_ms": index,
        "favorite_side": side,
        "favorite_probability": cost - 0.02,
        "label": side if won else ("Up" if side == "Down" else "Down"),
        "entry_cost": cost,
    }


class V027PostmortemTests(unittest.TestCase):
    def test_fixed_boundaries_are_deterministic(self):
        self.assertEqual(elapsed_four_hour_block(0), "h00_to_h04")
        self.assertEqual(elapsed_four_hour_block(4), "h04_to_h08")
        self.assertEqual(elapsed_four_hour_block(24), "h20_to_h24")
        self.assertEqual(utc_session(0), "utc_00_to_08")
        self.assertEqual(utc_session(8), "utc_08_to_16")
        self.assertEqual(utc_session(16), "utc_16_to_24")

    def test_cost_volatility_and_distance_boundaries(self):
        self.assertIsNone(cost_band(0.50))
        self.assertEqual(cost_band(0.60), "cost_060_to_070")
        self.assertEqual(cost_band(0.90), "cost_080_to_090")
        self.assertEqual(volatility_regime(0.749), "vol_ratio_low")
        self.assertEqual(volatility_regime(0.75), "vol_ratio_normal")
        self.assertEqual(volatility_regime(1.25), "vol_ratio_high")
        self.assertEqual(twap_distance_band(4.99), "twap_abs_lt_5bps")
        self.assertEqual(twap_distance_band(-5), "twap_abs_5_to_10bps")
        self.assertEqual(twap_distance_band(10), "twap_abs_ge_10bps")

    def test_twap_alignment_uses_only_decision_time_values(self):
        self.assertEqual(twap_alignment(-2, "Down"), "twap_agrees_favorite")
        self.assertEqual(twap_alignment(2, "Down"), "twap_disagrees_favorite")
        self.assertEqual(twap_alignment(None, "Down"), "twap_flat_or_missing")

    def test_summary_keeps_directions_separate(self):
        rows = [
            _row("Down", 0.60, True, 1),
            _row("Down", 0.60, False, 2),
            _row("Up", 0.60, True, 3),
        ]
        summary = summarize_rows(rows)
        self.assertEqual(summary["down"]["trades"], 2)
        self.assertEqual(summary["up"]["trades"], 1)
        self.assertTrue(summary["down_minus_up"]["available"])


if __name__ == "__main__":
    unittest.main()
