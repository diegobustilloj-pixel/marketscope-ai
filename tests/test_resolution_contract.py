from __future__ import annotations

import unittest

from polymarket_bot.resolution_contract import (
    resolution_twap_contract,
    twap_topic_for_window,
)


class ResolutionContractTests(unittest.TestCase):
    def test_explicit_30_second_url_is_verified(self) -> None:
        contract = resolution_twap_contract(
            "https://data.chain.link/streams/btc-usd-twap-30s-streams"
        )
        self.assertTrue(contract.verified)
        self.assertEqual(contract.window_seconds, 30)
        self.assertEqual(contract.topic, "crypto_prices_twap_thirty")

    def test_explicit_60_second_url_is_verified(self) -> None:
        contract = resolution_twap_contract(
            "https://data.chain.link/streams/btc-usd-twap-60s-streams"
        )
        self.assertTrue(contract.verified)
        self.assertEqual(contract.window_seconds, 60)
        self.assertEqual(contract.topic, "crypto_prices_twap_sixty")

    def test_topic_and_human_text_are_supported(self) -> None:
        self.assertEqual(
            resolution_twap_contract("crypto_prices_twap_thirty").window_seconds,
            30,
        )
        self.assertEqual(
            resolution_twap_contract("Chainlink BTC/USD TWAP: 60 seconds").window_seconds,
            60,
        )

    def test_missing_ambiguous_and_generic_sources_fail_closed(self) -> None:
        self.assertEqual(resolution_twap_contract(None).status, "MISSING")
        self.assertEqual(
            resolution_twap_contract("Chainlink BTC/USD").status,
            "UNSUPPORTED",
        )
        self.assertEqual(
            resolution_twap_contract(
                "btc-usd-twap-30s-streams and btc-usd-twap-60s-streams"
            ).status,
            "AMBIGUOUS",
        )

    def test_unsupported_topic_lookup_raises(self) -> None:
        with self.assertRaises(ValueError):
            twap_topic_for_window(45)


if __name__ == "__main__":
    unittest.main()
