from __future__ import annotations

import json

from polymarket_bot.ledger import blockscout_backfill
from polymarket_bot.ledger.blockscout_backfill import backfill_wallet_blockscout
from polymarket_bot.ledger.history_backfill import TRANSFER


WALLET = "0x" + "11" * 20
CONTRACT = "0x" + "22" * 20
TX = "0x" + "33" * 32


def h(number):
    return "0x" + f"{number:064x}"


class RPC:
    url = "https://rpc.example.invalid"

    def call(self, method, params):
        return {"eth_chainId": "0x89", "eth_blockNumber": "0x14"}[method]

    def block(self, number):
        return {
            "number": hex(number), "hash": h(number),
            "parentHash": h(max(number - 1, 0)), "timestamp": hex(number * 2),
        }


class Client:
    base_url = "https://polygon.blockscout.com/api/v2"

    def __init__(self, indexer_complete=True):
        self.indexer_complete = indexer_complete

    def get(self, path, params=None):
        if path.endswith("/token-transfers"):
            return {
                "items": [{
                    "block_hash": h(9), "block_number": 9,
                    "from": {"hash": WALLET}, "to": {"hash": "0x" + "00" * 20},
                    "log_index": 7, "timestamp": "2026-01-01T00:00:00Z",
                    "token": {"address_hash": CONTRACT, "type": "ERC-20"},
                    "token_type": "ERC-20", "total": {"value": "5"},
                    "transaction_hash": TX,
                }],
                "next_page_params": None,
            }
        if path == f"/transactions/{TX}":
            return {
                "hash": TX, "status": "ok", "position": 4,
                "block_number": 9, "timestamp": "2026-01-01T00:00:00Z",
                "method": "transfer",
            }
        if path == f"/transactions/{TX}/logs":
            return {
                "items": [{
                    "address": {"hash": CONTRACT}, "block_number": 9,
                    "block_hash": h(9), "transaction_hash": TX, "index": 7,
                    "topics": [TRANSFER, "0x" + "00" * 12 + WALLET[2:],
                               "0x" + "00" * 32, None],
                    "data": "0x" + "00" * 32,
                }],
                "next_page_params": None,
            }
        if path == "/main-page/indexing-status":
            return {
                "finished_indexing": self.indexer_complete,
                "finished_indexing_blocks": self.indexer_complete,
                "indexed_blocks_ratio": "1.00" if self.indexer_complete else "0.98",
            }
        if path == "/blocks/9":
            return {"height": 9, "hash": h(9)}
        raise AssertionError((path, params))


def inputs(tmp_path):
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({
        "verification_status": "VERIFIED", "verified_proxy_wallet": WALLET,
    }), encoding="utf-8")
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({
        "schema": 1, "chain": 137, "scope": "test scope",
        "balance_contracts": [{
            "label": "cash", "address": CONTRACT, "token_standard": "erc20",
        }],
    }), encoding="utf-8")
    return identity, scope


def test_blockscout_capture_closes_seed_and_seals(tmp_path):
    identity, scope = inputs(tmp_path)
    output = tmp_path / "capture"
    result = backfill_wallet_blockscout(
        output=output, wallet=WALLET, identity_path=identity, scope_path=scope,
        rpc=RPC(), client=Client(), last_block=9, confirmations=1,
        log_shard_size=1,
    )
    assert result["status"] == "RAW_INDEXER_CAPTURE_COMPLETE"
    assert result["coverage"] == {
        "starts_at_chain_origin": True,
        "transfer_pagination_complete": True,
        "full_transaction_log_closure": True,
        "indexer_reports_complete": True,
        "anchor_match": True,
        "raw_actions_complete": True,
    }
    assert result["progress"]["transactions_closed"] == 1
    assert result["transaction_logs"] == 1
    assert output.is_dir()


def test_incomplete_indexer_is_sealed_but_never_claims_raw_completeness(tmp_path):
    identity, scope = inputs(tmp_path)
    result = backfill_wallet_blockscout(
        output=tmp_path / "capture", wallet=WALLET, identity_path=identity,
        scope_path=scope, rpc=RPC(), client=Client(indexer_complete=False),
        last_block=9, confirmations=1, log_shard_size=1,
    )
    assert result["status"] == "CAPTURED_BLOCKED"
    assert result["coverage"]["raw_actions_complete"] is False
    assert result["ledger_approval"] is False


