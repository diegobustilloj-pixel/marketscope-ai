import tempfile
import unittest
import urllib.parse
from pathlib import Path

from polymarket_bot import sports_wallet_research as research


WALLET = "0x821dab0565ebf5b327f51db06223fdcfe01acf16"


class FakeClient:
    def __init__(self, callback):
        self.callback = callback
        self.requests = 0

    def get_json(self, url):
        self.requests += 1
        return self.callback(url)


class SportsWalletResearchTests(unittest.TestCase):
    def test_activity_adaptively_splits_before_offset_cap(self):
        def fetch(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            start = int(query["start"][0])
            end = int(query["end"][0])
            offset = int(query["offset"][0])
            limit = int(query["limit"][0])
            if end - start + 1 > 2:
                count = limit
            else:
                count = 1 if offset == 0 else 0
            return [
                {
                    "proxyWallet": WALLET,
                    "timestamp": start,
                    "type": "TRADE",
                    "transactionHash": f"0x{start}-{offset}-{i}",
                }
                for i in range(count)
            ]

        client = FakeClient(fetch)
        rows, pages, discarded = research._fetch_activity_window(
            client,
            WALLET,
            100,
            104,
            activity_types=("TRADE",),
            page_limit=2,
            max_offset=2,
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual([row["timestamp"] for row in rows], [100, 102])
        self.assertGreater(discarded, 0)
        self.assertEqual(sum(page.count for page in pages), 2)

    def test_sport_is_included_from_primary_tag(self):
        market = {
            "question": "Team A vs Team B",
            "sportsMarketType": "moneyline",
            "tags": [{"id": "200", "slug": "basketball"}],
            "events": [],
        }
        sports = [
            {
                "sport": "nba",
                "name": "NBA",
                "tags": "1,200",
                "primaryTagId": 200,
                "series": "10",
            }
        ]

        result = research.classify_market(market, sports)

        self.assertEqual(result["scope"], "SPORTS_INCLUDED")
        self.assertEqual(result["league"], "NBA")
        self.assertEqual(result["bet_family"], "moneyline_winner")

    def test_esports_is_separated_and_excluded(self):
        market = {
            "question": "Counter-Strike match",
            "sportsMarketType": "esports_match_winner",
            "tags": [{"id": "300", "slug": "esports"}],
            "events": [],
        }
        sports = [
            {
                "sport": "cs2",
                "name": "Counter-Strike 2",
                "tags": "1,300",
                "primaryTagId": 300,
                "series": "11",
            }
        ]

        result = research.classify_market(market, sports)

        self.assertEqual(result["scope"], "ESPORTS_EXCLUDED")
        self.assertTrue(result["is_esports"])

    def test_protocol_is_immutable_except_creation_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = research.prepare(root)
            second = research.prepare(root)

        self.assertEqual(first["status"], "CREATED")
        self.assertEqual(second["status"], "VERIFIED_EXISTING")

    def test_parallel_offset_pagination_finds_terminal_page(self):
        def fetch(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            offset = int(query["offset"][0])
            limit = int(query["limit"][0])
            total = 23
            return [{"index": i} for i in range(offset, min(offset + limit, total))]

        client = FakeClient(fetch)
        rows, pages, complete = research.paginate_offset_endpoint_parallel(
            client,
            "/closed-positions",
            {"user": WALLET},
            limit=5,
            max_offset=50,
            max_workers=3,
        )

        self.assertTrue(complete)
        self.assertEqual([row["index"] for row in rows], list(range(23)))
        self.assertEqual([page.count for page in pages], [5, 5, 5, 5, 3])


if __name__ == "__main__":
    unittest.main()
