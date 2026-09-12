from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v049_conversion_census import filter_linked_event, validate_linked_market
from polymarket_bot.v050_contract import (
    build_preregistration,
    frozen_contract,
    load_and_verify_preregistration,
)
from polymarket_bot.v050_partial_conversion_census import (
    census_v050,
    classify_census,
    enumerate_partial_subsets,
    evaluate_event,
    executable_sell_revenue,
)


PREREG = ROOT / "data" / "prereg_v050_neg_risk_partial_conversion_cross_market_census.json"
RESULT = ROOT / "data" / "resultado_v050_neg_risk_partial_conversion_cross_market_census.json"


def _market_id(event_index: int) -> str:
    return f"0x{event_index:062x}00"


def _market(event_index: int, index: int) -> dict[str, object]:
    numeric = event_index * 10 + index + 1
    market_id = _market_id(event_index)
    return {
        "id": str(numeric),
        "conditionId": f"0x{numeric:064x}",
        "questionID": market_id[:-2] + f"{index:02x}",
        "question": f"Outcome {event_index}-{index}?",
        "slug": f"outcome-{event_index}-{index}",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "negRisk": True,
        "negRiskMarketID": market_id,
        "negRiskOther": False,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([f"yes-{numeric}", f"no-{numeric}"]),
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True},
    }


def _event(event_index: int) -> dict[str, object]:
    return {
        "id": f"event-{event_index}",
        "slug": f"event-{event_index}",
        "title": f"Event {event_index}",
        "active": True,
        "closed": False,
        "negRisk": True,
        "enableNegRisk": True,
        "negRiskMarketID": _market_id(event_index),
        "negRiskAugmented": False,
        "volume24hr": 1000 - event_index,
        "markets": [_market(event_index, index) for index in range(3)],
    }


def _clob_info(raw_market: dict[str, object]) -> dict[str, object]:
    tokens = json.loads(str(raw_market["clobTokenIds"]))
    return {
        "mos": 5,
        "mts": 0.001,
        "neg_risk": True,
        "fd": {"r": 0.05, "e": 1, "to": True},
        "t": [{"t": tokens[0], "o": "Yes"}, {"t": tokens[1], "o": "No"}],
    }


def _validated_event(index: int) -> dict[str, object]:
    raw = _event(index)
    normalized, reason = filter_linked_event(raw, frozen_contract())
    assert reason is None and normalized is not None
    raw_by_condition = {
        str(item["conditionId"]): item for item in raw["markets"]  # type: ignore[index]
    }
    markets = []
    for market in normalized["markets"]:
        validated, why = validate_linked_market(
            market, _clob_info(raw_by_condition[str(market["condition_id"])]), 5
        )
        assert why is None and validated is not None
        markets.append(validated)
    return {**normalized, "markets": markets}


