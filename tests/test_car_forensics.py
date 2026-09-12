from __future__ import annotations

import unittest

from polymarket_bot.car_forensics import _duration_bucket, _price_bucket, _profit_factor, _winner
from polymarket_bot.car_shadow_tracker import simulate_book_fill


class CarForensicsTests(unittest.TestCase):
    def test_price_buckets_cover_edges(self) -> None:
        self.assertEqual(_price_bucket(0.0), "0–5¢")
        self.assertEqual(_price_bucket(0.05), "5–10¢")
        self.assertEqual(_price_bucket(0.999), "98–100¢")

    def test_profit_factor(self) -> None:
        self.assertAlmostEqual(_profit_factor([10.0, -2.0, -3.0]), 2.0)
        self.assertIsNone(_profit_factor([1.0, 2.0]))

    def test_duration_bucket(self) -> None:
        self.assertEqual(_duration_bucket(59), "<1m")
        self.assertEqual(_duration_bucket(60), "1–5m")
        self.assertEqual(_duration_bucket(2_419_200), ">4w")

    def test_winner_requires_unique_terminal_price(self) -> None:
        self.assertEqual(_winner(["Yes", "No"], ["0", "1"]), "No")
        self.assertIsNone(_winner(["Yes", "No"], ["0.5", "0.5"]))

    def test_book_fill_walks_asks_for_buy(self) -> None:
        book = {"asks": [{"price": "0.40", "size": "2"}, {"price": "0.45", "size": "3"}]}
        result = simulate_book_fill(book, "BUY", 4)
        self.assertEqual(result["filled_shares"], 4)
        self.assertAlmostEqual(result["vwap"], 0.425)
        self.assertEqual(result["execution_rate"], 1.0)

    def test_book_fill_walks_bids_for_sell(self) -> None:
        book = {"bids": [{"price": "0.40", "size": "2"}, {"price": "0.45", "size": "3"}]}
        result = simulate_book_fill(book, "SELL", 4)
        self.assertAlmostEqual(result["vwap"], 0.4375)


if __name__ == "__main__":
    unittest.main()
