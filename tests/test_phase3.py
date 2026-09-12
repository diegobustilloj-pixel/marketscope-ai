import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.phase2 import SILVER_DDL
from polymarket_bot.phase3 import build_gold_dataset, gold_status


def _create_silver(
    path: Path,
    *,
    source_schema_version: int,
    market_count: int,
    start_epoch: int,
) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(SILVER_DDL)
    connection.execute(
        "INSERT INTO silver_meta(key,value) VALUES(?,?)",
        ("schema_version", "1"),
    )
    connection.execute(
        "INSERT INTO silver_meta(key,value) VALUES(?,?)",
        ("source_schema_version", json.dumps(source_schema_version)),
    )
    connection.execute(
        "INSERT INTO silver_meta(key,value) VALUES(?,?)",
        ("training_eligible_markets", json.dumps(market_count)),
    )
    feature_placeholders = ",".join("?" for _ in range(39))
    for market_index in range(market_count):
        market_epoch = start_epoch + market_index * 300
        condition = f"condition-{source_schema_version}-{market_index}"
        label = "Up" if market_index % 2 == 0 else "Down"
        connection.execute(
            """
            INSERT INTO markets VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                condition,
                f"btc-updown-5m-{market_epoch}",
                f"event-{source_schema_version}-{market_index}",
                "Bitcoin Up or Down",
                market_epoch * 1000,
                (market_epoch + 300) * 1000,
                f"up-{source_schema_version}-{market_index}",
                f"down-{source_schema_version}-{market_index}",
                "Chainlink BTC/USD",
                label,
                "gamma_outcome_prices",
                1,
                f"gamma-{source_schema_version}-{market_index}",
                1,
                "resolved",
                100.0,
                "[1.0,0.0]" if label == "Up" else "[0.0,1.0]",
                "Up si final >= inicial; si no Down.",
                '{"closed":true}',
            ),
        )
        rows = []
        for second in range(300):
            chainlink = 100.0 + second * 0.001
            binance = (
                chainlink + 0.002
                if source_schema_version >= 4
                else None
            )
            up_mid = 0.45 + second * 0.0001
            down_mid = 1.0 - up_mid
            quality_flags = 0 if binance is not None else 8
            rows.append(
                (
                    condition,
                    second,
                    market_epoch * 1000 + second * 1000,
                    300 - second,
                    chainlink,
                    0,
                    binance,
                    0 if binance is not None else None,
                    up_mid - 0.01,
                    up_mid + 0.01,
                    up_mid,
                    0.02,
                    down_mid - 0.01,
                    down_mid + 0.01,
                    down_mid,
                    0.02,
                    100.0,
                    90.0,
                    200.0,
                    180.0,
                    95.0,
                    105.0,
                    190.0,
                    210.0,
                    up_mid,
                    2.0,
                    down_mid,
                    2.0,
                    1.0,
                    0.02,
                    1,
                    1,
                    1,
                    1,
                    2.0,
                    1,
                    1 if binance is not None else 0,
                    5.0 if binance is not None else 0.0,
                    quality_flags,
                )
            )
        connection.executemany(
            f"INSERT INTO second_features VALUES({feature_placeholders})",
            rows,
        )
    connection.commit()
    connection.close()


class Phase3Tests(unittest.TestCase):
    def test_build_gold_is_chronological_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            v3 = root / "silver-v3.db"
            v4 = root / "silver-v4.db"
            output = root / "gold.db"
            _create_silver(
                v3,
                source_schema_version=3,
                market_count=8,
                start_epoch=1_784_924_700,
            )
            _create_silver(
                v4,
                source_schema_version=4,
                market_count=4,
                start_epoch=1_784_927_100,
            )
            connection = sqlite3.connect(v3)
            connection.execute(
                """
                UPDATE markets
                SET price_to_beat=NULL
                WHERE condition_id='condition-3-0'
                """
            )
            connection.commit()
            connection.close()
            sizes_before = {"v3": v3.stat().st_size, "v4": v4.stat().st_size}

            result = build_gold_dataset(
                v3_silver_db=v3,
                v4_silver_db=v4,
                output_db=output,
            )
            status = gold_status(output)

            self.assertTrue(result["passed"])
            self.assertTrue(result["output_committed"])
            self.assertEqual(result["eligible_markets"], 12)
            self.assertTrue(result["eligible_count_matches_silver"])
            self.assertEqual(result["official_strike_markets"], 11)
            self.assertEqual(result["missing_strike_markets"], 1)
            self.assertEqual(result["feature_rows"], 48)
            self.assertEqual(
                result["feature_null_counts"]["price_to_beat"],
                4,
            )
            self.assertEqual(result["leakage_violations"], 0)
            self.assertTrue(result["split_order_valid"])
            self.assertEqual(
                {item["split"] for item in result["split_summary"]},
                {"train", "validation", "test"},
            )
            self.assertEqual(v3.stat().st_size, sizes_before["v3"])
            self.assertEqual(v4.stat().st_size, sizes_before["v4"])
            self.assertEqual(status["sqlite_quick_check"], "ok")
            self.assertEqual(status["feature_rows"], 48)
            self.assertEqual(status["official_strike_markets"], 11)
            self.assertEqual(status["missing_strike_markets"], 1)
            self.assertFalse(
                output.with_name(f"{output.name}.partial").exists()
            )


if __name__ == "__main__":
    unittest.main()
