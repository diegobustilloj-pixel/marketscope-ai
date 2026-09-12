import unittest

from polymarket_bot import sports_wallet_copy as copy


class SportsWalletCopyTests(unittest.TestCase):
    def test_assign_event_splits_never_splits_one_event(self):
        signals = []
        for index in range(10):
            for market in range(2):
                signals.append(
                    {
                        "trader_key": "t",
                        "event_id": f"event-{index}",
                        "condition_id": f"condition-{index}-{market}",
                        "signal_timestamp": index,
                    }
                )

        copy.assign_event_splits(signals)

        by_event = {}
        for signal in signals:
            by_event.setdefault(signal["event_id"], set()).add(signal["split"])
        self.assertTrue(all(len(splits) == 1 for splits in by_event.values()))
        self.assertEqual({signal["split"] for signal in signals}, {"TRAIN", "VALIDATION", "TEST"})

    def test_next_print_uses_worst_buy_price_at_first_second(self):
        rows = [
            {"asset": "a", "timestamp": 11, "price": 0.51, "size": 20},
            {"asset": "a", "timestamp": 11, "price": 0.53, "size": 5},
            {"asset": "a", "timestamp": 12, "price": 0.60, "size": 100},
            {"asset": "b", "timestamp": 10, "price": 0.99, "size": 100},
        ]

        result = copy.select_next_print(rows, "a", 10, 70)

        self.assertEqual(result["print_timestamp"], 11)
        self.assertAlmostEqual(result["print_price"], 0.53)
        self.assertAlmostEqual(result["print_size"], 5)

    def test_taker_fee_matches_official_curve(self):
        fee = copy.taker_fee_usd(
            100,
            0.5,
            True,
            {"rate": 0.03, "exponent": 1, "takerOnly": True},
        )

        self.assertAlmostEqual(fee, 0.75)
        self.assertEqual(copy.taker_fee_usd(100, 0.5, False, None), 0)

    def test_simulation_caps_at_quarter_of_print_notional(self):
        opportunity = {
            "trader_key": "t",
            "signal_id": "s",
            "split": "TEST",
            "delay_seconds": 15,
            "available": True,
            "print_price": 0.5,
            "print_size": 40,
            "payout": 1,
            "fees_enabled": True,
            "fee_schedule": {"rate": 0.03},
        }

        result = copy.simulate_execution(opportunity, 100, 0)

        self.assertAlmostEqual(result["executed_stake_usd"], 5)
        self.assertTrue(result["capacity_limited"])
        self.assertAlmostEqual(result["fee_usd"], 0.075)
        self.assertAlmostEqual(result["net_pnl_usd"], 4.925)

    def test_unavailable_opportunity_is_not_filled(self):
        opportunity = {
            "trader_key": "t",
            "signal_id": "s",
            "split": "TEST",
            "delay_seconds": 15,
            "available": False,
        }

        result = copy.simulate_execution(opportunity, 100, 0.01)

        self.assertEqual(result["executed_stake_usd"], 0)
        self.assertEqual(result["net_pnl_usd"], 0)

    def test_candidate_selection_uses_per_opportunity_stake(self):
        rows = []
        for split, pnl in (("TRAIN", 10), ("VALIDATION", 5), ("TEST", 3)):
            rows.append(
                {
                    "trader_key": "flaznorp",
                    "split": split,
                    "delay_seconds": copy.PRIMARY_DELAY_SECONDS,
                    "requested_stake_usd": copy.PRIMARY_STAKE_USD,
                    "total_requested_stake_usd": 10_000,
                    "adverse_impact": copy.PRIMARY_ADVERSE_IMPACT,
                    "fills": 50,
                    "net_pnl_usd": pnl,
                    "roi_on_executed_cost": 0.05,
                    "profit_factor": 1.2,
                }
            )

        result = copy.select_copy_candidate(rows)

        self.assertIn("flaznorp", result["evaluations"])
        self.assertEqual(result["winner"], "flaznorp")
        self.assertTrue(result["test_confirmed"])


if __name__ == "__main__":
    unittest.main()
