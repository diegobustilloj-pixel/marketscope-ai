from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from polymarket_bot.resolution_contract import resolution_twap_contract
from polymarket_bot.v031_capture import (
    QUALITY_MISSING_TWAP,
    PathBook,
    V031CaptureState,
    V031Store,
    open_read_only,
)
from polymarket_bot.v031_prereg import (
    V031PreregistrationError,
    load_and_verify_frozen_prereg,
    validate_frozen_prereg_payload,
)


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "data" / "prereg_v031_path_execution_capture.json"


def _book(token: str, timestamp_ms: int, *, bid_base: float, ask_base: float) -> str:
    return json.dumps(
        {
            "event_type": "book",
            "market": "condition",
            "asset_id": token,
            "timestamp": str(timestamp_ms),
            "bids": [
                {"price": str(bid_base - index * 0.01), "size": str(index + 1)}
                for index in range(6)
            ],
            "asks": [
                {"price": str(ask_base + index * 0.01), "size": str(index + 2)}
                for index in range(6)
            ],
        }
    )


def _rtds(topic: str, timestamp_ms: int, value: float, *, window: int | None = None) -> str:
    payload = {
        "symbol": "btc/usd",
        "timestamp": timestamp_ms,
        "value": value,
    }
    if window is not None:
        payload["window_s"] = window
        payload["full_accuracy_value"] = str(int(value * 1e18))
    return json.dumps({"topic": topic, "payload": payload, "timestamp": timestamp_ms})


def _market(start_ms: int = 1_800_000_000_000) -> SimpleNamespace:
    return SimpleNamespace(
        condition_id="condition",
        slug=f"btc-updown-5m-{start_ms // 1000}",
        event_id="event",
        start_ms=start_ms,
        end_ms=start_ms + 300_000,
        up_token_id="up-token",
        down_token_id="down-token",
        resolution_source="https://data.chain.link/streams/btc-usd-twap-60s-streams",
    )


class V031CaptureTests(unittest.TestCase):
    def test_frozen_preregistration_is_exact_and_safe(self) -> None:
        payload = load_and_verify_frozen_prereg(PREREG, project_root=ROOT)
        self.assertEqual(payload["capture_contract"]["technical_pilot_hours"], 1.0)
        self.assertFalse(payload["data_policy"]["signals_generated"])
        self.assertFalse(payload["safety"]["orders_enabled"])
        self.assertEqual(payload["safety"]["real_money"], "BLOQUEADO")
        changed = copy.deepcopy(payload)
        changed["technical_gates"]["minimum_snapshot_coverage"] = 0.80
        with self.assertRaises(V031PreregistrationError):
            validate_frozen_prereg_payload(changed)

    def test_book_keeps_top_five_and_rejects_out_of_order_change(self) -> None:
        book = PathBook()
        payload = json.loads(_book("up-token", 1_000, bid_base=0.50, ask_base=0.51))
        self.assertTrue(
            book.replace(
                payload,
                source_timestamp_ms=1_000,
                received_timestamp_ms=1_010,
            )
        )
        self.assertEqual(len(book.top("bid")), 5)
        self.assertEqual(book.top("bid")[0], [0.5, 1.0])
        self.assertEqual(book.top("ask")[0], [0.51, 2.0])
        self.assertFalse(
            book.change(
                side="SELL",
                price=0.51,
                size=0.0,
                source_timestamp_ms=999,
                received_timestamp_ms=1_011,
            )
        )
        self.assertEqual(book.out_of_order_updates, 1)
        self.assertEqual(book.top("ask")[0], [0.51, 2.0])

    def test_snapshot_requires_fresh_chainlink_exact_twap_and_both_books(self) -> None:
        start = 1_800_000_000_000
        state = V031CaptureState()
        state.activate_market(_market(start))  # type: ignore[arg-type]
        state.ingest_rtds(
            _rtds("crypto_prices_chainlink", start + 1_000, 100_000.0),
            received_timestamp_ms=start + 1_010,
        )
        state.ingest_rtds(
            _rtds("crypto_prices_twap_sixty", start + 1_000, 99_999.0, window=60),
            received_timestamp_ms=start + 1_020,
        )
        state.ingest_clob(
            _book("up-token", start + 1_000, bid_base=0.48, ask_base=0.49),
            received_timestamp_ms=start + 1_030,
        )
        state.ingest_clob(
            _book("down-token", start + 1_000, bid_base=0.50, ask_base=0.51),
            received_timestamp_ms=start + 1_040,
        )
        snapshot = state.snapshot(
            condition_id="condition",
            second_offset=2,
            snapshot_timestamp_ms=start + 2_000,
            official_twap_window_s=60,
            recorded_timestamp_ms=start + 2_010,
        )
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["quality_flags"], 0)
        self.assertEqual(snapshot["official_twap_window_s"], 60)
        self.assertEqual(len(snapshot["up_ask_levels"]), 5)
        self.assertEqual(snapshot["up_ask_depth_top5"], 20.0)

        missing_exact_window = state.snapshot(
            condition_id="condition",
            second_offset=3,
            snapshot_timestamp_ms=start + 3_000,
            official_twap_window_s=30,
            recorded_timestamp_ms=start + 3_010,
        )
        self.assertFalse(missing_exact_window["complete"])
        self.assertTrue(missing_exact_window["quality_flags"] & QUALITY_MISSING_TWAP)

    def test_store_namespace_has_no_economic_columns_and_preserves_safety(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "capture.db"
            store = V031Store(database)
            store.open(
                preregistration_sha256="a" * 64,
                launch_manifest_sha256="b" * 64,
                now_timestamp=1_800_000_000,
            )
            meta = store.meta()
            self.assertFalse(meta["orders_enabled"])
            self.assertEqual(meta["outcomes_read"], 0)
            self.assertFalse(meta["pnl_calculated"])
            self.assertFalse(meta["signals_generated"])
            store.close()

            connection = open_read_only(database)
            try:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("v031_snapshots", tables)
                columns = {
                    str(row[1]).lower()
                    for table in ("v031_markets", "v031_snapshots")
                    for row in connection.execute(f"PRAGMA table_info([{table}])")
                }
            finally:
                connection.close()
            for forbidden in ("label", "outcome", "pnl", "signal", "order"):
                self.assertFalse(any(forbidden in column for column in columns))


if __name__ == "__main__":
    unittest.main()