def test_empty_blockscout_logs_use_validated_rpc_receipt(tmp_path, monkeypatch):
    identity, scope = inputs(tmp_path)

    class EmptyLogsClient(Client):
        def get(self, path, params=None):
            if path == f"/transactions/{TX}/logs":
                return {"items": [], "next_page_params": None}
            return super().get(path, params)

    class ReceiptRPC:
        url = "https://archive.example.invalid"

        def __init__(self, *, url=None):
            self.url = url or self.url

        def call(self, method, params):
            assert method == "eth_getTransactionReceipt"
            assert params == [TX]
            return {
                "transactionHash": TX, "status": "0x1",
                "blockNumber": "0x9", "blockHash": h(9),
                "transactionIndex": "0x4",
                "logs": [{
                    "address": CONTRACT, "blockNumber": "0x9",
                    "blockHash": h(9), "transactionHash": TX,
                    "transactionIndex": "0x4", "logIndex": "0x7",
                    "topics": [TRANSFER, "0x" + "00" * 12 + WALLET[2:],
                               "0x" + "00" * 32],
                    "data": "0x" + "00" * 32, "removed": False,
                }],
            }

    monkeypatch.setattr(blockscout_backfill, "ReadOnlyRPC", ReceiptRPC)
    result = backfill_wallet_blockscout(
        output=tmp_path / "capture", wallet=WALLET, identity_path=identity,
        scope_path=scope, rpc=RPC(), client=EmptyLogsClient(), last_block=9,
        confirmations=1, log_shard_size=1,
        receipt_rpc_url="https://archive.example.invalid",
    )
    assert result["progress"]["transactions_closed"] == 1
    shard = next((tmp_path / "capture" / "transaction_log_shards").glob("*.json"))
    closure = json.loads(shard.read_text(encoding="utf-8"))["closures"][0]
    assert closure["log_source"] == "rpc:archive.example.invalid"
    assert closure["logs"][0]["transactionHash"] == TX


def test_rpc_receipt_batch_can_be_primary_log_source(tmp_path, monkeypatch):
    identity, scope = inputs(tmp_path)

    class ReceiptOnlyClient(Client):
        def get(self, path, params=None):
            if path.startswith("/transactions/"):
                raise AssertionError("Primary receipt batching must not query transaction endpoints")
            return super().get(path, params)

    class BatchRPC:
        url = "https://archive.example.invalid"

        def __init__(self, *, url=None):
            self.url = url or self.url

        def batch(self, calls):
            assert calls == [("eth_getTransactionReceipt", [TX])]
            return [{
                "transactionHash": TX, "status": "0x1",
                "blockNumber": "0x9", "blockHash": h(9),
                "transactionIndex": "0x4",
                "logs": [{
                    "address": CONTRACT, "blockNumber": "0x9",
                    "blockHash": h(9), "transactionHash": TX,
                    "transactionIndex": "0x4", "logIndex": "0x7",
                    "topics": [TRANSFER, "0x" + "00" * 12 + WALLET[2:],
                               "0x" + "00" * 32],
                    "data": "0x" + "00" * 32, "removed": False,
                }],
            }]

    monkeypatch.setattr(blockscout_backfill, "ReadOnlyRPC", BatchRPC)
    result = backfill_wallet_blockscout(
        output=tmp_path / "capture", wallet=WALLET, identity_path=identity,
        scope_path=scope, rpc=RPC(), client=ReceiptOnlyClient(), last_block=9,
        confirmations=1, log_shard_size=1,
        receipt_rpc_url="https://archive.example.invalid",
        receipt_batch_size=1, receipt_batch_workers=1,
    )
    assert result["coverage"]["full_transaction_log_closure"] is True
    shard = next((tmp_path / "capture" / "transaction_log_shards").glob("*.json"))
    closure = json.loads(shard.read_text(encoding="utf-8"))["closures"][0]
    assert closure["log_source"] == "rpc:archive.example.invalid"
    assert "transaction_response_hash" not in closure
