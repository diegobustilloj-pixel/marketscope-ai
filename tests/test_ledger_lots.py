import pytest

from polymarket_bot.ledger.common import EvidenceError
from polymarket_bot.ledger.lots import LotLedger
from test_ledger_decoders import A, B

X = "137:" + A + ":1"
Y = "137:" + A + ":2"
QUOTE = "137:" + B + ":erc20"


def opening():
    return {"evidence": "raw-opening-hash", "quote_asset": QUOTE, "cash": {A: 1000}, "lots": []}


def leg(asset, quantity):
    return {"asset": asset, "quantity": quantity}


def action(i, kind, inputs=(), outputs=(), cash=0, **extra):
    return {"id": str(i), "order": [i, 0, 0], "wallet": A, "kind": kind,
            "inputs": list(inputs), "outputs": list(outputs), "cash_delta": cash,
            "raw_ids": ["raw:" + str(i)], **extra}


def test_fifo_fees_fractional_basis_replay_and_atomic_batch():
    actions = [action(1, "buy", outputs=[leg(X, 3)], cash=-10),
               action(2, "sell", inputs=[leg(X, 1)], cash=5),
               action(3, "sell", inputs=[leg(X, 2)], cash=12)]
    first, second = LotLedger(opening()), LotLedger(opening())
    first.apply_batch(actions)
    for a in actions:
        second.apply_batch([a])
    first.apply_batch(actions)
    assert first.snapshot() == second.snapshot()
    assert first.snapshot()["known_realized_pnl"][A] == 7
    before = first.snapshot()
    with pytest.raises(EvidenceError, match="Insufficient"):
        first.apply_batch([action(4, "buy", outputs=[leg(X, 1)], cash=-1),
                           action(5, "sell", inputs=[leg(X, 9)], cash=10)])
    assert first.snapshot() == before


def test_split_convert_merge_redeem_preserve_basis():
    ledger = LotLedger(opening())
    ledger.apply_batch([action(1, "split", outputs=[leg(X, 10), leg(Y, 10)], cash=-10),
                        action(2, "convert", inputs=[leg(X, 10)], outputs=[leg(Y, 10)], cash=2),
                        action(3, "merge", inputs=[leg(Y, 10)], cash=6),
                        action(4, "redeem", inputs=[leg(Y, 10)], cash=10)])
    result = ledger.snapshot()
    assert result["cash"][A] == 1008
    assert result["known_realized_pnl"][A] == 8
    assert result["journal"][1]["realized_pnl"] == 0


def test_known_transfer_carries_basis_unknown_receipt_never_zero_basis():
    ledger = LotLedger(opening())
    ledger.apply_batch([action(1, "buy", outputs=[leg(X, 10)], cash=-20),
                        action(2, "transfer", inputs=[leg(X, 4)], counterparty=B),
                        action(3, "receive", outputs=[leg(Y, 5)]),
                        action(4, "sell", inputs=[leg(Y, 5)], cash=7)])
    result = ledger.snapshot({X: "3"})
    assert result["unrealized_pnl"][A] == "6"
    assert result["unrealized_pnl"][B] == "4"
    assert result["unknown_realizations"] == ["4"]
    assert result["complete_basis"] is False
    assert result["lots"][1]["cost"] == 8


def test_wrap_unwrap_reward_and_uint256():
    ledger = LotLedger(opening())
    large = 2**200
    ledger.apply_batch([action(1, "buy", outputs=[leg(X, large)], cash=-11),
                        action(2, "wrap", inputs=[leg(X, large)], outputs=[leg(Y, large)]),
                        action(3, "unwrap", inputs=[leg(Y, large)], outputs=[leg(X, large)]),
                        action(4, "reward", cash=3)])
    assert ledger.snapshot()["lots"][-1]["cost"] == 11
    assert ledger.snapshot()["balances"][A][X] == large
    assert ledger.snapshot()["known_realized_pnl"][A] == 3


