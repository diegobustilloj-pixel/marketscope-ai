import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.phase3 import GOLD_DDL
from polymarket_bot.phase4 import build_phase4_models, phase4_status


INSERT_FEATURE = """
INSERT INTO gold_features(
    source_dataset,
    condition_id,
    horizon_seconds,
    slug,
    market_start_ms,
    market_end_ms,
    decision_offset,
    decision_timestamp_ms,
    split,
    label,
    y_up,
    has_official_strike,
    chainlink_price,
    distance_to_strike_bps,
    chainlink_return_5s_bps,
    chainlink_return_15s_bps,
    chainlink_return_30s_bps,
    chainlink_return_60s_bps,
    chainlink_return_120s_bps,
    chainlink_vol_15s_bps,
    chainlink_vol_30s_bps,
    chainlink_vol_60s_bps,
    chainlink_vol_120s_bps,
    chainlink_up_fraction_15s,
    chainlink_up_fraction_30s,
    chainlink_up_fraction_60s,
    chainlink_nonzero_fraction_60s,
    chainlink_state,
    chainlink_state_run_length,
    markov_p_up_60s,
    volatility_regime_ratio,
    up_best_bid,
    up_best_ask,
    up_mid,
    up_spread,
    down_best_bid,
    down_best_ask,
    down_mid,
    down_spread,
    implied_up_mid_probability,
    market_mid_sum,
    market_ask_overround,
    up_order_imbalance_1c,
    up_order_imbalance_5c,
    down_order_imbalance_1c,
    down_order_imbalance_5c,
    up_last_trade_price,
    down_last_trade_price,
    polymarket_trade_count_15s,
    polymarket_trade_volume_15s,
    polymarket_trade_count_60s,
    polymarket_trade_volume_60s,
    price_change_messages_15s,
    price_change_messages_60s,
    book_messages_15s,
    book_messages_60s,
    best_bid_ask_messages_15s,
    best_bid_ask_messages_60s,
    chainlink_updates_15s,
    chainlink_updates_60s,
    binance_trade_count_15s,
    binance_trade_count_60s,
    binance_trade_volume_15s,
    binance_trade_volume_60s,
    decision_quality_flags
) VALUES(
    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
)
"""


def _create_gold(
    path: Path,
    *,
    markets: int,
    predictive: bool,
) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(GOLD_DDL)
    connection.execute(
        "INSERT INTO gold_meta(key,value) VALUES(?,?)",
        ("schema_version", json.dumps("2")),
    )
    connection.execute(
        "INSERT INTO gold_meta(key,value) VALUES(?,?)",
        ("data_contract_sha256", json.dumps("synthetic-gold-v2")),
    )
    for market_index in range(markets):
        if market_index < int(markets * 0.60):
            split = "train"
        elif market_index < int(markets * 0.80):
            split = "validation"
        else:
            split = "test"
        y_up = market_index % 2
        label = "Up" if y_up else "Down"
        signal = (1.0 if y_up else -1.0) if predictive else 0.0
        ask = 0.49 if predictive else 0.90
        condition_id = f"condition-{market_index}"
        slug = f"btc-updown-5m-{1_800_000_000 + market_index * 300}"
        start_ms = (1_800_000_000 + market_index * 300) * 1000
        end_ms = start_ms + 300_000
        connection.execute(
            """
            INSERT INTO market_splits VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "synthetic",
                condition_id,
                slug,
                start_ms,
                end_ms,
                label,
                y_up,
                100_000.0,
                1,
                split,
                1.0,
                0.0,
            ),
        )
        for horizon in (90, 60, 30, 15):
            values = (
                "synthetic",
                condition_id,
                horizon,
                slug,
                start_ms,
                end_ms,
                300 - horizon,
                end_ms - horizon * 1000,
                split,
                label,
                y_up,
                1,
                100_000.0 + market_index,
                signal * 10,
                signal,
                signal * 2,
                signal * 3,
                signal * 4,
                signal * 5,
                0.5,
                0.6,
                0.7,
                0.8,
                0.8 if signal > 0 else 0.2,
                0.8 if signal > 0 else 0.2,
                0.8 if signal > 0 else 0.2,
                1.0,
                1 if signal > 0 else (-1 if signal < 0 else 0),
                4 if predictive else 1,
                0.8 if signal > 0 else (0.2 if signal < 0 else 0.5),
                0.9,
                0.48,
                ask,
                0.50,
                ask - 0.48,
                0.48,
                ask,
                0.50,
                ask - 0.48,
                0.50,
                1.0,
                ask * 2,
                signal * 0.1,
                signal * 0.1,
                -signal * 0.1,
                -signal * 0.1,
                0.50,
                0.50,
                4,
                10.0,
                12,
                30.0,
                8,
                20,
                2,
                5,
                4,
                12,
                15,
                60,
                0,
                0,
                0.0,
                0.0,
                0,
            )
            connection.execute(INSERT_FEATURE, values)
    connection.commit()
    connection.close()


class Phase4Tests(unittest.TestCase):
    def test_profitable_candidate_unlocks_test_without_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gold = root / "gold.db"
            output = root / "phase4.db"
            model = root / "selected.joblib"
            _create_gold(gold, markets=400, predictive=True)
            size_before = gold.stat().st_size

            result = build_phase4_models(
                gold_db=gold,
                output_db=output,
                model_file=model,
            )
            status = phase4_status(output)

            self.assertTrue(result["pipeline_passed"])
            self.assertTrue(result["strategy_selected_on_validation"])
            self.assertTrue(result["test_accessed"])
            self.assertTrue(result["forward_paper_candidate"])
            self.assertTrue(result["model_artifact_created"])
            self.assertTrue(model.is_file())
            self.assertFalse(result["orders_created"])
            self.assertFalse(result["wallet_required"])
            self.assertEqual(gold.stat().st_size, size_before)
            self.assertEqual(status["sqlite_quick_check"], "ok")
            self.assertTrue(status["test_accessed"])

    def test_failed_validation_keeps_test_locked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gold = root / "gold.db"
            output = root / "phase4.db"
            model = root / "selected.joblib"
            _create_gold(gold, markets=200, predictive=False)

            result = build_phase4_models(
                gold_db=gold,
                output_db=output,
                model_file=model,
            )

            self.assertTrue(result["pipeline_passed"])
            self.assertFalse(result["strategy_selected_on_validation"])
            self.assertFalse(result["test_accessed"])
            self.assertFalse(result["forward_paper_candidate"])
            self.assertFalse(result["model_artifact_created"])
            self.assertFalse(model.exists())
            self.assertTrue(output.is_file())
            with self.assertRaises(ValueError):
                build_phase4_models(
                    gold_db=gold,
                    output_db=output,
                    model_file=model,
                )


if __name__ == "__main__":
    unittest.main()
