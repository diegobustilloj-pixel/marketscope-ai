from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v043_audit import audit_v043
from polymarket_bot.v043_chainlink_outage_design import (
    DECISION,
    analyze_v042_outage,
)
from polymarket_bot.v043_contract import PASS_VERDICT, load_and_verify_prereg
from polymarket_bot.v043_runner import load_and_verify_implementation


PREREG = ROOT / "data" / "prereg_v043_chainlink_outage_fail_closed_audit.json"
IMPLEMENTATION = (
    ROOT / "data" / "implementation_v043_chainlink_outage_fail_closed_audit.json"
)
DIAGNOSTIC = ROOT / "data" / "diagnostico_v043_chainlink_outage_fail_closed.json"
V042_DATABASE = ROOT / "data" / "capture_v042_planned_chainlink_reconnect_4h.db"
V042_RESULT = ROOT / "data" / "resultado_v042_planned_chainlink_reconnect_4h.json"
V042_PREREG = ROOT / "data" / "prereg_v042_planned_chainlink_reconnect_4h.json"


class V043ChainlinkOutageFailClosedTests(unittest.TestCase):
    def test_prereg_freezes_closed_read_only_scope_and_safety(self) -> None:
        prereg = load_and_verify_prereg(PREREG, project_root=ROOT)
        self.assertFalse(prereg["scope"]["fresh_forward_capture"])
        self.assertTrue(prereg["scope"]["closed_incident_audit"])
        self.assertFalse(prereg["scope"]["provider_liveness_can_be_claimed"])
        self.assertEqual(prereg["execution"]["fresh_capture_hours"], 0.0)
        self.assertFalse(prereg["execution"]["scheduled_supervision"])
        self.assertEqual(prereg["data_policy"]["outcomes_read"], 0)
        self.assertFalse(prereg["data_policy"]["prices_stored"])
        self.assertFalse(prereg["data_policy"]["pnl_calculated"])
        self.assertFalse(prereg["safety"]["orders_enabled"])
        self.assertFalse(prereg["safety"]["wallet_required"])
        self.assertEqual(prereg["safety"]["real_money"], "BLOQUEADO")

    def test_diagnostic_preserves_v042_failure_and_passes_controlled_gates(self) -> None:
        diagnostic = json.loads(DIAGNOSTIC.read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["decision"], DECISION)
        self.assertTrue(diagnostic["all_bot_controlled_gates_passed"])
        self.assertEqual(diagnostic["v042"]["verdict"], "FAIL_CHAINLINK_LIVENESS")
        self.assertEqual(
            diagnostic["v042"]["failed_technical_gates"],
            ["maximum_chainlink_stale_streak_seconds_passed"],
        )
        self.assertFalse(
            diagnostic["classification"]["provider_liveness_within_twenty_seconds"]
        )
        self.assertTrue(
            diagnostic["classification"][
                "coincident_reference_feed_staleness_observed"
            ]
        )

    def test_read_only_reanalysis_reproduces_outage_metrics(self) -> None:
        analysis = analyze_v042_outage(
            database=V042_DATABASE,
            result=V042_RESULT,
            v042_preregistration=V042_PREREG,
        )
        self.assertEqual(analysis["outage"]["stale_runs"], 18)
        self.assertEqual(analysis["outage"]["longest"]["seconds"], 31)
        self.assertEqual(
            analysis["outage"]["longest_run_joint_official_twap_stale_seconds"],
            31,
        )
        self.assertEqual(analysis["outage"]["watchdog_stale_events"], 7)
        self.assertEqual(analysis["outage"]["watchdog_reconnects"], 7)
        self.assertEqual(
            analysis["outage"]["watchdog_reconnect_accounting_rate"], 1.0
        )

    def test_stale_reference_is_fail_closed_for_every_capacity_entry(self) -> None:
        diagnostic = json.loads(DIAGNOSTIC.read_text(encoding="utf-8"))
        fail_closed = diagnostic["fail_closed_entry"]
        self.assertEqual(fail_closed["stale_decision_capacity_probes_blocked"], 124)
        self.assertEqual(fail_closed["capacity_entry_violations"], [])

    def test_positions_exit_without_chainlink_during_exercised_outage(self) -> None:
        diagnostic = json.loads(DIAGNOSTIC.read_text(encoding="utf-8"))
        exits = diagnostic["chainlink_independent_exit"]
        self.assertEqual(exits["capacity_positions_open_during_any_stale_run"], 312)
        self.assertEqual(exits["capacity_positions_exited"], 312)
        self.assertEqual(exits["exit_success_rate"], 1.0)
        self.assertEqual(exits["exits_completed_while_chainlink_stale"], 83)
        self.assertEqual(exits["trapped_positions"], 0)
        self.assertLessEqual(exits["maximum_exit_delay_seconds"], 10)

    def test_sealed_implementation_and_terminal_auditor(self) -> None:
        if not IMPLEMENTATION.is_file():
            self.skipTest("El manifiesto se sella despues de la primera pasada")
        implementation = load_and_verify_implementation(
            IMPLEMENTATION, project_root=ROOT
        )
        self.assertFalse(implementation["fresh_capture_built"])
        self.assertFalse(implementation["provider_liveness_claim_built"])
        self.assertFalse(implementation["economic_strategy_built"])
        with tempfile.TemporaryDirectory() as temporary:
            result_path = Path(temporary) / "result.json"
            result = audit_v043(
                prereg_path=PREREG,
                implementation_path=IMPLEMENTATION,
                result_path=result_path,
                project_root=ROOT,
            )
            repeated = audit_v043(
                prereg_path=PREREG,
                implementation_path=IMPLEMENTATION,
                result_path=result_path,
                project_root=ROOT,
            )
        self.assertEqual(result, repeated)
        self.assertEqual(result["verdict"], PASS_VERDICT)
        self.assertTrue(result["technical_passed"])
        self.assertTrue(result["safety_passed"])
        self.assertFalse(
            result["v042_official_result"]["provider_liveness_claimed_by_v043"]
        )
        self.assertFalse(result["promotion"]["repeat_v042_unchanged"])
        self.assertFalse(result["promotion"]["paper_or_money_candidate"])

    def test_status_never_exposes_run_or_monitor_action(self) -> None:
        command = [
            str(ROOT / ".venv" / "Scripts" / "python.exe"),
            str(ROOT / "v043_monitor.py"),
            "--status",
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        status = json.loads(completed.stdout)
        self.assertEqual(status["fresh_capture_hours"], 0.0)
        self.assertFalse(status["scheduled_supervision"])
        self.assertEqual(status["orders_created"], 0)
        self.assertEqual(status["paper_orders"], 0)
        self.assertFalse(status["wallet_required"])
        self.assertEqual(status["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
