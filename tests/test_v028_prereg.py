from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from polymarket_bot.v028_design import build_v028_design_evidence
from polymarket_bot.v028_prereg import (
    V028PreregistrationError,
    load_and_verify_frozen_prereg,
    validate_frozen_prereg_payload,
)
from polymarket_bot.v028_strategy import (
    PARENT_CONTROL_ID,
    PRIMARY_ID,
    eligible_arm_ids,
    frozen_arm_config,
)


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "data" / "prereg_v028_twap_lt5_replication.json"
DESIGN = ROOT / "data" / "diagnostico_v028_twap_lt5_candidate.json"
POSTMORTEM = ROOT / "data" / "postmortem_v027_up_low_vol_final.json"
RESULT = ROOT / "data" / "resultado_v027_regime_tournament.json"


def base_record() -> dict[str, object]:
    return {
        "favorite_side": "Up",
        "entry_cost": 0.70,
        "volatility_regime_ratio": 0.50,
        "twap_distance_to_open_bps": 4.999,
        "resolution_twap_window_s": 60,
    }


class V028PreregistrationTests(unittest.TestCase):
    def test_frozen_prereg_and_all_hashes_verify(self) -> None:
        payload = load_and_verify_frozen_prereg(PREREG, project_root=ROOT)
        self.assertEqual(payload["implementation"]["launch_status"], "NOT_LAUNCHED")
        self.assertFalse(payload["implementation"]["runner_built"])
        self.assertFalse(payload["safety"]["orders_enabled"])
        self.assertEqual(payload["safety"]["real_money"], "BLOQUEADO")

    def test_primary_boundaries_are_strict_and_control_is_nonselectable(self) -> None:
        arms = frozen_arm_config()
        record = base_record()
        self.assertEqual(
            eligible_arm_ids(record, arms), [PRIMARY_ID, PARENT_CONTROL_ID]
        )

        distance_boundary = dict(record, twap_distance_to_open_bps=5.0)
        self.assertEqual(
            eligible_arm_ids(distance_boundary, arms), [PARENT_CONTROL_ID]
        )
        negative_boundary = dict(record, twap_distance_to_open_bps=-5.0)
        self.assertEqual(
            eligible_arm_ids(negative_boundary, arms), [PARENT_CONTROL_ID]
        )
        missing_distance = dict(record, twap_distance_to_open_bps=None)
        self.assertEqual(
            eligible_arm_ids(missing_distance, arms), [PARENT_CONTROL_ID]
        )

        self.assertEqual(eligible_arm_ids(dict(record, entry_cost=0.50), arms), [])
        self.assertEqual(
            eligible_arm_ids(dict(record, volatility_regime_ratio=0.75), arms), []
        )
        self.assertEqual(
            eligible_arm_ids(dict(record, resolution_twap_window_s=30), arms), []
        )

    def test_threshold_or_duration_change_is_rejected(self) -> None:
        payload = json.loads(PREREG.read_text(encoding="utf-8"))
        changed_threshold = copy.deepcopy(payload)
        changed_threshold["hypothesis"][
            "maximum_abs_twap_distance_to_open_bps"
        ] = 5.1
        with self.assertRaises(V028PreregistrationError):
            validate_frozen_prereg_payload(changed_threshold)

        changed_arm = copy.deepcopy(payload)
        changed_arm["arms"][0]["maximum_abs_twap_distance_to_open_bps"] = 5.1
        with self.assertRaises(V028PreregistrationError):
            validate_frozen_prereg_payload(changed_arm)

        shortened = copy.deepcopy(payload)
        shortened["maximum_hours"] = 12.0
        with self.assertRaises(V028PreregistrationError):
            validate_frozen_prereg_payload(shortened)

    def test_design_is_reproducible_and_remains_development_only(self) -> None:
        design = build_v028_design_evidence(
            postmortem_path=POSTMORTEM,
            result_path=RESULT,
        )
        self.assertEqual(
            design["sample_size_planning"][
                "approximate_trades_for_positive_lcb_if_effect_repeats"
            ],
            33,
        )
        self.assertFalse(design["development_evidence_only"]["validated"])
        self.assertTrue(
            design["anti_overfit"][
                "threshold_selected_after_examining_13_segments"
            ]
        )
        stored = json.loads(DESIGN.read_text(encoding="utf-8"))
        self.assertEqual(design["source_hashes"], stored["source_hashes"])


if __name__ == "__main__":
    unittest.main()
