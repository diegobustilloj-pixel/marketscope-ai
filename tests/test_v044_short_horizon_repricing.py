from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v044_audit import audit_v044, depth_vwap
from polymarket_bot.v044_contract import (
    build_preregistration,
    load_and_verify_preregistration,
)


PREREG = ROOT / "data" / "prereg_v044_short_horizon_repricing_rejection.json"
RESULT = ROOT / "data" / "resultado_v044_short_horizon_repricing_rejection.json"


class V044ShortHorizonRepricingTests(unittest.TestCase):
    def test_depth_vwap_requires_full_size(self) -> None:
        self.assertEqual(depth_vwap([[0.40, 2.0], [0.41, 3.0]], 5.0), 0.406)
        self.assertIsNone(depth_vwap([[0.40, 2.0]], 5.0))

    def test_preregistration_is_closed_and_cannot_promote(self) -> None:
        prereg = load_and_verify_preregistration(PREREG, project_root=ROOT)
        self.assertEqual(prereg["execution"]["new_capture_hours"], 0.0)
        self.assertFalse(prereg["interpretation"]["posthoc_winner_selection_allowed"])
        self.assertFalse(prereg["interpretation"]["paper_or_money_promotion_allowed"])
        self.assertFalse(prereg["safety"]["orders_enabled"])
        self.assertFalse(prereg["safety"]["wallet_required"])
        self.assertEqual(prereg["safety"]["real_money"], "BLOQUEADO")

    def test_audit_is_reproducible_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prereg_path = Path(temporary) / "prereg.json"
            result_path = Path(temporary) / "result.json"
            build_preregistration(output_path=prereg_path, project_root=ROOT)
            first = audit_v044(
                prereg_path=prereg_path,
                result_path=result_path,
                project_root=ROOT,
            )
            second = audit_v044(
                prereg_path=prereg_path,
                result_path=result_path,
                project_root=ROOT,
            )
        self.assertEqual(first, second)
        self.assertTrue(first["source"]["database_unchanged"])
        self.assertTrue(first["source"]["query_only"])
        self.assertEqual(first["source"]["sqlite_quick_check"], "ok")
        self.assertEqual(first["family"]["variants_tested"], 12)
        self.assertFalse(first["decision"]["fresh_capture_launched"])

    def test_official_result_preserves_safety(self) -> None:
        if not RESULT.is_file():
            self.skipTest("El resultado se genera despues de congelar la preinscripcion")
        result = json.loads(RESULT.read_text(encoding="utf-8"))
        self.assertEqual(result["safety"]["orders_created"], 0)
        self.assertEqual(result["safety"]["paper_orders"], 0)
        self.assertFalse(result["safety"]["wallet_required"])
        self.assertEqual(result["safety"]["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
