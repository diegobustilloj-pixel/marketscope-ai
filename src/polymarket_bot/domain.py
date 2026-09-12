from __future__ import annotations

import hashlib
import itertools
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def payload_checksum(source: str, stream: str, payload_raw: str) -> str:
    material = f"{source}\0{stream}\0{payload_raw}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _timestamp_ms(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 10_000_000_000 else number * 1000
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return _timestamp_ms(int(stripped))
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            return None
        return int(parsed.timestamp() * 1000)
    return None


def parse_message_metadata(
    payload_raw: str, default_stream: str
) -> tuple[str, int | None, str | None, str | None]:
    """Extrae metadatos sin modificar el payload original."""
    try:
        decoded = json.loads(payload_raw)
    except json.JSONDecodeError:
        return default_stream, None, None, None

    item: dict[str, Any]
    if isinstance(decoded, list) and decoded and isinstance(decoded[0], dict):
        item = decoded[0]
    elif isinstance(decoded, dict):
        item = decoded
    else:
        return default_stream, None, None, None

    payload = item.get("payload")
    payload_dict = payload if isinstance(payload, dict) else {}
    stream = str(
        item.get("event_type")
        or item.get("topic")
        or item.get("type")
        or item.get("e")
        or default_stream
    )
    source_timestamp_ms = _timestamp_ms(
        payload_dict.get("timestamp")
        or item.get("timestamp")
        or item.get("T")
        or item.get("E")
    )
    market_id = item.get("market") or payload_dict.get("market")
    token_id = (
        item.get("asset_id")
        or item.get("token_id")
        or payload_dict.get("asset_id")
        or payload_dict.get("token_id")
        or payload_dict.get("tokenId")
    )
    return (
        stream,
        source_timestamp_ms,
        str(market_id) if market_id is not None else None,
        str(token_id) if token_id is not None else None,
    )


class Sequence:
    def __init__(self, start: int | None = None) -> None:
        initial = start if start is not None else time.time_ns()
        self._counter = itertools.count(initial)

    def next(self) -> int:
        return next(self._counter)


@dataclass(frozen=True, slots=True)
class RawEvent:
    event_id: str
    source: str
    stream: str
    received_at: str
    monotonic_ns: int
    source_timestamp_ms: int | None
    sequence: int
    schema_version: int
    market_id: str | None
    token_id: str | None
    payload_raw: str
    checksum: str

    @classmethod
    def create(
        cls,
        *,
        source: str,
        default_stream: str,
        payload_raw: str,
        sequence: int,
        event_id: str | None = None,
        market_id: str | None = None,
        token_id: str | None = None,
    ) -> "RawEvent":
        stream, source_ts, parsed_market, parsed_token = parse_message_metadata(
            payload_raw, default_stream
        )
        return cls(
            event_id=event_id or str(uuid.uuid4()),
            source=source,
            stream=stream,
            received_at=utc_now_iso(),
            monotonic_ns=time.monotonic_ns(),
            source_timestamp_ms=source_ts,
            sequence=sequence,
            schema_version=SCHEMA_VERSION,
            # El identificador incluido por la fuente es la autoridad. El
            # valor suministrado por el collector es solo contexto de respaldo.
            market_id=parsed_market or market_id,
            token_id=parsed_token or token_id,
            payload_raw=payload_raw,
            checksum=payload_checksum(source, stream, payload_raw),
        )


@dataclass(frozen=True, slots=True)
class MarketDefinition:
    slug: str
    event_id: str | None
    condition_id: str
    question: str
    start_at: str | None
    end_at: str | None
    resolution_source: str | None
    outcomes: tuple[str, ...]
    token_ids: tuple[str, ...]
    active: bool
    closed: bool
    accepting_orders: bool
    payload_raw: str

    @property
    def token_by_outcome(self) -> dict[str, str]:
        return dict(zip(self.outcomes, self.token_ids, strict=False))
