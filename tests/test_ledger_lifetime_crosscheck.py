from __future__ import annotations

import json
from pathlib import Path

from polymarket_bot.ledger import lifetime_crosscheck
from polymarket_bot.ledger.common import canonical, digest
from polymarket_bot.ledger.history_backfill import TRANSFER
from polymarket_bot.ledger.lifetime_crosscheck import crosscheck_lifetime_capture


WALLET = "0x" + "11" * 20
CONTRACT = "0x" + "22" * 20
TX = "0x" + "33" * 32
ORIGIN_TX = "0x" + "44" * 32
PROXY = (
    "0x363d3d373d3d3d363d73" + "55" * 20
    + "5af43d82803e903d91602b57fd5bf3"
)


def h(number):
    return "0x" + f"{number:064x}"


def raw_log():
    return {
        "address": CONTRACT,
        "blockHash": h(10),
        "blockNumber": "0xa",
        "data": "0x" + f"{7:064x}",
        "logIndex": "0x2",
        "removed": False,
        "topics": [TRANSFER, "0x" + "00" * 12 + WALLET[2:], h(0)],
        "transactionHash": TX,
        "transactionIndex": "0x1",
    }


class RPC:
    url = "https://rpc.example.invalid"

    def __init__(self, *, url=None):
        self.url = url or self.url

    def call(self, method, params):
        if method == "eth_chainId":
            return "0x89"
        if method == "eth_getTransactionReceipt":
            return {"transactionHash": ORIGIN_TX, "blockNumber": "0x9", "status": "0x1"}
        if method == "eth_getCode":
            return "0x" if params[1] == "0x8" else PROXY
        raise AssertionError((method, params))

    def block(self, number):
        return {"number": hex(number), "hash": h(number),
                "parentHash": h(max(0, number - 1)), "timestamp": hex(number * 2)}

    def batch(self, calls):
        result = []
        for method, params in calls:
            assert method == "eth_getLogs"
            query = params[0]
            includes_log = int(query["fromBlock"], 16) <= 10 <= int(query["toBlock"], 16)
            if (includes_log and query["topics"][0] == TRANSFER
                    and query["topics"][1] is not None):
                result.append([raw_log()])
            else:
                result.append([])
        return result


def _write_capture(root: Path):
    closure_dir = root / "transaction_log_shards"
    closure_dir.mkdir(parents=True)
    shard = closure_dir / "000000000-000000000.json"
    shard.write_text(canonical({"closures": [{"logs": [raw_log()]}]}) + "\n", encoding="utf-8")
    import hashlib
    shard_hash = hashlib.sha256(shard.read_bytes()).hexdigest()
    result = {
        "status": "CAPTURED_BLOCKED",
        "configuration": {"wallet": WALLET},
        "coverage": {"transfer_pagination_complete": True,
                     "full_transaction_log_closure": True, "anchor_match": True},
        "anchors": {"closing": {"number": "0xa", "hash": h(10)}},
        "capture_hash": digest("capture"),
        "transaction_log_manifest": [{"file": shard.name, "sha256": shard_hash}],
    }
    (root / "run_manifest.json").write_text(
        canonical({"schema": 1, "result": result}) + "\n", encoding="utf-8"
    )


def _inputs(tmp_path):
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({
        "verification_status": "VERIFIED", "verified_proxy_wallet": WALLET,
    }), encoding="utf-8")
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({
        "schema": 1, "chain": 137, "scope": "test",
        "balance_contracts": [{"label": "cash", "address": CONTRACT,
                               "token_standard": "erc20"}],
    }), encoding="utf-8")
    capture = tmp_path / "capture"
    _write_capture(capture)
    return capture, identity, scope


def test_crosscheck_proves_proxy_origin_continuity_and_exact_log_match(tmp_path, monkeypatch):
    capture, identity, scope = _inputs(tmp_path)
    monkeypatch.setattr(lifetime_crosscheck, "ReadOnlyRPC", RPC)
    output = tmp_path / "crosscheck"
    result = crosscheck_lifetime_capture(
        capture=capture, output=output, wallet=WALLET,
        identity_path=identity, scope_path=scope, origin_block=9,
        origin_transaction=ORIGIN_TX, rpc_url="https://rpc.example.invalid",
        range_blocks=2, shard_ranges=1, batch_size=2, workers=1,
    )
    assert result["status"] == "RPC_CROSSCHECK_COMPLETE"
    assert result["coverage"]["starts_at_wallet_origin"] is True
    assert result["coverage"]["continuous_blocks"] is True
    assert result["comparison"]["source_wallet_balance_logs"] == 1
    assert result["comparison"]["rpc_wallet_balance_logs"] == 1
    assert result["comparison"]["exact_match"] is True
    assert output.is_dir()


def test_crosscheck_is_resumable_and_does_not_claim_partial_coverage(tmp_path, monkeypatch):
    capture, identity, scope = _inputs(tmp_path)
    monkeypatch.setattr(lifetime_crosscheck, "ReadOnlyRPC", RPC)
    output = tmp_path / "crosscheck"
    result = crosscheck_lifetime_capture(
        capture=capture, output=output, wallet=WALLET,
        identity_path=identity, scope_path=scope, origin_block=9,
        origin_transaction=ORIGIN_TX, rpc_url="https://rpc.example.invalid",
        range_blocks=1, shard_ranges=1, batch_size=2, workers=1, max_shards=1,
    )
    assert result["status"] == "IN_PROGRESS"
    assert result["raw_actions_complete"] is False
    resumed = crosscheck_lifetime_capture(
        capture=capture, output=output, wallet=WALLET,
        identity_path=identity, scope_path=scope, origin_block=9,
        origin_transaction=ORIGIN_TX, rpc_url="https://rpc.example.invalid",
        range_blocks=1, shard_ranges=1, batch_size=2, workers=1,
    )
    assert resumed["status"] == "RPC_CROSSCHECK_COMPLETE"
    assert output.is_dir()
