import copy

from polymarket_bot.ledger.reconcile import reconcile, source_gate
from test_ledger_decoders import A, B
from test_ledger_lots import QUOTE, X
from test_ledger_store import h


def inputs():
    cut = {"chain": 137, "number": 10, "hash": h(10), "epoch": 0}
    evidence = {"chain": 137, "block_number": 10, "block_hash": h(10), "wallet": A,
                "complete": True, "raw_ids": [h(1)]}
    return dict(cursor=cut, wallet=A, ledger={"complete_basis": True, "balances": {A: {X: 10}},
                "quote_asset": QUOTE, "cash": {A: 90}}, chain_fills=[],
                clob={**evidence, "fills": [], "orders": [], "orders_complete": True},
                onchain={**evidence, "balances": {X: 10, QUOTE: 90}},
                independent_balances={X: 10, QUOTE: 90}, failures=[], required_assets=[X, QUOTE])


def test_reconciliation_matched_is_still_read_only():
    report = reconcile(**inputs())
    assert report["status"] == "MATCH"
    assert report["execution_allowed"] is False
    assert not any(report["safety"][k] for k in ("real_money", "signing", "automatic_orders", "withdrawals"))


def test_missing_zero_balance_cut_and_extra_asset_cannot_pass():
    for change in ("missing", "cut", "extra", "unknown_cost", "orders", "decode"):
        case = inputs()
        if change == "missing":
            del case["onchain"]["balances"][X]
        elif change == "cut":
            case["clob"]["block_hash"] = h(11)
        elif change == "extra":
            case["onchain"]["balances"]["137:" + B + ":9"] = 1
        elif change == "unknown_cost":
            case["ledger"]["complete_basis"] = False
        elif change == "orders":
            case["clob"]["orders_complete"] = False
        else:
            case["failures"] = [{"code": "DECODE_FAILURE"}]
        assert reconcile(**case)["status"] == "BLOCKED", change


def test_fill_identity_partial_ttl_and_amount_drift():
    case = inputs()
    fill = {"exchange": B, "tx": h(4), "log_index": 3, "order_hash": h(2), "maker": A,
            "side": "BUY", "token_id": "1", "quantity": 10, "quote": 5, "fee": 1}
    case["chain_fills"] = [fill]
    case["clob"]["fills"] = [copy.deepcopy(fill)]
    assert reconcile(**case)["status"] == "MATCH"
    case["clob"]["fills"][0]["fee"] = 0
    assert "CLOB_FILL_MISMATCH" in reconcile(**case)["reasons"]
    case["clob"]["as_of"] = 100
    case["clob"]["orders"] = [{"state": "PARTIAL", "expires_at": 99}]
    assert "ORDER_TTL_EXPIRED" in reconcile(**case)["reasons"]


def test_feed_gap_stale_reconnect_and_missing_source():
    health = [{"source": x, "received_at": 99, "gap": False, "resynced": True, "contract_verified": True}
              for x in ("rpc", "rest", "ws")]
    assert source_gate(health, now=100, max_age=10, required={"rpc", "rest", "ws"})["shadow_allowed"]
    for field, value in (("gap", True), ("resynced", False), ("received_at", 0), ("contract_verified", False)):
        case = copy.deepcopy(health)
        case[0][field] = value
        assert not source_gate(case, now=100, max_age=10, required={"rpc", "rest", "ws"})["shadow_allowed"]
