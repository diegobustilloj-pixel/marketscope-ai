import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from polymarket_bot import wallet_window


WALLET = "0xbf337426aa856996b8bb79b238345dd1a0276bf7"


class WalletWindowTests(unittest.TestCase):
    def test_identity_requires_leaderboard_and_profile_match(self):
        def fetch(url):
            if "/v1/leaderboard" in url:
                return [{"userName": "nagi777", "proxyWallet": WALLET}]
            return {
                "name": "nagi777",
                "proxyWallet": WALLET,
                "pseudonym": "Alarmed-Hide",
                "xUsername": "Nagi__777__",
            }

        identity = wallet_window.resolve_public_identity(
            "nagi777", fetch_json=fetch
        )

        self.assertEqual(identity["proxy_wallet"], WALLET)
        self.assertEqual(identity["x_username"], "Nagi__777__")

    def test_identity_rejects_profile_mismatch(self):
        def fetch(url):
            if "/v1/leaderboard" in url:
                return [{"userName": "nagi777", "proxyWallet": WALLET}]
            return {"name": "other", "proxyWallet": WALLET}

        with self.assertRaisesRegex(wallet_window.WalletWindowError, "username"):
            wallet_window.resolve_public_identity("nagi777", fetch_json=fetch)

    def test_activity_is_partitioned_and_paginated(self):
        seen = []

        def fetch(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            start = int(query["start"][0])
            end = int(query["end"][0])
            offset = int(query["offset"][0])
            seen.append((start, end, offset))
            if offset:
                return []
            return [
                {
                    "proxyWallet": WALLET,
                    "timestamp": start,
                    "type": "TRADE",
                    "transactionHash": f"0x{start}",
                    "asset": "1",
                    "side": "BUY",
                    "outcome": "Up",
                    "price": 0.4,
                    "size": 5,
                }
            ]

        rows, pages = wallet_window.fetch_activity_window(
            WALLET,
            1_000,
            1_100,
            fetch_json=fetch,
            segment_seconds=50,
            page_limit=1,
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(len(pages), 4)
        self.assertEqual(seen, [(1000, 1049, 0), (1000, 1049, 1), (1050, 1099, 0), (1050, 1099, 1)])

    def test_prereg_rejects_more_than_24_hours(self):
        payload = {
            "schema": wallet_window.PREREG_SCHEMA,
            "analysis_module_sha256": wallet_window.sha256_file(
                wallet_window.__file__
            ),
            "username": "nagi777",
            "proxy_wallet": WALLET,
            "window_start_unix": 0,
            "window_end_exclusive_unix": 86_401,
            "window_hours": 24.000277,
            "safety": {},
        }

        with self.assertRaisesRegex(wallet_window.WalletWindowError, "24 horas"):
            wallet_window.validate_preregistration(payload)

    def test_analysis_reconstructs_pair_and_residual_without_pnl(self):
        rows = [
            self._row(1000, "Up", 10, 0.40, "a"),
            self._row(1001, "Down", 8, 0.55, "b"),
            self._row(1002, "Up", 2, 0.45, "c"),
        ]
        snapshot = {
            "schema": wallet_window.SNAPSHOT_SCHEMA,
            "captured_at": "2026-08-14T00:00:00+00:00",
            "identity": {"username": "nagi777", "proxy_wallet": WALLET},
            "window": {
                "start_unix": 0,
                "end_exclusive_unix": 86_400,
                "hours": 24.0,
            },
            "activities": rows,
            "safety": {"market_outcomes_read": False},
        }

        result = wallet_window.analyze_snapshot(snapshot)
        market = result["market_summaries"][0]

        self.assertEqual(result["verdict"], "DESCRIPTIVE_ONLY")
        self.assertEqual(market["pairable_gross_buy_shares"], 8)
        self.assertEqual(market["directional_residual_side"], "Up")
        self.assertEqual(market["directional_residual_shares"], 4)
        self.assertNotIn("pnl", result["metrics"])
        self.assertNotIn("pnl", market)

    def test_write_is_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            first = wallet_window.write_json_exclusive_or_verify(path, {"a": 1})
            second = wallet_window.write_json_exclusive_or_verify(path, {"a": 1})
            with self.assertRaises(wallet_window.WalletWindowError):
                wallet_window.write_json_exclusive_or_verify(path, {"a": 2})

        self.assertEqual(first, "CREATED")
        self.assertEqual(second, "VERIFIED_EXISTING")

    @staticmethod
    def _row(timestamp, outcome, size, price, tx):
        return {
            "proxyWallet": WALLET,
            "timestamp": timestamp,
            "type": "TRADE",
            "conditionId": "0xcondition",
            "transactionHash": tx,
            "asset": outcome,
            "side": "BUY",
            "outcome": outcome,
            "price": price,
            "size": size,
            "usdcSize": size * price,
            "slug": "btc-updown-5m-1786646400",
        }


if __name__ == "__main__":
    unittest.main()
