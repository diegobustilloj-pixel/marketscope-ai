import copy
import json
import unittest
from pathlib import Path

from polymarket_bot.v027_prereg import (
    V027PreregistrationError,
    load_and_verify_frozen_prereg,
    validate_frozen_prereg_payload,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREREG = PROJECT_ROOT / "data" / "prereg_v027_regime_replication_tournament.json"


class V027PreregistrationTests(unittest.TestCase):
    def test_frozen_prereg_and_all_evidence_hashes_are_valid(self):
        payload = load_and_verify_frozen_prereg(PREREG, project_root=PROJECT_ROOT)
        self.assertEqual(payload["implementation"]["launch_status"], "NOT_LAUNCHED")
        self.assertEqual(payload["safety"]["real_money"], "BLOQUEADO")

    def test_changed_candidate_threshold_is_rejected(self):
        payload = json.loads(PREREG.read_text(encoding="utf-8"))
        changed = copy.deepcopy(payload)
        changed["arms"][0]["maximum_volatility_regime_ratio"] = 0.80
        with self.assertRaises(V027PreregistrationError):
            validate_frozen_prereg_payload(changed)

    def test_shortened_window_is_rejected(self):
        payload = json.loads(PREREG.read_text(encoding="utf-8"))
        changed = copy.deepcopy(payload)
        changed["maximum_hours"] = 12.0
        with self.assertRaises(V027PreregistrationError):
            validate_frozen_prereg_payload(changed)


if __name__ == "__main__":
    unittest.main()
