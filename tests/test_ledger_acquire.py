import pytest

from polymarket_bot.ledger.acquire import ExactPublicClient, ReadOnlyRPC, atomic_decimal, capture_range
from polymarket_bot.ledger.common import EvidenceError
from test_ledger_decoders import A
from test_ledger_store import block, h, raw_log


def test_exact_public_parser_and_atomic_conversion():
    client = ExactPublicClient()
    client.get_bytes = lambda _: b'{"price":0.123456,"size":999999999999999999999}'
    assert client.get_json("unused")["price"] == "0.123456"
    assert atomic_decimal("0.123456") == 123456
    with pytest.raises(EvidenceError, match="integral"):
        atomic_decimal("0.1234567")
    with pytest.raises(EvidenceError):
        atomic_decimal(0.1)


def test_rpc_never_signs_or_submits_and_checks_response_identity():
    rpc = ReadOnlyRPC(transport=lambda p: {"id": p["id"], "result": "0x89"})
    assert rpc.call("eth_chainId", []) == "0x89"
    for method in ("eth_sendTransaction", "eth_sendRawTransaction", "personal_sign", "eth_signTypedData_v4"):
        with pytest.raises(EvidenceError, match="prohibited"):
            rpc.call(method, [])
    with pytest.raises(EvidenceError, match="allowlisted"):
        rpc.call("eth_call", [{"to": A, "data": "0xdeadbeef"}, "latest"])
    rpc.transport = lambda p: {"id": p["id"] + 1, "result": "0x89"}
    with pytest.raises(EvidenceError, match="Invalid"):
        rpc.call("eth_chainId", [])


def test_two_acquisition_paths_and_reorg_during_read():
    def transport(p):
        method = p["method"]
        result = {"eth_chainId": "0x89", "eth_getBlockByNumber": {**block(10), "transactions": [h(900)]},
                  "eth_getLogs": [raw_log(10)], "eth_getTransactionReceipt":
                  {"transactionHash": h(900), "blockHash": h(10), "logs": [raw_log(10)]}}[method]
        return {"id": p["id"], "result": result}
    rpc = ReadOnlyRPC(transport=transport)
    assert rpc.acquire_block(137, 10, [A], method="logs") == rpc.acquire_block(137, 10, [A], method="receipts")
    count = 0
    def reorg(p):
        nonlocal count
        result = transport(p)
        if p["method"] == "eth_getBlockByNumber":
            count += 1
            if count > 1:
                result["result"]["hash"] = h(999)
        return result
    with pytest.raises(EvidenceError, match="Reorg during"):
        ReadOnlyRPC(transport=reorg).acquire_block(137, 10, [A])


def test_capture_preserves_partial_then_resumes(tmp_path):
    class FakeRPC:
        url = "https://example.invalid"
        broken = True
        def call(self, method, params):
            assert method == "eth_blockNumber"
            return "0x1000"
        def block(self, number):
            return block(number)
        def acquire_block(self, chain, number, contracts, method):
            if number == 11 and self.broken:
                raise EvidenceError("injected provider outage")
            return block(number), [raw_log(number)]
    rpc = FakeRPC()
    output = tmp_path / "capture"
    with pytest.raises(EvidenceError, match="outage"):
        capture_range(rpc, output, chain=137, first=10, last=11, contracts=[A])
    assert not output.exists()
    assert output.with_name("capture.partial").is_dir()
    rpc.broken = False
    result = capture_range(rpc, output, chain=137, first=10, last=11, contracts=[A])
    assert result["status"] == "RAW_CAPTURE_ONLY"
    assert (output / "capture.json").exists()


def test_rpc_batch_matches_ids_independent_of_return_order():
    rpc = ReadOnlyRPC(transport=lambda ps: [{"id": p["id"], "result": p["params"][0]} for p in reversed(ps)])
    assert rpc.batch([("eth_getTransactionReceipt", [h(1)]), ("eth_getTransactionReceipt", [h(2)])]) == [h(1), h(2)]
    rpc.transport = lambda ps: [{"id": ps[0]["id"], "result": 1}] * len(ps)
    with pytest.raises(EvidenceError, match="identity"):
        rpc.batch([("eth_getTransactionReceipt", [h(1)]), ("eth_getTransactionReceipt", [h(2)])])
