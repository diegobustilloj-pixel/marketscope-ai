import io
import json
import tempfile
import unittest
import urllib.parse
import zipfile
from pathlib import Path

from polymarket_bot import polyledger


WALLET = "0x1111111111111111111111111111111111111111"


def activity(
    timestamp,
    event_type,
    *,
    side="",
    asset="asset-a",
    size=0,
    price=0,
    usdc=0,
    tx=None,
):
    return {
        "proxyWallet": WALLET,
        "timestamp": timestamp,
        "type": event_type,
        "side": side,
        "asset": asset,
        "size": size,
        "price": price,
        "usdcSize": usdc,
        "transactionHash": tx or f"0x{timestamp}{event_type}{side}",
        "conditionId": "0x" + "2" * 64,
        "outcomeIndex": 0,
        "outcome": "YES",
        "title": "Mercado de prueba",
    }


class FakeClient:
    def __init__(self, callback):
        self.callback = callback
        self.requests = 0

    def get_json(self, url):
        self.requests += 1
        return self.callback(url)


class PolyLedgerTests(unittest.TestCase):
    def test_wallet_validation(self):
        self.assertEqual(polyledger.validate_wallet(WALLET.upper().replace("0X", "0x")), WALLET)
        with self.assertRaises(ValueError):
            polyledger.validate_wallet("0x123")

    def test_event_identity_ignores_mutable_profile(self):
        first = activity(10, "TRADE", side="BUY", size=10, price=0.2, usdc=2)
        second = {**first, "name": "nuevo nombre", "bio": "cambió"}
        self.assertEqual(polyledger.event_identity(first), polyledger.event_identity(second))

    def test_event_semantics_do_not_call_internal_movements_pnl(self):
        split = polyledger.normalize_event(activity(10, "SPLIT", usdc=50))
        redeem = polyledger.normalize_event(activity(11, "REDEEM", usdc=80))
        reward = polyledger.normalize_event(activity(12, "MAKER_REBATE", usdc=3))
        self.assertEqual(split["cash_delta_usd"], -50)
        self.assertEqual(split["economic_bucket"], "internal_conversion")
        self.assertFalse(split["pnl_directly_known"])
        self.assertFalse(redeem["pnl_directly_known"])
        self.assertTrue(reward["pnl_directly_known"])

    def test_fifo_is_matched_by_asset_and_flags_unmatched_sells(self):
        rows = [
            polyledger.normalize_event(activity(1, "TRADE", side="BUY", size=10, price=0.2, usdc=2)),
            polyledger.normalize_event(activity(2, "TRADE", side="SELL", size=6, price=0.5, usdc=3)),
            polyledger.normalize_event(activity(3, "TRADE", side="SELL", size=5, price=0.6, usdc=3)),
        ]
        result = polyledger.fifo_trade_analysis(rows)
        self.assertAlmostEqual(result["matched_realized_pnl_usd"], 3.4)
        self.assertAlmostEqual(result["matched_sell_shares"], 10)
        self.assertAlmostEqual(result["unmatched_sell_shares"], 1)
        self.assertFalse(result["is_complete_pnl"])

    def test_store_deduplicates_across_runs(self):
        row = activity(1, "TRADE", side="BUY", size=10, price=0.2, usdc=2)
        with tempfile.TemporaryDirectory() as directory:
            store = polyledger.LedgerStore(Path(directory) / "ledger.db")
            self.assertEqual(store.upsert_events([row]), (1, 1))
            self.assertEqual(store.upsert_events([{**row, "name": "updated"}]), (1, 0))
            self.assertEqual(len(store.events(WALLET)), 1)
            self.assertEqual(store.integrity(), "ok")
            store.close()

    def test_activity_window_splits_at_offset_cap(self):
        def fetch(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            start = int(query["start"][0])
            end = int(query["end"][0])
            offset = int(query["offset"][0])
            limit = int(query["limit"][0])
            if end > start:
                count = limit
            else:
                count = 1 if offset == 0 else 0
            return [activity(start, "TRADE", side="BUY", tx=f"0x{start}-{offset}-{i}") for i in range(count)]

        rows, pages = polyledger.fetch_activity_window(
            FakeClient(fetch), WALLET, 100, 101, limit=2, max_offset=2
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["timestamp"] for row in rows], [100, 101])
        self.assertTrue(pages)

    def test_activity_range_has_no_boundary_duplicates(self):
        def fetch(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            start = int(query["start"][0])
            end = int(query["end"][0])
            offset = int(query["offset"][0])
            if offset:
                return []
            return [activity(second, "TRADE", side="BUY", tx=f"0x{second}") for second in range(start, end + 1)]

        rows, _ = polyledger.fetch_activity_range(
            FakeClient(fetch), WALLET, 100, 104, window_seconds=2, workers=2
        )
        self.assertEqual([row["timestamp"] for row in rows], [100, 101, 102, 103, 104])

    def test_parallel_offset_pagination_stops_at_short_page(self):
        def fetch(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            offset = int(query["offset"][0])
            limit = int(query["limit"][0])
            return [
                {"proxyWallet": WALLET, "index": index}
                for index in range(offset, min(offset + limit, 13))
            ]

        rows, pages, complete = polyledger.fetch_offset_pages(
            FakeClient(fetch),
            "/closed-positions",
            {"user": WALLET},
            wallet=WALLET,
            limit=5,
            max_offset=50,
            workers=3,
        )
        self.assertTrue(complete)
        self.assertEqual([row["index"] for row in rows], list(range(13)))
        self.assertEqual([page.count for page in pages], [5, 5, 3])

    def test_positions_join_ascending_and_descending_slices(self):
        total = 40

        def fetch(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            offset = int(query["offset"][0])
            limit = int(query["limit"][0])
            direction = query["sortDirection"][0]
            threshold = float(query.get("sizeThreshold", [0])[0])
            ordered = [index for index in range(total) if index + 1 >= threshold]
            if direction == "DESC":
                ordered.reverse()
            selected = ordered[offset : offset + limit]
            return [
                {
                    "proxyWallet": WALLET,
                    "conditionId": "0x" + f"{index:064x}",
                    "asset": str(index),
                    "outcomeIndex": 0,
                    "size": index + 1,
                }
                for index in selected
            ]

        rows, _, complete = polyledger.fetch_all_positions(
            FakeClient(fetch),
            WALLET,
            limit=5,
            max_offset=10,
            workers=2,
        )
        self.assertTrue(complete)
        self.assertEqual(len(rows), total)

    def test_analysis_separates_official_sources_and_flags_mismatch(self):
        events = [
            polyledger.normalize_event(activity(1, "TRADE", side="BUY", size=10, price=0.2, usdc=2)),
            polyledger.normalize_event(activity(2, "REDEEM", size=10, usdc=10)),
            polyledger.normalize_event(activity(3, "REWARD", usdc=1)),
        ]
        report = polyledger.analyze_ledger(
            wallet=WALLET,
            events=events,
            positions=[{"proxyWallet": WALLET, "currentValue": 10, "cashPnl": 2, "realizedPnl": 1}],
            closed_positions=[{"proxyWallet": WALLET, "realizedPnl": 5, "title": "A", "totalBought": 3}],
            value_rows=[{"user": WALLET, "value": 9}],
            leaderboard_rows=[{"proxyWallet": WALLET, "pnl": 20, "vol": 100}],
            profile={},
            sync={},
        )
        codes = {row["code"] for row in report["discrepancies"]}
        self.assertIn("OPEN_VALUE_MISMATCH", codes)
        self.assertIn("PNL_DEFINITION_OR_TIMING_MISMATCH", codes)
        self.assertEqual(report["ledger_metrics"]["explicit_rewards_usd"], 1)
        self.assertIn("flujo, no PnL", polyledger.render_markdown(report))

    def test_accounting_snapshot_accepts_only_expected_csv_files(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("positions.csv", "asset,value\na,1\n")
            archive.writestr("equity.csv", "timestamp,equity\n1,2\n")
        result = polyledger.accounting_snapshot_summary(buffer.getvalue())
        self.assertTrue(result["captured"])
        self.assertEqual({row["name"] for row in result["files"]}, {"positions.csv", "equity.csv"})

    def test_end_to_end_runner_writes_report_and_csv_without_trading(self):
        row = activity(1, "TRADE", side="BUY", size=2, price=0.25, usdc=0.5)

        def fetch(url):
            parts = urllib.parse.urlsplit(url)
            if parts.path == "/activity":
                return [row]
            if parts.path in {"/positions", "/closed-positions"}:
                return []
            if parts.path == "/value":
                return [{"user": WALLET, "value": 0}]
            if parts.path == "/v1/leaderboard":
                return [{"proxyWallet": WALLET, "pnl": 0, "vol": 0}]
            if parts.path == "/public-profile":
                return {"proxyWallet": WALLET, "name": "Test"}
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = polyledger.run_sentinel(
                wallet=WALLET,
                database=root / "ledger.db",
                output_json=root / "report.json",
                output_markdown=root / "report.md",
                client=FakeClient(fetch),
                include_accounting_snapshot=False,
            )
            self.assertTrue((root / "report.json").exists())
            self.assertTrue((root / "report.md").exists())
            self.assertTrue((root / "report_open_positions.csv").exists())
            self.assertTrue((root / "report_closed_positions.csv").exists())
            self.assertTrue(report["safety"]["read_only"])
            self.assertFalse(report["safety"]["trading_enabled"])


if __name__ == "__main__":
    unittest.main()
