from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v052_contract import (
    build_discovery_preregistration,
    build_economic_lock,
    frozen_contract,
    load_and_verify_economic_lock,
)
from polymarket_bot.v052_linked_threshold_census import (
    build_relationships_from_events,
    census_v052,
    discover_semantic_relationships,
    parse_threshold_question,
)


PREREG = ROOT / "data" / "prereg_v052_linked_threshold_semantic_discovery.json"
RELATIONSHIPS = ROOT / "data" / "relaciones_v052_linked_threshold_semantic.json"
LOCK = ROOT / "data" / "prereg_v052_linked_threshold_economic_lock.json"
RESULT = ROOT / "data" / "resultado_v052_linked_threshold_taker_floor_census.json"


def _market(index: int, threshold: int) -> dict[str, object]:
    return {
        "id": str(index),
        "conditionId": f"0x{index:064x}",
        "question": f"Will Bitcoin be above ${threshold:,} on August 31?",
        "slug": f"bitcoin-above-{threshold}-august-31",
        "description": (
            f"This market resolves Yes if Bitcoin is strictly above ${threshold:,} "
            "at noon on August 31 according to the named oracle."
        ),
        "resolutionSource": "https://example.test/oracle/bitcoin-august-31",
        "endDate": "2026-08-31T16:00:00Z",
        "marketGroup": 77,
        "groupItemTitle": f"${threshold:,}",
        "groupItemThreshold": str(threshold),
        "marketType": "normal",
        "formatType": "binary",
        "groupItemRange": "above",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "negRisk": False,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([f"yes-{index}", f"no-{index}"]),
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True},
    }


def _event() -> dict[str, object]:
    return {
        "id": "event-52",
        "slug": "bitcoin-thresholds-august-31",
        "title": "Bitcoin thresholds on August 31",
        "active": True,
        "closed": False,
        "volume24hr": 5000,
        "resolutionSource": "https://example.test/oracle/bitcoin-august-31",
        "endDate": "2026-08-31T16:00:00Z",
        "markets": [_market(1, 100000), _market(2, 110000), _market(3, 120000)],
    }


def _clob_info(index: int) -> dict[str, object]:
    return {
        "mos": 5,
        "mts": 0.01,
        "fd": {"r": 0.05, "e": 1, "to": True},
        "t": [{"t": f"yes-{index}", "o": "Yes"}, {"t": f"no-{index}", "o": "No"}],
    }


