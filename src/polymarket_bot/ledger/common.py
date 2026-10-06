from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from polymarket_bot.polyledger import PolyLedgerError, now_utc, validate_wallet


class EvidenceError(PolyLedgerError):
    """Missing, contradictory or unsupported evidence; never substitute zero."""


def validate_exact(value: Any) -> None:
    """Reject values that cannot safely represent deterministic evidence.

    Validation intentionally does not copy the supplied object graph.  Large
    sealed evidence bundles can contain hundreds of thousands of actions, and
    copying them merely to reject floats or non-string JSON keys doubles peak
    memory without adding audit value.
    """
    if isinstance(value, float):
        raise EvidenceError("float is not an accounting or evidence value")
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise EvidenceError("JSON keys must be strings")
        for nested in value.values():
            validate_exact(nested)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            validate_exact(nested)


def exact(value: Any) -> Any:
    """Backward-compatible validator for exact JSON-compatible evidence.

    ``json.dumps`` already serializes tuples as JSON lists, so returning the
    original validated graph preserves canonical bytes while avoiding a second
    in-memory copy of large evidence structures.
    """
    validate_exact(value)
    return value


def canonical(value: Any) -> str:
    return json.dumps(exact(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    """Hash canonical JSON without materializing a second full JSON string."""
    validate_exact(value)
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False)
    result = hashlib.sha256()
    for chunk in encoder.iterencode(value):
        result.update(chunk.encode("utf-8"))
    return result.hexdigest()


def uint(value: Any) -> int:
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+|[0-9]+", value):
        result = int(value, 16 if value.startswith("0x") else 10)
    else:
        raise EvidenceError(f"Expected unsigned integer, got {type(value).__name__}")
    if not 0 <= result < 2**256:
        raise EvidenceError("Unsigned integer outside uint256")
    return result


def hex_bytes(value: str, length: int | None = None) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:[a-fA-F0-9]{2})*", value):
        raise EvidenceError("Invalid hex bytes")
    if length is not None and len(value) != 2 + 2 * length:
        raise EvidenceError(f"Expected {length} bytes")
    return value.lower()


def address(value: str) -> str:
    return validate_wallet(value)


ZERO = "0x" + "00" * 20
