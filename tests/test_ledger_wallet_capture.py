import json
from pathlib import Path

import pytest

from polymarket_bot.ledger.common import EvidenceError
from polymarket_bot.ledger.wallet_capture import (
    _abis,
    _balance_calls,
    _balances,
    _filters,
    _first_block_at_or_after,
    _save,
    _token_ids,
)


ROOT = Path(__file__).resolve().parents[1]
WALLET = "0x" + "11" * 20


def inputs():
    deployments = json.loads((ROOT / "configs/polyledger/polygon_wallet_capture.json").read_text())
    abis = _abis(ROOT / "configs/polyledger/abi_catalog.json",
                 ROOT / "configs/polyledger/combo_position_manager_abi.json")
    return deployments, abis


def test_capture_filters_are_wallet_indexed_and_economic_only():
    deployments, abis = inputs()
    filters = _filters(deployments, abis, WALLET)
    assert filters
    assert {row["event"] for row in filters} <= {
        "OrderFilled", "TransferSingle", "TransferBatch", "PositionSplit",
        "PositionsMerge", "PositionsConverted", "PayoutRedemption", "Transfer",
    }
    assert all(row["topics"][row["topic_index"]].endswith(WALLET[2:]) for row in filters)
    assert all(row["field"] in {"maker", "taker", "stakeholder", "redeemer", "from", "to", "operator"}
               for row in filters)
    assert not any(row["event"] in {"OrdersMatched", "FeeCharged", "Approval"} for row in filters)


def test_block_boundary_finds_first_timestamp_at_or_after_target():
    class RPC:
        def block(self, number):
            return {"timestamp": hex(number * 2)}
    assert _first_block_at_or_after(RPC(), 12345, 100000) == 6173


def test_balance_scope_includes_each_collateral_and_both_token_contracts():
    deployments, _ = inputs()
    calls = _balance_calls(WALLET, deployments, [7, 9])
    assets = [row[0] for row in calls]
    assert len(assets) == len(set(assets)) == 7
    assert sum(asset.endswith(":erc20") for asset in assets) == 3
    assert sum(asset.endswith(":7") for asset in assets) == 2
    assert sum(asset.endswith(":9") for asset in assets) == 2


def test_token_scope_expands_from_wallet_seed_transfers():
    from eth_abi import encode
    from polymarket_bot.ledger.wallet_capture import TRANSFER_TOPICS
    contract = "0x" + "22" * 20
    raw = {"address": contract, "topics": [TRANSFER_TOPICS["batch"]],
           "data": "0x" + encode(["uint256[]", "uint256[]"], [[7, 9], [1, 2]]).hex()}
    assert _token_ids([raw], {contract}) == {7, 9}


def test_resumable_json_refuses_conflicting_evidence(tmp_path):
    target = tmp_path / "evidence.json"
    _save(target, {"value": 1})
    _save(target, {"value": 1})
    with pytest.raises(EvidenceError, match="conflicts"):
        _save(target, {"value": 2})


def test_balance_failure_is_retained_instead_of_erasing_capture():
    class RPC:
        def batch(self, calls):
            raise EvidenceError("one failed entry")
        def call(self, method, params):
            if params[0]["to"].endswith("22" * 20):
                raise EvidenceError("unavailable")
            return "0x" + "00" * 31 + "07"
    calls = [("137:0x" + "11" * 20 + ":erc20", "0x70a08231" + "00" * 32),
             ("137:0x" + "22" * 20 + ":erc20", "0x70a08231" + "00" * 32)]
    result = _balances(RPC(), {"number": "0x1", "hash": "0x" + "33" * 32}, WALLET, calls)
    assert result["complete_for_declared_scope"] is False
    assert list(result["balances"].values()) == [7]
    assert result["errors"] == [calls[1][0]]
    assert result["pinning_methods"] == ["block-number-with-hash-recheck"]
