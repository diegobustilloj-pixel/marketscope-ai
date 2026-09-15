import hashlib
import json
from pathlib import Path

import pytest
from eth_abi import encode
from eth_hash.auto import keccak

from polymarket_bot.ledger.common import EvidenceError, canonical, digest
from polymarket_bot.ledger.lifetime_inventory import reconstruct_lifetime_inventory


WALLET = "0x" + "11" * 20
PEER = "0x" + "22" * 20
CASH = "0x" + "33" * 20
CTF = "0x" + "44" * 20
BLOCK_HASH = "0x" + "55" * 32
TX = "0x" + "66" * 32


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def topic_address(value: str) -> str:
    return "0x" + encode(["address"], [value]).hex()


def log(*, contract: str, signature: str, topics: list[str], data: str, index: int) -> dict:
    return {"address": contract, "blockHash": BLOCK_HASH, "blockNumber": hex(100),
            "data": data, "logIndex": hex(index), "removed": False,
            "topics": ["0x" + keccak(signature.encode()).hex(), *topics],
            "transactionHash": TX, "transactionIndex": "0x0"}


class StateRPC:
    url = "https://state.example"

    def __init__(self, balances):
        self.balances = balances

    def call(self, method, params):
        if method == "eth_chainId":
            return hex(137)
        raise AssertionError(method)

    def block(self, number):
        assert number == 100
        return {"number": hex(100), "hash": BLOCK_HASH}

    def batch(self, calls):
        result = []
        for method, params in calls:
            assert method == "eth_call"
            target = params[0]["to"]
            if target == CASH:
                result.append(f"0x{self.balances[f'137:{CASH}:erc20']:064x}")
            else:
                result.append("0x" + encode(["uint256[]"], [[self.balances[f"137:{CTF}:7"]]]).hex())
        return result


def fixture(root: Path) -> tuple[Path, Path]:
    crosscheck = root / "crosscheck"
    shards = crosscheck / "coverage_shards"
    shards.mkdir(parents=True)
    logs = [
        log(contract=CASH, signature="Transfer(address,address,uint256)",
            topics=[topic_address(PEER), topic_address(WALLET)],
            data="0x" + encode(["uint256"], [1_000_000]).hex(), index=0),
        log(contract=CTF, signature="TransferSingle(address,address,address,uint256,uint256)",
            topics=[topic_address(PEER), topic_address(PEER), topic_address(WALLET)],
            data="0x" + encode(["uint256", "uint256"], [7, 600_000]).hex(), index=1),
    ]
    config_hash = digest({"fixture": True})
    shard = shards / "000000100-000000100.json"
    shard.write_text(canonical({"schema": 1, "configuration_hash": config_hash,
                                "range": {"first_block": 100, "last_block": 100},
                                "logs": logs}) + "\n", encoding="utf-8")
    summary = {"schema": 1, "wallet": WALLET, "configuration_hash": config_hash,
               "range": {"start_block": 100, "end_block": 100},
               "origin": {"zero_opening_proven": True},
               "progress": {"ranges": 1, "ranges_total": 1},
               "coverage": {"continuous_blocks": True, "starts_at_wallet_origin": True},
               "comparison": {"rpc_wallet_balance_logs": 2, "extra_count": 0},
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
        {"label": "Cash", "address": CASH, "token_standard": "erc20"},
        {"label": "CTF", "address": CTF, "token_standard": "erc1155"},
    ]}) + "\n", encoding="utf-8")
    return crosscheck, scope


def test_reconstructs_and_reconciles_zero_origin_inventory(tmp_path):
    crosscheck, scope = fixture(tmp_path)
    report, inventory, state = reconstruct_lifetime_inventory(
        crosscheck=crosscheck, scope_path=scope, wallet=WALLET,
        state_rpc=StateRPC({f"137:{CASH}:erc20": 1_000_000, f"137:{CTF}:7": 600_000}),
    )
    assert report["status"] == "INVENTORY_RECONCILED"
    assert report["reconciliation"]["status"] == "MATCH"
    assert report["inventory"]["nonzero_assets"] == 2
    assert {row["quantity_6dp"] for row in inventory} == {"1.000000", "0.600000"}
    assert state["errors"] == []
    assert report["cost_basis"]["available"] is False


def test_state_mismatch_blocks_inventory(tmp_path):
    crosscheck, scope = fixture(tmp_path)
    report, _, _ = reconstruct_lifetime_inventory(
        crosscheck=crosscheck, scope_path=scope, wallet=WALLET,
        state_rpc=StateRPC({f"137:{CASH}:erc20": 999_999, f"137:{CTF}:7": 600_000}),
    )
    assert report["status"] == "INVENTORY_BLOCKED"
    assert report["reconciliation"]["mismatches"]


def test_tampered_shard_is_rejected(tmp_path):
    crosscheck, scope = fixture(tmp_path)
    shard = next((crosscheck / "coverage_shards").glob("*.json"))
    shard.write_text(shard.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(EvidenceError, match="hash mismatch"):
        reconstruct_lifetime_inventory(
            crosscheck=crosscheck, scope_path=scope, wallet=WALLET,
            state_rpc=StateRPC({}),
        )
