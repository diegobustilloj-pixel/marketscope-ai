from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import urllib.request
import zlib
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v056_rfq_observer import (
    AuthOnlySender,
    CredentialBundle,
    _receive_payloads,
    credential_preflight,
    database_footprint,
    sanitize_inbound,
)
from polymarket_bot.v057_contract import VARIANT, load_and_verify_preregistration


SCHEMA_VERSION = "1"

DDL = """
CREATE TABLE v057_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE v057_runs(
 run_id INTEGER PRIMARY KEY AUTOINCREMENT,started_at_ms INTEGER NOT NULL,finished_at_ms INTEGER,
 status TEXT NOT NULL,error_class TEXT,error_code TEXT,reconnects INTEGER NOT NULL DEFAULT 0,
 auth_successes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE v057_message_counts(
 run_id INTEGER NOT NULL,message_type TEXT NOT NULL,event_count INTEGER NOT NULL,
 PRIMARY KEY(run_id,message_type)
) WITHOUT ROWID;
CREATE TABLE v057_totals(
 run_id INTEGER PRIMARY KEY,all_requests INTEGER NOT NULL DEFAULT 0,
 eligible_requests INTEGER NOT NULL DEFAULT 0,sampled_requests INTEGER NOT NULL DEFAULT 0,
 queued_requests INTEGER NOT NULL DEFAULT 0,queue_dropped INTEGER NOT NULL DEFAULT 0,
 book_fetch_completed INTEGER NOT NULL DEFAULT 0,book_fetch_success INTEGER NOT NULL DEFAULT 0,
 book_fetch_failed INTEGER NOT NULL DEFAULT 0,trade_records INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE v057_joined_requests(
 request_id INTEGER PRIMARY KEY AUTOINCREMENT,run_id INTEGER NOT NULL,
 rfq_id_sha256 BLOB NOT NULL UNIQUE,payload_sha256 BLOB NOT NULL,
 received_at_ms INTEGER NOT NULL,submission_deadline_ms INTEGER NOT NULL,
 condition_id BLOB NOT NULL,leg_position_ids_json TEXT NOT NULL,
 requested_notional_e6 TEXT NOT NULL,book_status TEXT NOT NULL,
 book_started_at_ms INTEGER,book_finished_at_ms INTEGER,book_source_max_ms INTEGER,
 expected_books INTEGER NOT NULL,received_books INTEGER,book_payload_sha256 BLOB,
 asks_zlib BLOB,error_code TEXT
);
CREATE INDEX idx_v057_join_status ON v057_joined_requests(book_status);
CREATE TABLE v057_trades(
 trade_id INTEGER PRIMARY KEY AUTOINCREMENT,run_id INTEGER NOT NULL,
 rfq_id_sha256 BLOB NOT NULL,payload_sha256 BLOB NOT NULL,received_at_ms INTEGER NOT NULL,
 condition_id BLOB,leg_position_ids_json TEXT,direction TEXT,side TEXT,
 price_e6 TEXT,size_e6 TEXT,executed_at_ms INTEGER
);
CREATE INDEX idx_v057_trade_rfq ON v057_trades(rfq_id_sha256);
"""


class V057ReplayError(RuntimeError):
    pass


class V057StorageError(V057ReplayError):
    pass


