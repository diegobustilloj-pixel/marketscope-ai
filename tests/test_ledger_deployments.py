import copy
import json
from pathlib import Path

import pytest

from polymarket_bot.ledger.common import EvidenceError, digest
from polymarket_bot.ledger.combo import IDS_SOURCE_HASH, position_fields, require_supported_layout
from polymarket_bot.ledger.decoders import decode_event, semantics
from polymarket_bot.ledger.deployments import _source_url_contract, qualify_source, verify_deployment
from polymarket_bot.ledger.registry import code_hash
from test_ledger_decoders import A, B, observation
from test_ledger_store import block, h


def source_report():
    return {"chainId": "137", "address": A, "runtimeMatch": "match", "abi": [{"type": "event"}],
            "sources": {"Example.sol": {"content": "synthetic-source"}}, "compilation": {"compiler": "fixture"},
            "runtimeBytecode": {"onchainBytecode": "0x6000", "recompiledBytecode": "0x6000",
                                "transformations": [], "transformationValues": {}}}


def test_qualification_binds_source_to_chain_and_exact_runtime():
    report = source_report()
    result = qualify_source(report, chain=137, contract=A, observed_code="0x6000")
    assert result["code_hash"] == code_hash("0x6000")
    assert result["locally_recompiled"] is False
    for field, value in (("address", B), ("chainId", "1"), ("runtimeMatch", None)):
        bad = {**report, field: value}
        with pytest.raises(EvidenceError):
            qualify_source(bad, chain=137, contract=A, observed_code="0x6000")
    with pytest.raises(EvidenceError, match="differs"):
        qualify_source(report, chain=137, contract=A, observed_code="0x6001")


def test_only_declared_runtime_transformations_are_allowed():
    report = source_report()
    runtime = report["runtimeBytecode"]
    runtime.update(onchainBytecode="0x6000" + "11" * 32, recompiledBytecode="0x6000" + "00" * 32,
                   immutableReferences={"1": [{"start": 2, "length": 32}]},
                   transformations=[{"type": "replace", "reason": "immutable", "id": "1", "offset": 2}],
                   transformationValues={"immutables": {"1": "0x" + "11" * 32}})
    qualify_source(report, chain=137, contract=A, observed_code=runtime["onchainBytecode"])
    for change in ({"offset": 0}, {"reason": "library"}, {"type": "insert"}):
        bad = copy.deepcopy(report)
        bad["runtimeBytecode"]["transformations"][0].update(change)
        with pytest.raises(EvidenceError):
            qualify_source(bad, chain=137, contract=A, observed_code=runtime["onchainBytecode"])
    runtime["transformations"] *= 2
    with pytest.raises(EvidenceError, match="Overlapping"):
        qualify_source(report, chain=137, contract=A, observed_code=runtime["onchainBytecode"])


def test_terminal_metadata_change_is_checked_without_stripping_code():
    report = source_report()
    runtime = report["runtimeBytecode"]
    runtime.update(onchainBytecode="0x6000020001", recompiledBytecode="0x6000010001",
                   cborAuxdata={"1": {"offset": 2, "value": "0x010001"}},
                   transformations=[{"type": "replace", "reason": "cborAuxdata", "id": "1", "offset": 2}],
                   transformationValues={"cborAuxdata": {"1": "0x020001"}})
    qualify_source(report, chain=137, contract=A, observed_code=runtime["onchainBytecode"])
    runtime["recompiledBytecode"] = "0x6100010001"
    with pytest.raises(EvidenceError, match="do not reproduce"):
        qualify_source(report, chain=137, contract=A, observed_code=runtime["onchainBytecode"])


def test_command_persists_verified_and_blocked_evidence(tmp_path):
    class RPC:
        def call(self, method, params):
            return "0x89"
        def block(self, number):
            return block(number)
        def observe_contract(self, spec, header):
            return observation()
    report_path = tmp_path / "source.json"
    report = source_report()
    report_path.write_text(json.dumps(report))
    options = dict(report_path=report_path, chain=137, contract=A, block_number=10,
                   family="ctf", source_url="https://sourcify.dev/server/v2/contract/137/" + A + "?fields=all")
    success = verify_deployment(RPC(), output=tmp_path / "verified", **options)
    assert success["status"] == "VERIFIED_AT_BLOCK"
    registry = json.loads((tmp_path / "verified/registry.json").read_text())
    assert registry["contracts"][0]["valid_to_block"] == 10
    report["runtimeBytecode"]["onchainBytecode"] = "0x6001"
    report_path.write_text(json.dumps(report))
    blocked = verify_deployment(RPC(), output=tmp_path / "blocked", **options)
    assert blocked["status"] == "BLOCKED"
    assert (tmp_path / "blocked/observation.json").exists()
    assert not (tmp_path / "blocked/registry.json").exists()


