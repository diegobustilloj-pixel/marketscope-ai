from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v053_contract import (
    build_discovery_preregistration,
    build_economic_lock,
    frozen_contract,
    load_and_verify_economic_lock,
)
from polymarket_bot.v053_cross_event_threshold_census import (
    build_cross_event_relationships,
    census_v053,
    classify_census,
    collect_keyset_events,
    discover_cross_event_relationships,
)


PREREG = ROOT / "data" / "prereg_v053_cross_event_threshold_semantic_discovery.json"
RELATIONSHIPS = ROOT / "data" / "relaciones_v053_cross_event_threshold_semantic.json"
LOCK = ROOT / "data" / "prereg_v053_cross_event_threshold_economic_lock.json"
RESULT = ROOT / "data" / "resultado_v053_cross_event_threshold_taker_floor_census.json"


def _market(index: int, threshold: int) -> dict[str, object]:
    return {
        "id": str(index),
        "conditionId": f"0x{index:064x}",
        "question": f"Will Bitcoin hit ${threshold:,} by August 31?",
        "slug": f"will-bitcoin-hit-{threshold}-by-august-31-{index}",
        "description": (
            f"This market resolves Yes if Bitcoin hits ${threshold:,} by August 31 "
            "according to the named oracle."
        ),
        "resolutionSource": "https://example.test/oracle/bitcoin-august-31",
        "endDate": "2026-08-31T23:59:00Z",
        "marketType": "normal",
        "formatType": "binary",
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


def _event(event_index: int, market_index: int, threshold: int) -> dict[str, object]:
    return {
        "id": f"event-{event_index}",
        "slug": f"bitcoin-hit-{threshold}-event-{event_index}",
        "title": f"Will Bitcoin hit {threshold}?",
        "active": True,
        "closed": False,
        "volume24hr": 1000 - event_index,
        "resolutionSource": "https://example.test/oracle/bitcoin-august-31",
        "endDate": "2026-08-31T23:59:00Z",
        "markets": [_market(market_index, threshold)],
    }


def _events() -> list[dict[str, object]]:
    return [
        _event(1, 1, 100000),
        _event(2, 2, 110000),
        _event(3, 3, 120000),
    ]


def _clob_info(index: int) -> dict[str, object]:
    return {
        "mos": 5,
        "mts": 0.01,
        "fd": {"r": 0.05, "e": 1, "to": True},
        "t": [{"t": f"yes-{index}", "o": "Yes"}, {"t": f"no-{index}", "o": "No"}],
    }


class V053CrossEventThresholdCensusTests(unittest.TestCase):
    def test_distinct_events_form_adjacent_relationships_without_market_group(self) -> None:
        relationships, diagnostics = build_cross_event_relationships(
            _events(), frozen_contract()
        )
        self.assertEqual(diagnostics["semantic_markets_eligible"], 3)
        self.assertEqual(len(relationships), 2)
        self.assertNotEqual(
            relationships[0]["lower"]["event_id"],
            relationships[0]["upper"]["event_id"],
        )
        self.assertEqual(relationships[0]["lower"]["threshold"], 100000)
        self.assertEqual(relationships[0]["upper"]["threshold"], 110000)

    def test_same_event_pair_is_excluded(self) -> None:
        event = _event(1, 1, 100000)
        event["markets"].append(_market(2, 110000))  # type: ignore[union-attr]
        relationships, diagnostics = build_cross_event_relationships(
            [event], frozen_contract()
        )
        self.assertEqual(relationships, [])
        self.assertEqual(diagnostics["same_event_pairs_excluded"], 1)

    def test_duplicate_threshold_across_events_rejects_entire_group(self) -> None:
        events = _events()
        events.append(_event(4, 4, 110000))
        relationships, diagnostics = build_cross_event_relationships(
            events, frozen_contract()
        )
        self.assertEqual(relationships, [])
        self.assertEqual(diagnostics["duplicate_threshold_groups_rejected"], 1)

    def test_gamma_price_fields_cannot_change_relationships(self) -> None:
        baseline = _events()
        repriced = copy.deepcopy(baseline)
        for ordinal, event in enumerate(repriced):
            market = event["markets"][0]  # type: ignore[index]
            market["outcomePrices"] = json.dumps([str(ordinal / 10), str(1 - ordinal / 10)])
            market["bestBid"] = ordinal / 10
            market["bestAsk"] = 0.99 - ordinal / 10
            market["lastTradePrice"] = 0.5
        expected, _ = build_cross_event_relationships(baseline, frozen_contract())
        actual, _ = build_cross_event_relationships(repriced, frozen_contract())
        self.assertEqual(actual, expected)

    def test_keyset_uses_next_cursor_and_never_offset(self) -> None:
        responses = [
            {"events": _events()[:2], "next_cursor": "opaque-one"},
            {"events": _events()[2:]},
        ]
        urls: list[str] = []

        def fake_http(method: str, url: str, body: object | None) -> object:
            self.assertEqual(method, "GET")
            self.assertIsNone(body)
            urls.append(url)
            return responses[len(urls) - 1]

        events, diagnostics = collect_keyset_events(fake_http, frozen_contract())
        self.assertEqual(len(events), 3)
        self.assertEqual(diagnostics["pages_requested"], 2)
        self.assertTrue(diagnostics["pagination_complete"])
        self.assertNotIn("offset=", "".join(urls))
        self.assertNotIn("after_cursor", urls[0])
        self.assertIn("after_cursor=opaque-one", urls[1])

    def test_two_stage_offline_census_detects_candidate(self) -> None:
        gamma_events = _events()
        raw_markets = {
            str(event["markets"][0]["conditionId"]): event["markets"][0]  # type: ignore[index]
            for event in gamma_events
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
            call_classes.append("GAMMA")
            return {"events": gamma_events}

        def economic_http(method: str, url: str, body: object | None) -> object:
            if "/clob-markets/" in url:
                call_classes.append("CLOB_INFO")
                condition = url.rsplit("/", 1)[-1]
                return _clob_info(condition_to_index[condition])
            if url.endswith("/books"):
                call_classes.append("CLOB_BOOKS")
                return [
                    {
                        "asset_id": item["token_id"],
                        "market": token_to_market[item["token_id"]]["conditionId"],
                        "timestamp": "1000",
                        "hash": f"rest-{item['token_id']}",
                        "min_order_size": "5",
                        "tick_size": "0.01",
                        "neg_risk": False,
                    }
                    for item in body  # type: ignore[union-attr]
                ]
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

        with tempfile.TemporaryDirectory(dir=ROOT / "data") as temporary:
            folder = Path(temporary)
            prereg = folder / "prereg.json"
            semantic = folder / "semantic.json"
            lock = folder / "lock.json"
            result_path = folder / "result.json"
            build_discovery_preregistration(output_path=prereg, project_root=ROOT)
            discovered = discover_cross_event_relationships(
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
            result = census_v053(
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
        self.assertIn("CLOB_INFO", call_classes[1:])

    def test_zero_relationship_classification_closes_family(self) -> None:
        result = classify_census(
            selected_relationships=0,
            evaluated_relationships=0,
            candidates=0,
            contract=frozen_contract(),
        )
        self.assertEqual(
            result["verdict"],
            "CLOSE_STANDARD_THRESHOLD_RELATION_FAMILY_NO_SEMANTIC_PAIRS",
        )
        self.assertFalse(result["economic_conclusion_allowed"])

    def test_official_artifacts_preserve_safety(self) -> None:
        if not PREREG.is_file():
            self.skipTest("Se congela V0.53 antes del censo oficial")
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