def test_float_conflicting_action_and_missing_opening_cash():
    ledger = LotLedger(opening())
    with pytest.raises(EvidenceError, match="float"):
        ledger.apply_batch([action(1, "buy", outputs=[leg(X, 1)], cash=-0.5)])
    with pytest.raises(EvidenceError, match="collateral"):
        ledger.apply_batch([action(1, "buy", outputs=[leg(X, 1)], cash=-1001)])
    ledger.apply_batch([action(1, "buy", outputs=[leg(X, 1)], cash=-5)])
    with pytest.raises(EvidenceError, match="Conflicting"):
        ledger.apply_batch([action(1, "buy", outputs=[leg(X, 2)], cash=-5)])


def test_nested_merge_carries_basis_to_parent_output():
    ledger = LotLedger(opening())
    ledger.apply_batch([
        action(1, "buy", outputs=[leg(X, 10)], cash=-20),
        action(2, "merge", inputs=[leg(X, 10)], outputs=[leg(Y, 5)], cash=0),
    ])
    result = ledger.snapshot()
    assert result["lots"][-1]["remaining_cost"] == 20
    assert result["journal"][-1]["realized_pnl"] == 0


def test_cost_free_split_and_unsupported_received_basis_fail_closed():
    ledger = LotLedger(opening())
    with pytest.raises(EvidenceError, match="consumed collateral"):
        ledger.apply_batch([action(1, "split", outputs=[leg(X, 1)], cash=0)])
    with pytest.raises(EvidenceError, match="independent evidence"):
        ledger.apply_batch([action(1, "receive", outputs=[leg(X, 2)], received_basis=3)])


def test_evidenced_external_receipt_starts_known_basis():
    ledger = LotLedger(opening())
    ledger.apply_batch([action(1, "receive", outputs=[leg(X, 2)], received_basis=3,
                               basis_evidence=["external-mark:1"])])
    result = ledger.snapshot({X: "2"})
    assert result["complete_basis"] is True
    assert result["lots"][-1]["remaining_cost"] == 3
    assert "external-mark:1" in result["lots"][-1]["provenance"]
    assert result["unrealized_pnl"][A] == "1"


def test_indexed_fifo_matches_global_order_across_batches():
    ledger = LotLedger(opening())
    ledger.apply_batch([
        action(1, "buy", outputs=[leg(X, 3)], cash=-9),
        action(2, "buy", outputs=[leg(Y, 4)], cash=-8),
        action(3, "buy", outputs=[leg(X, 5)], cash=-25),
    ])
    # A second batch exercises deepcopy while preserving the internal index's
    # references to the canonical global lot records.
    ledger.apply_batch([action(4, "sell", inputs=[leg(X, 4)], cash=20)])
    result = ledger.snapshot()
    sale = result["journal"][-1]
    assert sale["consumed_lots"] == [
        {"lot_id": "1:0", "quantity": 3, "cost": 9},
        {"lot_id": "3:0", "quantity": 1, "cost": 5},
    ]
    assert sale["realized_pnl"] == 6
    assert result["balances"][A][X] == 4
    assert result["lots"][2]["remaining_cost"] == 20


def test_indexed_fifo_rolls_back_partial_consumption_in_failed_batch():
    ledger = LotLedger(opening())
    ledger.apply_batch([
        action(1, "buy", outputs=[leg(X, 2)], cash=-4),
        action(2, "buy", outputs=[leg(Y, 2)], cash=-6),
        action(3, "buy", outputs=[leg(X, 3)], cash=-9),
    ])
    before = ledger.snapshot()
    with pytest.raises(EvidenceError, match="Insufficient"):
        ledger.apply_batch([
            action(4, "sell", inputs=[leg(X, 4)], cash=14),
            action(5, "sell", inputs=[leg(Y, 3)], cash=10),
        ])
    assert ledger.snapshot() == before
