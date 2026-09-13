import json
from pathlib import Path

from eth_abi import encode

from polymarket_bot.ledger.valuation import (
    _cash_transfer_audit,
    _expanded_balances,
    _mark_before,
)
from polymarket_bot.ledger.wallet_capture import TRANSFER_TOPICS


WALLET = "0x" + "11" * 20
CTF = "0x" + "22" * 20
CASH = "0x" + "33" * 20


def test_mark_before_ignores_lookahead_and_marks_staleness():
    history = [{"t": 90, "p": "0.4"}, {"t": 101, "p": "0.9"}]
    assert _mark_before(history, 100, 15) == {
        "status": "FRESH", "price": "0.4", "timestamp": 90, "age_seconds": 10,
    }
    assert _mark_before(history, 100, 5)["status"] == "STALE"
    assert _mark_before([{"t": 101, "p": "0.9"}], 100, 15)["status"] == "MISSING"


def test_expanded_balances_decodes_erc1155_batch_and_cash():
    deployments = {"contracts": [
        {"address": CASH, "token_standard": "erc20"},
        {"address": CTF, "token_standard": "erc1155"},
    ]}

    class RPC:
        def batch(self, calls):
            result = []
            for _, params in calls:
                selector = params[0]["data"][:10]
                result.append("0x" + (encode(["uint256"], [7]) if selector == "0x70a08231"
                                      else encode(["uint256[]"], [[8, 9]])).hex())
            return result

        def block(self, number):
            return {"hash": "0x" + "44" * 32}

    result = _expanded_balances(
        RPC(), {"number": "0x1", "hash": "0x" + "44" * 32}, WALLET, deployments, [5, 6]
    )
    assert result["complete_for_declared_scope"] is True
    assert result["balances"][f"137:{CASH}:erc20"] == 7
    assert result["balances"][f"137:{CTF}:5"] == 8
    assert result["balances"][f"137:{CTF}:6"] == 9


def test_cash_transfer_audit_matches_activity_atomic_amount(tmp_path: Path):
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    tx = "0x" + "55" * 32
    wallet_topic = "0x" + "00" * 12 + WALLET[2:]
    counterparty = "0x" + "00" * 12 + "66" * 20
    receipt = {"transactionHash": tx, "logs": [{
        "address": CASH,
        "topics": [TRANSFER_TOPICS["erc20"], wallet_topic, counterparty],
        "data": "0x" + encode(["uint256"], [1_250_000]).hex(),
    }]}
    (receipt_dir / f"{tx}.json").write_text(json.dumps(receipt), encoding="utf-8")
    deployments = {"contracts": [{"address": CASH, "label": "cash", "token_standard": "erc20"}]}
    activity = [{"transactionHash": tx, "type": "TRADE", "side": "BUY", "usdcSize": "1.25"}]
    result = _cash_transfer_audit(tmp_path, WALLET, deployments, activity)
    assert result["status"] == "MATCH"
    assert result["actual_net_cash_delta_atomic"] == -1_250_000
    assert result["external_net_funding_atomic"] == 0