def test_source_url_is_bound_to_chain_contract_and_exact_endpoint(tmp_path):
    valid = "https://sourcify.dev/server/v2/contract/137/" + A + "?fields=all"
    assert _source_url_contract(valid, 137) == A
    for invalid in (
        "http://sourcify.dev/server/v2/contract/137/" + A + "?fields=all",
        "https://sourcify.dev/server/v2/contract/1/" + A + "?fields=all",
        "https://sourcify.dev/server/v2/contract/137/" + A,
        "https://sourcify.dev/server/v2/contract/137/" + A + "?fields=abi",
        "https://sourcify.dev.evil.test/server/v2/contract/137/" + A + "?fields=all",
    ):
        with pytest.raises(EvidenceError, match="exact Sourcify"):
            _source_url_contract(invalid, 137)

    report_path = tmp_path / "source.json"
    report_path.write_text(json.dumps(source_report()))
    with pytest.raises(EvidenceError, match="differs"):
        verify_deployment(
            object(),
            report_path=report_path,
            output=tmp_path / "must-not-exist",
            chain=137,
            contract=A,
            block_number=10,
            family="ctf",
            source_url="https://sourcify.dev/server/v2/contract/137/" + B + "?fields=all",
        )
    assert not (tmp_path / "must-not-exist.partial").exists()


def test_combo_fields_are_not_ctf_ids_and_preserve_unknown_fields():
    wire = bytes.fromhex("02" + "0123456789abcdef" * 2 + "0003" + "00" * 8 + "0000" + "0002" + "01")
    decoded = position_fields(int.from_bytes(wire, "big"))
    assert decoded["module_id"] == 2
    assert decoded["arity"] == 3
    assert decoded["condition_index"] == 2
    assert decoded["outcome_index"] == 1
    assert decoded["condition_id"] == "0x" + wire[:31].hex()
    assert decoded["event_id"] == "0x" + wire[:29].hex()
    assert position_fields(2**256 - 1)["reserved"] == 2**64 - 1


def test_combo_registry_requires_supported_source_hash():
    version = {"abi_hash": "abc", "implementation_code_hash": h(1), "source_verification": {
        "verification": "sourcify-source-and-rpc-runtime-match", "code_hash": h(1), "abi_hash": "abc",
        "source_hashes": {"src/libraries/Ids.sol": IDS_SOURCE_HASH}}}
    require_supported_layout(version)
    version["source_verification"]["source_hashes"]["src/libraries/Ids.sol"] = "new-layout"
    with pytest.raises(EvidenceError, match="supported"):
        require_supported_layout(version)


def test_combo_batch_uses_amounts_and_preserves_item_indices():
    from eth_abi import encode
    from polymarket_bot.ledger.decoders import event_topic
    path = Path(__file__).resolve().parents[1] / "configs/polyledger/combo_position_manager_abi.json"
    catalog = json.loads(path.read_text())
    abi = catalog["abi"]
    assert digest(abi) == catalog["selected_event_abi_hash"]
    assert catalog["ids_source_sha256"] == IDS_SOURCE_HASH
    assert catalog["economic_mapping"].startswith("blocked_")
    event = next(e for e in abi if e["name"] == "TransferBatch")
    token = int.from_bytes(bytes.fromhex("01" + "00" * 31), "big")
    raw = {"topics": [event_topic(event)] + ["0x" + encode(["address"], [a]).hex() for a in (A, A, B)],
           "data": "0x" + encode(["uint256[]", "uint256[]"], [[token, token + 1], [4, 7]]).hex()}
    name, values = decode_event(raw, abi)
    movements = semantics("combo_v2", name, values)["movements"]
    assert [m["quantity"] for m in movements] == [4, 7]
    assert [m["item_index"] for m in movements] == [0, 1]
    assert [m["position"]["outcome_index"] for m in movements] == [0, 1]
