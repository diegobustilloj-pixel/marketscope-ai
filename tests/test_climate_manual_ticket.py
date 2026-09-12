import unittest
from datetime import datetime, timezone

from polymarket_bot.climate_manual_ticket import select_ticket


class ClimateManualTicketTests(unittest.TestCase):
    def _report(self, *, gate: bool, deadline: str) -> dict:
        return {
            "profitability_gate": {"passed": gate, "verdict": "X", "reason": "X"},
            "highest_probability_observed": {"top_probability": 0.4},
            "primary_provisional_diagnostic": {
                "audit": [{
                    "reasons": "OUTCOME_OPEN_OR_AMBIGUOUS",
                    "entry_window_end": deadline,
                    "model_probability": 0.35,
                    "net_edge_after_fee": 0.08,
                }]
            },
        }

    def test_expired_candidate_is_no_entry(self) -> None:
        report = self._report(gate=True, deadline="2026-09-03T12:00:00Z")
        ticket = select_ticket(report, datetime(2026, 9, 3, 13, tzinfo=timezone.utc))
        self.assertEqual(ticket["decision"], "NO_ENTRY")

    def test_unproven_candidate_is_paper_only(self) -> None:
        report = self._report(gate=False, deadline="2026-09-03T14:00:00Z")
        ticket = select_ticket(report, datetime(2026, 9, 3, 13, tzinfo=timezone.utc))
        self.assertEqual(ticket["decision"], "PAPER_ONLY")
        self.assertFalse(ticket["real_money_allowed"])
