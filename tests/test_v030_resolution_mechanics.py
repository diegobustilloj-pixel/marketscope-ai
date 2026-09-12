from __future__ import annotations

import math
import json
import unittest
from pathlib import Path

from polymarket_bot.v030_development import evaluate_records, run_development_screen
from polymarket_bot.v030_strategy import (
    V030StrategyError,
    frozen_model_spec,
    mechanical_probability_up,
    select_side,
    validate_model_spec,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DATABASE = ROOT / "data" / "paper_v029_high_frequency_holdout.db"
SOURCE_RESULT = ROOT / "data" / "resultado_v029_high_frequency_holdout.json"
STORED_DEVELOPMENT = (
    ROOT / "data" / "diagnostico_v030_resolution_mechanics_final.json"
)


def _source_row(
    *,
    condition_id: str,
    market_start_ms: int,
    label: str,
    spot: float,
    opening: float = 100.0,
    volatility: float = 1.0,
    up_ask: float = 0.50,
    down_ask: float = 0.50,
    implied: float = 0.55,
) -> dict:
    return {
        "condition_id": condition_id,
        "market_start_ms": market_start_ms,
        "label": label,
        "label_verified": 1,
        "resolution_contract_status": "VERIFIED",
        "resolution_twap_window_s": 60,
        "feature": {
            "horizon_seconds": 60,
            "resolution_twap_window_s": 60,
            "twap_open_fresh": 1,
            "resolution_twap_fresh": 1,
            "chainlink_price": spot,
            "twap_open_price": opening,
            "chainlink_vol_60s_bps": volatility,
            "up_best_ask": up_ask,
            "down_best_ask": down_ask,
            "implied_up_mid_probability": implied,
        },
    }


class V030ResolutionMechanicsTests(unittest.TestCase):
    def test_probability_is_symmetric_and_uses_average_variance(self) -> None:
        at_open = mechanical_probability_up(
            chainlink_spot=100.0,
            opening_twap=100.0,
            volatility_60s_bps=1.0,
        )
        self.assertEqual(at_open, 0.5)
        one_average_std_bps = math.sqrt(60.0 / 3.0)
        above = mechanical_probability_up(
            chainlink_spot=100.0 * (1.0 + one_average_std_bps / 10_000.0),
            opening_twap=100.0,
            volatility_60s_bps=1.0,
        )
        below = mechanical_probability_up(
            chainlink_spot=100.0 * (1.0 - one_average_std_bps / 10_000.0),
            opening_twap=100.0,
            volatility_60s_bps=1.0,
        )
        assert above is not None and below is not None
        self.assertAlmostEqual(above, 0.841344746, places=8)
        self.assertAlmostEqual(above + below, 1.0, places=8)

    def test_invalid_or_incompatible_inputs_abstain(self) -> None:
        self.assertIsNone(
            mechanical_probability_up(
                chainlink_spot=100.0,
                opening_twap=100.0,
                volatility_60s_bps=0.0,
            )
        )
        self.assertIsNone(
            mechanical_probability_up(
                chainlink_spot=100.0,
                opening_twap=100.0,
                volatility_60s_bps=1.0,
                resolution_twap_window_seconds=30,
            )
        )

    def test_selection_is_cost_aware_and_single_sided(self) -> None:
        selected = select_side(
            probability_up=0.80,
            up_entry_cost=0.60,
            down_entry_cost=0.45,
        )
        assert selected is not None
        self.assertEqual(selected["side"], "Up")
        self.assertIsNone(
            select_side(
                probability_up=0.50,
                up_entry_cost=0.55,
                down_entry_cost=0.55,
            )
        )
        with self.assertRaises(V030StrategyError):
            select_side(
                probability_up=1.1,
                up_entry_cost=0.50,
                down_entry_cost=0.50,
            )

    def test_model_contract_has_no_fit_and_rejects_mutation(self) -> None:
        spec = frozen_model_spec()
        self.assertFalse(spec["fit_parameters"])
        self.assertFalse(spec["probability_calibration"])
        self.assertFalse(spec["market_probability_used_by_model"])
        validate_model_spec(spec)
        changed = dict(spec)
        changed["drift_bps_per_second"] = 0.01
        with self.assertRaises(V030StrategyError):
            validate_model_spec(changed)

    def test_evaluation_keeps_candidate_and_control_paired(self) -> None:
        rows = [
            _source_row(
                condition_id="up",
                market_start_ms=1_000,
                label="Up",
                spot=100.2,
            ),
            _source_row(
                condition_id="down",
                market_start_ms=2_000,
                label="Down",
                spot=99.8,
            ),
            _source_row(
                condition_id="invalid",
                market_start_ms=3_000,
                label="Up",
                spot=100.0,
                volatility=0.0,
            ),
        ]
        result = evaluate_records(source_rows=rows, experiment_start_ms=0)
        self.assertEqual(result["usable_markets"], 2)
        self.assertEqual(result["candidate_signal_count"], 2)
        self.assertEqual(result["candidate_metrics"]["trades"], 2)
        self.assertEqual(result["paired_control_metrics"]["trades"], 2)
        self.assertEqual(
            result["rejection_reasons"]["mechanical_probability_unavailable"], 1
        )

    def test_final_development_screen_is_reproducible_and_read_only(self) -> None:
        expected = json.loads(STORED_DEVELOPMENT.read_text(encoding="utf-8"))
        actual = run_development_screen(
            database=SOURCE_DATABASE,
            source_result_path=SOURCE_RESULT,
        )
        self.assertEqual(actual, expected)
        self.assertEqual(
            actual["evaluation"]["decision"],
            "CLOSE_RESOLUTION_MECHANICS_FAMILY",
        )
        self.assertTrue(actual["source"]["database_unchanged"])
        self.assertFalse(actual["safety"]["orders_enabled"])
        self.assertEqual(actual["safety"]["paper_orders"], 0)
        self.assertFalse(actual["safety"]["wallet_required"])
        self.assertEqual(actual["safety"]["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
