import unittest

from polymarket_bot.climate_shadow_monitor import best_decision, book_metrics, strict_contract


class ClimateShadowMonitorTests(unittest.TestCase):
    def test_book_metrics_calculate_depth_and_vwap(self) -> None:
        result = book_metrics(
            {
                "bids": [{"price": "0.39", "size": "10"}],
                "asks": [
                    {"price": "0.40", "size": "100"},
                    {"price": "0.42", "size": "200"},
                ],
            },
            requested_notional=50.0,
        )
        self.assertAlmostEqual(result["spread"], 0.01)
        self.assertAlmostEqual(result["ask_depth_1c_usdc"], 40.0)
        self.assertAlmostEqual(result["ask_depth_5c_usdc"], 124.0)
        self.assertAlmostEqual(result["executable_cost_100_usdc"], 50.0)
        self.assertAlmostEqual(result["vwap_100_usdc"], 50.0 / (100.0 + 10.0 / 0.42))

    def test_contract_requires_explicit_timezone_and_frequency(self) -> None:
        complete, reason = strict_contract(
            {
                "rules_complete": "True",
                "station_metadata_complete": "True",
                "timezone": "",
                "frequency": "OBSERVATION_TABLE_UNRESOLVED",
            }
        )
        self.assertFalse(complete)
        self.assertIn("TIMEZONE_EXPLICIT", reason)
        self.assertIn("OBSERVATION_FREQUENCY", reason)

    def test_high_edge_is_still_blocked_by_contract(self) -> None:
        decision = best_decision(
            {"A": 0.80, "B": 0.20},
            {
                ("A", "YES"): {"best_ask": 0.50, "ask_size": 20.0},
                ("A", "NO"): {"best_ask": 0.55, "ask_size": 20.0},
                ("B", "YES"): {"best_ask": 0.25, "ask_size": 20.0},
                ("B", "NO"): {"best_ask": 0.80, "ask_size": 20.0},
            },
            threshold=0.05,
            strict=False,
            contract_reason="TIMEZONE_EXPLICIT",
            lead_days=1,
        )
        self.assertEqual(decision["would_pass_gross_edge"], 1)
        self.assertEqual(decision["would_pass_edge"], 0)
        self.assertEqual(decision["decision"], "BLOCKED_CONTRACT")


if __name__ == "__main__":
    unittest.main()
