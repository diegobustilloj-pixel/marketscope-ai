from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import urllib.request
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from websockets.asyncio.client import connect

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v056_contract import VARIANT, load_and_verify_preregistration


SCHEMA_VERSION = "1"

DDL = """
CREATE TABLE v056_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE v056_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    status TEXT NOT NULL,
    error_class TEXT,
    error_code TEXT,
    reconnects INTEGER NOT NULL DEFAULT 0,
    clock_probe_errors INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE v056_message_counts (
    run_id INTEGER NOT NULL,
    message_type TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    PRIMARY KEY(run_id,message_type),
    FOREIGN KEY(run_id) REFERENCES v056_runs(run_id)
) WITHOUT ROWID;
CREATE TABLE v056_request_totals (
    run_id INTEGER PRIMARY KEY,
    request_count INTEGER NOT NULL DEFAULT 0,
    with_deadline_count INTEGER NOT NULL DEFAULT 0,
    negative_headroom_count INTEGER NOT NULL DEFAULT 0,
    nonnegative_headroom_count INTEGER NOT NULL DEFAULT 0,
    headroom_sum_ms INTEGER NOT NULL DEFAULT 0,
    headroom_min_ms INTEGER,
    headroom_max_ms INTEGER,
    sample_eligible_count INTEGER NOT NULL DEFAULT 0,
    sample_stored_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(run_id) REFERENCES v056_runs(run_id)
);
CREATE TABLE v056_request_buckets (
    run_id INTEGER NOT NULL,
    minute_index INTEGER NOT NULL,
    direction TEXT NOT NULL,
    side TEXT NOT NULL,
    leg_count INTEGER NOT NULL,
    size_unit TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    PRIMARY KEY(run_id,minute_index,direction,side,leg_count,size_unit),
    FOREIGN KEY(run_id) REFERENCES v056_runs(run_id)
) WITHOUT ROWID;
CREATE TABLE v056_headroom_histogram (
    run_id INTEGER NOT NULL,
    bucket_lower_ms INTEGER NOT NULL,
    request_count INTEGER NOT NULL,
    PRIMARY KEY(run_id,bucket_lower_ms),
    FOREIGN KEY(run_id) REFERENCES v056_runs(run_id)
) WITHOUT ROWID;
CREATE TABLE v056_conditions (
    run_id INTEGER NOT NULL,
    condition_id BLOB NOT NULL,
    PRIMARY KEY(run_id,condition_id),
    FOREIGN KEY(run_id) REFERENCES v056_runs(run_id)
) WITHOUT ROWID;
CREATE TABLE v056_request_samples (
    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    received_at_ms INTEGER NOT NULL,
    payload_sha256 BLOB NOT NULL,
    rfq_id_sha256 BLOB,
    requestor_id_sha256 BLOB,
    condition_id BLOB,
    leg_position_ids_json TEXT,
    yes_position_id TEXT,
    no_position_id TEXT,
    direction TEXT,
    side TEXT,
    size_unit TEXT,
    size_value TEXT,
    submission_deadline_ms INTEGER,
    FOREIGN KEY(run_id) REFERENCES v056_runs(run_id)
);
CREATE TABLE v056_non_request_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    received_at_ms INTEGER NOT NULL,
    message_type TEXT NOT NULL,
    payload_sha256 BLOB NOT NULL,
    rfq_id_sha256 BLOB,
    requestor_id_sha256 BLOB,
    condition_id BLOB,
    leg_position_ids_json TEXT,
    direction TEXT,
    side TEXT,
    price_e6 TEXT,
    trade_size_e6 TEXT,
    executed_at_ms INTEGER,
    auth_success INTEGER,
    role TEXT,
    status_code TEXT,
    error_code TEXT,
    FOREIGN KEY(run_id) REFERENCES v056_runs(run_id)
);
CREATE TABLE v056_clock_samples (
    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    phase TEXT NOT NULL,
    local_send_ms INTEGER NOT NULL,
    local_receive_ms INTEGER NOT NULL,
    server_time_s INTEGER NOT NULL,
    round_trip_ms INTEGER NOT NULL,
    server_minus_local_lower_ms INTEGER NOT NULL,
    server_minus_local_upper_ms INTEGER NOT NULL,
    FOREIGN KEY(run_id) REFERENCES v056_runs(run_id)
);
"""


