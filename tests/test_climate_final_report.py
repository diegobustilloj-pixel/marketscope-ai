import json
import unittest

from polymarket_bot.climate_final_report import contract_audit, normalized_prediction_metrics


class ClimateFinalReportTests(unittest.TestCase):
    def test_prediction_metrics_normalize_native_units_and_calculate_probabilistic_scores(self) -> None:
        rows = [
            {
                "market_type": "HIGHEST",
                "lead": "1",
                "candidate": "model",
                "target": "50",
                "point_prediction": "51.8",
                "unit": "°F",
                "winner_bucket": "A",
                "predicted_bucket": "A",
                "probabilities_json": json.dumps({"A": 0.75, "B": 0.25}),
            },
            {
                "market_type": "HIGHEST",
                "lead": "1",
                "candidate": "model",
                "target": "10",
                "point_prediction": "11",
                "unit": "°C",
                "winner_bucket": "B",
                "predicted_bucket": "A",
                "probabilities_json": json.dumps({"A": 0.75, "B": 0.25}),
            },
        ]

        result = normalized_prediction_metrics(rows)[0]

        self.assertAlmostEqual(result["mae_c"], 1.0)
        self.assertAlmostEqual(result["rmse_c"], 1.0)
        self.assertAlmostEqual(result["bucket_hit_rate"], 0.5)
        self.assertAlmostEqual(result["brier"], 0.625)

    def test_contract_gate_is_strict_about_timezone_and_frequency(self) -> None:
        rows = [
            {
                "rules_complete": "True",
                "station_metadata_complete": "True",
                "timezone": "",
                "frequency": "OBSERVATION_TABLE_UNRESOLVED",
            },
            {
                "rules_complete": "True",
                "station_metadata_complete": "True",
                "timezone": "Europe/London",
                "frequency": "HOURLY",
            },
        ]

        result = contract_audit(rows)

        self.assertEqual(result["legacy_core_complete"], 2)
        self.assertEqual(result["strict_operational_complete"], 1)


if __name__ == "__main__":
    unittest.main()
