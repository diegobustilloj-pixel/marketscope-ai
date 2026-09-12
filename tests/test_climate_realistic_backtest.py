from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from polymarket_bot.climate_realistic_backtest import (
    evidence_verdict,
    fee_per_share,
    round_up_tick,
    scenario_candidates,
    simulate_capital,
)


def covered_row(number: int, outcome: int = 1) -> dict:
    entry = datetime(2026, 8, 1, 15, tzinfo=timezone.utc)
    return {
        "slug": f"market-{number:02d}",
        "market_date": "2026-08-02",
        "entry_at": entry.isoformat().replace("+00:00", "Z"),
        "closed_at": (entry + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "reference_price": 0.30,
        "tick_size": 0.01,
        "minimum_order_size": 5.0,
        "fees_enabled": True,
        "fee_schedule": {"rate": 0.05, "exponent": 1, "takerOnly": True},
        "model_probability": 0.50,
        "outcome": outcome,
    }


class ClimateRealisticBacktestTests(unittest.TestCase):
    def test_weather_fee_curve(self):
        contract = {
            "fees_enabled": True,
            "fee_schedule": {"rate": 0.05, "exponent": 1, "takerOnly": True},
        }
        self.assertAlmostEqual(fee_per_share(0.40, contract), 0.012)
        self.assertEqual(fee_per_share(0.40, {"fees_enabled": False}), 0.0)

    def test_execution_rounds_against_trader(self):
        self.assertAlmostEqual(round_up_tick(0.331, 0.01), 0.34)
        self.assertAlmostEqual(round_up_tick(0.33, 0.01), 0.33)

    def test_outcome_cannot_change_signal(self):
        winners = scenario_candidates([covered_row(1, 1)], 0.03)
        losers = scenario_candidates([covered_row(1, 0)], 0.03)
        self.assertEqual(winners[0]["signal"], losers[0]["signal"])
        self.assertAlmostEqual(winners[0]["net_edge"], losers[0]["net_edge"])

    def test_capital_limits_ten_simultaneous_positions(self):
        rows = scenario_candidates([covered_row(i, i % 2) for i in range(11)], 0.03)
        trades, skipped, capital = simulate_capital(rows)
        self.assertEqual(len(trades), 10)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["capital_skip_reason"], "MAX_OPEN_POSITIONS")
        self.assertAlmostEqual(capital["ending_capital_usdc"] - 1000.0, sum(t["pnl_usdc"] for t in trades))

    def test_verdict_fails_when_bootstrap_crosses_zero(self):
        row = {
            "trades": 40,
            "net_pnl_usdc": 10.0,
            "roi_on_cost": 0.01,
            "profit_factor": 1.30,
            "daily_block_bootstrap_total_pnl_lower_95": -5.0,
        }
        verdict, reasons = evidence_verdict(row)
        self.assertEqual(verdict, "EVIDENCE_FAIL")
        self.assertTrue(any("bootstrap" in reason for reason in reasons))


if __name__ == "__main__":
    unittest.main()
