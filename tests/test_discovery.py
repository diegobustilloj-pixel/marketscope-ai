import json
import unittest

from polymarket_bot.discovery import (
    candidate_btc_5m_slugs,
    parse_market_payload,
)


class DiscoveryTests(unittest.TestCase):
    def test_candidate_slugs_are_aligned_to_five_minutes(self) -> None:
        slugs = candidate_btc_5m_slugs(1_800_000_123)
        values = [int(slug.rsplit("-", 1)[1]) for slug in slugs]
        self.assertEqual(values[0] % 300, 0)
        self.assertIn(values[0] - 300, values)
        self.assertIn(values[0] + 300, values)

    def test_parse_event_market_json_strings(self) -> None:
        payload = {
            "id": "event-1",
            "slug": "btc-updown-5m-1800000000",
            "title": "Bitcoin Up or Down",
            "resolutionSource": "Chainlink BTC/USD",
            "markets": [
                {
                    "conditionId": "0xcondition",
                    "slug": "btc-updown-5m-1800000000",
                    "question": "Bitcoin Up or Down",
                    "outcomes": '["Up","Down"]',
                    "clobTokenIds": '["token-up","token-down"]',
                    "active": True,
                    "closed": False,
                    "acceptingOrders": True,
                }
            ],
        }
        raw = json.dumps(payload)
        market = parse_market_payload(payload, raw)
        self.assertEqual(market.condition_id, "0xcondition")
        self.assertEqual(market.token_by_outcome["Up"], "token-up")
        self.assertTrue(market.accepting_orders)
        self.assertEqual(market.payload_raw, raw)


if __name__ == "__main__":
    unittest.main()

