from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v054_combo_public_census import (
    V054CensusError,
    census_v054,
    collect_combo_catalog,
    normalize_combo_market,
)
from polymarket_bot.v054_contract import (
    build_preregistration,
    frozen_contract,
    load_and_verify_preregistration,
)


PREREG = ROOT / "data" / "prereg_v054_public_combo_observability_census.json"
RESULT = ROOT / "data" / "resultado_v054_public_combo_observability_census.json"


def _market(index: int, yes_price: float = 0.6) -> dict[str, object]:
    return {
        "id": str(index),
        "condition_id": f"0x{index:064x}",
        "position_ids": [str(index * 2), str(index * 2 + 1)],
        "slug": f"market-{index}",
        "title": f"Will example {index} happen?",
        "outcomes": ["Yes", "No"],
        "outcome_prices": [str(yes_price), str(1.0 - yes_price)],
        "image": "https://example.test/image.png",
        "volume": 1000.0 - index,
        "tags": ["test", "binary"],
    }


class V054ComboPublicCensusTests(unittest.TestCase):
    def test_normalization_preserves_documented_index_mapping(self) -> None:
        normalized, reason = normalize_combo_market(_market(1), frozen_contract())
        self.assertIsNone(reason)
        self.assertEqual(normalized["outcomes"], ["Yes", "No"])
        self.assertEqual(normalized["position_ids"], ["2", "3"])
        self.assertEqual(normalized["outcome_prices"], [0.6, 0.4])

    def test_invalid_binary_mapping_fails_closed(self) -> None:
        market = _market(1)
        market["outcomes"] = ["No", "Yes"]
        normalized, reason = normalize_combo_market(market, frozen_contract())
        self.assertIsNone(normalized)
        self.assertEqual(reason, "OUTCOMES_NOT_EXACT_YES_NO")

    def test_cursor_is_passed_unchanged_and_only_get_is_used(self) -> None:
        responses = [
            {"markets": [_market(1)], "next_cursor": "opaque+/="},
            {"markets": [_market(2)], "next_cursor": None},
        ]
        calls: list[tuple[str, str, object | None]] = []

        def fake_http(method: str, url: str, body: object | None) -> object:
            calls.append((method, url, body))
            return responses[len(calls) - 1]

        markets, diagnostics = collect_combo_catalog(fake_http, frozen_contract())
        self.assertEqual(len(markets), 2)
        self.assertTrue(diagnostics["pagination_complete"])
        self.assertEqual([call[0] for call in calls], ["GET", "GET"])
        self.assertEqual([call[2] for call in calls], [None, None])
        self.assertIn("cursor=opaque%2B%2F%3D", calls[1][1])

    def test_repeated_cursor_fails_closed(self) -> None:
        def fake_http(method: str, url: str, body: object | None) -> object:
            return {"markets": [_market(1)], "next_cursor": "same"}

        with self.assertRaises(V054CensusError):
            collect_combo_catalog(fake_http, frozen_contract())

    def test_price_changes_cannot_select_or_remove_markets(self) -> None:
        baseline = [_market(index, 0.1 + index / 100) for index in range(1, 11)]
        repriced = copy.deepcopy(baseline)
        for index, market in enumerate(repriced):
            market["outcome_prices"] = [str(index / 10), str(1 - index / 10)]
        baseline_ids = [normalize_combo_market(item, frozen_contract())[0]["condition_id"] for item in baseline]
        repriced_ids = [normalize_combo_market(item, frozen_contract())[0]["condition_id"] for item in repriced]
        self.assertEqual(repriced_ids, baseline_ids)

    def test_offline_census_stops_without_economic_inference(self) -> None:
        markets = [_market(index) for index in range(1, 11)]

        def fake_http(method: str, url: str, body: object | None) -> object:
            self.assertEqual(method, "GET")
            self.assertIsNone(body)
            return {"markets": markets, "next_cursor": None}

        with tempfile.TemporaryDirectory(dir=ROOT / "data") as temporary:
            folder = Path(temporary)
            prereg = folder / "prereg.json"
            result_path = folder / "result.json"
            build_preregistration(output_path=prereg, project_root=ROOT)
            result = census_v054(
                prereg_path=prereg,
                result_path=result_path,
                project_root=ROOT,
                http_json=fake_http,
            )
        self.assertEqual(result["catalog"]["valid_markets"], 10)
        self.assertEqual(
            result["verdict"],
            "STOP_COMBO_RFQ_PUBLIC_READ_ONLY_NOT_ECONOMICALLY_OBSERVABLE",
        )
        self.assertFalse(result["observability"]["profitability_measured"])
        self.assertIsNone(result["observability"]["candidate_count"])
        self.assertEqual(result["safety"]["orders_created"], 0)
        self.assertFalse(result["safety"]["authentication_used"])

    def test_official_artifacts_preserve_safety(self) -> None:
        if not PREREG.is_file():
            self.skipTest("Se congela V0.54 antes del censo oficial")
        prereg = load_and_verify_preregistration(PREREG, project_root=ROOT)
        self.assertFalse(prereg["safety"]["orders_enabled"])
        if RESULT.is_file():
            result = json.loads(RESULT.read_text(encoding="utf-8"))
            self.assertEqual(result["safety"]["orders_created"], 0)
            self.assertEqual(result["safety"]["transactions_created"], 0)
            self.assertEqual(result["safety"]["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
