from __future__ import annotations

import unittest

from polymarket_bot.v027_final_postmortem import (
    contribution_diagnostics,
    descriptive_distribution,
    exploratory_segment_profile,
)


def row(
    *,
    index: int,
    cost: float,
    won: bool,
    elapsed_hours: float,
) -> dict[str, object]:
    return {
        "condition_id": f"condition-{index}",
        "market_start_ms": index * 300_000,
        "entry_cost": cost,
        "favorite_side": "Up",
        "label": "Up" if won else "Down",
        "elapsed_hours": elapsed_hours,
        "elapsed_block": f"h{int(elapsed_hours // 4) * 4:02d}_to_h{int(elapsed_hours // 4) * 4 + 4:02d}",
        "volatility_regime_ratio": 0.5,
        "twap_distance_to_open_bps": 2.0,
    }


class V027FinalPostmortemTests(unittest.TestCase):
    def test_distribution_uses_deterministic_linear_percentiles(self) -> None:
        self.assertEqual(
            descriptive_distribution([1.0, 2.0, 3.0, 4.0]),
            {
                "count": 4,
                "minimum": 1.0,
                "q25": 1.75,
                "median": 2.5,
                "q75": 3.25,
                "maximum": 4.0,
                "mean": 2.5,
            },
        )

    def test_concentration_detects_best_trade_dependency(self) -> None:
        rows = [
            row(index=1, cost=0.60, won=True, elapsed_hours=1.0),
            row(index=2, cost=0.55, won=False, elapsed_hours=2.0),
            row(index=3, cost=0.20, won=True, elapsed_hours=3.0),
            row(index=4, cost=0.60, won=False, elapsed_hours=4.0),
        ]
        result = contribution_diagnostics(rows)
        self.assertAlmostEqual(result["net_pnl_per_share"], 0.05)
        self.assertAlmostEqual(result["net_without_best_trade_per_share"], -0.75)
        self.assertFalse(result["positive_without_best_trade"])

    def test_exploratory_screen_requires_both_halves_and_best_trade_robustness(
        self,
    ) -> None:
        rows = []
        for index in range(12):
            elapsed = float(index * 2)
            rows.append(
                row(
                    index=index,
                    cost=0.55,
                    won=index not in {2, 8},
                    elapsed_hours=elapsed,
                )
            )
        result = exploratory_segment_profile(rows)
        self.assertTrue(result["exploratory_screen_passed"])
        self.assertTrue(
            result["exploratory_screen_gates"][
                "positive_without_best_trade_passed"
            ]
        )


if __name__ == "__main__":
    unittest.main()
