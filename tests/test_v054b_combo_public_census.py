from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v054b_combo_public_census import census_v054b, normalize_combo_market
from polymarket_bot.v054b_contract import (
    build_preregistration,
    frozen_contract,
    load_and_verify_preregistration,
)


PREREG = ROOT / "data" / "prereg_v054b_public_combo_binary_labels_census.json"
RESULT = ROOT / "data" / "resultado_v054b_public_combo_binary_labels_census.json"


def _market(index: int, outcomes: list[str] | None = None) -> dict[str, object]:
    return {
        "id": str(index),
        "condition_id": f"0x{index:064x}",
        "position_ids": [str(index * 2), str(index * 2 + 1)],
        "slug": f"market-{index}",
        "title": f"Competitor A vs Competitor B {index}",
        "outcomes": outcomes or ["Competitor A", "Competitor B"],
        "outcome_prices": ["0.6", "0.4"],
        "volume": 1000.0,
        "tags": ["sports"],
    }


class V054BComboPublicCensusTests(unittest.TestCase):
    def test_two_competitor_labels_are_valid_without_renaming(self) -> None:
        normalized, reason = normalize_combo_market(_market(1), frozen_contract())
        self.assertIsNone(reason)
        self.assertEqual(normalized["outcomes"], ["Competitor A", "Competitor B"])

    def test_over_under_labels_are_valid(self) -> None:
        normalized, reason = normalize_combo_market(
            _market(1, ["Over", "Under"]), frozen_contract()
        )
        self.assertIsNone(reason)
        self.assertEqual(normalized["outcomes"], ["Over", "Under"])

    def test_empty_duplicate_or_three_labels_fail_closed(self) -> None:
        for outcomes in (["", "Under"], ["Same", "Same"], ["A", "B", "C"]):
            normalized, reason = normalize_combo_market(
                _market(1, list(outcomes)), frozen_contract()
            )
            self.assertIsNone(normalized)
            self.assertEqual(reason, "OUTCOME_LABELS_INVALID")

    def test_offline_corrected_census_reaches_observability_stop(self) -> None:
        markets = [_market(index) for index in range(1, 11)]

        def fake_http(method: str, url: str, body: object | None) -> object:
            self.assertEqual((method, body), ("GET", None))
            return {"markets": markets, "next_cursor": None}

        with tempfile.TemporaryDirectory(dir=ROOT / "data") as temporary:
            folder = Path(temporary)
            prereg = folder / "prereg.json"
            result_path = folder / "result.json"
            build_preregistration(output_path=prereg, project_root=ROOT)
            result = census_v054b(
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
        self.assertEqual(result["safety"]["orders_created"], 0)

    def test_official_artifacts_preserve_safety(self) -> None:
        if not PREREG.is_file():
            self.skipTest("Se congela V0.54b antes del censo oficial")
        prereg = load_and_verify_preregistration(PREREG, project_root=ROOT)
        self.assertFalse(prereg["safety"]["orders_enabled"])
        if RESULT.is_file():
            result = json.loads(RESULT.read_text(encoding="utf-8"))
            self.assertEqual(result["safety"]["orders_created"], 0)
            self.assertEqual(result["safety"]["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
