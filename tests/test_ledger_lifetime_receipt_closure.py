import hashlib
import json
from pathlib import Path

import pytest
from eth_abi import encode
from eth_hash.auto import keccak

from polymarket_bot.ledger.common import EvidenceError, canonical, digest
from polymarket_bot.ledger.lifetime_receipt_closure import close_lifetime_receipt_gap


WALLET = "0x" + "11" * 20
PEER = "0x" + "22" * 20
CASH = "0x" + "33" * 20
BLOCK_HASH = "0x" + "44" * 32
KNOWN_TX = "0x" + "55" * 32
GAP_TX = "0x" + "66" * 32


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def transfer(transaction: str, index: int, quantity: int) -> dict:
    return {"address": CASH, "blockHash": BLOCK_HASH, "blockNumber": hex(100),
            "data": "0x" + encode(["uint256"], [quantity]).hex(),
            "logIndex": hex(index), "removed": False,
            "topics": ["0x" + keccak(b"Transfer(address,address,uint256)").hex(),
                       "0x" + encode(["address"], [PEER]).hex(),
                       "0x" + encode(["address"], [WALLET]).hex()],
            "transactionHash": transaction, "transactionIndex": "0x0"}


def fixture(root: Path) -> tuple[Path, Path, Path, dict]:
    source = root / "source"
    source.mkdir()
    capture_hash = "capture:fixture"
    (source / "transactions.json").write_text(canonical([KNOWN_TX]) + "\n", encoding="utf-8")
    (source / "run_manifest.json").write_text(canonical({
        "result": {"capture_hash": capture_hash}
    }) + "\n", encoding="utf-8")

    crosscheck = root / "crosscheck"
    shards = crosscheck / "coverage_shards"
    shards.mkdir(parents=True)
    known, gap = transfer(KNOWN_TX, 0, 1), transfer(GAP_TX, 1, 2)
    config_hash = digest({"fixture": True})
    shard = shards / "000000100-000000100.json"
    shard.write_text(canonical({"schema": 1, "configuration_hash": config_hash,
                                "range": {"first_block": 100, "last_block": 100},
                                "logs": [known, gap]}) + "\n", encoding="utf-8")
    summary = {"schema": 1, "wallet": WALLET, "configuration_hash": config_hash,
               "source_capture_hash": capture_hash,
               "range": {"start_block": 100, "end_block": 100},
               "origin": {"zero_opening_proven": True},
               "progress": {"ranges": 1, "ranges_total": 1},
               "coverage": {"continuous_blocks": True, "starts_at_wallet_origin": True},
               "comparison": {"rpc_wallet_balance_logs": 2, "extra_count": 1},
               "coverage_manifest": [{"file": shard.name, "sha256": sha(shard)}]}
    configuration = {"schema": 1, "chain": 137, "wallet": WALLET,
                     "closing_block_hash": BLOCK_HASH}
    summary_path, config_path = crosscheck / "summary.json", crosscheck / "configuration.json"
    summary_path.write_text(canonical(summary) + "\n", encoding="utf-8")
    config_path.write_text(canonical(configuration) + "\n", encoding="utf-8")
    (crosscheck / "run_manifest.json").write_text(canonical({
        "summary_sha256": sha(summary_path), "configuration_sha256": sha(config_path)
    }) + "\n", encoding="utf-8")
    scope = root / "scope.json"
    scope.write_text(canonical({"schema": 1, "chain": 137, "balance_contracts": [
        {"label": "Cash", "address": CASH, "token_standard": "erc20"}
    ]}) + "\n", encoding="utf-8")
    return crosscheck, source, scope, gap


def closure(transaction: str, raw: dict) -> dict:
    return {"transaction_hash": transaction, "block_number": 100,
            "block_hash": BLOCK_HASH, "transaction_index": 0,
            "timestamp": None, "method": None, "logs": [raw],
            "log_source": "rpc:fixture", "receipt_response_hash": digest(raw)}


def test_closes_only_omitted_transaction_and_reproduces_log(tmp_path):
    crosscheck, source, scope, gap = fixture(tmp_path)

    def fetch(_url, transactions):
        assert transactions == [GAP_TX]
        return [closure(GAP_TX, gap)]

    output = tmp_path / "closed"
    result = close_lifetime_receipt_gap(
        crosscheck=crosscheck, source_capture=source, scope_path=scope,
        wallet=WALLET, rpc_url="https://rpc.example", output=output,
        fetch_batch=fetch,
    )
    assert result["status"] == "RECEIPTS_CLOSED"
    assert result["progress"]["gap_transactions"] == 1
    assert result["progress"]["union_transactions"] == 2
    assert result["progress"]["crosscheck_wallet_logs_reproduced"] == 1
    assert result["coverage"]["bundle_created"] is False
    assert (output / "run_manifest.json").is_file()


def test_receipt_must_reproduce_sealed_gap_log(tmp_path):
    crosscheck, source, scope, gap = fixture(tmp_path)
    wrong = {**gap, "data": "0x" + encode(["uint256"], [3]).hex()}
    with pytest.raises(EvidenceError, match="reproduce"):
        close_lifetime_receipt_gap(
            crosscheck=crosscheck, source_capture=source, scope_path=scope,
            wallet=WALLET, rpc_url="https://rpc.example", output=tmp_path / "closed",
            retries=1, fetch_batch=lambda _url, _transactions: [closure(GAP_TX, wrong)],
        )
