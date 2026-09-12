from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v045_census import filter_event, validate_clob_market
from polymarket_bot.v045_contract import frozen_contract as v045_frozen_contract
from polymarket_bot.v047_contract import build_preregistration, frozen_contract, load_and_verify_preregistration
from polymarket_bot.v047_ws_census import census_v047, classify_census, evaluate_ws_event


PREREG = ROOT / "data" / "prereg_v047_ws_neg_risk_economic_census.json"
RESULT = ROOT / "data" / "resultado_v047_ws_neg_risk_economic_census.json"


def _market(index: int) -> dict[str, object]:
    return {
        "id": str(index),
        "conditionId": f"0x{index:064x}",
        "question": f"Outcome {index}?",
        "slug": f"outcome-{index}",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "negRisk": True,
        "negRiskOther": False,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([f"yes-{index}", f"no-{index}"]),
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True},
    }


def _event(event_index: int) -> dict[str, object]:
    first = event_index * 10
    return {
        "id": f"event-{event_index}",
        "slug": f"event-{event_index}",
        "title": f"Event {event_index}",
        "active": True,
        "closed": False,
        "negRisk": True,
        "negRiskAugmented": False,
        "volume24hr": 1000 - event_index,
        "markets": [_market(first + offset) for offset in range(1, 4)],
    }


def _clob_info(raw_market: dict[str, object]) -> dict[str, object]:
    tokens = json.loads(str(raw_market["clobTokenIds"]))
    return {
        "mos": 5,
        "mts": 0.001,
        "fd": {"r": 0.05, "e": 1, "to": True},
        "t": [{"t": tokens[0], "o": "Yes"}, {"t": tokens[1], "o": "No"}],
    }


def _validated_event(event_index: int) -> dict[str, object]:
    raw = _event(event_index)
    normalized, reason = filter_event(raw, v045_frozen_contract())
    assert reason is None and normalized is not None
    markets = []
    for market, raw_market in zip(normalized["markets"], raw["markets"]):  # type: ignore[index]
        validated, why = validate_clob_market(market, _clob_info(raw_market), 5)
        assert why is None and validated is not None
        markets.append(validated)
    return {**normalized, "markets": markets}


def _books(event: dict[str, object], price: float, *, spread_ms: int = 0) -> dict[str, dict[str, object]]:
    result = {}
    for index, market in enumerate(event["markets"]):  # type: ignore[index]
        token = str(market["yes_token_id"])
        result[token] = {
            "token_id": token,
            "condition_id": market["condition_id"],
            "source_timestamp_ms": 1000 + index * 5000,
            "received_timestamp_ms": 2000 + index * spread_ms,
            "official_hash": f"hash-{token}",
            "levels_sha256": f"levels-{token}",
            "asks": [{"price": str(price), "size": "5"}],
            "bids": [],
            "minimum_order_size": 5,
            "tick_size": 0.001,
            "neg_risk": True,
        }
    return result


class V047WsCensusTests(unittest.TestCase):
    def test_economic_candidate_uses_receive_window_not_source_spread(self) -> None:
        event = _validated_event(1)
        record, reason = evaluate_ws_event(event, _books(event, 0.2), frozen_contract())
        self.assertIsNone(reason)
        assert record is not None
        self.assertEqual(record["websocket_receive_spread_ms"], 0)
        self.assertEqual(record["websocket_source_spread_ms"], 10_000)
        self.assertTrue(record["cost_candidate"])
        self.assertFalse(record["semantic_exhaustiveness_verified"])
        self.assertFalse(record["executable_atomically"])

    def test_receive_spread_still_fails_closed(self) -> None:
        event = _validated_event(1)
        record, reason = evaluate_ws_event(event, _books(event, 0.2, spread_ms=1500), frozen_contract())
        self.assertIsNone(record)
        self.assertEqual(reason, "WEBSOCKET_RECEIVE_SPREAD_EXCEEDED")

    def test_ws_market_metadata_must_match_clob(self) -> None:
        event = _validated_event(1)
        books = _books(event, 0.2)
        first = next(iter(books.values()))
        first["neg_risk"] = False
        self.assertEqual(evaluate_ws_event(event, books, frozen_contract())[1], "WEBSOCKET_NEG_RISK_NOT_TRUE")

    def test_classification_requires_coverage_before_rejection(self) -> None:
        contract = frozen_contract()
        failed = classify_census(selected_events=10, evaluated_events=4, cost_candidates=0, contract=contract)
        self.assertFalse(failed["economic_snapshot_conclusion_allowed"])
        passed = classify_census(selected_events=10, evaluated_events=10, cost_candidates=0, contract=contract)
        self.assertEqual(passed["verdict"], "REJECT_CURRENT_WS_BUY_ALL_YES_FEASIBILITY")

    def test_offline_census_is_idempotent_and_safe(self) -> None:
        gamma_events = [_event(index) for index in range(1, 6)]
        market_map = {
            str(market["conditionId"]): market
            for event in gamma_events
            for market in event["markets"]  # type: ignore[index]
        }

        def fake_http(method: str, url: str, body: object | None) -> object:
            if "gamma-api" in url:
                return gamma_events
            if "/clob-markets/" in url:
                condition = url.rsplit("/", 1)[-1]
                return _clob_info(market_map[condition])
            raise AssertionError(url)

        async def fake_ws(endpoint: str, tokens: object, contract: object) -> dict[str, object]:
            books = {}
            for event_index in range(1, 6):
                event = _validated_event(event_index)
                books.update(_books(event, 0.34))
            return {
                "connected_at_ms": 2000,
                "finished_at_ms": 2001,
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
            first = census_v047(
                prereg_path=prereg_path,
                result_path=result_path,
                project_root=ROOT,
                http_json=fake_http,
                ws_collector=fake_ws,
            )
            second = census_v047(
                prereg_path=prereg_path,
                result_path=result_path,
                project_root=ROOT,
                http_json=fake_http,
                ws_collector=fake_ws,
            )
        self.assertEqual(first, second)
        self.assertEqual(first["census"]["events_evaluated"], 5)
        self.assertEqual(first["census"]["cost_candidates"], 0)
        self.assertEqual(first["safety"]["orders_created"], 0)

    def test_official_artifacts_preserve_safety(self) -> None:
        if not PREREG.is_file():
            self.skipTest("Se congela antes del censo oficial")
        prereg = load_and_verify_preregistration(PREREG, project_root=ROOT)
        self.assertFalse(prereg["safety"]["orders_enabled"])
        self.assertFalse(prereg["safety"]["wallet_required"])
        if RESULT.is_file():
            result = json.loads(RESULT.read_text(encoding="utf-8"))
            self.assertEqual(result["safety"]["orders_created"], 0)
            self.assertEqual(result["safety"]["paper_orders"], 0)
            self.assertEqual(result["safety"]["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
