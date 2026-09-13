"""Read-only qualification of a source-verifier report against pinned RPC code.

No Solidity implementation is copied into the bot or compiled/executed here.
The source-to-compiler relationship is attested by Sourcify; this module checks
the declared runtime transformations and exact deployed bytes independently.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import SAFETY, VERSION
from .common import EvidenceError, address, canonical, digest, hex_bytes, now_utc, uint
from .registry import ContractRegistry, code_hash
from .store import EvidenceStore


def _source_url_contract(source_url: str, chain: int) -> str:
    """Return the contract encoded by the one accepted Sourcify v2 URL shape."""
    parsed = urlsplit(source_url)
    parts = parsed.path.rstrip("/").split("/")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != "https" or parsed.hostname != "sourcify.dev" or parsed.port is not None
            or parsed.username or parsed.password or parsed.fragment
            or len(parts) != 6 or parts[:5] != ["", "server", "v2", "contract", str(chain)]
            or query != {"fields": ["all"]}):
        raise EvidenceError("Source URL must be the exact Sourcify v2 fields=all endpoint")
    return address(parts[5])


def qualify_source(report: dict, *, chain: int, contract: str, observed_code: str) -> dict:
    if uint(report["chainId"]) != chain or address(report["address"]) != address(contract):
        raise EvidenceError("Source report belongs to a different chain or contract")
    if report.get("runtimeMatch") not in {"match", "exact_match"}:
        raise EvidenceError("Source service has not verified the runtime")
    if not report.get("abi") or not report.get("sources") or not report.get("compilation"):
        raise EvidenceError("Source, ABI and compiler provenance are required")
    runtime = report["runtimeBytecode"]
    onchain = hex_bytes(runtime["onchainBytecode"])
    observed_code = hex_bytes(observed_code)
    if observed_code != onchain:
        raise EvidenceError("Source report bytecode differs from pinned RPC bytecode")
    compiled = bytearray.fromhex(hex_bytes(runtime["recompiledBytecode"])[2:])
    if len(compiled) != len(bytes.fromhex(onchain[2:])):
        raise EvidenceError("Recompiled runtime size mismatch")
    modified = set()
    transformations = runtime.get("transformations") or []
    values = runtime.get("transformationValues") or {}
    for change in transformations:
        reason, identity, offset = change["reason"], str(change["id"]), uint(change["offset"])
        if change["type"] != "replace":
            raise EvidenceError("Unsupported runtime transformation")
        if reason == "immutable":
            replacement = bytes.fromhex(hex_bytes(values["immutables"][identity])[2:])
            spans = (runtime.get("immutableReferences") or {}).get(identity, [])
            if {"start": offset, "length": len(replacement)} not in spans or len(replacement) != 32:
                raise EvidenceError("Transformation is not a declared immutable slot")
        elif reason == "cborAuxdata":
            metadata = runtime["cborAuxdata"][identity]
            original = bytes.fromhex(hex_bytes(metadata["value"])[2:])
            replacement = bytes.fromhex(hex_bytes(values["cborAuxdata"][identity])[2:])
            # Only terminal compiler metadata is supported; never strip code.
            if (metadata["offset"] != offset or len(original) != len(replacement)
                    or offset + len(original) != len(compiled)
                    or len(original) < 2 or int.from_bytes(original[-2:], "big") + 2 != len(original)
                    or int.from_bytes(replacement[-2:], "big") + 2 != len(replacement)
                    or compiled[offset:] != original):
                raise EvidenceError("Invalid terminal CBOR metadata transformation")
        else:
            raise EvidenceError("Unreviewed bytecode transformation: " + reason)
        affected = set(range(offset, offset + len(replacement)))
        if modified & affected or offset + len(replacement) > len(compiled):
            raise EvidenceError("Overlapping/out-of-range runtime transformation")
        modified |= affected
        compiled[offset:offset + len(replacement)] = replacement
    if "0x" + compiled.hex() != onchain:
        raise EvidenceError("Declared transformations do not reproduce deployed runtime")
    return {"verification": "sourcify-source-and-rpc-runtime-match",
            "locally_recompiled": False, "chain": chain, "address": address(contract),
            "code_hash": code_hash(onchain), "abi_hash": digest(report["abi"]),
            "report_hash": digest(report), "verified_at": report.get("verifiedAt"),
            "compiler": report["compilation"], "transformations": transformations,
            "source_hashes": {p: hashlib.sha256(v["content"].encode()).hexdigest()
                              for p, v in sorted(report["sources"].items())}}


def verify_deployment(rpc, *, report_path: Path, output: Path, chain: int, contract: str,
                      block_number: int, family: str, proxy_kind: str = "direct",
                      expected_implementation: str | None = None, source_url: str) -> dict:
    """Create an exact-block registry and retain successful or failed evidence.

    For proxies, the verifier report must describe the implementation. Proxy
    bytes are observed and pinned, not misrepresented as locally compiled code.
    The proxy's ABI is never used as the implementation's event interface.
    """
    report_path, output = Path(report_path).resolve(), Path(output).resolve()
    chain, block_number, contract = uint(chain), uint(block_number), address(contract)
    expected_implementation = address(expected_implementation) if expected_implementation else None
    if family == "combo_v2" and proxy_kind != "eip1967":
        raise EvidenceError("Current Combo support requires an explicit EIP-1967 implementation")
    if proxy_kind not in {"direct", "eip1967"}:
        raise EvidenceError("This command supports direct and EIP-1967 deployments")
    if proxy_kind != "direct" and not expected_implementation:
        raise EvidenceError("Expected implementation must be supplied explicitly")
    source_contract = _source_url_contract(source_url, chain)
    expected_source_contract = expected_implementation if proxy_kind != "direct" else contract
    if source_contract != expected_source_contract:
        raise EvidenceError("Source URL contract differs from the deployment being verified")
    if output.exists():
        raise EvidenceError("Verification output already exists")
    partial = output.with_name(output.name + ".partial")
    partial.mkdir(parents=True, exist_ok=False)
    report_bytes = report_path.read_bytes()
    (partial / "source_report.json").write_bytes(report_bytes)
    report = json.loads(report_bytes)
    configuration = {"chain": chain, "contract": contract, "block_number": block_number,
                     "family": family, "proxy_kind": proxy_kind, "expected_implementation": expected_implementation,
                     "source_url": source_url}
    (partial / "configuration.json").write_text(canonical(configuration) + "\n", encoding="utf-8")
    summary = {"version": VERSION, "safety": SAFETY, "configuration": configuration,
               "source_file_sha256": hashlib.sha256(report_bytes).hexdigest(), "execution_allowed": False}
    try:
        if uint(rpc.call("eth_chainId", [])) != chain:
            raise EvidenceError("Wrong RPC chain")
        block = rpc.block(block_number)
        observation = rpc.observe_contract({"address": contract, "proxy_kind": proxy_kind}, block)
        (partial / "observation.json").write_text(canonical(observation) + "\n", encoding="utf-8")
        target = contract
        target_code = observation["code"]
        spec = {"chain": chain, "address": contract, "family": family, "valid_from_block": block_number,
                "valid_to_block": block_number, "abi": report["abi"], "code_hash": code_hash(observation["code"]),
                "proxy_kind": proxy_kind, "source": source_url}
        if proxy_kind != "direct":
            target = address(expected_implementation)
            if int(observation["implementation_slot"], 16) != int(target, 16):
                raise EvidenceError("Observed proxy implementation differs from the expected address")
            target_code = observation["implementation_code"]
            spec.update(implementation=target, implementation_code_hash=code_hash(target_code))
        proof = qualify_source(report, chain=chain, contract=target, observed_code=target_code)
        spec["source_verification"] = proof
        if rpc.block(block_number)["hash"] != block["hash"]:
            raise EvidenceError("Reorg during deployment verification")
        with EvidenceStore(partial / "registry.db") as store:
            registry = ContractRegistry(store)
            registry.register(spec)
            registered = registry.resolve(chain, contract, block_number)
            observation_id = registry.attest(registered, observation)
        document = {"schema": 1, "contracts": [spec], "contract_observations": [observation],
                    "block": {k: block[k] for k in ("number", "hash", "parentHash", "timestamp")}}
        (partial / "registry.json").write_text(canonical(document) + "\n", encoding="utf-8")
        summary.update(status="VERIFIED_AT_BLOCK", registry_id=registered["id"], observation_id=observation_id,
                       block_hash=block["hash"], runtime_hash=proof["code_hash"], abi_hash=proof["abi_hash"],
                       locally_recompiled=False)
    except EvidenceError as exc:
        summary.update(status="BLOCKED", error=str(exc))
    (partial / "summary.json").write_text(canonical(summary) + "\n", encoding="utf-8")
    (partial / "run_manifest.json").write_text(canonical({"completed_at": now_utc(), "summary_hash": digest(summary),
                                                        "safety": SAFETY, "version": VERSION}) + "\n", encoding="utf-8")
    os.rename(partial, output)
    return summary
