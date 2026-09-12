from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from polymarket_bot.btc_5m_90.v002_confirmatory import (
    MAXIMUM_HOURS,
    TARGET_COMPLETE_MARKETS,
    ConfirmatoryError,
    build_prereg,
    freeze_prereg,
    load_and_verify_prereg,
    status,
)
from polymarket_bot.btc_5m_90.v002_shadow import (
    load_and_verify_prereg as load_base_prereg,
)


class V002ConfirmatoryTests(unittest.TestCase):
    def test_duration_target_and_recovery_reserve_are_consistent(self) -> None:
        payload = build_prereg()
        self.assertEqual(TARGET_COMPLETE_MARKETS, 276)
        self.assertEqual(MAXIMUM_HOURS, 24.0)
        self.assertAlmostEqual(payload["expected_capture_hours"], 23.0)
        self.assertAlmostEqual(payload["network_recovery_reserve_hours"], 1.0)

    def test_strategy_is_identical_to_frozen_base(self) -> None:
        payload = build_prereg()
        base = load_base_prereg()
        self.assertEqual(payload["strategy"], base["strategy"])
        self.assertEqual(payload["execution"], base["execution"])
        self.assertTrue(payload["evaluation"]["no_posthoc_rule_change"])

    def test_frequency_gate_is_derived_before_capture(self) -> None:
        payload = build_prereg()
        self.assertEqual(payload["gates"]["minimum_signals"], 70)
        self.assertGreater(payload["reference_only"]["signal_frequency"], 0.0)
        self.assertTrue(
            payload["reference_only"][
                "minimum_signals_is_floor_of_lower_bound_times_target"
            ]
        )

    def test_money_and_outcomes_remain_disabled(self) -> None:
        safety = build_prereg()["safety"]
        self.assertFalse(safety["wallet_required"])
        self.assertFalse(safety["orders_enabled"])
        self.assertFalse(safety["paper_orders"])
        self.assertFalse(safety["outcomes_during_capture"])
        self.assertEqual(safety["real_money"], "BLOQUEADO")

    def test_freeze_is_exclusive_and_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prereg.json"
            freeze_prereg(path)
            self.assertEqual(
                load_and_verify_prereg(path)["status"],
                "FROZEN_CONFIRMATORY_SHADOW_ONLY",
            )
            with self.assertRaises(ConfirmatoryError):
                freeze_prereg(path)

    def test_missing_database_status_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = status(Path(temporary) / "missing.db")
        self.assertEqual(result["status"], "NOT_STARTED")
        self.assertEqual(result["cohort"], "BTC5M90_V002_CONFIRMATORY_24H")
        self.assertEqual(result["orders_sent"], 0)
        self.assertEqual(result["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
