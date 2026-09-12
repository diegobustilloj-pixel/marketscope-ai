from __future__ import annotations

import json
import asyncio
import tempfile
import unittest
import zlib
from pathlib import Path

from polymarket_bot.v056_rfq_observer import CredentialBundle, sanitize_inbound
from polymarket_bot.v057_audit import evaluate_trade
from polymarket_bot.v057_contract import frozen_contract
from polymarket_bot.v057_joined_replay import (
    V057Store,
    collect_joined_replay,
    request_job,
    sanitize_books,
)


def _request(rfq_id: str = "rfq-a") -> dict[str, object]:
    return {
        "type": "RFQ_REQUEST",
        "rfq_id": rfq_id,
        "requestor_public_id": "secret-requester",
        "condition_id": "0x" + "ab" * 32,
        "leg_position_ids": ["11", "22"],
        "yes_position_id": "33",
        "no_position_id": "44",
        "direction": "BUY",
        "side": "YES",
        "requested_size": {"unit": "notional", "value_e6": "5000000"},
        "submission_deadline": 2_000_000,
    }


class V057JoinedReplayTests(unittest.TestCase):
    def test_scope_accepts_buy_yes_and_hash_sampling_is_deterministic(self) -> None:
        record = sanitize_inbound(_request(), 1_999_700)
        first = request_job(record, frozen_contract()["scope"])
        second = request_job(record, frozen_contract()["scope"])
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertEqual(first["leg_position_ids"], ["11", "22"])
        self.assertNotIn("secret-requester", json.dumps(first, default=str))

    def test_scope_rejects_sell(self) -> None:
        payload = _request()
        payload["direction"] = "SELL"
        self.assertIsNone(request_job(sanitize_inbound(payload, 1), frozen_contract()["scope"]))

    def test_sanitized_books_keep_only_exact_asks_and_identifiers(self) -> None:
        result = sanitize_books(
            [
                {"asset_id": "11", "market": "0x1", "timestamp": "100", "bids": [{"price": "0.09", "size": "99"}], "asks": [{"price": "0.12", "size": "4"}]},
                {"asset_id": "22", "market": "0x2", "timestamp": "101", "asks": [{"price": "0.20", "size": "5"}]},
            ],
            ["11", "22"],
        )
        decoded = zlib.decompress(result["compressed"]).decode("utf-8")
        self.assertNotIn("bids", decoded)
        self.assertEqual(result["source_max_ms"], 101)

    def test_economic_replay_uses_exact_depth_fee_and_technical_budget(self) -> None:
        books = [
            {"asset_id": "11", "market": "0x1", "timestamp": 100, "asks": [["0.10", "10"]]},
            {"asset_id": "22", "market": "0x2", "timestamp": 100, "asks": [["0.30", "10"]]},
        ]
        request = {
            "received_at_ms": 100, "submission_deadline_ms": 500,
            "book_finished_at_ms": 300, "book_source_max_ms": 100,
            "leg_position_ids_json": '["11","22"]',
            "asks_zlib": zlib.compress(json.dumps(books).encode("utf-8")),
        }
        trade = {
            "direction": "BUY", "side": "YES", "price_e6": "150000",
            "size_e6": "5000000", "leg_position_ids_json": '["11","22"]',
        }

        def info(_: str) -> dict[str, object]:
            return {"mos": "1", "mts": "0.01", "fees_enabled": False, "t": []}

        result = evaluate_trade(request, trade, info, frozen_contract()["scope"])
        self.assertTrue(result["evaluable"])
        self.assertTrue(result["technical_within_400ms_budget"])
        self.assertTrue(result["candidate"])
        self.assertAlmostEqual(result["hedge_total_cost_usdc"], 0.5)
        self.assertAlmostEqual(result["locked_edge_per_share"], 0.049999, places=6)

    def test_store_never_has_secret_or_order_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "v057.db"
            store = V057Store(path, frozen_contract()["storage"])
            store.open_new("a" * 64, 3600)
            run_id = store.start_run()
            store.record_payload(run_id, _request(), 1_999_700, frozen_contract()["scope"])
            tables = ["v057_meta", "v057_runs", "v057_message_counts", "v057_totals", "v057_joined_requests", "v057_trades"]
            columns = {
                row[1]
                for table in tables
                for row in store.db.execute(f"PRAGMA table_info({table})")
            }
            store.close()
            forbidden = {"api_key", "api_secret", "private_key", "raw_payload", "requestor_public_id", "order"}
            self.assertFalse(columns.intersection(forbidden))

    def test_contract_blocks_every_trading_path(self) -> None:
        contract = frozen_contract()
        self.assertEqual(contract["outbound"]["rfq_allowed_json_message_types"], ["auth"])
        for key in (
            "rfq_quote_allowed", "rfq_quote_cancel_allowed", "rfq_confirmation_response_allowed",
            "orders_allowed", "signatures_allowed", "transactions_allowed",
        ):
            self.assertFalse(contract["outbound"][key])
        self.assertEqual(contract["safety"]["real_money"], "BLOQUEADO")

    def test_short_fake_capture_authenticates_joins_and_never_sends_quote(self) -> None:
        scope = frozen_contract()["scope"]
        sampled_payload = None
        for index in range(100):
            candidate = _request(f"rfq-{index}")
            job = request_job(sanitize_inbound(candidate, 1_999_700), scope)
            if job and job["sampled"]:
                sampled_payload = candidate
                break
        self.assertIsNotNone(sampled_payload)

        class FakeSocket:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.messages = [json.dumps({"type": "auth", "success": True}), json.dumps(sampled_payload)]

            async def send(self, message: str) -> None:
                self.sent.append(message)

            async def recv(self) -> str:
                if self.messages:
                    return self.messages.pop(0)
                await asyncio.Future()
                raise AssertionError("unreachable")

        class Context:
            def __init__(self, socket: FakeSocket) -> None:
                self.socket = socket

            async def __aenter__(self) -> FakeSocket:
                return self.socket

            async def __aexit__(self, *args: object) -> None:
                return None

        socket = FakeSocket()

        def books(_: str, tokens: list[str], __: float) -> list[dict[str, object]]:
            return [
                {"asset_id": token, "market": f"0x{ordinal}", "timestamp": 1_999_800, "asks": [{"price": "0.2", "size": "10"}]}
                for ordinal, token in enumerate(tokens)
            ]

        contract = frozen_contract()
        contract["transport"]["receive_poll_seconds"] = 0.01
        with tempfile.TemporaryDirectory() as temporary:
            store = V057Store(Path(temporary) / "capture.db", contract["storage"])
            store.open_new("a" * 64, 3600)
            run_id = store.start_run()
            result = asyncio.run(
                collect_joined_replay(
                    endpoint="wss://example.invalid",
                    credentials=CredentialBundle("k", "s", "p", "0x" + "1" * 40, "0x" + "2" * 40, 2),
                    store=store,
                    run_id=run_id,
                    contract=contract,
                    connect_factory=lambda *args, **kwargs: Context(socket),
                    book_fetcher=books,
                    duration_seconds=0.05,
                )
            )
            totals = dict(store.db.execute("SELECT * FROM v057_totals").fetchone()) if store.db.row_factory else None
            raw_totals = store.db.execute(
                "SELECT all_requests,eligible_requests,sampled_requests,book_fetch_success FROM v057_totals"
            ).fetchone()
            store.close()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(raw_totals, (1, 1, 1, 1))
        self.assertEqual(len(socket.sent), 1)
        self.assertEqual(json.loads(socket.sent[0])["type"], "auth")


if __name__ == "__main__":
    unittest.main()
