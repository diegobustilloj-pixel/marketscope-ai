from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from polymarket_bot.v031_data_readiness import (
    V031ReadinessError,
    build_readiness_report,
    frozen_capture_contract,
    validate_capture_contract,
)


ROOT = Path(__file__).resolve().parents[1]
STORED = ROOT / "data" / "diagnostico_v031_path_execution_readiness.json"


class V031DataReadinessTests(unittest.TestCase):
    def test_capture_contract_is_exact_and_mutation_is_rejected(self) -> None:
        contract = frozen_capture_contract()
        validate_capture_contract(contract)
        self.assertEqual(contract["technical_pilot_hours"], 1.0)
        self.assertFalse(contract["outcomes_read_during_technical_pilot"])
        changed = copy.deepcopy(contract)
        changed["snapshot_frequency_hz"] = 0.5
        with self.assertRaises(V031ReadinessError):
            validate_capture_contract(changed)

    def test_existing_sources_are_not_combined_into_false_readiness(self) -> None:
        result = json.loads(STORED.read_text(encoding="utf-8"))
        self.assertFalse(result["all_requirements_met_by_one_existing_dataset"])
        self.assertEqual(result["status"], "BLOCKED_REQUIRES_NEW_CAPTURE")
        self.assertFalse(result["sources"]["v018_per_second"]["has_ask_depth"])
        self.assertTrue(
            result["sources"]["raw_two_hour_v4"]["has_raw_clob_book_events"]
        )
        self.assertFalse(
            result["sources"]["raw_two_hour_v4"]["has_official_twap_events"]
        )

    def test_report_is_reproducible_read_only_and_safe(self) -> None:
        expected = json.loads(STORED.read_text(encoding="utf-8"))
        actual = build_readiness_report(project_root=ROOT)
        self.assertEqual(actual, expected)
        self.assertTrue(actual["source_databases_unchanged"])
        self.assertFalse(actual["safety"]["orders_enabled"])
        self.assertEqual(actual["safety"]["paper_orders"], 0)
        self.assertEqual(actual["safety"]["outcomes_read"], 0)
        self.assertFalse(actual["safety"]["wallet_required"])
        self.assertEqual(actual["safety"]["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
