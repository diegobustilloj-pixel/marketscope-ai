import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket_bot import finalizer


class FinalizerTests(unittest.TestCase):
    def test_24h_analysis_waits_for_official_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "missing.json"
            with patch.object(finalizer, "ANALYSIS24_OUTPUT", output), patch.object(
                finalizer, "analyze_last_24h"
            ) as analyze:
                result = finalizer._analysis24_if_complete(
                    {"status": "WAITING_FORWARD_COMPLETION"}, mutate=True
                )

        self.assertEqual(result["status"], "WAITING_OFFICIAL_7D_AUDIT")
        self.assertEqual(result["partial_outcomes_read"], 0)
        analyze.assert_not_called()

    def test_incomplete_forward_never_runs_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "missing-audit.json"
            with patch.object(finalizer, "FORWARD_AUDIT", audit_path), patch.object(
                finalizer, "audit_shadow_forward"
            ) as audit:
                result = finalizer._audit_if_complete(
                    {"experiment_complete": False, "sqlite_quick_check": "ok"},
                    mutate=True,
                )

        self.assertEqual(result["status"], "WAITING_FORWARD_COMPLETION")
        audit.assert_not_called()

    def test_result_summary_excludes_trade_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(
                json.dumps(
                    {
                        "status": "PASS_V015_CONFIRMATION10",
                        "labels_read": 10,
                        "trades_detail": [{"condition_id": "x", "outcome": "UP"}],
                        "selected_stratum": "LOW",
                    }
                ),
                encoding="utf-8",
            )

            summary = finalizer._result_summary(path, "v015")

        self.assertNotIn("trades_detail", summary)
        self.assertNotIn("outcome", json.dumps(summary))

    def test_ready_v015_is_activated_once_by_reconcile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary_path = root / "summary.json"
            missing = root / "missing.json"
            states = [
                {"status": "READY_TO_ACTIVATE", "selected_stratum": "LOW"},
                {"status": "ACTIVE", "selected_stratum": "LOW"},
            ]
            with patch.multiple(
                finalizer,
                SUMMARY=summary_path,
                RESULT_V013=missing,
                RESULT_V014=missing,
                RESULT_V015=missing,
            ), patch.object(
                finalizer.v015, "activation_status", side_effect=states
            ), patch.object(
                finalizer.v015, "activate_if_ready", return_value={"selected_stratum": "LOW"}
            ) as activate, patch.object(
                finalizer,
                "_forward_state",
                return_value={"experiment_complete": False},
            ), patch.object(
                finalizer,
                "_audit_if_complete",
                return_value={"exists": False, "status": "WAITING_FORWARD_COMPLETION"},
            ):
                payload = finalizer.ExperimentFinalizer().reconcile(mutate=True)
                summary_written = summary_path.is_file()

        activate.assert_called_once_with()
        self.assertEqual(payload["v015_activation"]["status"], "ACTIVE")
        self.assertEqual(payload["raw_labels_or_outcomes_read_by_finalizer"], 0)
        self.assertTrue(summary_written)


if __name__ == "__main__":
    unittest.main()
