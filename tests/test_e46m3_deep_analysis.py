from __future__ import annotations

import unittest

from polymarket_bot.e46m3_deep_analysis import _gap_stats


class E46m3DeepAnalysisTests(unittest.TestCase):
    def test_gap_stats_are_reproducible(self) -> None:
        result = _gap_stats([0, 1, 5, 60, 300, 301])
        self.assertEqual(result["observations"], 6)
        self.assertEqual(result["median_seconds"], 5.0)
        self.assertAlmostEqual(result["fraction_le_60s"], 4 / 6)

    def test_gap_stats_ignore_negative_gaps(self) -> None:
        self.assertEqual(_gap_stats([-1, -10]), {"observations": 0})


if __name__ == "__main__":
    unittest.main()