def _sources(
    event: dict[str, object], *, no_ask: float, yes_bid: float, receive_spread_ms: int = 0
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    metadata: dict[str, dict[str, object]] = {}
    books: dict[str, dict[str, object]] = {}
    counter = 0
    for market in event["markets"]:  # type: ignore[index]
        for side, token_field in (("yes", "yes_token_id"), ("no", "no_token_id")):
            token = str(market[token_field])
            metadata[token] = {
                "condition_id": market["condition_id"],
                "source_timestamp_ms": 1000,
                "official_hash": f"rest-{token}",
                "minimum_order_size": 5,
                "tick_size": 0.001,
                "neg_risk": True,
            }
            books[token] = {
                "token_id": token,
                "condition_id": market["condition_id"],
                "source_timestamp_ms": 1000 + counter,
                "received_timestamp_ms": 2000 + counter * receive_spread_ms,
                "official_hash": f"ws-{token}",
                "levels_sha256": f"levels-{token}",
                "asks": [{"price": str(no_ask), "size": "5"}] if side == "no" else [],
                "bids": [{"price": str(yes_bid), "size": "5"}] if side == "yes" else [],
            }
            counter += 1
    return metadata, books


def _rpc_response(method: str, url: str, body: object | None) -> object:
    if isinstance(body, dict) and body.get("method") == "eth_blockNumber":
        return {"jsonrpc": "2.0", "id": 1, "result": "0x64"}
    assert isinstance(body, list)
    response = []
    for item in body:
        request_id = int(item["id"])
        if item["method"] == "eth_getCode":
            result = "0x60006000"
        else:
            selector = str(item["params"][0]["data"])[:10]
            value = 0 if selector == "0x2582cb5e" else 3
            result = "0x" + f"{value:064x}"
        response.append({"jsonrpc": "2.0", "id": request_id, "result": result})
    return response


class V050PartialConversionCensusTests(unittest.TestCase):
    def test_subset_enumeration_is_exhaustive_and_excludes_trivial_sets(self) -> None:
        self.assertEqual(
            enumerate_partial_subsets(3),
            [(0,), (1,), (2,), (0, 1), (0, 2), (1, 2)],
        )
        self.assertEqual(len(enumerate_partial_subsets(5)), 30)
        self.assertEqual(len(enumerate_partial_subsets(10)), 1022)
        self.assertTrue(all(1 <= len(item) < 10 for item in enumerate_partial_subsets(10)))

    def test_sell_revenue_walks_bids_and_subtracts_symmetric_fee(self) -> None:
        result = executable_sell_revenue(
            [{"price": "0.5", "size": "5"}],
            shares=5,
            fee_rate=0.05,
            fee_exponent=1,
        )
        assert result is not None
        self.assertAlmostEqual(result["gross_proceeds"], 2.5)
        self.assertAlmostEqual(result["fee"], 0.0625)
        self.assertAlmostEqual(result["net_revenue"], 2.4375)
        conservative = executable_sell_revenue(
            [{"price": "0.5", "size": "5"}],
            shares=5,
            fee_rate=0.05,
            fee_exponent=1,
            reserve_tick=0.01,
        )
        assert conservative is not None
        self.assertLess(conservative["net_revenue"], result["net_revenue"])

    def test_partial_conversion_evaluates_all_subsets_and_finds_edge(self) -> None:
        event = _validated_event(1)
        metadata, books = _sources(event, no_ask=0.3, yes_bid=0.5)
        record, reason = evaluate_event(
            event,
            {"fee_bips": 0, "question_count": 3},
            metadata,
            books,
            frozen_contract(),
        )
        self.assertIsNone(reason)
        assert record is not None
        self.assertEqual(record["subsets_screened"], 6)
        self.assertEqual(record["subsets_economically_evaluated"], 6)
        self.assertEqual(record["cost_candidates_before_gas"], 6)
        self.assertIn(record["best_economic_subset"]["conversion_collateral_multiplier"], {0, 1})
        self.assertFalse(record["semantic_parser_required"])

    def test_onchain_fee_can_make_complementary_sales_too_small(self) -> None:
        event = _validated_event(1)
        metadata, books = _sources(event, no_ask=0.3, yes_bid=0.5)
        record, reason = evaluate_event(
            event,
            {"fee_bips": 100, "question_count": 3},
            metadata,
            books,
            frozen_contract(),
        )
        self.assertIsNone(reason)
        assert record is not None
        self.assertAlmostEqual(record["conversion_amount_out_shares"], 4.95)
        self.assertEqual(record["subsets_economically_evaluated"], 0)
        self.assertEqual(
            record["subset_status_counts"],
            {"NON_EXECUTABLE_YES_OUTPUT_BELOW_MINIMUM_ORDER": 6},
        )

    def test_snapshot_and_coverage_fail_closed(self) -> None:
        event = _validated_event(1)
        metadata, books = _sources(event, no_ask=0.3, yes_bid=0.5, receive_spread_ms=500)
        self.assertEqual(
            evaluate_event(
                event,
                {"fee_bips": 0, "question_count": 3},
                metadata,
                books,
                frozen_contract(),
            )[1],
            "WEBSOCKET_RECEIVE_SPREAD_EXCEEDED",
        )
        classification = classify_census(
            selected_events=10,
            synchronized_events=4,
            cost_candidates=0,
            contract=frozen_contract(),
        )
        self.assertFalse(classification["structural_snapshot_conclusion_allowed"])

    def test_offline_census_is_idempotent_and_negative(self) -> None:
        gamma_events = [_event(index) for index in range(1, 6)]
        market_map = {
            str(market["conditionId"]): market
            for event in gamma_events
            for market in event["markets"]  # type: ignore[index]
        }
        token_map = {
            token: market
            for market in market_map.values()
            for token in json.loads(str(market["clobTokenIds"]))
        }

        def fake_http(method: str, url: str, body: object | None) -> object:
            if "gamma-api" in url:
                return gamma_events
            if "/clob-markets/" in url:
                return _clob_info(market_map[url.rsplit("/", 1)[-1]])
            if url.endswith("/books"):
                response = []
                for item in body:  # type: ignore[union-attr]
                    token = item["token_id"]
                    raw_market = token_map[token]
                    response.append(
                        {
                            "asset_id": token,
                            "market": raw_market["conditionId"],
                            "timestamp": "1000",
                            "hash": f"rest-{token}",
                            "min_order_size": "5",
                            "tick_size": "0.001",
                            "neg_risk": True,
                            "asks": [],
                            "bids": [],
                        }
                    )
                return response
            raise AssertionError(url)

        async def fake_ws(endpoint: str, tokens: object, contract: object) -> dict[str, object]:
            books = {}
            for event_index in range(1, 6):
                event = _validated_event(event_index)
                _, event_books = _sources(event, no_ask=0.7, yes_bid=0.28)
                books.update(event_books)
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

        with tempfile.TemporaryDirectory() as temporary:
            prereg_path = Path(temporary) / "prereg.json"
            result_path = Path(temporary) / "result.json"
            build_preregistration(output_path=prereg_path, project_root=ROOT)
            first = census_v050(
                prereg_path=prereg_path,
                result_path=result_path,
                project_root=ROOT,
                http_json=fake_http,
                rpc_json=_rpc_response,
                ws_collector=fake_ws,
            )
            second = census_v050(
                prereg_path=prereg_path,
                result_path=result_path,
                project_root=ROOT,
                http_json=fake_http,
                rpc_json=_rpc_response,
                ws_collector=fake_ws,
            )
        self.assertEqual(first, second)
        self.assertEqual(first["census"]["events_synchronized"], 5)
        self.assertEqual(first["census"]["subsets_screened"], 30)
        self.assertEqual(first["census"]["cost_candidates_before_gas"], 0)
        self.assertEqual(first["verdict"], "REJECT_PARTIAL_CONVERSION_CROSS_MARKET_FEASIBILITY")
        self.assertEqual(first["safety"]["transactions_created"], 0)

    def test_official_artifacts_preserve_safety(self) -> None:
        if not PREREG.is_file():
            self.skipTest("Se congela antes del censo oficial")
        prereg = load_and_verify_preregistration(PREREG, project_root=ROOT)
        self.assertFalse(prereg["safety"]["orders_enabled"])
        self.assertFalse(prereg["safety"]["transactions_enabled"])
        if RESULT.is_file():
            result = json.loads(RESULT.read_text(encoding="utf-8"))
            self.assertEqual(result["safety"]["orders_created"], 0)
            self.assertEqual(result["safety"]["transactions_created"], 0)
            self.assertEqual(result["safety"]["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
