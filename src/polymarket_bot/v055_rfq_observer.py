from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from websockets.asyncio.client import connect

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v055_contract import VARIANT, load_and_verify_preregistration


SCHEMA_VERSION = "1"

DDL = """
CREATE TABLE v055_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE v055_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    status TEXT NOT NULL,
    error_class TEXT
);
CREATE TABLE v055_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    received_at_ms INTEGER NOT NULL,
    message_type TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    rfq_id_sha256 TEXT,
    requestor_id_sha256 TEXT,
    condition_id TEXT,
    leg_position_ids_json TEXT,
    yes_position_id TEXT,
    no_position_id TEXT,
    direction TEXT,
    side TEXT,
    size_unit TEXT,
    size_value TEXT,
    submission_deadline_ms INTEGER,
    price_e6 TEXT,
    trade_size_e6 TEXT,
    executed_at_ms INTEGER,
    auth_success INTEGER,
    role TEXT,
    status_code TEXT,
    error_code TEXT,
    FOREIGN KEY(run_id) REFERENCES v055_runs(run_id)
);
CREATE INDEX idx_v055_events_type ON v055_events(message_type);
CREATE INDEX idx_v055_events_rfq ON v055_events(rfq_id_sha256);
"""


class V055ObserverError(RuntimeError):
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
            raise V055ObserverError("Solo se permite un auth por conexion")
        payload = credentials.auth_payload()
        if payload.get("type") != "auth" or set(payload) != {"type", "auth", "identity"}:
            raise V055ObserverError("Payload auth incompatible")
        await websocket.send(json.dumps(payload, separators=(",", ":")))
        self.auth_messages_sent = 1

    async def send(self, websocket: Any, payload: Mapping[str, Any]) -> None:
        del websocket
        message_type = str(payload.get("type") or "")
        raise V055ObserverError(f"Mensaje saliente bloqueado: {message_type or 'SIN_TIPO'}")


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


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


def _finalize_fingerprint(record: dict[str, Any]) -> dict[str, Any]:
    safe = {key: value for key, value in record.items() if key != "payload_sha256"}
    record["payload_sha256"] = hashlib.sha256(
        _json_compact(safe).encode("utf-8")
    ).hexdigest()
    return record


