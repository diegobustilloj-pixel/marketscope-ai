from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket_bot.v056_rfq_observer import CredentialBundle, sanitize_inbound
from polymarket_bot.v057_joined_replay import collect_joined_replay
from polymarket_bot.v058_contract import frozen_contract
from polymarket_bot.v058_mapped_replay import (
    V058MappingError,
    V058Store,
    fetch_position_map,
    mapped_request_job,
    parse_mapping_page,
)


CONDITION_A = "0x" + "aa" * 32
CONDITION_B = "0x" + "bb" * 32


def _page() -> dict[str, object]:
    return {
        "markets": [
            {
                "closed": False, "comboStatus": "enabled", "conditionId": CONDITION_A,
                "positionIds": ["101", "102"], "clobTokenIds": '["1001","1002"]',
            },
            {
                "closed": False, "comboStatus": "enabled", "conditionId": CONDITION_B,
                "positionIds": ["201", "202"], "clobTokenIds": '["2001","2002"]',
            },
        ],
        "next_cursor": None,
    }


def _mapping() -> dict[str, dict[str, object]]:
    return parse_mapping_page(_page())[0]


def _request(rfq_id: str = "rfq-mapped") -> dict[str, object]:
    return {
        "type": "RFQ_REQUEST", "rfq_id": rfq_id, "requestor_public_id": "requester-secret",
        "condition_id": "0x" + "cc" * 32, "leg_position_ids": ["102", "201"],
        "yes_position_id": "301", "no_position_id": "302", "direction": "BUY", "side": "YES",
        "requested_size": {"unit": "notional", "value_e6": "5000000"},
        "submission_deadline": 2_000_000,
    }


