from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v051_contract import (
    build_preregistration,
    frozen_contract,
    load_and_verify_preregistration,
)
from polymarket_bot.v051_prefunded_maker_census import (
    census_v051,
    classify_census,
    evaluate_market,
    normalize_standard_market,
    validate_standard_clob_market,
)


PREREG = ROOT / "data" / "prereg_v051_prefunded_maker_single_fill_hedge_census.json"
RESULT = ROOT / "data" / "resultado_v051_prefunded_maker_single_fill_hedge_census.json"


def _raw_market(index: int) -> dict[str, object]:
    return {
        "id": str(index),
        "conditionId": f"0x{index:064x}",
        "question": f"Question {index}?",
        "slug": f"market-{index}",
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


def _raw_event(index: int) -> dict[str, object]:
    return {
        "id": f"event-{index}",
        "slug": f"event-{index}",
        "title": f"Event {index}",
        "active": True,
        "closed": False,
        "volume24hr": 1000 - index,
        "markets": [_raw_market(index)],
    }


def _clob_info(index: int) -> dict[str, object]:
    return {
        "mos": 5,
        "mts": 0.01,
        "neg_risk": False,
        "fd": {"r": 0.05, "e": 1, "to": True},
        "t": [{"t": f"yes-{index}", "o": "Yes"}, {"t": f"no-{index}", "o": "No"}],
    }


def _validated(index: int) -> dict[str, object]:
    raw_event = _raw_event(index)
    market, reason = normalize_standard_market(
        raw_event,
        raw_event["markets"][0],  # type: ignore[index]
        frozen_contract(),
    )
    assert reason is None and market is not None
    validated, why = validate_standard_clob_market(market, _clob_info(index), 5)
    assert why is None and validated is not None
    return validated


def _sources(
    market: dict[str, object],
    *,
    yes_ask: float,
    yes_bid: float,
    no_ask: float,
    no_bid: float,
    receive_spread_ms: int = 0,
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    metadata: dict[str, dict[str, object]] = {}
    books: dict[str, dict[str, object]] = {}
    for ordinal, (side, token_field, ask, bid) in enumerate(
        (
            ("yes", "yes_token_id", yes_ask, yes_bid),
            ("no", "no_token_id", no_ask, no_bid),
        )
    ):
        token = str(market[token_field])
        metadata[token] = {
            "condition_id": market["condition_id"],
            "source_timestamp_ms": 1000,
            "official_hash": f"rest-{token}",
            "minimum_order_size": 5,
            "tick_size": 0.01,
            "neg_risk": False,
        }
        books[token] = {
            "token_id": token,
            "condition_id": market["condition_id"],
            "source_timestamp_ms": 1000 + ordinal,
            "received_timestamp_ms": 2000 + ordinal * receive_spread_ms,
            "official_hash": f"ws-{token}",
            "levels_sha256": f"levels-{side}-{token}",
            "asks": [{"price": str(ask), "size": "10"}],
            "bids": [{"price": str(bid), "size": "10"}],
        }
    return metadata, books


class V051PrefundedMakerCensusTests(unittest.TestCase):
    def test_filter_is_standard_binary_only(self) -> None:
        event = _raw_event(1)
        market, reason = normalize_standard_market(
            event, event["markets"][0], frozen_contract()  # type: ignore[index]
        )
        self.assertIsNone(reason)
        assert market is not None
        self.assertEqual(market["yes_token_id"], "yes-1")
        event["markets"][0]["negRisk"] = True  # type: ignore[index]
        self.assertEqual(
            normalize_standard_market(
                event, event["markets"][0], frozen_contract()  # type: ignore[index]
            )[1],
            "NEG_RISK_MARKET_EXCLUDED",
        )

    def test_clob_validation_requires_explicit_standard_market(self) -> None:
        event = _raw_event(1)
        market, _ = normalize_standard_market(
            event, event["markets"][0], frozen_contract()  # type: ignore[index]
        )
        assert market is not None
        info = _clob_info(1)
        info["neg_risk"] = True
        self.assertEqual(
            validate_standard_clob_market(market, info, 5)[1],
            "CLOB_NEG_RISK_NOT_EXPLICITLY_FALSE",
        )

    def test_single_fill_uses_prefunded_complement_and_can_find_edge(self) -> None:
        market = _validated(1)
        metadata, books = _sources(
            market,
            yes_ask=0.70,
            yes_bid=0.59,
            no_ask=0.48,
            no_bid=0.40,
        )
        result, reason = evaluate_market(market, metadata, books, frozen_contract())
        self.assertIsNone(reason)
        assert result is not None
        self.assertEqual(result["directions_screened"], 2)
        self.assertGreaterEqual(result["cost_candidates_before_gas"], 1)
        yes_direction = next(item for item in result["directions"] if item["maker_side"] == "YES")
        self.assertEqual(yes_direction["inventory_cost"], 5.0)
        self.assertEqual(yes_direction["maker_proceeds"], 3.5)
        self.assertGreater(yes_direction["conservative_edge_per_complete_set_before_gas"], 0.01)

    def test_ordinary_complementary_book_is_negative_after_hedge_fee(self) -> None:
        market = _validated(1)
        metadata, books = _sources(
            market,
            yes_ask=0.60,
            yes_bid=0.55,
            no_ask=0.45,
            no_bid=0.40,
        )
        result, reason = evaluate_market(market, metadata, books, frozen_contract())
        self.assertIsNone(reason)
        assert result is not None
        self.assertEqual(result["cost_candidates_before_gas"], 0)
        self.assertTrue(
            all(
                item["conservative_edge_per_complete_set_before_gas"] < 0
                for item in result["directions"]
            )
        )

    def test_receive_spread_and_coverage_fail_closed(self) -> None:
        market = _validated(1)
        metadata, books = _sources(
            market,
            yes_ask=0.60,
            yes_bid=0.55,
            no_ask=0.45,
            no_bid=0.40,
            receive_spread_ms=600,
        )
        self.assertEqual(
            evaluate_market(market, metadata, books, frozen_contract())[1],
            "WEBSOCKET_RECEIVE_SPREAD_EXCEEDED",
        )
        classification = classify_census(
            selected_markets=30,
            synchronized_markets=14,
            cost_candidates=0,
            contract=frozen_contract(),
        )
        self.assertFalse(classification["conditional_fill_conclusion_allowed"])

    def test_offline_census_is_idempotent_and_negative(self) -> None:
        gamma_events = [_raw_event(index) for index in range(1, 16)]
        market_map = {
            str(event["markets"][0]["conditionId"]): event["markets"][0]  # type: ignore[index]
            for event in gamma_events
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
                condition = url.rsplit("/", 1)[-1]
                raw = market_map[condition]
                return _clob_info(int(raw["id"]))
            if url.endswith("/books"):
                response = []
                for item in body:  # type: ignore[union-attr]
                    token = item["token_id"]
                    raw = token_map[token]
                    response.append(
                        {
                            "asset_id": token,
                            "market": raw["conditionId"],
                            "timestamp": "1000",
                            "hash": f"rest-{token}",
                            "min_order_size": "5",
                            "tick_size": "0.01",
                            "neg_risk": False,
                            "asks": [],
                            "bids": [],
                        }
                    )
                return response
            raise AssertionError(url)

        async def fake_ws(endpoint: str, tokens: object, contract: object) -> dict[str, object]:
            books = {}
            for index in range(1, 16):
                market = _validated(index)
                _, event_books = _sources(
                    market,
                    yes_ask=0.60,
                    yes_bid=0.55,
                    no_ask=0.45,
                    no_bid=0.40,
                )
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
            first = census_v051(
                prereg_path=prereg_path,
                result_path=result_path,
                project_root=ROOT,
                http_json=fake_http,
                ws_collector=fake_ws,
            )
            second = census_v051(
                prereg_path=prereg_path,
                result_path=result_path,
                project_root=ROOT,
                http_json=fake_http,
                ws_collector=fake_ws,
            )
        self.assertEqual(first, second)
        self.assertEqual(first["census"]["markets_synchronized"], 15)
        self.assertEqual(first["census"]["directions_screened"], 30)
        self.assertEqual(first["census"]["cost_candidates_before_gas"], 0)
        self.assertEqual(first["verdict"], "REJECT_PREFUNDED_MAKER_SINGLE_FILL_HEDGE_FEASIBILITY")
        self.assertTrue(
            first["interpretation"]["prefunded_complete_set_removes_directional_residual_after_hedge"]
        )
        self.assertFalse(
            first["interpretation"]["partial_maker_fill_below_minimum_hedge_size_solved"]
        )

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