class V057StorageLimitError(V057ReplayError):
    pass


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def request_job(
    record: Mapping[str, Any], scope: Mapping[str, Any]
) -> dict[str, Any] | None:
    if (
        record.get("message_type") != "RFQ_REQUEST"
        or record.get("direction") != scope["direction"]
        or record.get("side") != scope["side"]
        or record.get("size_unit") != scope["requested_size_unit"]
        or not isinstance(record.get("rfq_id_sha256"), bytes)
        or not isinstance(record.get("condition_id"), bytes)
        or not isinstance(record.get("submission_deadline_ms"), int)
    ):
        return None
    try:
        legs = json.loads(str(record.get("leg_position_ids_json")))
        requested = int(str(record.get("size_value")))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(legs, list)
        or not int(scope["minimum_leg_count"]) <= len(legs) <= int(scope["maximum_leg_count"])
        or any(not str(token).isdigit() for token in legs)
        or requested <= 0
    ):
        return None
    sample_value = int.from_bytes(record["rfq_id_sha256"][:8], "big")
    sampled = sample_value % int(scope["request_hash_modulus"]) == int(
        scope["request_hash_remainder"]
    )
    return {
        "sampled": sampled,
        "rfq_id_sha256": record["rfq_id_sha256"],
        "payload_sha256": record["payload_sha256"],
        "received_at_ms": int(record["received_at_ms"]),
        "submission_deadline_ms": int(record["submission_deadline_ms"]),
        "condition_id": record["condition_id"],
        "leg_position_ids": [str(token) for token in legs],
        "requested_notional_e6": str(requested),
    }


def sanitize_books(payload: Any, expected_tokens: list[str]) -> dict[str, Any]:
    if not isinstance(payload, list):
        raise V057ReplayError("BOOK_RESPONSE_NOT_LIST")
    expected = set(expected_tokens)
    result: list[dict[str, Any]] = []
    for raw in payload:
        if not isinstance(raw, Mapping):
            raise V057ReplayError("BOOK_ITEM_INVALID")
        token = str(raw.get("asset_id", raw.get("assetId", "")))
        if token not in expected:
            raise V057ReplayError("BOOK_TOKEN_MISMATCH")
        try:
            timestamp = int(raw.get("timestamp"))
        except (TypeError, ValueError) as exc:
            raise V057ReplayError("BOOK_TIMESTAMP_INVALID") from exc
        asks_raw = raw.get("asks")
        if not isinstance(asks_raw, list):
            raise V057ReplayError("BOOK_ASKS_INVALID")
        asks: list[list[str]] = []
        for level in asks_raw:
            if not isinstance(level, Mapping):
                raise V057ReplayError("BOOK_LEVEL_INVALID")
            price = str(level.get("price", ""))
            size = str(level.get("size", ""))
            try:
                price_number = float(price)
                size_number = float(size)
            except ValueError as exc:
                raise V057ReplayError("BOOK_LEVEL_NOT_NUMERIC") from exc
            if not 0.0 < price_number < 1.0 or size_number <= 0.0:
                raise V057ReplayError("BOOK_LEVEL_OUT_OF_RANGE")
            asks.append([price, size])
        result.append(
            {
                "asset_id": token,
                "market": str(raw.get("market", "")),
                "timestamp": timestamp,
                "asks": sorted(asks, key=lambda item: float(item[0])),
            }
        )
    if {book["asset_id"] for book in result} != expected or len(result) != len(expected_tokens):
        raise V057ReplayError("BOOK_SET_INCOMPLETE")
    compact = _compact(sorted(result, key=lambda book: expected_tokens.index(book["asset_id"])))
    return {
        "books": json.loads(compact),
        "compressed": zlib.compress(compact.encode("utf-8"), level=9),
        "sha256": hashlib.sha256(compact.encode("utf-8")).digest(),
        "source_max_ms": max(book["timestamp"] for book in result),
    }


def fetch_public_books(
    endpoint: str, token_ids: list[str], timeout_seconds: float
) -> Any:
    body = _compact([{"token_id": token} for token in token_ids]).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "PolyMarkerQuantBot-V0.57-public-book-replay/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=float(timeout_seconds)) as response:
        return json.loads(response.read(4 * 1024 * 1024).decode("utf-8"))


class RateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        self.interval = 1.0 / float(requests_per_second)
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            if now < self.next_at:
                await asyncio.sleep(self.next_at - now)
                now = time.monotonic()
            self.next_at = max(now, self.next_at) + self.interval