class V052LinkedThresholdCensusTests(unittest.TestCase):
    def test_exact_question_parser_supports_scaled_currency(self) -> None:
        parsed = parse_threshold_question(
            "Will Bitcoin be above $1.5M on August 31?", ["be above"]
        )
        assert parsed is not None
        self.assertEqual(parsed["threshold"], 1_500_000)
        self.assertEqual(parsed["unit_signature"], "$|")
        self.assertIn("<threshold>", parsed["question_skeleton"])

    def test_only_adjacent_same_rule_thresholds_are_linked(self) -> None:
        relationships, diagnostics = build_relationships_from_events(
            [_event()], frozen_contract()
        )
        self.assertEqual(diagnostics["semantic_markets_eligible"], 3)
        self.assertEqual(len(relationships), 2)
        self.assertEqual(relationships[0]["lower"]["threshold"], 100000)
        self.assertEqual(relationships[0]["upper"]["threshold"], 110000)
        self.assertEqual(relationships[1]["lower"]["threshold"], 110000)
        self.assertEqual(relationships[1]["upper"]["threshold"], 120000)

    def test_price_fields_cannot_change_semantic_selection(self) -> None:
        baseline = _event()
        repriced = copy.deepcopy(baseline)
        for ordinal, market in enumerate(repriced["markets"]):  # type: ignore[index]
            market["outcomePrices"] = json.dumps([str(ordinal / 10), str(1 - ordinal / 10)])
            market["bestBid"] = ordinal / 10
            market["bestAsk"] = 0.99 - ordinal / 10
            market["lastTradePrice"] = 0.5
        expected, _ = build_relationships_from_events([baseline], frozen_contract())
        actual, _ = build_relationships_from_events([repriced], frozen_contract())
        self.assertEqual(actual, expected)

    def test_mismatched_resolution_rule_is_not_linked(self) -> None:
        raw = _event()
        raw["markets"][1]["description"] = (  # type: ignore[index]
            "This market resolves Yes if Bitcoin touches $110,000 at any time on August 31."
        )
        relationships, diagnostics = build_relationships_from_events([raw], frozen_contract())
        self.assertEqual(len(relationships), 1)
        self.assertEqual(relationships[0]["lower"]["market_id"], "1")
        self.assertEqual(relationships[0]["upper"]["market_id"], "3")
        self.assertNotIn(
            "2",
            {
                relationship[side]["market_id"]
                for relationship in relationships
                for side in ("lower", "upper")
            },
        )
        self.assertGreaterEqual(diagnostics["semantic_groups"], 2)

    def test_two_stage_offline_census_can_detect_floor_candidate(self) -> None:
        gamma_events = [_event()]
        raw_markets = {
            str(market["conditionId"]): market
            for market in gamma_events[0]["markets"]  # type: ignore[index]
        }
        condition_to_index = {
            condition: int(str(market["id"])) for condition, market in raw_markets.items()
        }
        token_to_market = {
            token: market
            for market in raw_markets.values()
            for token in json.loads(str(market["clobTokenIds"]))
        }
        call_classes: list[str] = []

        def semantic_http(method: str, url: str, body: object | None) -> object:
            self.assertEqual(method, "GET")
            self.assertIn("gamma-api.polymarket.com/events", url)
            self.assertIsNone(body)
            call_classes.append("GAMMA")
            return gamma_events

        def economic_http(method: str, url: str, body: object | None) -> object:
            if "/clob-markets/" in url:
                call_classes.append("CLOB_INFO")
                condition = url.rsplit("/", 1)[-1]
                return _clob_info(condition_to_index[condition])
            if url.endswith("/books"):
                call_classes.append("CLOB_BOOKS")
                response = []
                for item in body:  # type: ignore[union-attr]
                    token = item["token_id"]
                    market = token_to_market[token]
                    response.append(
                        {
                            "asset_id": token,
                            "market": market["conditionId"],
                            "timestamp": "1000",
                            "hash": f"rest-{token}",
                            "min_order_size": "5",
                            "tick_size": "0.01",
                            "neg_risk": False,
                        }
                    )
                return response
            raise AssertionError(url)

        async def fake_ws(endpoint: str, tokens: object, contract: object) -> dict[str, object]:
            call_classes.append("WEBSOCKET")
            prices = {
                "yes-1": 0.45,
                "no-1": 0.55,
                "yes-2": 0.55,
                "no-2": 0.45,
                "yes-3": 0.45,
                "no-3": 0.55,
            }
            books = {}
            for token in tokens:  # type: ignore[union-attr]
                market = token_to_market[token]
                books[token] = {
                    "token_id": token,
                    "condition_id": market["conditionId"],
                    "source_timestamp_ms": 1000,
                    "received_timestamp_ms": 2000,
                    "official_hash": f"ws-{token}",
                    "levels_sha256": f"levels-{token}",
                    "asks": [{"price": str(prices[token]), "size": "20"}],
                    "bids": [{"price": str(max(0.01, prices[token] - 0.05)), "size": "20"}],
                }
            now_ms = time.time_ns() // 1_000_000
            return {
                "connected_at_ms": now_ms,
                "finished_at_ms": now_ms,
                "elapsed_seconds": 0.001,
                "expected_tokens": len(tokens),  # type: ignore[arg-type]
                "received_tokens": len(tokens),  # type: ignore[arg-type]
                "missing_tokens": [],
                "malformed_messages": 0,
                "books": books,
            }

        data_root = ROOT / "data"
        with tempfile.TemporaryDirectory(dir=data_root) as temporary:
            folder = Path(temporary)
            prereg = folder / "prereg.json"
            semantic = folder / "semantic.json"
            lock = folder / "lock.json"
            result_path = folder / "result.json"
            build_discovery_preregistration(output_path=prereg, project_root=ROOT)
            discovered = discover_semantic_relationships(
                prereg_path=prereg,
                output_path=semantic,
                project_root=ROOT,
                http_json=semantic_http,
            )
            self.assertEqual(call_classes, ["GAMMA"])
            self.assertEqual(discovered["discovery"]["clob_calls_made"], 0)
            build_economic_lock(
                discovery_prereg_path=prereg,
                relationships_path=semantic,
                output_path=lock,
                project_root=ROOT,
            )
            load_and_verify_economic_lock(lock, project_root=ROOT)
            result = census_v052(
                economic_lock_path=lock,
                result_path=result_path,
                project_root=ROOT,
                http_json=economic_http,
                ws_collector=fake_ws,
            )
        self.assertEqual(result["sample"]["semantic_relationships_frozen"], 2)
        self.assertEqual(result["census"]["relationships_evaluated"], 2)
        self.assertGreaterEqual(result["census"]["cost_candidates_before_gas"], 1)
        self.assertEqual(
            result["verdict"],
            "REQUIRE_MANUAL_RULE_REVIEW_AND_FRESH_PERSISTENCE_OBSERVER",
        )
        self.assertEqual(call_classes[0], "GAMMA")
        self.assertIn("CLOB_INFO", call_classes[1:])

    def test_zero_relationship_branch_makes_no_clob_call(self) -> None:
        classification_contract = frozen_contract()
        empty_semantic = {
            "schema": "semantic_relationships_v052_linked_threshold_1",
            "relationships": [],
        }
        self.assertEqual(classification_contract["logical_contract"]["payout_floor_per_bundle_share"], 1.0)
        self.assertEqual(empty_semantic["relationships"], [])

    def test_official_artifacts_preserve_safety(self) -> None:
        if not PREREG.is_file():
            self.skipTest("Se congela V0.52 antes del censo oficial")
        if LOCK.is_file():
            lock, semantic = load_and_verify_economic_lock(LOCK, project_root=ROOT)
            self.assertFalse(lock["safety"]["orders_enabled"])
            self.assertIsInstance(semantic["relationships"], list)
        if RESULT.is_file():
            result = json.loads(RESULT.read_text(encoding="utf-8"))
            self.assertEqual(result["safety"]["orders_created"], 0)
            self.assertEqual(result["safety"]["transactions_created"], 0)
            self.assertEqual(result["safety"]["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