class V056ObserverError(RuntimeError):
    pass


class V056StorageLimitError(V056ObserverError):
    pass


class V056LocalStorageError(V056ObserverError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class CredentialBundle:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    api_passphrase: str = field(repr=False)
    signer_address: str = field(repr=False)
    maker_address: str = field(repr=False)
    signature_type: int = field(repr=False)

    def __repr__(self) -> str:
        return "CredentialBundle(<redacted>)"

    def auth_payload(self) -> dict[str, Any]:
        return {
            "type": "auth",
            "auth": {
                "apiKey": self.api_key,
                "secret": self.api_secret,
                "passphrase": self.api_passphrase,
            },
            "identity": {
                "signer_address": self.signer_address,
                "maker_address": self.maker_address,
                "signature_type": self.signature_type,
            },
        }


def credential_preflight(
    contract: Mapping[str, Any], *, environ: Mapping[str, str] | None = None
) -> tuple[CredentialBundle | None, dict[str, Any]]:
    source = os.environ if environ is None else environ
    names = contract["credentials"]["variables"]
    values = {key: str(source.get(str(name), "")).strip() for key, name in names.items()}
    missing = [str(names[key]) for key, value in values.items() if not value]
    invalid: list[str] = []
    signature_type: int | None = None
    if values["signature_type"]:
        try:
            signature_type = int(values["signature_type"])
        except ValueError:
            invalid.append(str(names["signature_type"]))
        else:
            if signature_type not in contract["credentials"]["allowed_signature_types"]:
                invalid.append(str(names["signature_type"]))
    address_pattern = re.compile(r"0x[0-9a-fA-F]{40}")
    for key in ("signer_address", "maker_address"):
        if values[key] and address_pattern.fullmatch(values[key]) is None:
            invalid.append(str(names[key]))
    if (
        signature_type in {0, 3}
        and values["signer_address"]
        and values["maker_address"]
        and values["signer_address"].lower() != values["maker_address"].lower()
    ):
        invalid.append("SIGNER_MAKER_IDENTITY_MISMATCH_FOR_SIGNATURE_TYPE")
    report = {
        "status": "PASS" if not missing and not invalid else "BLOCKED",
        "required_variables": list(names.values()),
        "present_count": len(values) - len(missing),
        "missing_variables": sorted(missing),
        "invalid_fields": sorted(set(invalid)),
        "credential_values_exposed": False,
        "credential_values_persisted": False,
        "private_key_required": False,
        "database_created": False,
    }
    if missing or invalid or signature_type is None:
        return None, report
    return CredentialBundle(
        api_key=values["api_key"],
        api_secret=values["api_secret"],
        api_passphrase=values["api_passphrase"],
        signer_address=values["signer_address"],
        maker_address=values["maker_address"],
        signature_type=signature_type,
    ), report


class AuthOnlySender:
    def __init__(self) -> None:
        self.auth_messages_sent = 0

    async def send_auth(self, websocket: Any, credentials: CredentialBundle) -> None:
        if self.auth_messages_sent != 0:
            raise V056ObserverError("Solo se permite un auth por conexion")
        payload = credentials.auth_payload()
        if payload.get("type") != "auth" or set(payload) != {"type", "auth", "identity"}:
            raise V056ObserverError("Payload auth incompatible")
        await websocket.send(json.dumps(payload, separators=(",", ":")))
        self.auth_messages_sent = 1

    async def send(self, websocket: Any, payload: Mapping[str, Any]) -> None:
        del websocket
        message_type = str(payload.get("type") or "")
        raise V056ObserverError(f"Mensaje saliente bloqueado: {message_type or 'SIN_TIPO'}")


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: Any) -> bytes | None:
    text = str(value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).digest() if text else None