class V057Store:
    def __init__(self, path: str | Path, storage: Mapping[str, Any]) -> None:
        self.path = Path(path).resolve()
        self.storage = dict(storage)
        self.connection: sqlite3.Connection | None = None
        self.pending = 0
        self.last_commit = time.monotonic()

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise V057StorageError("Base V0.57 no abierta")
        return self.connection

    def open_new(self, preregistration_sha256: str, duration_seconds: int) -> None:
        if self.path.exists():
            raise V057StorageError("La base V0.57 ya existe; no se reanuda ni sobrescribe")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=60)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
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
            "realized_pnl_measured": False,
            "real_money": "BLOQUEADO",
        }
        self.connection.executemany(
            "INSERT INTO v057_meta(key,value) VALUES(?,?)",
            [(key, _compact(value)) for key, value in values.items()],
        )
        self.connection.commit()

    def start_run(self) -> int:
        cursor = self.db.execute(
            "INSERT INTO v057_runs(started_at_ms,status) VALUES(?,?)",
            (time.time_ns() // 1_000_000, "RUNNING"),
        )
        run_id = int(cursor.lastrowid)
        self.db.execute("INSERT INTO v057_totals(run_id) VALUES(?)", (run_id,))
        self.db.commit()
        return run_id

    def _type(self, run_id: int, message_type: str) -> None:
        self.db.execute(
            "INSERT INTO v057_message_counts VALUES(?,?,1) "
            "ON CONFLICT(run_id,message_type) DO UPDATE SET event_count=event_count+1",
            (run_id, message_type),
        )

    def record_payload(
        self, run_id: int, payload: Mapping[str, Any], received_at_ms: int, scope: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        record = sanitize_inbound(payload, received_at_ms)
        message_type = str(record["message_type"])
        self._type(run_id, message_type)
        job: dict[str, Any] | None = None
        if message_type == "auth" and record.get("auth_success") == 1:
            self.db.execute(
                "UPDATE v057_runs SET auth_successes=auth_successes+1 WHERE run_id=?", (run_id,)
            )
        elif message_type == "RFQ_REQUEST":
            self.db.execute("UPDATE v057_totals SET all_requests=all_requests+1 WHERE run_id=?", (run_id,))
            job = request_job(record, scope)
            if job is not None:
                self.db.execute(
                    "UPDATE v057_totals SET eligible_requests=eligible_requests+1 WHERE run_id=?", (run_id,)
                )
                if job["sampled"]:
                    maximum = int(self.storage["maximum_joined_requests"])
                    stored = int(
                        self.db.execute("SELECT sampled_requests FROM v057_totals WHERE run_id=?", (run_id,)).fetchone()[0]
                    )
                    if stored < maximum:
                        self.db.execute(
                            """INSERT OR IGNORE INTO v057_joined_requests(
                            run_id,rfq_id_sha256,payload_sha256,received_at_ms,submission_deadline_ms,
                            condition_id,leg_position_ids_json,requested_notional_e6,book_status,expected_books
                            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (
                                run_id, job["rfq_id_sha256"], job["payload_sha256"],
                                job["received_at_ms"], job["submission_deadline_ms"], job["condition_id"],
                                _compact(job["leg_position_ids"]), job["requested_notional_e6"],
                                "PENDING", len(job["leg_position_ids"]),
                            ),
                        )
                        if self.db.execute("SELECT changes()").fetchone()[0]:
                            self.db.execute(
                                "UPDATE v057_totals SET sampled_requests=sampled_requests+1 WHERE run_id=?", (run_id,)
                            )
                        else:
                            job = None
                    else:
                        job = None
                else:
                    job = None
        elif message_type == "RFQ_TRADE":
            if isinstance(record.get("rfq_id_sha256"), bytes):
                self.db.execute(
                    """INSERT INTO v057_trades(
                    run_id,rfq_id_sha256,payload_sha256,received_at_ms,condition_id,
                    leg_position_ids_json,direction,side,price_e6,size_e6,executed_at_ms
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id, record["rfq_id_sha256"], record["payload_sha256"], received_at_ms,
                        record.get("condition_id"), record.get("leg_position_ids_json"),
                        record.get("direction"), record.get("side"), record.get("price_e6"),
                        record.get("trade_size_e6"), record.get("executed_at_ms"),
                    ),
                )
                self.db.execute("UPDATE v057_totals SET trade_records=trade_records+1 WHERE run_id=?", (run_id,))
        self.pending += 1
        if self.pending >= int(self.storage["transaction_batch_events"]) or (
            time.monotonic() - self.last_commit >= float(self.storage["transaction_max_seconds"])
        ):
            self.flush(True)
        return job

    def mark_queued(self, run_id: int) -> None:
        self.db.execute("UPDATE v057_totals SET queued_requests=queued_requests+1 WHERE run_id=?", (run_id,))

    def mark_queue_dropped(self, run_id: int, rfq_hash: bytes) -> None:
        self.db.execute(
            "UPDATE v057_joined_requests SET book_status='QUEUE_DROPPED',error_code='QUEUE_FULL' WHERE rfq_id_sha256=?",
            (rfq_hash,),
        )
        self.db.execute("UPDATE v057_totals SET queue_dropped=queue_dropped+1 WHERE run_id=?", (run_id,))

    def record_join(
        self,
        run_id: int,
        rfq_hash: bytes,
        *,
        status: str,
        started_at_ms: int,
        finished_at_ms: int,
        sanitized: Mapping[str, Any] | None,
        error_code: str | None,
    ) -> None:
        success = status == "SUCCESS" and sanitized is not None
        self.db.execute(
            """UPDATE v057_joined_requests SET book_status=?,book_started_at_ms=?,book_finished_at_ms=?,
            book_source_max_ms=?,received_books=?,book_payload_sha256=?,asks_zlib=?,error_code=?
            WHERE rfq_id_sha256=?""",
            (
                status, started_at_ms, finished_at_ms,
                sanitized.get("source_max_ms") if sanitized else None,
                len(sanitized["books"]) if sanitized else None,
                sanitized.get("sha256") if sanitized else None,
                sanitized.get("compressed") if sanitized else None,
                error_code, rfq_hash,
            ),
        )
        self.db.execute(
            """UPDATE v057_totals SET book_fetch_completed=book_fetch_completed+1,
            book_fetch_success=book_fetch_success+?,book_fetch_failed=book_fetch_failed+? WHERE run_id=?""",
            (int(success), int(not success), run_id),
        )
        self.pending += 1
        if self.pending >= int(self.storage["transaction_batch_events"]):
            self.flush(True)

    def flush(self, enforce_limit: bool) -> None:
        self.db.commit()
        self.pending = 0
        self.last_commit = time.monotonic()
        if enforce_limit and database_footprint(self.path) > int(self.storage["maximum_database_bytes"]):
            raise V057StorageLimitError("V057_DATABASE_SIZE_LIMIT")

    def finish(self, run_id: int, status: str, error: BaseException | None, reconnects: int) -> None:
        self.flush(False)
        self.db.execute(
            "UPDATE v057_runs SET finished_at_ms=?,status=?,error_class=?,error_code=?,reconnects=? WHERE run_id=?",
            (
                time.time_ns() // 1_000_000, status,
                type(error).__name__ if error else None, str(error)[:128] if error else None,
                reconnects, run_id,
            ),
        )
        self.db.execute("UPDATE v057_meta SET value=? WHERE key='completion_reason'", (_compact(status),))
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


async def _book_worker(
    *,
    queue: asyncio.Queue[dict[str, Any]],
    limiter: RateLimiter,
    store: V057Store,
    run_id: int,
    endpoint: str,
    timeout_seconds: float,
    fetcher: Callable[[str, list[str], float], Any],
    fatal_errors: list[BaseException],
) -> None:
    while True:
        job = await queue.get()
        try:
            await limiter.wait()
            started = time.time_ns() // 1_000_000
            try:
                raw = await asyncio.to_thread(fetcher, endpoint, job["leg_position_ids"], timeout_seconds)
                sanitized = sanitize_books(raw, job["leg_position_ids"])
                status, error_code = "SUCCESS", None
            except Exception as exc:
                sanitized = None
                status, error_code = "FAILED", f"{type(exc).__name__}:{str(exc)[:80]}"
            finished = time.time_ns() // 1_000_000
            try:
                store.record_join(
                    run_id, job["rfq_id_sha256"], status=status, started_at_ms=started,
                    finished_at_ms=finished, sanitized=sanitized, error_code=error_code,
                )
            except BaseException as exc:
                fatal_errors.append(exc)
                raise
        finally:
            queue.task_done()


async def collect_joined_replay(
    *,
    endpoint: str,
    credentials: CredentialBundle,
    store: V057Store,
    run_id: int,
    contract: Mapping[str, Any],
    connect_factory: Callable[..., Any] = connect,
    book_fetcher: Callable[[str, list[str], float], Any] = fetch_public_books,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    transport = contract["transport"]
    duration = float(transport["duration_seconds"] if duration_seconds is None else duration_seconds)
    deadline = time.monotonic() + duration
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=int(transport["book_queue_maximum"]))
    limiter = RateLimiter(float(transport["book_requests_per_second"]))
    worker_errors: list[BaseException] = []
    workers = [
        asyncio.create_task(
            _book_worker(
                queue=queue, limiter=limiter, store=store, run_id=run_id,
                endpoint=str(transport["clob_books_endpoint"]),
                timeout_seconds=float(transport["book_http_timeout_seconds"]), fetcher=book_fetcher,
                fatal_errors=worker_errors,
            )
        )
        for _ in range(int(transport["book_workers"]))
    ]
    counters: Counter[str] = Counter()
    reconnects = 0
    authenticated = False
    auth_failed = False
    terminal_error: BaseException | None = None
    backoffs = [float(value) for value in transport["reconnect_backoff_seconds"]]
    try:
        while time.monotonic() < deadline and reconnects <= int(transport["maximum_reconnects"]):
            try:
                async with connect_factory(
                    endpoint, ping_interval=None, open_timeout=float(transport["open_timeout_seconds"]),
                    close_timeout=float(transport["close_timeout_seconds"]), max_size=2 * 1024 * 1024,
                    max_queue=1024, user_agent_header="PolyMarkerQuantBot-V0.57-joined-replay/1.0", proxy=None,
                ) as websocket:
                    sender = AuthOnlySender()
                    await sender.send_auth(websocket, credentials)
                    counters["outbound_auth_messages"] += 1
                    auth_deadline = time.monotonic() + float(transport["auth_timeout_seconds"])
                    connection_authenticated = False
                    while time.monotonic() < auth_deadline and not connection_authenticated:
                        for payload in await _receive_payloads(websocket, max(0.001, auth_deadline - time.monotonic())):
                            received = time.time_ns() // 1_000_000
                            store.record_payload(run_id, payload, received, contract["scope"])
                            counters[str(payload.get("type") or "UNKNOWN")] += 1
                            if payload.get("type") == "auth":
                                if payload.get("success") is True:
                                    authenticated = connection_authenticated = True
                                else:
                                    auth_failed = True
                                    break
                    if auth_failed:
                        break
                    if not connection_authenticated:
                        raise V057ReplayError("RFQ_AUTH_TIMEOUT")
                    while time.monotonic() < deadline:
                        timeout = min(float(transport["receive_poll_seconds"]), deadline - time.monotonic())
                        if timeout <= 0:
                            break
                        try:
                            payloads = await _receive_payloads(websocket, timeout)
                        except TimeoutError:
                            continue
                        for payload in payloads:
                            if worker_errors:
                                raise worker_errors[0]
                            received = time.time_ns() // 1_000_000
                            job = store.record_payload(run_id, payload, received, contract["scope"])
                            counters[str(payload.get("type") or "UNKNOWN")] += 1
                            if job is not None:
                                try:
                                    queue.put_nowait(job)
                                    store.mark_queued(run_id)
                                except asyncio.QueueFull:
                                    store.mark_queue_dropped(run_id, job["rfq_id_sha256"])
                if time.monotonic() < deadline and not auth_failed:
                    raise V057ReplayError("RFQ_CONNECTION_CLOSED_BEFORE_DEADLINE")
            except asyncio.CancelledError:
                raise
            except (V057StorageError, V057StorageLimitError, sqlite3.Error) as exc:
                terminal_error = exc
                break
            except Exception as exc:
                terminal_error = exc
                reconnects += 1
                counters["transport_errors"] += 1
                if reconnects > int(transport["maximum_reconnects"]):
                    break
                await asyncio.sleep(min(backoffs[min(reconnects - 1, len(backoffs) - 1)], max(0.0, deadline - time.monotonic())))
        try:
            await asyncio.wait_for(queue.join(), timeout=float(transport["queue_drain_seconds"]))
        except TimeoutError:
            counters["queue_drain_timeout"] += 1
        if worker_errors:
            terminal_error = worker_errors[0]
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
    store.flush(True)
    completed_runtime = time.monotonic() >= deadline
    if auth_failed:
        status = "AUTH_FAILED"
    elif isinstance(terminal_error, V057StorageLimitError):
        status = "STORAGE_LIMIT_REACHED"
    elif isinstance(terminal_error, (V057StorageError, sqlite3.Error)):
        status = "LOCAL_STORAGE_FAILED"
    elif completed_runtime and authenticated:
        status = "COMPLETED"
    else:
        status = "TRANSPORT_FAILED"
    return {
        "status": status,
        "authenticated_at_least_once": authenticated,
        "completed_runtime": completed_runtime,
        "reconnects": reconnects,
        "terminal_error_class": type(terminal_error).__name__ if terminal_error else None,
        "terminal_error_code": str(terminal_error)[:128] if terminal_error else None,
        "counters": dict(sorted(counters.items())),
        "orders_created": 0,
        "paper_orders": 0,
        "quotes_submitted": 0,
        "confirmations_sent": 0,
        "transactions_created": 0,
    }


async def run_v057_async(
    *,
    prereg_path: str | Path,
    database_path: str | Path,
    project_root: str | Path = ROOT,
    environ: Mapping[str, str] | None = None,
    connect_factory: Callable[..., Any] = connect,
    book_fetcher: Callable[[str, list[str], float], Any] = fetch_public_books,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    prereg = load_and_verify_preregistration(prereg_file, project_root=root)
    credentials, preflight = credential_preflight(prereg["contract"], environ=environ)
    if credentials is None:
        return {
            "status": "BLOCKED_CREDENTIAL_PREFLIGHT", "preflight": preflight,
            "database_created": False, "network_connection_attempted": False,
            "orders_created": 0, "paper_orders": 0, "transactions_created": 0,
            "real_money": "BLOQUEADO",
        }
    database = Path(database_path).resolve()
    store = V057Store(database, prereg["contract"]["storage"])
    store.open_new(sha256_file(prereg_file), int(prereg["contract"]["transport"]["duration_seconds"]))
    run_id = store.start_run()
    try:
        result = await collect_joined_replay(
            endpoint=str(prereg["contract"]["transport"]["rfq_endpoint"]), credentials=credentials,
            store=store, run_id=run_id, contract=prereg["contract"], connect_factory=connect_factory,
            book_fetcher=book_fetcher, duration_seconds=duration_seconds,
        )
        error = (
            V057ReplayError(str(result["terminal_error_code"]))
            if result.get("terminal_error_class") else None
        )
        store.finish(run_id, str(result["status"]), error, int(result["reconnects"]))
        return {
            **result, "preflight": {**preflight, "database_created": True},
            "database_created": True, "network_connection_attempted": True,
            "database_path": str(database), "real_money": "BLOQUEADO",
        }
    except BaseException as exc:
        store.finish(run_id, "INTERRUPTED", exc, 0)
        raise
    finally:
        store.close()


def run_v057(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_v057_async(**kwargs))


__all__ = [
    "DDL", "RateLimiter", "SCHEMA_VERSION", "V057ReplayError", "V057StorageError",
    "V057StorageLimitError", "V057Store", "collect_joined_replay", "fetch_public_books",
    "request_job", "run_v057", "run_v057_async", "sanitize_books",
]
