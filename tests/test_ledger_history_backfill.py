from __future__ import annotations

import json
from pathlib import Path

import pytest

from polymarket_bot.ledger.common import EvidenceError
from polymarket_bot.ledger.history_backfill import (
    TRANSFER,
    adaptive_logs,
    backfill_wallet_history,
    discovery_filters,
)


WALLET = "0x" + "11" * 20
CONTRACT = "0x" + "22" * 20
BLOCK_HASH = "0x" + "33" * 32
TX = "0x" + "44" * 32


def scope():
    return {
        "schema": 1,
        "chain": 137,
        "scope": "test declared protocol scope",
        "balance_contracts": [
            {"label": "cash", "address": CONTRACT, "token_standard": "erc20"}
        ],
    }


def log(block=2):
    return {
        "address": CONTRACT,
        "topics": [TRANSFER, "0x" + "00" * 12 + WALLET[2:], "0x" + "00" * 32],
        "data": "0x" + "00" * 32,
        "blockNumber": hex(block),
        "transactionHash": TX,
        "transactionIndex": "0x0",
        "blockHash": BLOCK_HASH,
        "logIndex": "0x0",
        "removed": False,
    }


def test_discovery_is_minimal_and_balance_bearing():
    queries = discovery_filters(scope(), WALLET)
    assert [(row["event"], row["field"], row["topic_index"]) for row in queries] == [
        ("Transfer", "from", 1), ("Transfer", "to", 2)
    ]
    assert all(row["contract"] == CONTRACT for row in queries)


def test_adaptive_query_bisects_without_gaps():
    query = discovery_filters(scope(), WALLET)[0]

    class RPC:
        def call(self, method, params):
            first = int(params[0]["fromBlock"], 16)
            last = int(params[0]["toBlock"], 16)
            if last - first + 1 > 2:
                raise EvidenceError("provider range limit")
            return [log(2)] if first <= 2 <= last else []

    rows, pieces = adaptive_logs(RPC(), query, 1, 5, min_query_blocks=1)
    assert len(rows) == 1
    assert [(row["first_block"], row["last_block"]) for row in pieces] == [
        (1, 2), (3, 3), (4, 5)
    ]


class FakeRPC:
    url = "https://example.invalid"

    def __init__(self, head=100):
        self.head = head

    def call(self, method, params):
        if method == "eth_chainId":
            return "0x89"
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            return {
                "number": hex(number),
                "hash": "0x" + f"{number:064x}",
                "parentHash": "0x" + f"{max(0, number - 1):064x}",
                "timestamp": hex(number * 2),
            }
        if method == "eth_getLogs":
            query = params[0]
            first = int(query["fromBlock"], 16)
            last = int(query["toBlock"], 16)
            return [log(2)] if query["topics"][1] is not None and first <= 2 <= last else []
        raise AssertionError(method)

    def block(self, number):
        return self.call("eth_getBlockByNumber", [hex(number), False])

    def batch(self, calls):
        result = []
        for method, params in calls:
            assert method == "eth_getTransactionReceipt"
            result.append({
                "transactionHash": params[0],
                "blockNumber": "0x2",
                "blockHash": BLOCK_HASH,
                "status": "0x1",
                "logs": [log(2)],
            })
        return result


def write_inputs(tmp_path: Path):
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({
        "verification_status": "VERIFIED", "verified_proxy_wallet": WALLET
    }), encoding="utf-8")
    scope_path = tmp_path / "scope.json"
    scope_path.write_text(json.dumps(scope()), encoding="utf-8")
    return identity, scope_path


def test_history_backfill_resumes_then_seals(tmp_path):
    identity, scope_path = write_inputs(tmp_path)
    output = tmp_path / "history"
    first = backfill_wallet_history(
        output=output, wallet=WALLET, identity_path=identity, scope_path=scope_path,
        rpc=FakeRPC(), first_block=1, last_block=5, confirmations=1,
        segment_blocks=2, min_query_blocks=1, receipt_shard_size=1,
        max_segments=1, max_receipt_shards=0,
    )
    assert first["status"] == "IN_PROGRESS"
    assert first["progress"]["segments_complete"] == 1
    final = backfill_wallet_history(
        output=output, wallet=WALLET, identity_path=identity, scope_path=scope_path,
        rpc=FakeRPC(), first_block=1, last_block=5, confirmations=1,
        segment_blocks=2, min_query_blocks=1, receipt_shard_size=1,
    )
    assert final["status"] == "RAW_HISTORY_CAPTURE_COMPLETE"
    assert final["coverage"]["starts_at_chain_origin"] is True
    assert final["coverage"]["full_receipt_closure"] is True
    assert final["progress"] == {
        "segments_complete": 3, "segments_total": 3,
        "transactions_discovered": 1, "receipts_complete": 1, "receipts_total": 1,
    }
    assert output.exists() and not output.with_name("history.partial").exists()


def test_history_backfill_rejects_config_change_on_resume(tmp_path):
    identity, scope_path = write_inputs(tmp_path)
    output = tmp_path / "history"
    backfill_wallet_history(
        output=output, wallet=WALLET, identity_path=identity, scope_path=scope_path,
        rpc=FakeRPC(), first_block=1, last_block=5, confirmations=1,
        segment_blocks=2, min_query_blocks=1, max_segments=0,
    )
    with pytest.raises(EvidenceError, match="configuration"):
        backfill_wallet_history(
            output=output, wallet=WALLET, identity_path=identity, scope_path=scope_path,
            rpc=FakeRPC(), first_block=1, last_block=5, confirmations=1,
            segment_blocks=3, min_query_blocks=1, max_segments=0,
        )


def test_implicit_end_block_is_frozen_across_resume(tmp_path):
    identity, scope_path = write_inputs(tmp_path)
    output = tmp_path / "history"
    first = backfill_wallet_history(
        output=output, wallet=WALLET, identity_path=identity, scope_path=scope_path,
        rpc=FakeRPC(head=100), first_block=1, confirmations=1,
        segment_blocks=50, min_query_blocks=1, max_segments=0,
    )
    second = backfill_wallet_history(
        output=output, wallet=WALLET, identity_path=identity, scope_path=scope_path,
        rpc=FakeRPC(head=110), first_block=1, confirmations=1,
        segment_blocks=50, min_query_blocks=1, max_segments=0,
    )
    assert first["configuration"]["last_block"] == 99
    assert second["configuration"]["last_block"] == 99