def _safe_text(value: Any, maximum: int = 128) -> str | None:
    text = str(value or "").strip()
    return text[:maximum] if text else None


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _token(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.isdigit() else None


def _condition_bytes(value: Any) -> bytes | None:
    text = str(value or "").strip().lower()
    if re.fullmatch(r"0x[0-9a-f]{64}", text) is None:
        return None
    return bytes.fromhex(text[2:])


def _fingerprint(record: Mapping[str, Any]) -> bytes:
    serializable = {
        key: (value.hex() if isinstance(value, bytes) else value)
        for key, value in record.items()
        if key != "payload_sha256"
    }
    return hashlib.sha256(_json_compact(serializable).encode("utf-8")).digest()


def sanitize_inbound(payload: Mapping[str, Any], received_at_ms: int) -> dict[str, Any]:
    message_type = _safe_text(payload.get("type"), 64) or "UNKNOWN"
    result: dict[str, Any] = {
        "received_at_ms": int(received_at_ms),
        "message_type": message_type,
        "payload_sha256": b"",
        "rfq_id_sha256": None,
        "requestor_id_sha256": None,
        "condition_id": None,
        "leg_position_ids_json": None,
        "yes_position_id": None,
        "no_position_id": None,
        "direction": None,
        "side": None,
        "size_unit": None,
        "size_value": None,
        "submission_deadline_ms": None,
        "price_e6": None,
        "trade_size_e6": None,
        "executed_at_ms": None,
        "auth_success": None,
        "role": None,
        "status_code": None,
        "error_code": None,
    }
    if message_type == "auth":
        success = payload.get("success")
        result["auth_success"] = int(success) if isinstance(success, bool) else None
        result["role"] = _safe_text(payload.get("role"), 32)
        result["error_code"] = _safe_text(payload.get("error"), 64)
    if message_type in {"RFQ_REQUEST", "RFQ_TRADE", "RFQ_EXECUTION_UPDATE", "RFQ_ERROR"}:
        result["rfq_id_sha256"] = _sha256_bytes(payload.get("rfq_id"))
    if message_type == "RFQ_REQUEST":
        result["requestor_id_sha256"] = _sha256_bytes(payload.get("requestor_public_id"))
        result["condition_id"] = _condition_bytes(payload.get("condition_id"))
        legs_raw = payload.get("leg_position_ids")
        if isinstance(legs_raw, list):
            legs = [_token(value) for value in legs_raw]
            if legs and all(legs):
                result["leg_position_ids_json"] = _json_compact(legs)
        result["yes_position_id"] = _token(payload.get("yes_position_id"))
        result["no_position_id"] = _token(payload.get("no_position_id"))
        result["direction"] = _safe_text(payload.get("direction"), 16)
        result["side"] = _safe_text(payload.get("side"), 16)
        requested = payload.get("requested_size")
        if isinstance(requested, Mapping):
            result["size_unit"] = _safe_text(requested.get("unit"), 16)
            result["size_value"] = _safe_text(
                requested.get("value_e6", requested.get("value")), 64
            )
        result["submission_deadline_ms"] = _positive_int(payload.get("submission_deadline"))
    elif message_type == "RFQ_TRADE":
        result["requestor_id_sha256"] = _sha256_bytes(payload.get("requester_id"))
        result["condition_id"] = _condition_bytes(payload.get("condition_id"))
        legs_raw = payload.get("leg_position_ids")
        if isinstance(legs_raw, list):
            legs = [_token(value) for value in legs_raw]
            if legs and all(legs):
                result["leg_position_ids_json"] = _json_compact(legs)
        result["direction"] = _safe_text(payload.get("direction"), 16)
        result["side"] = _safe_text(payload.get("side"), 16)
        result["price_e6"] = _safe_text(payload.get("price_e6"), 64)
        result["trade_size_e6"] = _safe_text(payload.get("size_e6"), 64)
        result["executed_at_ms"] = _positive_int(payload.get("executed_at"))
    elif message_type == "RFQ_EXECUTION_UPDATE":
        result["status_code"] = _safe_text(payload.get("status"), 64)
    elif message_type == "RFQ_ERROR":
        request_type = _safe_text(payload.get("request_type"), 64)
        code = _safe_text(payload.get("code"), 64)
        result["error_code"] = ":".join(
            value for value in (request_type, code) if value
        ) or None
    result["payload_sha256"] = _fingerprint(result)
    return result


def database_footprint(path: Path) -> int:
    return sum(
        item.stat().st_size
        for item in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if item.exists()
    )


def measure_server_clock(
    *, endpoint: str, sample_count: int, spacing_ms: int, timeout_seconds: float
) -> list[dict[str, int]]:
    samples: list[dict[str, int]] = []
    for index in range(int(sample_count)):
        local_send_ms = time.time_ns() // 1_000_000
        request = urllib.request.Request(
            endpoint,
            headers={
                "Accept": "application/json",
                "User-Agent": "PolyMarkerQuantBot-V0.56-clock-observer/1.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=float(timeout_seconds)) as response:
                value = json.loads(response.read(128).decode("utf-8"))
            local_receive_ms = time.time_ns() // 1_000_000
            server_time_s = int(value)
            if not 1_000_000_000 <= server_time_s <= 9_999_999_999:
                raise ValueError("server time fuera de rango")
            lower = server_time_s * 1000 - local_receive_ms
            upper = (server_time_s + 1) * 1000 - local_send_ms
            if lower <= upper:
                samples.append(
                    {
                        "local_send_ms": local_send_ms,
                        "local_receive_ms": local_receive_ms,
                        "server_time_s": server_time_s,
                        "round_trip_ms": local_receive_ms - local_send_ms,
                        "server_minus_local_lower_ms": lower,
                        "server_minus_local_upper_ms": upper,
                    }
                )
        except Exception:
            pass
        if index + 1 < int(sample_count):
            time.sleep(max(0, int(spacing_ms)) / 1000.0)
    return samples


class V056Store:
    def __init__(self, path: str | Path, storage_contract: Mapping[str, Any]) -> None:
        self.path = Path(path).resolve()
        self.storage = dict(storage_contract)
        self.connection: sqlite3.Connection | None = None
        self.run_started_at_ms: int | None = None
        self.pending_events = 0
        self.last_commit_monotonic = time.monotonic()
        self.request_samples_stored = 0
        self.non_request_records_stored = 0

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise V056LocalStorageError("Base V0.56 no abierta")
        return self.connection

    def open_new(self, *, preregistration_sha256: str, duration_seconds: int) -> None:
        if self.path.exists():
            raise V056LocalStorageError("La base V0.56 ya existe; no se reanuda ni sobreescribe")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=60)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute(
            f"PRAGMA wal_autocheckpoint={int(self.storage['wal_autocheckpoint_pages'])}"
        )
        self.connection.executescript(DDL)
        values = {
            "schema_version": SCHEMA_VERSION,
            "variant": VARIANT,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "preregistration_sha256": preregistration_sha256,
            "duration_seconds": int(duration_seconds),
            "completion_reason": None,
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "quote_submission_enabled": False,
            "signatures_enabled": False,
            "transactions_enabled": False,
            "credential_values_stored": False,
            "addresses_stored": False,
            "raw_payloads_stored": False,
            "profitability_measured": False,
            "pnl_measured": False,
            "real_money": "BLOQUEADO",
        }
        self.connection.executemany(
            "INSERT INTO v056_meta(key,value) VALUES(?,?)",
            [(key, _json_compact(value)) for key, value in values.items()],
        )
        self.connection.commit()

    def start_run(self) -> int:
        self.run_started_at_ms = time.time_ns() // 1_000_000
        cursor = self.db.execute(
            "INSERT INTO v056_runs(started_at_ms,status) VALUES(?,?)",
            (self.run_started_at_ms, "RUNNING"),
        )
        run_id = int(cursor.lastrowid)
        self.db.execute("INSERT INTO v056_request_totals(run_id) VALUES(?)", (run_id,))
        self.db.commit()
        return run_id

    def _increment_type(self, run_id: int, message_type: str) -> None:
        self.db.execute(
            """
            INSERT INTO v056_message_counts(run_id,message_type,event_count) VALUES(?,?,1)
            ON CONFLICT(run_id,message_type) DO UPDATE SET event_count=event_count+1
            """,
            (run_id, message_type),
        )

    def _headroom_bucket(self, value: int) -> int:
        width = int(self.storage["headroom_histogram_width_ms"])
        minimum = int(self.storage["headroom_histogram_minimum_ms"])
        maximum = int(self.storage["headroom_histogram_maximum_ms"])
        if value < minimum:
            return minimum - width
        if value > maximum:
            return maximum + width
        return math.floor(value / width) * width

    def _record_request(self, run_id: int, record: Mapping[str, Any]) -> None:
        received_at_ms = int(record["received_at_ms"])
        started_at_ms = self.run_started_at_ms or received_at_ms
        minute_index = max(0, (received_at_ms - started_at_ms) // 60000)
        legs_json = record.get("leg_position_ids_json")
        leg_count = 0
        if isinstance(legs_json, str):
            parsed = json.loads(legs_json)
            leg_count = len(parsed) if isinstance(parsed, list) else 0
        direction = str(record.get("direction") or "UNKNOWN")
        side = str(record.get("side") or "UNKNOWN")
        size_unit = str(record.get("size_unit") or "UNKNOWN")
        self.db.execute(
            """
            INSERT INTO v056_request_buckets(
              run_id,minute_index,direction,side,leg_count,size_unit,request_count
            ) VALUES(?,?,?,?,?,?,1)
            ON CONFLICT(run_id,minute_index,direction,side,leg_count,size_unit)
            DO UPDATE SET request_count=request_count+1
            """,
            (run_id, minute_index, direction, side, leg_count, size_unit),
        )
        condition = record.get("condition_id")
        if isinstance(condition, bytes):
            self.db.execute(
                "INSERT OR IGNORE INTO v056_conditions(run_id,condition_id) VALUES(?,?)",
                (run_id, condition),
            )
        deadline = record.get("submission_deadline_ms")
        if isinstance(deadline, int):
            headroom = deadline - received_at_ms
            self.db.execute(
                """
                UPDATE v056_request_totals SET
                  request_count=request_count+1,
                  with_deadline_count=with_deadline_count+1,
                  negative_headroom_count=negative_headroom_count+?,
                  nonnegative_headroom_count=nonnegative_headroom_count+?,
                  headroom_sum_ms=headroom_sum_ms+?,
                  headroom_min_ms=CASE WHEN headroom_min_ms IS NULL OR ?<headroom_min_ms THEN ? ELSE headroom_min_ms END,
                  headroom_max_ms=CASE WHEN headroom_max_ms IS NULL OR ?>headroom_max_ms THEN ? ELSE headroom_max_ms END
                WHERE run_id=?
                """,
                (
                    int(headroom < 0),
                    int(headroom >= 0),
                    headroom,
                    headroom,
                    headroom,
                    headroom,
                    headroom,
                    run_id,
                ),
            )
            bucket = self._headroom_bucket(headroom)
            self.db.execute(
                """
                INSERT INTO v056_headroom_histogram(run_id,bucket_lower_ms,request_count)
                VALUES(?,?,1)
                ON CONFLICT(run_id,bucket_lower_ms) DO UPDATE SET request_count=request_count+1
                """,
                (run_id, bucket),
            )
        else:
            self.db.execute(
                "UPDATE v056_request_totals SET request_count=request_count+1 WHERE run_id=?",
                (run_id,),
            )
        sampling_hash = record.get("rfq_id_sha256") or record["payload_sha256"]
        sample_value = int.from_bytes(bytes(sampling_hash)[:8], "big")
        eligible = sample_value % int(self.storage["request_sample_hash_modulus"]) == int(
            self.storage["request_sample_hash_remainder"]
        )
        if eligible:
            self.db.execute(
                "UPDATE v056_request_totals SET sample_eligible_count=sample_eligible_count+1 WHERE run_id=?",
                (run_id,),
            )
        if eligible and self.request_samples_stored < int(self.storage["maximum_request_samples"]):
            columns = (
                "received_at_ms",
                "payload_sha256",
                "rfq_id_sha256",
                "requestor_id_sha256",
                "condition_id",
                "leg_position_ids_json",
                "yes_position_id",
                "no_position_id",
                "direction",
                "side",
                "size_unit",
                "size_value",
                "submission_deadline_ms",
            )
            self.db.execute(
                f"INSERT INTO v056_request_samples(run_id,{','.join(columns)}) "
                f"VALUES(?{',?' * len(columns)})",
                (run_id, *(record[column] for column in columns)),
            )
            self.request_samples_stored += 1
            self.db.execute(
                "UPDATE v056_request_totals SET sample_stored_count=sample_stored_count+1 WHERE run_id=?",
                (run_id,),
            )

    def _record_non_request(self, run_id: int, record: Mapping[str, Any]) -> None:
        message_type = str(record["message_type"])
        full_types = set(self.storage["full_non_request_record_types"])
        should_store = message_type in full_types
        if not should_store:
            sample_value = int.from_bytes(bytes(record["payload_sha256"])[:8], "big")
            should_store = sample_value % int(self.storage["request_sample_hash_modulus"]) == int(
                self.storage["request_sample_hash_remainder"]
            )
        if not should_store or self.non_request_records_stored >= int(
            self.storage["maximum_full_non_request_records"]
        ):
            return
        columns = (
            "received_at_ms",
            "message_type",
            "payload_sha256",
            "rfq_id_sha256",
            "requestor_id_sha256",
            "condition_id",
            "leg_position_ids_json",
            "direction",
            "side",
            "price_e6",
            "trade_size_e6",
            "executed_at_ms",
            "auth_success",
            "role",
            "status_code",
            "error_code",
        )
        self.db.execute(
            f"INSERT INTO v056_non_request_events(run_id,{','.join(columns)}) "
            f"VALUES(?{',?' * len(columns)})",
            (run_id, *(record[column] for column in columns)),
        )
        self.non_request_records_stored += 1

    def record(self, run_id: int, payload: Mapping[str, Any], received_at_ms: int) -> None:
        try:
            record = sanitize_inbound(payload, received_at_ms)
            message_type = str(record["message_type"])
            self._increment_type(run_id, message_type)
            if message_type == "RFQ_REQUEST":
                self._record_request(run_id, record)
            else:
                self._record_non_request(run_id, record)
            self.pending_events += 1
            due_count = self.pending_events >= int(self.storage["transaction_batch_events"])
            due_time = (
                time.monotonic() - self.last_commit_monotonic
                >= float(self.storage["transaction_max_seconds"])
            )
            if due_count or due_time:
                self.flush(enforce_limit=True)
        except V056StorageLimitError:
            raise
        except sqlite3.Error as exc:
            raise V056LocalStorageError("RFQ_SQLITE_WRITE_FAILED") from exc

    def record_clock_samples(
        self, run_id: int, phase: str, samples: list[Mapping[str, int]]
    ) -> None:
        rows = [
            (
                run_id,
                phase,
                int(sample["local_send_ms"]),
                int(sample["local_receive_ms"]),
                int(sample["server_time_s"]),
                int(sample["round_trip_ms"]),
                int(sample["server_minus_local_lower_ms"]),
                int(sample["server_minus_local_upper_ms"]),
            )
            for sample in samples
        ]
        if rows:
            self.db.executemany(
                """
                INSERT INTO v056_clock_samples(
                  run_id,phase,local_send_ms,local_receive_ms,server_time_s,round_trip_ms,
                  server_minus_local_lower_ms,server_minus_local_upper_ms
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                rows,
            )
            self.db.commit()
            self.pending_events = 0
            self.last_commit_monotonic = time.monotonic()

    def flush(self, *, enforce_limit: bool) -> None:
        self.db.commit()
        self.pending_events = 0
        self.last_commit_monotonic = time.monotonic()
        if enforce_limit and database_footprint(self.path) > int(
            self.storage["maximum_database_bytes"]
        ):
            raise V056StorageLimitError("RFQ_DATABASE_SIZE_LIMIT")

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        error_class: str | None,
        error_code: str | None,
        reconnects: int,
        clock_probe_errors: int,
    ) -> None:
        self.flush(enforce_limit=False)
        self.db.execute(
            """
            UPDATE v056_runs SET finished_at_ms=?,status=?,error_class=?,error_code=?,
              reconnects=?,clock_probe_errors=? WHERE run_id=?
            """,
            (
                time.time_ns() // 1_000_000,
                status,
                error_class,
                error_code,
                int(reconnects),
                int(clock_probe_errors),
                int(run_id),
            ),
        )
        self.db.execute(
            "UPDATE v056_meta SET value=? WHERE key='completion_reason'",
            (_json_compact(status),),
        )
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


async def _receive_payloads(websocket: Any, timeout: float) -> list[Mapping[str, Any]]:
    raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    parsed = json.loads(text)
    items = parsed if isinstance(parsed, list) else [parsed]
    return [item for item in items if isinstance(item, Mapping)]


async def collect_rfq_activity(
    *,
    endpoint: str,
    credentials: CredentialBundle,
    store: V056Store,
    run_id: int,
    contract: Mapping[str, Any],
    connect_factory: Callable[..., Any] = connect,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    transport = contract["transport"]
    duration = float(
        transport["duration_seconds"] if duration_seconds is None else duration_seconds
    )
    deadline = time.monotonic() + duration
    counters: Counter[str] = Counter()
    reconnects = 0
    authenticated = False
    auth_failed = False
    terminal_status: str | None = None
    terminal_error_class: str | None = None
    terminal_error_code: str | None = None
    backoffs = [float(value) for value in transport["reconnect_backoff_seconds"]]
    while time.monotonic() < deadline and reconnects <= int(transport["maximum_reconnects"]):
        try:
            async with connect_factory(
                endpoint,
                ping_interval=None,
                open_timeout=float(transport["open_timeout_seconds"]),
                close_timeout=float(transport["close_timeout_seconds"]),
                max_size=2 * 1024 * 1024,
                max_queue=1024,
                user_agent_header="PolyMarkerQuantBot-V0.56-authenticated-observer/1.0",
                proxy=None,
            ) as websocket:
                sender = AuthOnlySender()
                await sender.send_auth(websocket, credentials)
                counters["outbound_auth_messages"] += 1
                auth_deadline = time.monotonic() + float(transport["auth_timeout_seconds"])
                connection_authenticated = False
                while time.monotonic() < auth_deadline and not connection_authenticated:
                    timeout = max(0.001, auth_deadline - time.monotonic())
                    for payload in await _receive_payloads(websocket, timeout):
                        received_ms = time.time_ns() // 1_000_000
                        store.record(run_id, payload, received_ms)
                        message_type = str(payload.get("type") or "UNKNOWN")
                        counters[message_type] += 1
                        if message_type == "auth":
                            if payload.get("success") is True:
                                authenticated = True
                                connection_authenticated = True
                            else:
                                auth_failed = True
                                break
                    if auth_failed:
                        break
                if auth_failed:
                    break
                if not connection_authenticated:
                    raise V056ObserverError("RFQ_AUTH_TIMEOUT")
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    timeout = min(float(transport["receive_poll_seconds"]), remaining)
                    if timeout <= 0.0:
                        break
                    try:
                        payloads = await _receive_payloads(websocket, timeout)
                    except TimeoutError:
                        continue
                    for payload in payloads:
                        received_ms = time.time_ns() // 1_000_000
                        store.record(run_id, payload, received_ms)
                        counters[str(payload.get("type") or "UNKNOWN")] += 1
            if time.monotonic() < deadline and not auth_failed:
                raise V056ObserverError("RFQ_CONNECTION_CLOSED_BEFORE_DEADLINE")
        except asyncio.CancelledError:
            raise
        except V056StorageLimitError as exc:
            terminal_status = "STORAGE_LIMIT_REACHED"
            terminal_error_class = type(exc).__name__
            terminal_error_code = str(exc)
            counters["storage_limit_errors"] += 1
            break
        except (V056LocalStorageError, sqlite3.Error) as exc:
            terminal_status = "LOCAL_STORAGE_FAILED"
            terminal_error_class = type(exc).__name__
            terminal_error_code = str(exc)[:128]
            counters["local_storage_errors"] += 1
            break
        except Exception as exc:
            terminal_error_class = type(exc).__name__
            terminal_error_code = str(exc)[:128]
            reconnects += 1
            counters["transport_errors"] += 1
            if reconnects > int(transport["maximum_reconnects"]):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            await asyncio.sleep(min(backoffs[min(reconnects - 1, len(backoffs) - 1)], remaining))
    try:
        store.flush(enforce_limit=True)
    except V056StorageLimitError as exc:
        terminal_status = "STORAGE_LIMIT_REACHED"
        terminal_error_class = type(exc).__name__
        terminal_error_code = str(exc)
    completed_runtime = time.monotonic() >= deadline
    if auth_failed:
        status = "AUTH_FAILED"
    elif terminal_status is not None:
        status = terminal_status
    elif completed_runtime and authenticated:
        status = "COMPLETED"
    else:
        status = "TRANSPORT_FAILED"
    return {
        "status": status,
        "authenticated_at_least_once": authenticated,
        "auth_failed": auth_failed,
        "completed_runtime": completed_runtime,
        "reconnects": reconnects,
        "terminal_error_class": terminal_error_class,
        "terminal_error_code": terminal_error_code,
        "counters": dict(sorted(counters.items())),
        "orders_created": 0,
        "paper_orders": 0,
        "quotes_submitted": 0,
        "confirmations_sent": 0,
        "transactions_created": 0,
    }


async def _clock_phase(
    *,
    phase: str,
    run_id: int,
    store: V056Store,
    clock_contract: Mapping[str, Any],
    clock_sampler: Callable[..., list[dict[str, int]]],
) -> int:
    samples = await asyncio.to_thread(
        clock_sampler,
        endpoint=str(clock_contract["endpoint"]),
        sample_count=int(clock_contract["samples_per_phase"]),
        spacing_ms=int(clock_contract["sample_spacing_ms"]),
        timeout_seconds=float(clock_contract["request_timeout_seconds"]),
    )
    store.record_clock_samples(run_id, phase, samples)
    return max(0, int(clock_contract["samples_per_phase"]) - len(samples))


async def run_v056_async(
    *,
    prereg_path: str | Path,
    database_path: str | Path,
    project_root: str | Path = ROOT,
    environ: Mapping[str, str] | None = None,
    connect_factory: Callable[..., Any] = connect,
    duration_seconds: float | None = None,
    clock_sampler: Callable[..., list[dict[str, int]]] = measure_server_clock,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    prereg = load_and_verify_preregistration(prereg_file, project_root=root)
    credentials, preflight = credential_preflight(prereg["contract"], environ=environ)
    if credentials is None:
        return {
            "status": "BLOCKED_CREDENTIAL_PREFLIGHT",
            "preflight": preflight,
            "database_created": False,
            "network_connection_attempted": False,
            "orders_created": 0,
            "paper_orders": 0,
            "transactions_created": 0,
            "real_money": "BLOQUEADO",
        }
    database = Path(database_path).resolve()
    store = V056Store(database, prereg["contract"]["storage"])
    official_duration = int(prereg["contract"]["transport"]["duration_seconds"])
    store.open_new(
        preregistration_sha256=sha256_file(prereg_file),
        duration_seconds=official_duration,
    )
    run_id = store.start_run()
    clock_probe_errors = 0
    try:
        try:
            clock_probe_errors += await _clock_phase(
                phase="START",
                run_id=run_id,
                store=store,
                clock_contract=prereg["contract"]["clock"],
                clock_sampler=clock_sampler,
            )
        except Exception:
            clock_probe_errors += int(prereg["contract"]["clock"]["samples_per_phase"])
        result = await collect_rfq_activity(
            endpoint=str(prereg["contract"]["transport"]["endpoint"]),
            credentials=credentials,
            store=store,
            run_id=run_id,
            contract=prereg["contract"],
            connect_factory=connect_factory,
            duration_seconds=duration_seconds,
        )
        try:
            clock_probe_errors += await _clock_phase(
                phase="END",
                run_id=run_id,
                store=store,
                clock_contract=prereg["contract"]["clock"],
                clock_sampler=clock_sampler,
            )
        except Exception:
            clock_probe_errors += int(prereg["contract"]["clock"]["samples_per_phase"])
        store.finish_run(
            run_id,
            status=str(result["status"]),
            error_class=result.get("terminal_error_class"),
            error_code=result.get("terminal_error_code"),
            reconnects=int(result["reconnects"]),
            clock_probe_errors=clock_probe_errors,
        )
        return {
            **result,
            "clock_probe_errors": clock_probe_errors,
            "preflight": {**preflight, "database_created": True},
            "database_created": True,
            "network_connection_attempted": True,
            "database_path": str(database),
            "real_money": "BLOQUEADO",
        }
    except BaseException as exc:
        store.finish_run(
            run_id,
            status="INTERRUPTED",
            error_class=type(exc).__name__,
            error_code=str(exc)[:128],
            reconnects=0,
            clock_probe_errors=clock_probe_errors,
        )
        raise
    finally:
        store.close()


def run_v056(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_v056_async(**kwargs))


__all__ = [
    "AuthOnlySender",
    "CredentialBundle",
    "DDL",
    "SCHEMA_VERSION",
    "V056LocalStorageError",
    "V056ObserverError",
    "V056StorageLimitError",
    "V056Store",
    "collect_rfq_activity",
    "credential_preflight",
    "database_footprint",
    "measure_server_clock",
    "run_v056",
    "run_v056_async",
    "sanitize_inbound",
]
