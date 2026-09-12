from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.v058_contract import frozen_contract as v058_contract
from polymarket_bot.v059_active_mapping_replay import V059Store
from polymarket_bot.v059_contract import VARIANT, frozen_contract


CONDITION_A = "0x" + "aa" * 32


class V059ActiveMappingTests(unittest.TestCase):
    def test_economic_and_live_mapping_gates_are_unchanged_from_v058(self) -> None:
        old = v058_contract()
        new = frozen_contract()
        self.assertEqual(new["scope"], old["scope"])
        self.assertEqual(new["transport"], old["transport"])
        self.assertEqual(new["gates"], old["gates"])
        self.assertEqual(new["gates"]["minimum_mapping_coverage"], 0.90)

    def test_historical_seed_gate_is_lifecycle_aware(self) -> None:
        mapping = frozen_contract()["mapping"]
        self.assertEqual(mapping["query"]["closed"], "false")
        self.assertTrue(mapping["closed_markets_intentionally_excluded"])
        self.assertEqual(mapping["minimum_seed_resolution_rate"], 0.70)
        self.assertEqual(
            mapping["seed_denominator_semantics"],
            "HISTORICAL_V057_POSITIONS_STILL_OPEN_AT_PREFLIGHT",
        )

    def test_store_marks_v059_but_preserves_safe_compatible_schema(self) -> None:
        mapping = {
            "101": {"position_id": "101", "clob_token_id": "1001", "condition_id": CONDITION_A, "outcome_index": 0},
            "102": {"position_id": "102", "clob_token_id": "1002", "condition_id": CONDITION_A, "outcome_index": 1},
        }
        compact = json.dumps(
            [mapping[key] for key in sorted(mapping, key=int)],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        summary = {
            "positions": 2,
            "sha256": hashlib.sha256(compact.encode("utf-8")).hexdigest(),
            "seed_resolution_rate": 1.0,
            "closed_markets_included": False,
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = V059Store(Path(temporary) / "capture.db", frozen_contract()["storage"], mapping, summary)
            store.open_new("b" * 64, 3600)
            meta = dict(store.db.execute("SELECT key,value FROM v058_meta"))
            tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            store.close()
        self.assertEqual(json.loads(meta["variant"]), VARIANT)
        self.assertFalse(json.loads(meta["orders_enabled"]))
        self.assertFalse(json.loads(meta["quote_submission_enabled"]))
        self.assertIn("v058_joined_requests", tables)

    def test_contract_blocks_all_trading_paths(self) -> None:
        outbound = frozen_contract()["outbound"]
        self.assertEqual(outbound["rfq_allowed_json_message_types"], ["auth"])
        for key in (
            "rfq_quote_allowed", "rfq_quote_cancel_allowed", "rfq_confirmation_response_allowed",
            "orders_allowed", "signatures_allowed", "transactions_allowed",
        ):
            self.assertFalse(outbound[key])


if __name__ == "__main__":
    unittest.main()
