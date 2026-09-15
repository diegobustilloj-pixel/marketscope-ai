from polymarket_bot.ledger.lifetime_basis_bundle import classify_transaction


WALLET = "0x" + "11" * 20
QUOTE = "137:0x" + "22" * 20 + ":erc20"
YES = "137:0x" + "33" * 20 + ":1"
NO = "137:0x" + "33" * 20 + ":2"
PUSD = "137:0x" + "44" * 20 + ":erc20"


def delta(values):
    return {"block": 100, "tx_index": 2, "tx": "0x" + "55" * 32,
            "deltas": values, "raw_ids": ["raw:balance"]}


def features(*, fills=(), actions=(), failures=()):
    return {"fills": list(fills), "action_kinds": list(actions),
            "decode_failures": list(failures), "names": [], "raw_ids": ["raw:event"]}


def test_classifies_unambiguous_quote_buy():
    action, failure = classify_transaction(
        delta({QUOTE: -40, YES: 10}), features(fills=[{"side": "BUY"}]),
        WALLET, QUOTE,
    )
    assert failure is None
    assert action["kind"] == "buy"
    assert action["cash_delta"] == -40
    assert action["outputs"] == [{"asset": YES, "quantity": 10}]


def test_classifies_split_and_nonquote_conversion():
    action, failure = classify_transaction(
        delta({QUOTE: -10, YES: 10, NO: 10}), features(actions=["split"]),
        WALLET, QUOTE,
    )
    assert failure is None and action["kind"] == "split"
    action, failure = classify_transaction(
        delta({PUSD: -7, YES: 5}), features(fills=[{"side": "BUY"}]),
        WALLET, QUOTE,
    )
    assert failure is None and action["kind"] == "convert"


def test_quarantines_mixed_or_unknown_economics():
    action, failure = classify_transaction(
        delta({QUOTE: -10, YES: 5, NO: 5}), features(fills=[{"side": "BUY"}]),
        WALLET, QUOTE,
    )
    assert action is None
    assert "MIXED_OR_MULTI_ASSET_FILL" in failure["codes"]
    action, failure = classify_transaction(
        delta({QUOTE: -10, YES: 10}), features(failures=[{"error": "unknown"}]),
        WALLET, QUOTE,
    )
    assert action is None
    assert "RELEVANT_EVENT_DECODE_FAILURE" in failure["codes"]
