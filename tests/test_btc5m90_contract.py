from __future__ import annotations

import json
import unittest

from polymarket_bot.btc_5m_90.contract import (
    ContractError,
    ThresholdDetector,
    validate_gamma_market,
)


def _payload() -> dict[str, object]:
    return {
        "id": "1",
        "conditionId": "0xabc",
        "slug": "btc-updown-5m-1788786600",
        # startDate es publicación, no inicio del intervalo y se ignora deliberadamente.
        "startDate": "2026-09-06T00:00:00Z",
        "endDate": "2026-09-07T13:15:00Z",
        "resolutionSource": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
        "description": (
            "This market resolves Up if the time-weighted average price is greater than "
            "or equal to the price at the beginning; otherwise Down."
        ),
        "outcomes": json.dumps(["Up", "Down"]),
        "outcomePrices": json.dumps(["1", "0"]),
        "clobTokenIds": json.dumps(["up-token", "down-token"]),
        "closed": True,
        "closedTime": "2026-09-07 13:15:50+00",
        "feesEnabled": True,
        "feeSchedule": {
            "exponent": 1,
            "rate": 0.07,
            "takerOnly": True,
            "rebateRate": 0.2,
        },
        "orderPriceMinTickSize": 0.001,
        "orderMinSize": 5,
    }


class ContractTests(unittest.TestCase):
    def test_contract_uses_slug_epoch_and_actual_fee(self) -> None:
        contract = validate_gamma_market(_payload())
        self.assertEqual(contract.market_end_ms - contract.market_start_ms, 300_000)
        self.assertEqual(contract.winner, "Up")
        self.assertAlmostEqual(contract.fee.fee_per_share(0.90), 0.0063)
        self.assertAlmostEqual(contract.fee.break_even_win_rate(0.90), 0.9063)

    def test_contract_fails_closed_on_wrong_resolution_source(self) -> None:
        payload = _payload()
        payload["resolutionSource"] = "https://data.chain.link/streams/btc-usd"
        with self.assertRaisesRegex(ContractError, "TWAP 60s"):
            validate_gamma_market(payload)

    def test_threshold_is_exact_not_literal_leq(self) -> None:
        detector = ThresholdDetector(0.90, 0.001)
        self.assertIsNone(detector.observe("c", 1, 0.50, 0.51))
        signal = detector.observe("c", 2, 0.90, 0.11)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "Up")
        self.assertIsNone(detector.observe("c", 3, 0.90, 0.11))

    def test_jump_over_threshold_is_not_false_fill(self) -> None:
        detector = ThresholdDetector(0.90, 0.001)
        self.assertIsNone(detector.observe("c", 1, 0.89, 0.12))
        self.assertIsNone(detector.observe("c", 2, 0.93, 0.08))

    def test_simultaneous_touch_is_ambiguous_and_locks_market(self) -> None:
        detector = ThresholdDetector(0.90, 0.001)
        signal = detector.observe("c", 1, 0.90, 0.90)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.status, "AMBIGUOUS")
        self.assertIsNone(signal.side)
        self.assertIsNone(detector.observe("c", 2, 0.90, 0.10))


if __name__ == "__main__":
    unittest.main()
