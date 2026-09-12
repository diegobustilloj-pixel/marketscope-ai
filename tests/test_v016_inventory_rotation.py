import unittest

from polymarket_bot.inventory_rotation import (
    development_eligible,
    executable_cost,
    simulate_market,
    summarize_trades,
    taker_cost,
    validation_passes,
)


EXECUTION = {
    "order_size_shares": 5.0,
    "slippage_per_share": 0.005,
    "fee_rate": 0.07,
}
STRATEGY = {
    "entry_window_second_offsets_inclusive": [30, 210],
    "hedge_deadline_second_offset_inclusive": 285,
}
CANDIDATE = {
    "name": "TEST",
    "entry_cost_cap": 0.35,
    "pair_cost_cap": 0.97,
}


def row(offset, up_ask, down_ask, *, up_depth=10, down_depth=10, quality=0):
    return {
        "second_offset": offset,
        "quality_flags": quality,
        "up_best_ask": up_ask,
        "up_ask_depth_1c": up_depth,
        "down_best_ask": down_ask,
        "down_ask_depth_1c": down_depth,
    }


class InventoryRotationTests(unittest.TestCase):
    def test_taker_cost_matches_project_formula(self):
        fill, fee, total = taker_cost(
            0.30, fee_rate=0.07, slippage_per_share=0.005
        )
        self.assertAlmostEqual(fill, 0.305)
        self.assertAlmostEqual(fee, 0.07 * 0.305 * 0.695)
        self.assertAlmostEqual(total, fill + fee)

    def test_temporal_pair_locks_positive_pnl(self):
        trade = simulate_market(
            [row(30, 0.29, 0.72), row(31, 0.70, 0.58)],
            condition_id="cid",
            market_start_ms=1,
            label="Down",
            candidate=CANDIDATE,
            execution=EXECUTION,
            strategy=STRATEGY,
        )
        self.assertIsNotNone(trade)
        self.assertTrue(trade["paired"])
        self.assertEqual(trade["label"], "DOWN")
        self.assertEqual(trade["entry_side"], "UP")
        self.assertEqual(trade["hedge_offset"], 31)
        self.assertGreater(trade["pnl"], 0)

    def test_same_second_is_not_used_as_temporal_hedge(self):
        trade = simulate_market(
            [row(30, 0.29, 0.58)],
            condition_id="cid",
            market_start_ms=1,
            label="DOWN",
            candidate=CANDIDATE,
            execution=EXECUTION,
            strategy=STRATEGY,
        )
        self.assertIsNotNone(trade)
        self.assertFalse(trade["paired"])
        self.assertLess(trade["pnl"], 0)

    def test_low_depth_and_bad_quality_cannot_enter(self):
        trade = simulate_market(
            [
                row(30, 0.20, 0.80, up_depth=4),
                row(31, 0.20, 0.80, quality=1),
            ],
            condition_id="cid",
            market_start_ms=1,
            label="UP",
            candidate=CANDIDATE,
            execution=EXECUTION,
            strategy=STRATEGY,
        )
        self.assertIsNone(trade)

    def test_corrected_mask_ignores_only_unused_external_feeds(self):
        corrected = {**EXECUTION, "required_clear_quality_mask": 22}
        self.assertIsNotNone(executable_cost(row(30, 0.20, 0.80, quality=8), "UP", corrected))
        self.assertIsNotNone(executable_cost(row(30, 0.20, 0.80, quality=1), "UP", corrected))
        self.assertIsNone(executable_cost(row(30, 0.20, 0.80, quality=2), "UP", corrected))
        self.assertIsNone(executable_cost(row(30, 0.20, 0.80, quality=16), "UP", corrected))

    def test_summaries_and_gates_are_fail_closed(self):
        trades = [
            {
                "market_start_ms": index,
                "paired": True,
                "pnl": 0.1,
                "capital": 4.0,
            }
            for index in range(12)
        ]
        summary = summarize_trades(trades)
        development_gate = {
            "minimum_entries": 12,
            "minimum_paired_rate": 0.5,
            "maximum_single_positive_trade_share": 0.35,
        }
        validation_gate = {
            "minimum_entries": 6,
            "minimum_paired_rate": 0.5,
            "maximum_single_positive_trade_share": 0.5,
            "maximum_drawdown": 2.5,
        }
        self.assertTrue(development_eligible(summary, development_gate))
        self.assertTrue(validation_passes(summary, validation_gate))
        summary["net_pnl"] = -0.01
        self.assertFalse(development_eligible(summary, development_gate))
        self.assertFalse(validation_passes(summary, validation_gate))


if __name__ == "__main__":
    unittest.main()