def _summary(mapping: dict[str, dict[str, object]]) -> dict[str, object]:
    compact = json.dumps(
        [mapping[key] for key in sorted(mapping, key=int)],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return {
        "batches": 1, "markets": 2, "positions": len(mapping),
        "seed_positions": len(mapping), "seed_resolution_rate": 1.0,
        "sha256": hashlib.sha256(compact.encode("utf-8")).hexdigest(),
        "retries": 0, "raw_payloads_persisted": False, "credentials_sent": False,
    }


class V058MappedReplayTests(unittest.TestCase):
    def test_filtered_batches_repeat_position_ids_parameter(self) -> None:
        pages = [
            _page(),
            {**_page(), "markets": []},
        ]
        requested_urls: list[str] = []

        class Response:
            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = payload

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, _: int) -> bytes:
                return json.dumps(self.payload).encode("utf-8")

        def open_url(request: object, timeout: float) -> Response:
            self.assertEqual(timeout, 10.0)
            requested_urls.append(request.full_url)
            return Response(pages.pop(0))

        with patch("polymarket_bot.v058_mapped_replay.urllib.request.urlopen", side_effect=open_url):
            mapping, summary = fetch_position_map(
                "https://gamma.example/markets/keyset", {"limit": 100},
                [str(value) for value in range(1, 22)], "position_ids", 20, 2, 10.0,
            )
        self.assertEqual(summary["batches"], 2)
        self.assertEqual(len(mapping), 4)
        self.assertEqual(summary["resolved_seed_positions"], 0)
        self.assertEqual(summary["seed_resolution_rate"], 0.0)
        self.assertEqual(requested_urls[0].count("position_ids="), 20)
        self.assertEqual(requested_urls[1].count("position_ids="), 1)

    def test_mapping_page_retries_one_transient_reset(self) -> None:
        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, _: int) -> bytes:
                return json.dumps(_page()).encode("utf-8")

        with (
            patch(
                "polymarket_bot.v058_mapped_replay.urllib.request.urlopen",
                side_effect=[ConnectionResetError("reset"), Response()],
            ),
            patch("polymarket_bot.v058_mapped_replay.time.sleep") as sleep,
        ):
            mapping, summary = fetch_position_map(
                "https://gamma.example/markets/keyset", {"limit": 100}, ["101"],
                "position_ids", 20, 1, 10.0, 3, [0.5, 2.0],
            )
        self.assertEqual(len(mapping), 4)
        self.assertEqual(summary["retries"], 1)
        sleep.assert_called_once_with(0.5)

    def test_gamma_positions_align_to_clob_tokens_by_index(self) -> None:
        mapping, cursor, markets = parse_mapping_page(_page())
        self.assertIsNone(cursor)
        self.assertEqual(markets, 2)
        self.assertEqual(mapping["102"]["clob_token_id"], "1002")
        self.assertEqual(mapping["102"]["condition_id"], CONDITION_A)
        self.assertEqual(mapping["201"]["clob_token_id"], "2001")

    def test_invalid_alignment_fails_closed(self) -> None:
        page = _page()
        page["markets"][0]["clobTokenIds"] = '["1001"]'
        with self.assertRaisesRegex(V058MappingError, "ALIGNMENT"):
            parse_mapping_page(page)

    def test_request_maps_original_positions_to_real_clob_tokens(self) -> None:
        record = sanitize_inbound(_request(), 1_999_700)
        classification, job = mapped_request_job(record, frozen_contract()["scope"], _mapping())
        self.assertEqual(classification, "MAPPED")
        self.assertEqual(job["leg_position_ids_original"], ["102", "201"])
        self.assertEqual(job["leg_position_ids"], ["1002", "2001"])
        self.assertNotIn("requester-secret", json.dumps(job, default=str))

    def test_missing_position_is_counted_unmapped_and_never_queued(self) -> None:
        payload = _request()
        payload["leg_position_ids"] = ["102", "999"]
        classification, job = mapped_request_job(
            sanitize_inbound(payload, 1_999_700), frozen_contract()["scope"], _mapping()
        )
        self.assertEqual(classification, "UNMAPPED")
        self.assertIsNone(job)

    def test_short_capture_requests_books_with_clob_tokens_not_positions(self) -> None:
        mapping = _mapping()
        scope = frozen_contract()["scope"]
        sampled = None
        for index in range(100):
            payload = _request(f"rfq-{index}")
            classification, job = mapped_request_job(sanitize_inbound(payload, 1_999_700), scope, mapping)
            if classification == "MAPPED" and job and job["sampled"]:
                sampled = payload
                break
        self.assertIsNotNone(sampled)

        class Socket:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.messages = [json.dumps({"type": "auth", "success": True}), json.dumps(sampled)]

            async def send(self, message: str) -> None:
                self.sent.append(message)

            async def recv(self) -> str:
                if self.messages:
                    return self.messages.pop(0)
                await asyncio.Future()
                raise AssertionError("unreachable")

        class Context:
            def __init__(self, socket: Socket) -> None:
                self.socket = socket

            async def __aenter__(self) -> Socket:
                return self.socket

            async def __aexit__(self, *args: object) -> None:
                return None

        seen_tokens: list[list[str]] = []

        def books(_: str, tokens: list[str], __: float) -> list[dict[str, object]]:
            seen_tokens.append(tokens)
            return [
                {"asset_id": token, "market": CONDITION_A if token == "1002" else CONDITION_B,
                 "timestamp": 1_999_800, "asks": [{"price": "0.2", "size": "10"}]}
                for token in tokens
            ]

        contract = frozen_contract()
        contract["transport"]["receive_poll_seconds"] = 0.01
        with tempfile.TemporaryDirectory() as temporary:
            store = V058Store(Path(temporary) / "capture.db", contract["storage"], mapping, _summary(mapping))
            store.open_new("a" * 64, 3600)
            run_id = store.start_run()
            result = asyncio.run(
                collect_joined_replay(
                    endpoint="wss://example.invalid",
                    credentials=CredentialBundle("k", "s", "p", "0x" + "1" * 40, "0x" + "2" * 40, 2),
                    store=store, run_id=run_id, contract=contract,
                    connect_factory=lambda *args, **kwargs: Context(Socket()),
                    book_fetcher=books, duration_seconds=0.05,
                )
            )
            stored = store.db.execute(
                "SELECT leg_position_ids_json,leg_clob_token_ids_json,book_status FROM v058_joined_requests"
            ).fetchone()
            store.close()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(seen_tokens, [["1002", "2001"]])
        self.assertEqual(json.loads(stored[0]), ["102", "201"])
        self.assertEqual(json.loads(stored[1]), ["1002", "2001"])
        self.assertEqual(stored[2], "SUCCESS")

    def test_contract_blocks_all_trading_paths(self) -> None:
        outbound = frozen_contract()["outbound"]
        self.assertEqual(outbound["rfq_allowed_json_message_types"], ["auth"])
        for key in (
            "rfq_quote_allowed", "rfq_quote_cancel_allowed", "rfq_confirmation_response_allowed",
            "orders_allowed", "signatures_allowed", "transactions_allowed",
        ):
            self.assertFalse(outbound[key])

    def test_trade_storage_respects_the_frozen_cap(self) -> None:
        mapping = _mapping()
        contract = frozen_contract()
        contract["storage"]["maximum_trade_records"] = 1
        trade = {
            "type": "RFQ_TRADE", "rfq_id": "trade-1", "requester_id": "private",
            "condition_id": "0x" + "cc" * 32, "leg_position_ids": ["102", "201"],
            "direction": "BUY", "side": "YES", "price_e6": "500000",
            "size_e6": "1000000", "executed_at": 2_000_100,
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = V058Store(Path(temporary) / "capture.db", contract["storage"], mapping, _summary(mapping))
            store.open_new("a" * 64, 3600)
            run_id = store.start_run()
            store.record_payload(run_id, trade, 2_000_100, contract["scope"])
            trade["rfq_id"] = "trade-2"
            store.record_payload(run_id, trade, 2_000_200, contract["scope"])
            totals = store.db.execute(
                "SELECT trade_records,trade_records_dropped FROM v058_totals WHERE run_id=?", (run_id,)
            ).fetchone()
            stored_count = store.db.execute("SELECT COUNT(*) FROM v058_trades").fetchone()[0]
            store.close()
        self.assertEqual(tuple(totals), (1, 1))
        self.assertEqual(stored_count, 1)


if __name__ == "__main__":
    unittest.main()
