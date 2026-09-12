from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v045_census import (
    census_v045,
    classify_census,
    evaluate_event_books,
    executable_buy_cost,
    filter_event,
    validate_clob_market,
)
from polymarket_bot.v045_contract import (
    build_preregistration,
    frozen_contract,
    load_and_verify_preregistration,
)


PREREG = ROOT / "data" / "prereg_v045_neg_risk_structural_census.json"
RESULT = ROOT / "data" / "resultado_v045_neg_risk_structural_census.json"


def _market(index: int, *, ask: float = 0.3) -> dict[str, object]:
    return {
        "id": str(index),
        "conditionId": f"0x{index:064x}",
        "question": f"Outcome {index}?",
        "slug": f"outcome-{index}",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "negRisk": True,
        "negRiskOther": False,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([f"yes-{index}", f"no-{index}"]),
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True},
        "_ask": ask,
    }


def _event(prices: list[float]) -> dict[str, object]:
    return {
        "id": "event-1",
        "slug": "event-one",
        "title": "Event one",
        "active": True,
        "closed": False,
        "negRisk": True,
        "negRiskAugmented": False,
        "volume24hr": 1000,
        "markets": [_market(i + 1, ask=price) for i, price in enumerate(prices)],
    }


def _clob_info(market: dict[str, object]) -> dict[str, object]:
    token_ids = json.loads(str(market["clobTokenIds"]))
    return {
        "mos": 5,
        "mts": 0.001,
        "fd": {"r": 0.05, "e": 1, "to": True},
        "t": [
            {"t": token_ids[0], "o": "Yes"},
            {"t": token_ids[1], "o": "No"},
        ],
    }


class V045NegRiskCensusTests(unittest.TestCase):
    def test_low_comparable_coverage_fails_without_economic_rejection(self) -> None:
        classification = classify_census(
            eligible_events=10,
            evaluated_events=1,
            cost_candidates=0,
        )
        self.assertEqual(classification["verdict"], "FAIL_CENSUS_INSUFFICIENT_COMPARABLE_BOOKS")
        self.assertFalse(classification["economic_family_conclusion_allowed"])
        self.assertEqual(classification["minimum_evaluated_events"], 5)

    def test_fee_and_depth_are_calculated_per_level(self) -> None:
        result = executable_buy_cost(
            [{"price": "0.2", "size": "2"}, {"price": "0.3", "size": "3"}],
            shares=5,
            fee_rate=0.05,
            fee_exponent=1,
        )
        self.assertIsNotNone(result)
        assert result is not None
        expected_fee = 2 * 0.05 * 0.2 * 0.8 + 3 * 0.05 * 0.3 * 0.7
        self.assertAlmostEqual(result["fee"], expected_fee)
        self.assertIsNone(
            executable_buy_cost(
                [{"price": "0.2", "size": "4.9"}],
                shares=5,
                fee_rate=0.05,
                fee_exponent=1,
            )
        )

    def test_partial_or_augmented_event_is_rejected(self) -> None:
        contract = frozen_contract()
        augmented = _event([0.3, 0.3, 0.3])
        augmented["negRiskAugmented"] = True
        self.assertEqual(filter_event(augmented, contract)[1], "EVENT_AUGMENTED")
        partial = _event([0.3, 0.3, 0.3])
        partial["markets"][1]["closed"] = True  # type: ignore[index]
        self.assertEqual(filter_event(partial, contract)[1], "PARTIAL_EVENT_MARKET_NOT_OPEN")

    def test_fee_metadata_must_match(self) -> None:
        normalized, reason = filter_event(_event([0.3, 0.3, 0.3]), frozen_contract())
        self.assertIsNone(reason)
        assert normalized is not None
        raw_market = _event([0.3, 0.3, 0.3])["markets"][0]  # type: ignore[index]
        info = _clob_info(raw_market)
        info["fd"] = {"r": 0.04, "e": 1, "to": True}
        self.assertEqual(validate_clob_market(normalized["markets"][0], info, 5)[1], "FEE_RATE_MISMATCH")

    def test_candidate_remains_conditional_and_non_atomic(self) -> None:
        contract = frozen_contract()
        normalized, reason = filter_event(_event([0.2, 0.2, 0.2]), contract)
        self.assertIsNone(reason)
        assert normalized is not None
        validated = []
        raw_markets = _event([0.2, 0.2, 0.2])["markets"]  # type: ignore[index]
        for market, raw in zip(normalized["markets"], raw_markets):
            valid, why = validate_clob_market(market, _clob_info(raw), 5)
            self.assertIsNone(why)
            assert valid is not None
            validated.append(valid)
        normalized["markets"] = validated
        books = [
            {"asset_id": f"yes-{i}", "timestamp": "1000", "hash": str(i), "asks": [{"price": "0.2", "size": "5"}]}
            for i in range(1, 4)
        ]
        record, why = evaluate_event_books(normalized, books, contract)
        self.assertIsNone(why)
        assert record is not None
        self.assertTrue(record["cost_candidate"])
        self.assertFalse(record["semantic_exhaustiveness_verified"])
        self.assertFalse(record["executable_atomically"])

    def test_preregistration_and_offline_census_are_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prereg_path = Path(temporary) / "prereg.json"
            result_path = Path(temporary) / "result.json"
            build_preregistration(output_path=prereg_path, project_root=ROOT)
            event = _event([0.34, 0.34, 0.34])
            raw_markets = event["markets"]  # type: ignore[index]

            def fake_http(method: str, url: str, body: object | None) -> object:
                if "gamma-api" in url:
                    return [event]
                if "/clob-markets/" in url:
                    condition = url.rsplit("/", 1)[-1]
                    for raw in raw_markets:
                        if raw["conditionId"] == condition:
                            return _clob_info(raw)
                if url.endswith("/books"):
                    return [
                        {"asset_id": item["token_id"], "timestamp": "1000", "hash": "h", "asks": [{"price": "0.34", "size": "5"}]}
                        for item in body  # type: ignore[union-attr]
                    ]
                raise AssertionError(url)

            first = census_v045(
                prereg_path=prereg_path,
                result_path=result_path,
                project_root=ROOT,
                http_json=fake_http,
            )
            second = census_v045(
                prereg_path=prereg_path,
                result_path=result_path,
                project_root=ROOT,
                http_json=fake_http,
            )
        self.assertEqual(first, second)
        self.assertEqual(first["census"]["cost_candidates"], 0)
        self.assertEqual(first["safety"]["orders_created"], 0)

    def test_official_preregistration_and_result_preserve_safety(self) -> None:
        if not PREREG.is_file():
            self.skipTest("Se congela antes del censo oficial")
        prereg = load_and_verify_preregistration(PREREG, project_root=ROOT)
        self.assertFalse(prereg["safety"]["orders_enabled"])
        self.assertFalse(prereg["safety"]["wallet_required"])
        if RESULT.is_file():
            result = json.loads(RESULT.read_text(encoding="utf-8"))
            self.assertEqual(result["safety"]["orders_created"], 0)
            self.assertEqual(result["safety"]["paper_orders"], 0)
            self.assertFalse(result["safety"]["wallet_required"])
            self.assertEqual(result["safety"]["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
