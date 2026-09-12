import unittest

from polymarket_bot.v026b_reconciliation import (
    reconciled_verdict,
    terminal_feed_shutdown_is_expected,
)


def _checkpoint(*, passed: bool = True) -> dict:
    return {
        "checkpoint_hour": 20.0,
        "technical": {
            "passed": passed,
            "latest_connections": {
                "shadow-rtds": "CONNECTED",
                "shadow-binance": "CONNECTED",
            },
        },
    }


class V026BReconciliationTests(unittest.TestCase):
    def test_completed_run_accepts_expected_terminal_cancellation(self):
        self.assertTrue(
            terminal_feed_shutdown_is_expected(
                completion_reason="FULL_24H_REACHED",
                run_rows=[{"status": "COMPLETED"}],
                final_connections={
                    "shadow-rtds": "CANCELLED",
                    "shadow-binance": "CANCELLED",
                },
                final_non_feed_gates={"sqlite": True, "coverage": True},
                checkpoints=[_checkpoint()],
            )
        )

    def test_unexpected_disconnect_remains_fail_closed(self):
        self.assertFalse(
            terminal_feed_shutdown_is_expected(
                completion_reason="FULL_24H_REACHED",
                run_rows=[{"status": "COMPLETED"}],
                final_connections={
                    "shadow-rtds": "DISCONNECTED",
                    "shadow-binance": "CANCELLED",
                },
                final_non_feed_gates={"sqlite": True},
                checkpoints=[_checkpoint()],
            )
        )

    def test_failed_last_checkpoint_cannot_be_reconciled(self):
        self.assertFalse(
            terminal_feed_shutdown_is_expected(
                completion_reason="FULL_24H_REACHED",
                run_rows=[{"status": "COMPLETED"}],
                final_connections={
                    "shadow-rtds": "CANCELLED",
                    "shadow-binance": "CANCELLED",
                },
                final_non_feed_gates={"sqlite": True},
                checkpoints=[_checkpoint(passed=False)],
            )
        )

    def test_valid_technical_evidence_exposes_economic_failure(self):
        result = {
            "safety_passed": True,
            "frequency_passed": True,
            "economic_replication_passed": False,
            "full_statistical_passed": False,
        }
        self.assertEqual(
            reconciled_verdict(result, technical_passed=True),
            "FAIL_REPLICATION",
        )


if __name__ == "__main__":
    unittest.main()
