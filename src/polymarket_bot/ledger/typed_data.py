"""Unsigned EIP-712 digest vectors only. No keys, signatures or order transport."""
from eth_abi import encode
from eth_hash.auto import keccak

from .common import EvidenceError, address, hex_bytes, uint

ORDER_FIELDS = [("salt", "uint256"), ("maker", "address"), ("signer", "address"),
                ("tokenId", "uint256"), ("makerAmount", "uint256"), ("takerAmount", "uint256"),
                ("side", "uint8"), ("signatureType", "uint8"), ("timestamp", "uint256"),
                ("metadata", "bytes32"), ("builder", "bytes32")]
ORDER_TYPE = "Order(" + ",".join(kind + " " + name for name, kind in ORDER_FIELDS) + ")"
ORDER_TYPEHASH = "0x" + keccak(ORDER_TYPE.encode()).hex()


def order_digest(order: dict, chain: int, exchange: str) -> str:
    if set(order) != {name for name, _ in ORDER_FIELDS}:
        raise EvidenceError("V2 order fields must be exact; expiration/nonce are not signed")
    values = []
    for name, kind in ORDER_FIELDS:
        value = order[name]
        values.append(address(value) if kind == "address" else
                      bytes.fromhex(hex_bytes(value, 32)[2:]) if kind == "bytes32" else uint(value))
    if uint(order["side"]) > 1 or uint(order["signatureType"]) > 3:
        raise EvidenceError("Invalid V2 side or signature type")
    struct_hash = keccak(encode(["bytes32"] + [k for _, k in ORDER_FIELDS],
                                [bytes.fromhex(ORDER_TYPEHASH[2:])] + values))
    domain_type = keccak(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
    domain = keccak(encode(["bytes32", "bytes32", "bytes32", "uint256", "address"],
                          [domain_type, keccak(b"Polymarket CTF Exchange"), keccak(b"2"), uint(chain), address(exchange)]))
    return "0x" + keccak(b"\x19\x01" + domain + struct_hash).hex()
