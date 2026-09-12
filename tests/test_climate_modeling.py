import unittest

from polymarket_bot.climate_modeling import Event, Prediction, _prediction_metrics, _select, _selection_coverage


class ClimateModelSelectionTests(unittest.TestCase):
    def test_aggregate_error_normalizes_fahrenheit_to_celsius(self) -> None:
        buckets = (
            {"label": "LOW", "winner": True},
            {"label": "HIGH", "winner": False},
        )
        events = {
            "fahrenheit": Event(
                "fahrenheit", "KF", "2026-01-01", "HIGHEST", "°F", 1.0,
                "TEST", 50.0, "LOW", "F City", 1.0, buckets,
            ),
            "celsius": Event(
                "celsius", "KC", "2026-01-01", "HIGHEST", "°C", 1.0,
                "TEST", 10.0, "LOW", "C City", 1.0, buckets,
            ),
        }
        predictions = [
            Prediction("fahrenheit", 1, "model", 51.8, (0.8, 0.2), 1),
            Prediction("celsius", 1, "model", 11.0, (0.8, 0.2), 1),
        ]

        metrics = _prediction_metrics(predictions, events)

        self.assertAlmostEqual(metrics["mae_c"], 1.0)
        self.assertAlmostEqual(metrics["rmse_c"], 1.0)

    def test_unavailable_horizons_remain_no_evaluable(self) -> None:
        validation = [
            {
                "market_type": "HIGHEST",
                "lead": 1,
                "candidate": "model_a",
                "n": 30,
                "brier": 0.40,
                "log_loss": 0.80,
            },
            {
                "market_type": "HIGHEST",
                "lead": 1,
                "candidate": "model_b",
                "n": 30,
                "brier": 0.50,
                "log_loss": 0.70,
            },
            {
                "market_type": "LOWEST",
                "lead": 1,
                "candidate": "model_c",
                "n": 29,
                "brier": 0.30,
                "log_loss": 0.60,
            },
        ]

        selection = _select(validation)
        selected, unavailable = _selection_coverage(selection)

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["candidate"], "model_a")
        self.assertEqual(len(unavailable), 13)
        self.assertTrue(
            any(
                row["market_type"] == "LOWEST"
                and row["lead"] == 1
                and row["status"] == "NO_EVALUABLE"
                for row in unavailable
            )
        )


if __name__ == "__main__":
    unittest.main()
