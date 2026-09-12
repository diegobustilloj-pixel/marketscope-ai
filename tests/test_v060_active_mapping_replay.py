from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.v059_contract import frozen_contract as v059_contract
from polymarket_bot.v060_active_mapping_replay import (
    V060MappingError,
    V060Store,
    validate_active_mapping_summary,
)
from polymarket_bot.v060_contract import VARIANT, frozen_contract


CONDITION_A = "0x" + "aa" * 32


class V060ActiveMappingTests(unittest.TestCase):
    def test_economics_sampling_and_live_gates_are_unchanged_from_v059(self) -> None:
        old = v059_contract()
        new = frozen_contract()
        self.assertEqual(new["scope"], old["scope"])
        self.assertEqual(new["transport"], old["transport"])
        self.assertEqual(new["gates"], old["gates"])
        self.assertEqual(new["gates"]["minimum_mapping_coverage"], 0.90)
        self.assertEqual(new["gates"]["minimum_mapped_buy_yes_requests"], 10000)

    def test_historical_rate_is_replaced_by_absolute_active_minimums(self) -> None:
        mapping = frozen_contract()["mapping"]
        self.assertNotIn("minimum_seed_resolution_rate", mapping)
        self.assertFalse(mapping["historical_seed_resolution_rate_gate_enabled"])
        self.assertEqual(mapping["minimum_markets"], 1000)
        self.assertEqual(mapping["minimum_mapped_positions"], 2000)
        self.assertEqual(mapping["minimum_resolved_seed_positions"], 1000)

    def test_observed_v059_map_passes_all_absolute_minimums(self) -> None:
        validate_active_mapping_summary(
            {"markets": 3623, "positions": 7228, "resolved_seed_positions": 4459},
            frozen_contract()["mapping"],
        )

    def test_each_absolute_minimum_fails_closed(self) -> None:
        cases = (
            ({"markets": 999, "positions": 7228, "resolved_seed_positions": 4459}, "MARKETS_TOO_SMALL"),
            ({"markets": 3623, "positions": 1999, "resolved_seed_positions": 4459}, "POSITION_MAP_TOO_SMALL"),
            ({"markets": 3623, "positions": 7228, "resolved_seed_positions": 999}, "RESOLVED_SEED_TOO_SMALL"),
        )
        for summary, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(V060MappingError, error):
                validate_active_mapping_summary(summary, frozen_contract()["mapping"])

    def test_store_marks_v060_and_preserves_safe_compatible_schema(self) -> None:
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
            "resolved_seed_positions": 2,
            "seed_resolution_rate": 1.0,
            "historical_seed_resolution_rate_is_gate": False,
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = V060Store(Path(temporary) / "capture.db", frozen_contract()["storage"], mapping, summary)
            store.open_new("c" * 64, 3600)
            meta = dict(store.db.execute("SELECT key,value FROM v058_meta"))
            tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            store.close()
        self.assertEqual(json.loads(meta["variant"]), VARIANT)
        self.assertFalse(json.loads(meta["orders_enabled"]))
        self.assertFalse(json.loads(meta["quote_submission_enabled"]))
        self.assertIn("v058_joined_requests", tables)

    def test_contract_blocks_every_trading_path(self) -> None:
        outbound = frozen_contract()["outbound"]
        self.assertEqual(outbound["rfq_allowed_json_message_types"], ["auth"])
        for key in (
            "rfq_quote_allowed", "rfq_quote_cancel_allowed", "rfq_confirmation_response_allowed",
            "orders_allowed", "signatures_allowed", "transactions_allowed",
        ):
            self.assertFalse(outbound[key])


if __name__ == "__main__":
    unittest.main()
