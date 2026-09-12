from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from polymarket_bot.resolution_contract import resolution_twap_contract
from polymarket_bot.twap_contract_v095 import (
    TwapContractProbeStore,
    finalize_probe_status,
    probe_status,
)


class TwapContractV095Tests(unittest.TestCase):
    def test_store_preserves_both_windows_at_same_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "probe.db"
            store = TwapContractProbeStore(database)
            store.open(duration_seconds=1.0)
            contract = resolution_twap_contract(
                "https://data.chain.link/streams/btc-usd-twap-60s-streams"
            )
            store.save_market(
                slug="btc-updown-5m-1800000000",
                condition_id="condition",
                contract=contract,
            )
            timestamp = 1_800_000_000_000
            for window_s, word in ((30, "thirty"), (60, "sixty")):
                outcome = store.save_twap_message(
                    {
                        "topic": f"crypto_prices_twap_{word}",
                        "payload": {
                            "symbol": "btc/usd",
                            "timestamp": timestamp,
                            "window_s": window_s,
                            "value": 100000.0 + window_s,
                        },
                    }
                )
                self.assertEqual(outcome, "SAVED")
            self.assertEqual(
                store.save_twap_message(
                    {
                        "topic": "crypto_prices_twap_sixty",
                        "payload": {
                            "symbol": "btc/usd",
                            "timestamp": timestamp,
                            "window_s": 60,
                            "value": 100060.0,
                        },
                    }
                ),
                "DUPLICATE",
            )
            store.set_meta("topic_window_mismatches", 0)
            store.set_meta("finished_at", "2026-08-21T12:00:01+00:00")
            store.set_meta("counters", {})
            store.close()
            result = probe_status(database)
            self.assertEqual(result["twap_updates_by_window"], {"30": 1, "60": 1})
            self.assertEqual(result["selected_window_ticks"], 1)
            self.assertFalse(result["transfer_model_compatible"])
            self.assertTrue(result["retraining_required_for_selected_window"])
            self.assertTrue(result["technical_passed"])
            self.assertEqual(result["paper_orders"], 0)
            self.assertEqual(result["real_money"], "BLOQUEADO")
            output = Path(directory) / "result.json"
            finalized = finalize_probe_status(
                database=database,
                output_json=output,
            )
            self.assertTrue(output.is_file())
            self.assertTrue(finalized["technical_passed"])
            with self.assertRaises(ValueError):
                finalize_probe_status(database=database, output_json=output)

    def test_topic_window_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TwapContractProbeStore(Path(directory) / "probe.db")
            store.open(duration_seconds=1.0)
            outcome = store.save_twap_message(
                {
                    "topic": "crypto_prices_twap_thirty",
                    "payload": {
                        "symbol": "btc/usd",
                        "timestamp": 1,
                        "window_s": 60,
                        "value": 100000.0,
                    },
                }
            )
            self.assertEqual(outcome, "TOPIC_WINDOW_MISMATCH")
            store.close()


if __name__ == "__main__":
    unittest.main()
