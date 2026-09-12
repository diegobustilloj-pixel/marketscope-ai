from __future__ import annotations

import json
from pathlib import Path

from eth_hash.auto import keccak

from .common import EvidenceError, ZERO, address, canonical, digest, hex_bytes, uint
from .store import EvidenceStore

FAMILIES = {"clob_v1", "clob_v2_ctf", "ctf", "negrisk", "collateral", "combo_v2"}
IMPLEMENTATION_SLOT = "0x" + (int.from_bytes(keccak(b"eip1967.proxy.implementation"), "big") - 1).to_bytes(32, "big").hex()
BEACON_SLOT = "0x" + (int.from_bytes(keccak(b"eip1967.proxy.beacon"), "big") - 1).to_bytes(32, "big").hex()


def code_hash(code: str) -> str:
    code = hex_bytes(code)
    if code == "0x":
        raise EvidenceError("No deployed code at requested block")
    return "0x" + keccak(bytes.fromhex(code[2:])).hex()


class ContractRegistry:
    def __init__(self, store: EvidenceStore):
        self.store = store

    def register(self, spec: dict) -> str:
        spec = json.loads(canonical(spec))
        for name in ("chain", "valid_from_block", "valid_to_block"):
            spec[name] = uint(spec[name])
        spec["address"] = address(spec["address"])
        if spec["family"] not in FAMILIES or not spec.get("source"):
            raise EvidenceError("Known family and source provenance required")
        if spec["valid_from_block"] > spec["valid_to_block"]:
            raise EvidenceError("Invalid block range")
        if not isinstance(spec["abi"], list) or not spec["abi"]:
            raise EvidenceError("Missing ABI: contract remains quarantined")
        if spec.get("abi_hash", digest(spec["abi"])) != digest(spec["abi"]):
            raise EvidenceError("ABI hash mismatch")
        spec["abi_hash"] = digest(spec["abi"])
        spec["code_hash"] = hex_bytes(spec["code_hash"], 32)
        kind = spec.get("proxy_kind", "direct")
        if kind not in {"direct", "eip1967", "beacon"}:
            raise EvidenceError("Unsupported proxy: obtain verified implementation evidence")
        spec["proxy_kind"] = kind
        if kind != "direct":
            spec["implementation"] = address(spec["implementation"])
            spec["implementation_code_hash"] = hex_bytes(spec["implementation_code_hash"], 32)
            if spec["implementation"] == ZERO:
                raise EvidenceError("Proxy implementation is zero")
        if kind == "beacon":
            spec["beacon"] = address(spec["beacon"])
        key = digest(spec)
        with self.store.transaction():
            if self.store.db.execute("SELECT 1 FROM contract_versions WHERE id=?", (key,)).fetchone():
                return key
            overlap = self.store.db.execute(
                "SELECT id FROM contract_versions WHERE chain=? AND address=? AND first_block<=? AND last_block>=?",
                (spec["chain"], spec["address"], spec["valid_to_block"], spec["valid_from_block"])).fetchone()
            if overlap:
                raise EvidenceError("Overlapping contract era; registry is append-only")
            self.store.db.execute("INSERT INTO contract_versions VALUES(?,?,?,?,?,?)",
                                  (key, spec["chain"], spec["address"], spec["valid_from_block"], spec["valid_to_block"], canonical(spec)))
        return key

    def resolve(self, chain: int, contract: str, block_number: int) -> dict:
        row = self.store.db.execute("SELECT id,payload FROM contract_versions WHERE chain=? AND address=? AND first_block<=? AND last_block>=?",
                                    (chain, address(contract), block_number, block_number)).fetchone()
        if not row:
            raise EvidenceError("Unknown contract or block era")
        return {**json.loads(row["payload"]), "id": row["id"]}

    def attest(self, version: dict, evidence: dict) -> str:
        """Check a pinned code/storage observation, not just a claimed verified flag.

        A hash match is relative to the registry's reviewed code/ABI source. It
        does not prove that an arbitrary RPC provider or supplied source is honest.
        Observations are exact-block and survive reorgs as orphan evidence.
        """
        evidence = json.loads(canonical(evidence))
        registered = self.store.db.execute("SELECT payload FROM contract_versions WHERE id=?", (version["id"],)).fetchone()
        if not registered or {k: v for k, v in version.items() if k != "id"} != json.loads(registered[0]):
            raise EvidenceError("Observation must use the immutable registered contract version")
        number = uint(evidence["block_number"])
        evidence["block_hash"] = hex_bytes(evidence["block_hash"], 32)
        known_block = self.store.db.execute("SELECT number FROM blocks WHERE chain=? AND hash=?",
                                           (evidence["chain"], evidence["block_hash"])).fetchone()
        if known_block and known_block[0] != number:
            raise EvidenceError("Observation block number/hash mismatch")
        if (evidence["chain"] != version["chain"] or address(evidence["address"]) != version["address"]
                or not version["valid_from_block"] <= number <= version["valid_to_block"] or not evidence.get("source")):
            raise EvidenceError("Contract observation outside registered era")
        matches = code_hash(evidence["code"]) == version["code_hash"]
        implementation = hex_bytes(evidence["implementation_slot"], 32)
        beacon = hex_bytes(evidence["beacon_slot"], 32)
        if version["proxy_kind"] == "direct":
            matches &= int(implementation, 16) == 0 and int(beacon, 16) == 0
        else:
            if version["proxy_kind"] == "eip1967":
                matches &= int(implementation, 16) == int(version["implementation"], 16) and int(beacon, 16) == 0
            else:
                matches &= int(beacon, 16) == int(version["beacon"], 16) and int(implementation, 16) == 0
                matches &= address(evidence["beacon_implementation"]) == version["implementation"]
            matches &= code_hash(evidence["implementation_code"]) == version["implementation_code_hash"]
        evidence["matches_registry"] = bool(matches)
        key = digest([version["id"], evidence])
        with self.store.transaction():
            self.store.db.execute("INSERT OR IGNORE INTO contract_observations VALUES(?,?,?)", (key, version["id"], canonical(evidence)))
            if not matches:
                self.store.incident("CONTRACT_DRIFT", {"version": version["id"], "observation": key})
        if not matches:
            raise EvidenceError("Contract code/proxy drift; decoding blocked")
        return key

    def require_attestation(self, version: dict, block_hash: str):
        observations = [json.loads(r[0]) for r in self.store.db.execute(
            "SELECT payload FROM contract_observations WHERE version_id=?", (version["id"],))]
        relevant = [x for x in observations if x["block_hash"] == block_hash]
        if not relevant or any(not x["matches_registry"] for x in relevant):
            raise EvidenceError("Missing or conflicting code/proxy verification at this block")

    def load(self, path: Path) -> list[str]:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        return [self.register(spec) for spec in document["contracts"]]
