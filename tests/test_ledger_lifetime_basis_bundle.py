import json
from pathlib import Path

from eth_hash.auto import keccak

from polymarket_bot.ledger.lifetime_basis_bundle import (
    _raw_mentions_wallet,
    classify_transaction,
)


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


def test_unknown_event_is_relevant_only_when_raw_log_names_wallet():
    padded = "0x" + "00" * 12 + WALLET[2:]
    raw = {"topics": ["0x" + "aa" * 32, padded], "data": "0x"}
    assert _raw_mentions_wallet(raw, WALLET)
    raw = {"topics": ["0x" + "aa" * 32], "data": padded}
    assert _raw_mentions_wallet(raw, WALLET)
    raw = {"topics": ["0x" + "aa" * 32], "data": "0x" + "00" * 32}
    assert not _raw_mentions_wallet(raw, WALLET)


def test_classifies_exact_nonquote_collateral_exchange():
    action, failure = classify_transaction(
        delta({PUSD: -1_000_000, QUOTE: 999_900}), features(),
        WALLET, QUOTE, {PUSD},
    )
    assert failure is None and action["kind"] == "sell"
    action, failure = classify_transaction(
        delta({QUOTE: -999_900, PUSD: 1_000_000}), features(),
        WALLET, QUOTE, {PUSD},
    )
    assert failure is None and action["kind"] == "buy"


def test_compound_fill_and_unannotated_exchange_preserve_net_basis():
    action, failure = classify_transaction(
        delta({YES: -10, NO: 20, QUOTE: 3}),
        features(fills=[{"side": "SELL"}]), WALLET, QUOTE,
    )
    assert failure is None and action["kind"] == "convert"
    action, failure = classify_transaction(
        delta({YES: -10, NO: 10}), features(), WALLET, QUOTE,
    )
    assert failure is None and action["kind"] == "convert"
    action, failure = classify_transaction(
        delta({YES: -10, NO: -10, QUOTE: 10}), features(), WALLET, QUOTE,
    )
    assert failure is None and action["kind"] == "sell"


def test_catalog_contains_verified_pusd_wrap_events():
    catalog = json.loads((Path(__file__).parents[1]
                          / "configs/polyledger/abi_catalog.json").read_text())
    events = {row["name"]: row for row in catalog["families"]["collateral"]["abi"]}
    for name, topic in {
        "Wrapped": "c00a5c84859ae82a7f5e6a2773283fb525335d5b3195f61174aa1ecc7e15dd84",
        "Unwrapped": "18b42b684d0b621cc609f4d888916e5ed9e934a476259ec1c11ec116f2b9aa7f",
    }.items():
        signature = name + "(" + ",".join(row["type"] for row in events[name]["inputs"]) + ")"
        assert keccak(signature.encode()).hex() == topic
