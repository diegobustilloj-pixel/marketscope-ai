from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v046_contract import build_preregistration, frozen_contract, load_and_verify_preregistration
from polymarket_bot.v046_transport_probe import (
    _normalize_ws_book,
    classify_probe,
    evaluate_event_transport,
    probe_v046,
)


PREREG = ROOT / "data" / "prereg_v046_rest_websocket_transport_probe.json"
RESULT = ROOT / "data" / "resultado_v046_rest_websocket_transport_probe.json"


def _gamma_market(index: int) -> dict[str, object]:
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


def _gamma_event(event_index: int) -> dict[str, object]:
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
        "markets": [_gamma_market(first + offset) for offset in range(1, 4)],
    }


def _normalized_event(event_index: int) -> dict[str, object]:
    first = event_index * 10
    return {
        "event_id": f"event-{event_index}",
        "slug": f"event-{event_index}",
        "title": f"Event {event_index}",
        "market_count": 3,
        "markets": [{"yes_token_id": f"yes-{first + offset}"} for offset in range(1, 4)],
    }


def _rest(tokens: list[str], timestamp_base: int = 1000) -> dict[str, object]:
    return {
        "valid": True,
        "books": {
            token: {"source_timestamp_ms": timestamp_base + index * 5000, "official_hash": f"hash-{token}"}
            for index, token in enumerate(tokens)
        },
    }


class V046TransportProbeTests(unittest.TestCase):
    def test_raw_and_sdk_websocket_shapes_are_normalized(self) -> None:
        raw = _normalize_ws_book(
            {
                "event_type": "book",
                "asset_id": "token",
                "market": "condition",
                "timestamp": "1000",
                "hash": "0xABC",
                "asks": [],
                "bids": [],
            },
            1100,
        )
        sdk = _normalize_ws_book(
            {
                "type": "book",
                "payload": {
                    "tokenId": "token",
                    "market": "condition",
                    "timestamp": "1000",
                    "hash": "ABC",
                    "asks": [],
                    "bids": [],
                },
            },
            1100,
        )
        self.assertEqual(raw, sdk)
        assert raw is not None
        self.assertEqual(raw["official_hash"], "abc")

    def test_receive_window_and_hashes_can_pass_with_old_source_spread(self) -> None:
        contract = frozen_contract()
        event = _normalized_event(1)
        tokens = [str(item["yes_token_id"]) for item in event["markets"]]  # type: ignore[index]
        before = _rest(tokens)
        after = _rest(tokens)
        websocket = {
            "books": {
                token: {
                    "source_timestamp_ms": 1000 + index * 5000,
                    "received_timestamp_ms": 20_000 + index,
                    "official_hash": f"hash-{token}",
                }
                for index, token in enumerate(tokens)
            }
        }
        result = evaluate_event_transport(event, before, websocket, after, contract)
        self.assertTrue(result["transport_complete"])
        self.assertEqual(result["websocket_receive_spread_ms"], 2)
        self.assertEqual(result["websocket_source_spread_ms"], 10_000)
        self.assertTrue(result["source_spread_exceeds_receive_gate"])

    def test_hash_mismatch_fails_closed(self) -> None:
        contract = frozen_contract()
        event = _normalized_event(1)
        tokens = [str(item["yes_token_id"]) for item in event["markets"]]  # type: ignore[index]
        before = _rest(tokens)
        after = _rest(tokens)
        websocket = {
            "books": {
                token: {
                    "source_timestamp_ms": 1000,
                    "received_timestamp_ms": 2000,
                    "official_hash": "different",
                }
                for token in tokens
            }
        }
        result = evaluate_event_transport(event, before, websocket, after, contract)
        self.assertFalse(result["transport_complete"])
        self.assertFalse(result["all_hashes_continuous"])

    def test_classification_requires_five_complete_events(self) -> None:
        events = [{"transport_complete": index < 4} for index in range(10)]
        result = classify_probe(events, 10, frozen_contract())
        self.assertFalse(result["transport_validated"])
        events[4]["transport_complete"] = True
        result = classify_probe(events, 10, frozen_contract())
        self.assertTrue(result["transport_validated"])

    def test_offline_probe_is_idempotent_and_never_economic(self) -> None:
        gamma_events = [_gamma_event(index) for index in range(1, 6)]

        def fake_http(method: str, url: str, body: object | None) -> object:
            if "gamma-api" in url:
                return gamma_events
            if url.endswith("/books"):
                return [
                    {
                        "asset_id": item["token_id"],
                        "timestamp": "1000",
                        "hash": f"hash-{item['token_id']}",
                        "asks": [],
                        "bids": [],
                    }
                    for item in body  # type: ignore[union-attr]
                ]
            raise AssertionError(url)

        async def fake_ws(endpoint: str, tokens: object, contract: object) -> dict[str, object]:
            return {
                "connected_at_ms": 2000,
                "finished_at_ms": 2001,
                "expected_tokens": len(tokens),  # type: ignore[arg-type]
                "received_tokens": len(tokens),  # type: ignore[arg-type]
                "missing_tokens": [],
                "malformed_messages": 0,
                "books": {
                    token: {
                        "token_id": token,
                        "source_timestamp_ms": 1000,
                        "received_timestamp_ms": 2000,
                        "official_hash": f"hash-{token}",
                    }
                    for token in tokens  # type: ignore[union-attr]
                },
            }

        with tempfile.TemporaryDirectory() as temporary:
            prereg_path = Path(temporary) / "prereg.json"
            result_path = Path(temporary) / "result.json"
            build_preregistration(output_path=prereg_path, project_root=ROOT)
            first = probe_v046(
                prereg_path=prereg_path,
                result_path=result_path,
                project_root=ROOT,
                http_json=fake_http,
                ws_collector=fake_ws,
            )
            second = probe_v046(
                prereg_path=prereg_path,
                result_path=result_path,
                project_root=ROOT,
                http_json=fake_http,
                ws_collector=fake_ws,
            )
        self.assertEqual(first, second)
        self.assertEqual(first["verdict"], "PASS_WS_RECEIVE_WINDOW_AND_HASH_CONTINUITY")
        self.assertFalse(first["interpretation"]["economic_result"])
        self.assertEqual(first["safety"]["orders_created"], 0)

    def test_official_artifacts_preserve_safety(self) -> None:
        if not PREREG.is_file():
            self.skipTest("Se congela antes del probe oficial")
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
