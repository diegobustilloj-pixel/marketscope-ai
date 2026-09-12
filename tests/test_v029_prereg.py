from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from polymarket_bot.v029_design import build_v029_design_evidence
from polymarket_bot.v029_prereg import (
    V029PreregistrationError,
    load_and_verify_frozen_prereg,
    validate_frozen_prereg_payload,
)
from polymarket_bot.v029_strategy import (
    CANDIDATE_ID,
    feature_vector,
    select_side,
)


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "data" / "prereg_v029_high_frequency_holdout.json"
DESIGN = ROOT / "data" / "diagnostico_v029_high_frequency_holdout.json"
POSTMORTEM = ROOT / "data" / "postmortem_v028_twap_lt5_final.json"
RESULT = ROOT / "data" / "resultado_v028_twap_lt5_replication.json"


class V029PreregistrationTests(unittest.TestCase):
    def test_frozen_prereg_and_hashes_verify(self) -> None:
        payload = load_and_verify_frozen_prereg(PREREG, project_root=ROOT)
        self.assertEqual(payload["candidate"]["id"], CANDIDATE_ID)
        self.assertEqual(payload["implementation"]["launch_status"], "NOT_LAUNCHED")
        self.assertFalse(payload["implementation"]["runner_built"])
        self.assertFalse(payload["safety"]["orders_enabled"])
        self.assertEqual(payload["safety"]["real_money"], "BLOQUEADO")

    def test_model_selection_is_cost_aware_and_abstains_on_nonpositive_ev(self) -> None:
        self.assertEqual(
            select_side(probability_up=0.70, up_entry_cost=0.65, down_entry_cost=0.40)[
                "side"
            ],
            "Up",
        )
        self.assertEqual(
            select_side(probability_up=0.30, up_entry_cost=0.80, down_entry_cost=0.60)[
                "side"
            ],
            "Down",
        )
        self.assertIsNone(
            select_side(probability_up=0.50, up_entry_cost=0.55, down_entry_cost=0.55)
        )
        self.assertEqual(
            feature_vector(
                {
                    "implied_up_mid_probability": 0.6,
                    "twap_distance_to_open_bps": -2.0,
                    "binance_return_60s_bps": 1.0,
                }
            ),
            [0.6, -2.0, 1.0],
        )

    def test_holdout_or_model_change_is_rejected(self) -> None:
        payload = json.loads(PREREG.read_text(encoding="utf-8"))
        shortened = copy.deepcopy(payload)
        shortened["temporal_holdout"]["validation_hours"] = 8.0
        with self.assertRaises(V029PreregistrationError):
            validate_frozen_prereg_payload(shortened)

        changed_model = copy.deepcopy(payload)
        changed_model["candidate"]["model"]["estimator"]["C"] = 1.0
        with self.assertRaises(V029PreregistrationError):
            validate_frozen_prereg_payload(changed_model)

        changed_threshold = copy.deepcopy(payload)
        changed_threshold["candidate"]["selection"][
            "minimum_expected_pnl_per_share_strict"
        ] = 0.01
        with self.assertRaises(V029PreregistrationError):
            validate_frozen_prereg_payload(changed_threshold)

    def test_design_is_reproducible_and_uses_outcome_blind_capacity_only(self) -> None:
        design = build_v029_design_evidence(
            postmortem_path=POSTMORTEM,
            result_path=RESULT,
        )
        self.assertEqual(
            design["outcome_blind_capacity_planning"][
                "projected_24h_usable_features"
            ],
            282,
        )
        self.assertEqual(
            design["outcome_blind_capacity_planning"][
                "outcomes_used_for_capacity_planning"
            ],
            0,
        )
        self.assertFalse(
            design["anti_overfit"][
                "v028_trade_outcomes_used_to_choose_thresholds"
            ]
        )
        stored = json.loads(DESIGN.read_text(encoding="utf-8"))
        self.assertEqual(design["source_hashes"], stored["source_hashes"])


if __name__ == "__main__":
    unittest.main()
