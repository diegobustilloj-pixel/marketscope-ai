import unittest

from polymarket_bot.hedge_completion import extract_entry_record, matrix


BASE = {
    "candidate": "CAUTIOUS_030",
    "entry_cost_cap": 0.30,
    "pair_cost_cap": 0.97,
    "entry_window_second_offsets_inclusive": [30, 210],
    "hedge_deadline_second_offset_inclusive": 285,
    "order_size_shares": 5.0,
    "slippage_per_share": 0.005,
    "fee_rate": 0.07,
    "required_clear_quality_mask": 22,
}
FEATURES = [
    "entry_offset",
    "side_up",
    "entry_total_cost_per_share",
    "opposite_total_cost_per_share",
    "gap_to_pair_cap",
    "entry_spread",
    "opposite_spread",
    "log1p_entry_ask_depth_1c",
    "log1p_opposite_ask_depth_1c",
    "entry_ask_delta_5s",
    "entry_ask_delta_15s",
    "entry_ask_delta_30s",
    "opposite_ask_delta_5s",
    "opposite_ask_delta_15s",
    "opposite_ask_delta_30s",
    "entry_ask_range_30s",
    "opposite_ask_range_30s",
    "signed_chainlink_return_bps_from_open",
    "absolute_chainlink_return_bps_from_open",
    "market_mid_sum",
    "log1p_polymarket_trade_volume",
    "polymarket_trade_count",
    "book_messages",
    "price_change_messages",
]


def row(offset, up_ask, down_ask, chainlink):
    return {
        "second_offset": offset,
        "quality_flags": 8,
        "chainlink_price": chainlink,
        "up_best_bid": up_ask - 0.01,
        "up_best_ask": up_ask,
        "up_spread": 0.01,
        "up_ask_depth_1c": 10,
        "down_best_bid": down_ask - 0.01,
        "down_best_ask": down_ask,
        "down_spread": 0.01,
        "down_ask_depth_1c": 10,
        "market_mid_sum": 1.0,
        "polymarket_trade_count": 2,
        "polymarket_trade_volume": 10.0,
        "book_messages": 4,
        "price_change_messages": 3,
    }


class HedgeCompletionTests(unittest.TestCase):
    def test_features_use_entry_and_past_while_target_uses_future(self):
        rows = [row(i, 0.60, 0.42, 100.0 + i / 100) for i in range(31)]
        rows[-1] = row(30, 0.27, 0.72, 100.30)
        rows.append(row(31, 0.70, 0.58, 100.31))
        record = extract_entry_record(
            rows,
            condition_id="cid",
            market_start_ms=1,
            label="Up",
            base=BASE,
            feature_names=FEATURES,
        )
        self.assertIsNotNone(record)
        self.assertEqual(record["target_paired"], 1)
        self.assertEqual(record["trade"]["entry_offset"], 30)
        self.assertEqual(len(record["features"]), len(FEATURES))
        self.assertEqual(matrix([record]).shape, (1, len(FEATURES)))

    def test_no_entry_produces_no_record(self):
        rows = [row(i, 0.60, 0.42, 100.0) for i in range(40)]
        record = extract_entry_record(
            rows,
            condition_id="cid",
            market_start_ms=1,
            label="Down",
            base=BASE,
            feature_names=FEATURES,
        )
        self.assertIsNone(record)


if __name__ == "__main__":
    unittest.main()
