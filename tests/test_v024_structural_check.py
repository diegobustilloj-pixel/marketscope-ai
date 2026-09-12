import unittest

from polymarket_bot.v024_structural_check import segment_metrics


class V024StructuralCheckTests(unittest.TestCase):
    def test_segments_are_fixed_and_partition_expected_diagnostics(self):
        rows = [
            {
                "favorite_side": "Up",
                "label": "Up",
                "favorite_probability": 0.55,
                "entry_cost": 0.58,
                "model_agrees": True,
            },
            {
                "favorite_side": "Down",
                "label": "Up",
                "favorite_probability": 0.65,
                "entry_cost": 0.68,
                "model_agrees": False,
            },
            {
                "favorite_side": "Up",
                "label": "Up",
                "favorite_probability": 0.95,
                "entry_cost": 0.93,
                "model_agrees": True,
            },
        ]
        metrics = segment_metrics(rows)
        self.assertEqual(metrics["favorite_cap_all"]["trades"], 2)
        self.assertEqual(metrics["favorite_cap_up"]["trades"], 1)
        self.assertEqual(metrics["favorite_cap_down"]["trades"], 1)
        self.assertEqual(metrics["favorite_cap_model_agrees"]["trades"], 1)
        self.assertEqual(metrics["favorite_cap_model_disagrees"]["trades"], 1)
        self.assertEqual(metrics["favorite_cap_cost_050_060"]["trades"], 1)
        self.assertEqual(metrics["favorite_cap_cost_060_070"]["trades"], 1)


if __name__ == "__main__":
    unittest.main()
