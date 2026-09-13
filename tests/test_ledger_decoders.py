import json
from pathlib import Path

import pytest
from eth_abi import encode

from polymarket_bot.car_onchain import ORDER_FILLED_V1, ORDER_FILLED_V2
from polymarket_bot.ledger.common import EvidenceError, digest
from polymarket_bot.ledger.decoders import decode_canonical, decode_event, event_topic, semantics
from polymarket_bot.ledger.registry import ContractRegistry, code_hash
from polymarket_bot.ledger.store import EvidenceStore
from polymarket_bot.ledger.typed_data import ORDER_TYPEHASH, order_digest
from test_ledger_store import SCOPE, block, h, raw_log

CATALOG = json.loads((Path(__file__).resolve().parents[1] / "configs/polyledger/abi_catalog.json").read_text())
A = "0x" + "11" * 20
B = "0x" + "22" * 20


def abi(family):
    return CATALOG["families"][family]["abi"]


def event_log(family, name, args, number=10):
    event = next(e for e in abi(family) if e["name"] == name)
    topics = [event_topic(event)]
    types, values = [], []
    for item in event["inputs"]:
        value = args[item["name"]]
        if item["type"] == "bytes32" and isinstance(value, str):
            value = bytes.fromhex(value[2:])
        if item["indexed"]:
            topics.append("0x" + encode([item["type"]], [value]).hex())
        else:
            types.append(item["type"])
            values.append(value)
    return {**raw_log(number), "topics": topics, "data": "0x" + encode(types, values).hex()}


def spec(family="ctf", **extra):
    return {"chain": 137, "address": A, "family": family, "valid_from_block": 10,
            "valid_to_block": 20, "abi": abi(family), "code_hash": code_hash("0x6000"),
            "source": "synthetic-test-only", **extra}


def observation(number=10, **extra):
    return {"chain": 137, "address": A, "block_number": number, "block_hash": h(number),
            "code": "0x6000", "implementation_slot": h(0), "beacon_slot": h(0),
            "source": "synthetic-test-only", **extra}


def test_registry_ranges_hashes_and_drift(tmp_path):
    with EvidenceStore(tmp_path / "e.db") as store:
        reg = ContractRegistry(store)
        reg.register(spec())
        with pytest.raises(EvidenceError, match="Overlapping"):
            reg.register(spec(valid_from_block=20, valid_to_block=30))
        with pytest.raises(EvidenceError, match="ABI hash"):
            reg.register(spec(abi_hash="wrong"))
        version = reg.resolve(137, A, 10)
        reg.attest(version, observation())
        reg.require_attestation(version, h(10))
        with pytest.raises(EvidenceError, match="drift"):
            reg.attest(version, observation(code="0x6001"))
        with pytest.raises(EvidenceError, match="conflicting"):
            reg.require_attestation(version, h(10))
        with pytest.raises(EvidenceError, match="Unknown"):
            reg.resolve(137, A, 21)


def test_proxy_implementation_and_block_pinning(tmp_path):
    with EvidenceStore(tmp_path / "e.db") as store:
        reg = ContractRegistry(store)
        reg.register(spec(proxy_kind="eip1967", implementation=B, implementation_code_hash=code_hash("0x6002")))
        version = reg.resolve(137, A, 10)
        reg.attest(version, observation(implementation_slot="0x" + "00" * 12 + B[2:], implementation_code="0x6002"))
        with pytest.raises(EvidenceError, match="Missing"):
            reg.require_attestation(version, h(11))


def test_batch_expansion_and_strict_payload():
    args = {"operator": A, "from": A, "to": B, "ids": [2**200, 7], "values": [123, 456]}
    raw = event_log("ctf", "TransferBatch", args)
    name, decoded = decode_event(raw, abi("ctf"))
    movements = semantics("ctf", name, decoded)["movements"]
    assert [x["item_index"] for x in movements] == [0, 1]
    assert movements[0]["token_id"] == str(2**200)
    with pytest.raises(EvidenceError, match="trailing"):
        decode_event({**raw, "data": raw["data"] + "00" * 32}, abi("ctf"))
    with pytest.raises(EvidenceError, match="lengths"):
        semantics("ctf", name, {**decoded, "values": [123]})


@pytest.mark.parametrize("family,topic", [("clob_v1", ORDER_FILLED_V1), ("clob_v2_ctf", ORDER_FILLED_V2)])
def test_fill_spec_matches_existing_independent_decoder(family, topic):
    e = next(e for e in abi(family) if e["name"] == "OrderFilled")
    assert event_topic(e) == topic
    args = {"orderHash": h(55), "maker": A, "taker": B, "makerAmountFilled": 40,
            "takerAmountFilled": 100, "fee": 2, "makerAssetId": 0, "takerAssetId": 99,
            "side": 0, "tokenId": 99, "builder": h(1), "metadata": h(2)}
    name, decoded = decode_event(event_log(family, "OrderFilled", args), abi(family))
    result = semantics(family, name, decoded)
    assert (result["quantity"], result["quote"], result["side"]) == (100, 40, "BUY")
    assert result["fee_asset"] == ("outcome" if family == "clob_v1" else "collateral")


def test_canonical_decode_preserves_provenance_and_quarantines_unknown(tmp_path):
    with EvidenceStore(tmp_path / "e.db") as store:
        reg = ContractRegistry(store)
        reg.register(spec())
        raw = event_log("ctf", "TransferSingle", {"operator": A, "from": A, "to": B, "id": 7, "value": 5})
        store.ingest(137, [block(10)], [raw], expected_cursor=None, scope=SCOPE)
        events, failures = decode_canonical(reg, 137)
        assert not events and len(failures) == 1
        reg.attest(reg.resolve(137, A, 10), observation())
        events, failures = decode_canonical(reg, 137)
        assert not failures and events[0]["raw_id"] == next(store.logs(137))["id"]
        decode_canonical(reg, 137)
        assert store.db.execute("SELECT COUNT(*) FROM decoded_events").fetchone()[0] == 1
        with pytest.raises(EvidenceError, match="Unsupported combo"):
            semantics("combo_v2", "UnverifiedModuleConversion", {})


def test_unsigned_v2_vectors_separate_ctf_negrisk_and_http_fields():
    assert ORDER_TYPEHASH == "0xbb86318a2138f5fa8ae32fbe8e659f8fcf13cc6ae4014a707893055433818589"
    order = dict(salt=1, maker=A, signer=B, tokenId=7, makerAmount=4, takerAmount=10,
                 side=0, signatureType=0, timestamp=1700000000000, metadata=h(0), builder=h(0))
    ctf = order_digest(order, 137, "0xe111180000d2663c0091e4f400237545b87b996b")
    neg = order_digest(order, 137, "0xe2222d279d744050d28e00520010520000310f59")
    assert ctf != neg
    assert ctf == order_digest(order, 137, "0xe111180000d2663c0091e4f400237545b87b996b")
    with pytest.raises(EvidenceError, match="expiration"):
        order_digest({**order, "expiration": 1}, 137, A)
