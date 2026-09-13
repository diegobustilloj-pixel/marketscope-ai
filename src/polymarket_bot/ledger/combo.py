"""PositionManager wire interface, pinned to a verified Ids.sol source hash.

This reads fields and ERC-1155 movements only. Module-specific economics,
resolution rules and conversion formulas are deliberately not inferred.
"""
from .common import EvidenceError, uint

IDS_SOURCE_HASH = "8c752ea7edb0518f4e3575be4a1b1b92852cb5b2c6c264e2055d16828f5b6268"
LAYOUT_VERSION = "combo-position-id/1"


def require_supported_layout(version: dict):
    proof = version.get("source_verification", {})
    expected_runtime = version.get("implementation_code_hash", version.get("code_hash"))
    if (proof.get("verification") != "sourcify-source-and-rpc-runtime-match"
            or proof.get("code_hash") != expected_runtime or proof.get("abi_hash") != version["abi_hash"]
            or proof.get("source_hashes", {}).get("src/libraries/Ids.sol") != IDS_SOURCE_HASH):
        raise EvidenceError("Combo requires a verified supported PositionManager/ID layout")


def position_fields(token: int | str) -> dict:
    # Decode the published wire byte layout; retain unknown module/resolution
    # values as data. Structural parsing does not authorize an economic mapping.
    raw = uint(token).to_bytes(32, "big")
    return {"layout": LAYOUT_VERSION, "module_id": raw[0], "base_hash": "0x" + raw[1:17].hex(),
            "arity": int.from_bytes(raw[17:19], "big"), "reserved": int.from_bytes(raw[19:27], "big"),
            "resolution_chain": int.from_bytes(raw[27:29], "big"),
            "condition_index": int.from_bytes(raw[29:31], "big"), "outcome_index": raw[31],
            "condition_id": "0x" + raw[:31].hex(), "event_id": "0x" + raw[:29].hex()}