def sanitize_inbound(payload: Mapping[str, Any], received_at_ms: int) -> dict[str, Any]:
    message_type = _safe_text(payload.get("type"), 64) or "UNKNOWN"
    result: dict[str, Any] = {
        "received_at_ms": int(received_at_ms),
        "message_type": message_type,
        "payload_sha256": "",
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
        return _finalize_fingerprint(result)
    if message_type in {"RFQ_REQUEST", "RFQ_TRADE", "RFQ_EXECUTION_UPDATE", "RFQ_ERROR"}:
        result["rfq_id_sha256"] = _sha256_text(payload.get("rfq_id"))
    if message_type == "RFQ_REQUEST":
        result["requestor_id_sha256"] = _sha256_text(payload.get("requestor_public_id"))
        condition = str(payload.get("condition_id") or "").strip().lower()
        if re.fullmatch(r"0x[0-9a-f]{64}", condition):
            result["condition_id"] = condition
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
        result["requestor_id_sha256"] = _sha256_text(payload.get("requester_id"))
        condition = str(payload.get("condition_id") or "").strip().lower()
        if re.fullmatch(r"0x[0-9a-f]{64}", condition):
            result["condition_id"] = condition
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
        result["error_code"] = ":".join(value for value in (request_type, code) if value) or None
    return _finalize_fingerprint(result)


def database_footprint(path: Path) -> int:
    return sum(
        item.stat().st_size
        for item in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if item.exists()
    )


class V055Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.connection: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise V055ObserverError("Base V0.55 no abierta")
        return self.connection

    def open_new(self, *, preregistration_sha256: str, duration_seconds: int) -> None:
        if self.path.exists():
            raise V055ObserverError("La base V0.55 ya existe; no se reanuda ni sobreescribe")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=60)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
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
            "INSERT INTO v055_meta(key,value) VALUES(?,?)",
            [(key, _json_compact(value)) for key, value in values.items()],
        )
        self.connection.commit()

    def start_run(self) -> int:
        cursor = self.db.execute(
            "INSERT INTO v055_runs(started_at_ms,status) VALUES(?,?)",
            (time.time_ns() // 1_000_000, "RUNNING"),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def record(self, run_id: int, payload: Mapping[str, Any], received_at_ms: int) -> None:
        record = sanitize_inbound(payload, received_at_ms)
        columns = list(record)
        self.db.execute(
            f"INSERT INTO v055_events(run_id,{','.join(columns)}) VALUES(?{',?' * len(columns)})",
            [int(run_id), *(record[column] for column in columns)],
        )
        self.db.commit()

    def finish_run(self, run_id: int, *, status: str, error_class: str | None = None) -> None:
        self.db.execute(
            "UPDATE v055_runs SET finished_at_ms=?,status=?,error_class=? WHERE run_id=?",
            (time.time_ns() // 1_000_000, status, error_class, int(run_id)),
        )
        self.db.execute(
            "UPDATE v055_meta SET value=? WHERE key='completion_reason'",
            (_json_compact(status),),
        )
        self.db.commit()

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
    store: V055Store,
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
    terminal_error_class: str | None = None
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
                user_agent_header="PolyMarkerQuantBot-V0.55-authenticated-observer/1.0",
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
                    raise V055ObserverError("RFQ_AUTH_TIMEOUT")
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
                        if database_footprint(store.path) > int(
                            contract["storage"]["maximum_database_bytes"]
                        ):
                            raise V055ObserverError("RFQ_DATABASE_SIZE_LIMIT")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            terminal_error_class = type(exc).__name__
            reconnects += 1
            counters["transport_errors"] += 1
            if reconnects > int(transport["maximum_reconnects"]):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            await asyncio.sleep(min(backoffs[min(reconnects - 1, len(backoffs) - 1)], remaining))
    completed_runtime = time.monotonic() >= deadline
    if auth_failed:
        status = "AUTH_FAILED"
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
        "counters": dict(sorted(counters.items())),
        "orders_created": 0,
        "paper_orders": 0,
        "quotes_submitted": 0,
        "confirmations_sent": 0,
        "transactions_created": 0,
    }


async def run_v055_async(
    *,
    prereg_path: str | Path,
    database_path: str | Path,
    project_root: str | Path = ROOT,
    environ: Mapping[str, str] | None = None,
    connect_factory: Callable[..., Any] = connect,
    duration_seconds: float | None = None,
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
    store = V055Store(database)
    official_duration = int(prereg["contract"]["transport"]["duration_seconds"])
    store.open_new(
        preregistration_sha256=sha256_file(prereg_file),
        duration_seconds=official_duration,
    )
    run_id = store.start_run()
    try:
        result = await collect_rfq_activity(
            endpoint=str(prereg["contract"]["transport"]["endpoint"]),
            credentials=credentials,
            store=store,
            run_id=run_id,
            contract=prereg["contract"],
            connect_factory=connect_factory,
            duration_seconds=duration_seconds,
        )
        store.finish_run(
            run_id,
            status=str(result["status"]),
            error_class=result.get("terminal_error_class"),
        )
        return {
            **result,
            "preflight": {**preflight, "database_created": True},
            "database_created": True,
            "network_connection_attempted": True,
            "database_path": str(database),
            "real_money": "BLOQUEADO",
        }
    except BaseException as exc:
        store.finish_run(run_id, status="INTERRUPTED", error_class=type(exc).__name__)
        raise
    finally:
        store.close()


def run_v055(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_v055_async(**kwargs))


__all__ = [
    "AuthOnlySender",
    "CredentialBundle",
    "DDL",
    "SCHEMA_VERSION",
    "V055ObserverError",
    "V055Store",
    "collect_rfq_activity",
    "credential_preflight",
    "database_footprint",
    "run_v055",
    "run_v055_async",
    "sanitize_inbound",
]
