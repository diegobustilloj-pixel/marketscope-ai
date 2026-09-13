import json
from pathlib import Path

from eth_abi import encode

from polymarket_bot.ledger.valuation import (
    _cash_transfer_audit,
    _combo_resolution_marks,
    _ctf_settlement_marks,
    _expanded_balances,
    _mark_before,
    _merge_token_metadata,
    _merge_position_rows,
    _value_cut,
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


def test_cash_only_outbound_transfer_is_external_funding(tmp_path: Path):
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    tx = "0x" + "77" * 32
    wallet_topic = "0x" + "00" * 12 + WALLET[2:]
    counterparty = "0x" + "00" * 12 + "66" * 20
    receipt = {"transactionHash": tx, "logs": [{
        "address": CASH,
        "topics": [TRANSFER_TOPICS["erc20"], wallet_topic, counterparty],
        "data": "0x" + encode(["uint256"], [2_000_000]).hex(),
    }]}
    (receipt_dir / f"{tx}.json").write_text(json.dumps(receipt), encoding="utf-8")
    deployments = {"contracts": [{"address": CASH, "label": "cash", "token_standard": "erc20"}]}
    result = _cash_transfer_audit(tmp_path, WALLET, deployments, [])
    assert result["status"] == "MATCH"
    assert result["external_net_funding_atomic"] == -2_000_000
    assert result["transactions"][0]["classification"] == "ONCHAIN_DIRECT_EXTERNAL_OUTFLOW"


def test_combo_resolved_loss_is_zero_at_both_cuts_when_balances_match():
    combo = "0x" + "88" * 20
    token = "123"
    asset = f"137:{combo}:{token}"
    opening = {"balances": {asset: 1_500_000}}
    closing = {"balances": {asset: 1_500_000}}
    positions = [{"combo_position_id": token, "status": "RESOLVED_LOSS",
                  "resolved_at": "2026-01-01T00:00:00Z", "current_size": "1.5"}]
    result = _combo_resolution_marks(opening, closing, combo, positions, 1_800_000_000)
    assert result["errors"] == []
    assert result["marks"][token]["opening"]["price"] == "0"


def test_ctf_settlement_mark_uses_exact_payout_fraction():
    condition = "0x" + "44" * 32

    class RPC:
        def batch(self, calls):
            return ["0x" + encode(["uint256"], [2 if params[0]["data"][:10] == "0xdd34de67" else 1]).hex()
                    for _, params in calls]

        def block(self, number):
            return {"hash": "0x" + "55" * 32}

    result = _ctf_settlement_marks(
        RPC(), {"number": "0x1", "hash": "0x" + "55" * 32}, CTF, [7],
        {"7": {"condition_id": condition, "outcome_index": 1, "outcome": "Yes"}},
    )
    assert result["marks"]["7"]["status"] == "SETTLED"
    assert result["marks"]["7"]["price"] == "0.5"


def test_position_views_are_merged_without_double_counting():
    active = {"conditionId": "0xabc", "asset": "7", "outcomeIndex": 1,
              "redeemable": False, "currentValue": "1"}
    redeemable = {**active, "redeemable": True, "currentValue": "2"}
    assert _merge_position_rows([active], [redeemable]) == [redeemable]


def test_unpriced_position_produces_conservative_equity_bounds():
    balances = {"balances": {
        f"137:{CASH}:erc20": 1_000_000,
        f"137:{CTF}:7": 2_000_000,
    }}
    value = _value_cut(balances, {"7": {"opening": {"price": None}}}, {},
                       CTF, "0x" + "88" * 20, {CASH}, "opening")
    assert value["total_equity_usd"] is None
    assert value["total_equity_lower_bound_usd"] == "1"
    assert value["total_equity_upper_bound_usd"] == "3"


def test_metadata_merge_retains_primary_and_reports_conflict():
    primary = {"7": {"condition_id": "0x" + "11" * 32,
                     "outcome_index": 0, "outcome": "Yes"}}
    supplement = {"7": {"condition_id": "0x" + "22" * 32,
                        "outcome_index": 1, "outcome": "No"},
                  "8": {"condition_id": "0x" + "33" * 32,
                        "outcome_index": 1, "outcome": "No"}}
    merged, conflicts = _merge_token_metadata(primary, supplement)
    assert merged["7"] == primary["7"]
    assert merged["8"] == supplement["8"]
    assert conflicts == ["7"]
