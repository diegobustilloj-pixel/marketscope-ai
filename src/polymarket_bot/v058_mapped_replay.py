from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v056_rfq_observer import credential_preflight, database_footprint, sanitize_inbound
from polymarket_bot.v057_joined_replay import (
    V057StorageError,
    V057StorageLimitError,
    collect_joined_replay,
    fetch_public_books,
)
from polymarket_bot.v058_contract import VARIANT, load_and_verify_preregistration


DDL = """
CREATE TABLE v058_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE v058_runs(
 run_id INTEGER PRIMARY KEY AUTOINCREMENT,started_at_ms INTEGER NOT NULL,finished_at_ms INTEGER,
 status TEXT NOT NULL,error_class TEXT,error_code TEXT,reconnects INTEGER NOT NULL DEFAULT 0,
 auth_successes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE v058_position_map(
 position_id TEXT PRIMARY KEY,clob_token_id TEXT NOT NULL,condition_id TEXT NOT NULL,
 outcome_index INTEGER NOT NULL CHECK(outcome_index IN (0,1))
) WITHOUT ROWID;
CREATE TABLE v058_message_counts(
 run_id INTEGER NOT NULL,message_type TEXT NOT NULL,event_count INTEGER NOT NULL,
 PRIMARY KEY(run_id,message_type)
) WITHOUT ROWID;
CREATE TABLE v058_totals(
 run_id INTEGER PRIMARY KEY,all_requests INTEGER NOT NULL DEFAULT 0,
 structurally_eligible_requests INTEGER NOT NULL DEFAULT 0,mapped_requests INTEGER NOT NULL DEFAULT 0,
 unmapped_requests INTEGER NOT NULL DEFAULT 0,sampled_requests INTEGER NOT NULL DEFAULT 0,
 queued_requests INTEGER NOT NULL DEFAULT 0,queue_dropped INTEGER NOT NULL DEFAULT 0,
 book_fetch_completed INTEGER NOT NULL DEFAULT 0,book_fetch_success INTEGER NOT NULL DEFAULT 0,
 book_fetch_failed INTEGER NOT NULL DEFAULT 0,trade_records INTEGER NOT NULL DEFAULT 0,
 trade_records_dropped INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE v058_joined_requests(
 request_id INTEGER PRIMARY KEY AUTOINCREMENT,run_id INTEGER NOT NULL,
 rfq_id_sha256 BLOB NOT NULL UNIQUE,payload_sha256 BLOB NOT NULL,
 received_at_ms INTEGER NOT NULL,submission_deadline_ms INTEGER NOT NULL,
 combo_condition_id BLOB NOT NULL,leg_position_ids_json TEXT NOT NULL,
 leg_clob_token_ids_json TEXT NOT NULL,leg_condition_ids_json TEXT NOT NULL,
 requested_notional_e6 TEXT NOT NULL,book_status TEXT NOT NULL,
 book_started_at_ms INTEGER,book_finished_at_ms INTEGER,book_source_max_ms INTEGER,
 expected_books INTEGER NOT NULL,received_books INTEGER,book_payload_sha256 BLOB,
 asks_zlib BLOB,error_code TEXT
);
CREATE INDEX idx_v058_join_status ON v058_joined_requests(book_status);
CREATE TABLE v058_trades(
 trade_id INTEGER PRIMARY KEY AUTOINCREMENT,run_id INTEGER NOT NULL,
 rfq_id_sha256 BLOB NOT NULL,payload_sha256 BLOB NOT NULL,received_at_ms INTEGER NOT NULL,
 condition_id BLOB,leg_position_ids_json TEXT,direction TEXT,side TEXT,
 price_e6 TEXT,size_e6 TEXT,executed_at_ms INTEGER
);
CREATE INDEX idx_v058_trade_rfq ON v058_trades(rfq_id_sha256);
"""

POSITION_SEED_SCHEMA = "v058_position_seed_from_v057_1"


class V058ReplayError(RuntimeError):
    pass


class V058MappingError(V058ReplayError):
    pass


class V058StorageError(V057StorageError):
    pass


class V058StorageLimitError(V057StorageLimitError):
    pass


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_position_seed(
    *, source_database_path: str | Path, early_abort_path: str | Path, output_path: str | Path
) -> dict[str, Any]:
    database = Path(source_database_path).resolve()
    early_abort_file = Path(early_abort_path).resolve()
    output = Path(output_path).resolve()
    early_abort = json.loads(early_abort_file.read_text(encoding="utf-8"))
    if early_abort.get("verdict") != "FAIL_POSITION_ID_WAS_USED_AS_CLOB_TOKEN_ID":
        raise V058MappingError("V057_EARLY_ABORT_EVIDENCE_INVALID")
    evidence = early_abort.get("evidence", {})
    artifacts = {
        "database_sha256": database,
        "database_wal_sha256": Path(f"{database}-wal"),
        "database_shm_sha256": Path(f"{database}-shm"),
    }
    source_hashes: dict[str, str] = {}
    for key, path in artifacts.items():
        if not path.is_file():
            raise V058MappingError(f"V057_SEED_SOURCE_MISSING:{key}")
        actual = sha256_file(path)
        if actual != evidence.get(key):
            raise V058MappingError(f"V057_SEED_SOURCE_HASH_MISMATCH:{key}")
        source_hashes[key] = actual
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        connection.execute("PRAGMA query_only=ON")
        if str(connection.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
            raise V058MappingError("V057_SEED_SOURCE_QUICK_CHECK_FAILED")
        positions: set[str] = set()
        rows = 0
        for (raw,) in connection.execute("SELECT leg_position_ids_json FROM v057_joined_requests"):
            rows += 1
            try:
                values = json.loads(str(raw))
            except json.JSONDecodeError as exc:
                raise V058MappingError("V057_SEED_POSITION_JSON_INVALID") from exc
            if not isinstance(values, list) or any(not str(value).isdigit() for value in values):
                raise V058MappingError("V057_SEED_POSITION_ID_INVALID")
            positions.update(str(value) for value in values)
    finally:
        connection.close()
    ordered = sorted(positions, key=int)
    if not ordered:
        raise V058MappingError("V057_SEED_EMPTY")
    payload = {
        "schema": POSITION_SEED_SCHEMA,
        "source_variant": "V0.57_RFQ_CLOB_JOINED_PAPER_REPLAY_NO_QUOTES",
        "source_database": database.name,
        "source_early_abort_sha256": sha256_file(early_abort_file),
        "source_hashes": source_hashes,
        "sampled_request_rows": rows,
        "position_count": len(ordered),
        "position_ids_sha256": hashlib.sha256(_compact(ordered).encode("utf-8")).hexdigest(),
        "position_ids": ordered,
        "contains_credentials_or_addresses": False,
        "contains_requestor_identifiers": False,
    }
    if output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if existing != payload:
            raise V058MappingError("V058_POSITION_SEED_ALREADY_DIFFERS")
        return existing
    _write_atomic(output, payload)
    return payload


def load_position_seed(path: str | Path) -> tuple[list[str], dict[str, Any]]:
    payload = json.loads(Path(path).resolve().read_text(encoding="utf-8"))
    positions = payload.get("position_ids")
    if (
        payload.get("schema") != POSITION_SEED_SCHEMA
        or not isinstance(positions, list)
        or not positions
        or any(not str(position).isdigit() for position in positions)
    ):
        raise V058MappingError("V058_POSITION_SEED_INVALID")
    ordered = [str(position) for position in positions]
    if ordered != sorted(set(ordered), key=int):
        raise V058MappingError("V058_POSITION_SEED_NOT_SORTED_UNIQUE")
    digest = hashlib.sha256(_compact(ordered).encode("utf-8")).hexdigest()
    if digest != payload.get("position_ids_sha256") or len(ordered) != int(payload.get("position_count", -1)):
        raise V058MappingError("V058_POSITION_SEED_HASH_MISMATCH")
    return ordered, {
        "schema": POSITION_SEED_SCHEMA,
        "positions": len(ordered),
        "sha256": digest,
        "sampled_request_rows": int(payload.get("sampled_request_rows", 0)),
    }


def parse_mapping_page(payload: Any) -> tuple[dict[str, dict[str, Any]], str | None, int]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("markets"), list):
        raise V058MappingError("GAMMA_MAPPING_RESPONSE_INVALID")
    result: dict[str, dict[str, Any]] = {}
    market_count = 0
    for market in payload["markets"]:
        if not isinstance(market, Mapping):
            raise V058MappingError("GAMMA_MAPPING_MARKET_INVALID")
        if market.get("closed") is True or str(market.get("comboStatus", "")).lower() != "enabled":
            continue
        positions = market.get("positionIds")
        tokens_raw = market.get("clobTokenIds")
        try:
            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        except json.JSONDecodeError as exc:
            raise V058MappingError("GAMMA_CLOB_TOKENS_INVALID") from exc
        condition = str(market.get("conditionId", "")).lower()
        if (
            not isinstance(positions, list) or not isinstance(tokens, list)
            or len(positions) != 2 or len(tokens) != 2
            or re.fullmatch(r"0x[0-9a-f]{64}", condition) is None
        ):
            raise V058MappingError("GAMMA_POSITION_TOKEN_ALIGNMENT_INVALID")
        for index, (position, token) in enumerate(zip(positions, tokens, strict=True)):
            position_text, token_text = str(position), str(token)
            if not position_text.isdigit() or not token_text.isdigit():
                raise V058MappingError("GAMMA_POSITION_TOKEN_NOT_DECIMAL")
            record = {
                "position_id": position_text,
                "clob_token_id": token_text,
                "condition_id": condition,
                "outcome_index": index,
            }
            previous = result.get(position_text)
            if previous is not None and previous != record:
                raise V058MappingError("GAMMA_DUPLICATE_POSITION_CONFLICT")
            result[position_text] = record
        market_count += 1
    cursor_value = payload.get("next_cursor")
    cursor = str(cursor_value) if cursor_value not in (None, "") else None
    return result, cursor, market_count


def fetch_position_map(
    endpoint: str,
    query: Mapping[str, Any],
    position_ids: list[str],
    position_ids_parameter: str,
    position_ids_per_batch: int,
    maximum_batches: int,
    timeout_seconds: float,
    maximum_attempts_per_batch: int = 3,
    retry_backoff_seconds: tuple[float, ...] | list[float] = (0.5, 2.0),
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    markets = 0
    batches = 0
    retries = 0
    batch_size = int(position_ids_per_batch)
    if batch_size <= 0 or int(maximum_batches) <= 0 or not position_ids:
        raise V058MappingError("GAMMA_MAPPING_BATCH_CONFIG_INVALID")
    required_batches = (len(position_ids) + batch_size - 1) // batch_size
    if required_batches > int(maximum_batches):
        raise V058MappingError("GAMMA_MAPPING_MAXIMUM_BATCHES")
    for start in range(0, len(position_ids), batch_size):
        batch = position_ids[start : start + batch_size]
        parameters = [(str(key), str(value)) for key, value in query.items()]
        parameters.extend((str(position_ids_parameter), str(position)) for position in batch)
        url = f"{endpoint}?{urllib.parse.urlencode(parameters)}"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "PolyMarkerQuantBot-V0.58-position-map/1.0"},
        )
        payload: Any = None
        for attempt in range(int(maximum_attempts_per_batch)):
            try:
                with urllib.request.urlopen(request, timeout=float(timeout_seconds)) as response:
                    payload = json.loads(response.read(8 * 1024 * 1024).decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt + 1 >= int(maximum_attempts_per_batch):
                    raise
            except (urllib.error.URLError, OSError, http.client.IncompleteRead, json.JSONDecodeError):
                if attempt + 1 >= int(maximum_attempts_per_batch):
                    raise
            retries += 1
            backoff_index = min(attempt, len(retry_backoff_seconds) - 1)
            time.sleep(float(retry_backoff_seconds[backoff_index]))
        page_map, next_cursor, page_markets = parse_mapping_page(payload)
        if next_cursor is not None:
            raise V058MappingError("GAMMA_FILTERED_BATCH_UNEXPECTED_CURSOR")
        for position, record in page_map.items():
            previous = mapping.get(position)
            if previous is not None and previous != record:
                raise V058MappingError("GAMMA_CROSS_PAGE_POSITION_CONFLICT")
            mapping[position] = record
        markets += page_markets
        batches += 1
    digest = hashlib.sha256(
        _compact([mapping[key] for key in sorted(mapping, key=int)]).encode("utf-8")
    ).hexdigest()
    resolved_seed_positions = sum(position in mapping for position in position_ids)
    return mapping, {
        "batches": batches, "markets": markets, "positions": len(mapping), "sha256": digest,
        "seed_positions": len(position_ids),
        "resolved_seed_positions": resolved_seed_positions,
        "seed_resolution_rate": resolved_seed_positions / len(position_ids),
        "retries": retries,
        "raw_payloads_persisted": False, "credentials_sent": False,
    }


def mapped_request_job(
    record: Mapping[str, Any], scope: Mapping[str, Any], position_map: Mapping[str, Mapping[str, Any]]
) -> tuple[str, dict[str, Any] | None]:
    if (
        record.get("message_type") != "RFQ_REQUEST"
        or record.get("direction") != scope["direction"]
        or record.get("side") != scope["side"]
        or record.get("size_unit") != scope["requested_size_unit"]
        or not isinstance(record.get("rfq_id_sha256"), bytes)
        or not isinstance(record.get("condition_id"), bytes)
        or not isinstance(record.get("submission_deadline_ms"), int)
    ):
        return "NOT_STRUCTURAL", None
    try:
        positions = json.loads(str(record.get("leg_position_ids_json")))
        requested = int(str(record.get("size_value")))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "NOT_STRUCTURAL", None
    if (
        not isinstance(positions, list)
        or not int(scope["minimum_leg_count"]) <= len(positions) <= int(scope["maximum_leg_count"])
        or any(not str(position).isdigit() for position in positions)
        or requested <= 0
    ):
        return "NOT_STRUCTURAL", None
    if any(str(position) not in position_map for position in positions):
        return "UNMAPPED", None
    records = [position_map[str(position)] for position in positions]
    sample_value = int.from_bytes(record["rfq_id_sha256"][:8], "big")
    sampled = sample_value % int(scope["request_hash_modulus"]) == int(scope["request_hash_remainder"])
    return "MAPPED", {
        "sampled": sampled,
        "rfq_id_sha256": record["rfq_id_sha256"],
        "payload_sha256": record["payload_sha256"],
        "received_at_ms": int(record["received_at_ms"]),
        "submission_deadline_ms": int(record["submission_deadline_ms"]),
        "combo_condition_id": record["condition_id"],
        "leg_position_ids_original": [str(value) for value in positions],
        "leg_position_ids": [str(item["clob_token_id"]) for item in records],
        "leg_condition_ids": [str(item["condition_id"]) for item in records],
        "requested_notional_e6": str(requested),
    }


class V058Store:
    def __init__(
        self,
        path: str | Path,
        storage: Mapping[str, Any],
        position_map: Mapping[str, Mapping[str, Any]],
        mapping_summary: Mapping[str, Any],
    ) -> None:
        self.path = Path(path).resolve()
        self.storage = dict(storage)
        self.position_map = dict(position_map)
        self.mapping_summary = dict(mapping_summary)
        self.connection: sqlite3.Connection | None = None
        self.pending = 0
        self.last_commit = time.monotonic()

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise V058StorageError("Base V0.58 no abierta")
        return self.connection

    def open_new(self, preregistration_sha256: str, duration_seconds: int) -> None:
        if self.path.exists():
            raise V058StorageError("La base V0.58 ya existe; no se reanuda ni sobrescribe")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=60)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(f"PRAGMA wal_autocheckpoint={int(self.storage['wal_autocheckpoint_pages'])}")
        self.connection.executescript(DDL)
        values = {
            "schema_version": "1", "variant": VARIANT,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "preregistration_sha256": preregistration_sha256,
            "duration_seconds": int(duration_seconds), "completion_reason": None,
            "mapping_summary": self.mapping_summary,
            "orders_enabled": False, "paper_orders_enabled": False,
            "quote_submission_enabled": False, "signatures_enabled": False,
            "transactions_enabled": False, "credential_values_stored": False,
            "addresses_stored": False, "raw_payloads_stored": False,
            "realized_pnl_measured": False, "real_money": "BLOQUEADO",
        }
        self.connection.executemany(
            "INSERT INTO v058_meta(key,value) VALUES(?,?)",
            [(key, _compact(value)) for key, value in values.items()],
        )
        self.connection.executemany(
            "INSERT INTO v058_position_map VALUES(?,?,?,?)",
            [
                (record["position_id"], record["clob_token_id"], record["condition_id"], record["outcome_index"])
                for record in self.position_map.values()
            ],
        )
        self.connection.commit()

    def start_run(self) -> int:
        cursor = self.db.execute(
            "INSERT INTO v058_runs(started_at_ms,status) VALUES(?,?)",
            (time.time_ns() // 1_000_000, "RUNNING"),
        )
        run_id = int(cursor.lastrowid)
        self.db.execute("INSERT INTO v058_totals(run_id) VALUES(?)", (run_id,))
        self.db.commit()
        return run_id

    def _type(self, run_id: int, message_type: str) -> None:
        self.db.execute(
            "INSERT INTO v058_message_counts VALUES(?,?,1) ON CONFLICT(run_id,message_type) DO UPDATE SET event_count=event_count+1",
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
            self.db.execute("UPDATE v058_runs SET auth_successes=auth_successes+1 WHERE run_id=?", (run_id,))
        elif message_type == "RFQ_REQUEST":
            self.db.execute("UPDATE v058_totals SET all_requests=all_requests+1 WHERE run_id=?", (run_id,))
            classification, job = mapped_request_job(record, scope, self.position_map)
            if classification in {"MAPPED", "UNMAPPED"}:
                self.db.execute(
                    "UPDATE v058_totals SET structurally_eligible_requests=structurally_eligible_requests+1 WHERE run_id=?", (run_id,)
                )
            if classification == "UNMAPPED":
                self.db.execute("UPDATE v058_totals SET unmapped_requests=unmapped_requests+1 WHERE run_id=?", (run_id,))
            elif classification == "MAPPED" and job is not None:
                self.db.execute("UPDATE v058_totals SET mapped_requests=mapped_requests+1 WHERE run_id=?", (run_id,))
                if job["sampled"]:
                    stored = int(self.db.execute("SELECT sampled_requests FROM v058_totals WHERE run_id=?", (run_id,)).fetchone()[0])
                    if stored < int(self.storage["maximum_joined_requests"]):
                        self.db.execute(
                            """INSERT OR IGNORE INTO v058_joined_requests(
                            run_id,rfq_id_sha256,payload_sha256,received_at_ms,submission_deadline_ms,
                            combo_condition_id,leg_position_ids_json,leg_clob_token_ids_json,
                            leg_condition_ids_json,requested_notional_e6,book_status,expected_books
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                run_id, job["rfq_id_sha256"], job["payload_sha256"], job["received_at_ms"],
                                job["submission_deadline_ms"], job["combo_condition_id"],
                                _compact(job["leg_position_ids_original"]), _compact(job["leg_position_ids"]),
                                _compact(job["leg_condition_ids"]), job["requested_notional_e6"],
                                "PENDING", len(job["leg_position_ids"]),
                            ),
                        )
                        if self.db.execute("SELECT changes()").fetchone()[0]:
                            self.db.execute("UPDATE v058_totals SET sampled_requests=sampled_requests+1 WHERE run_id=?", (run_id,))
                        else:
                            job = None
                    else:
                        job = None
                else:
                    job = None
        elif message_type == "RFQ_TRADE" and isinstance(record.get("rfq_id_sha256"), bytes):
            stored_trades = int(
                self.db.execute(
                    "SELECT trade_records FROM v058_totals WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )
            if stored_trades < int(self.storage["maximum_trade_records"]):
                self.db.execute(
                    """INSERT INTO v058_trades(
                    run_id,rfq_id_sha256,payload_sha256,received_at_ms,condition_id,
                    leg_position_ids_json,direction,side,price_e6,size_e6,executed_at_ms
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id, record["rfq_id_sha256"], record["payload_sha256"], received_at_ms,
                        record.get("condition_id"), record.get("leg_position_ids_json"), record.get("direction"),
                        record.get("side"), record.get("price_e6"), record.get("trade_size_e6"), record.get("executed_at_ms"),
                    ),
                )
                self.db.execute(
                    "UPDATE v058_totals SET trade_records=trade_records+1 WHERE run_id=?", (run_id,)
                )
            else:
                self.db.execute(
                    "UPDATE v058_totals SET trade_records_dropped=trade_records_dropped+1 WHERE run_id=?", (run_id,)
                )
        self.pending += 1
        if self.pending >= int(self.storage["transaction_batch_events"]) or time.monotonic() - self.last_commit >= float(self.storage["transaction_max_seconds"]):
            self.flush(True)
        return job

    def mark_queued(self, run_id: int) -> None:
        self.db.execute("UPDATE v058_totals SET queued_requests=queued_requests+1 WHERE run_id=?", (run_id,))

    def mark_queue_dropped(self, run_id: int, rfq_hash: bytes) -> None:
        self.db.execute(
            "UPDATE v058_joined_requests SET book_status='QUEUE_DROPPED',error_code='QUEUE_FULL' WHERE rfq_id_sha256=?", (rfq_hash,)
        )
        self.db.execute("UPDATE v058_totals SET queue_dropped=queue_dropped+1 WHERE run_id=?", (run_id,))

    def record_join(
        self, run_id: int, rfq_hash: bytes, *, status: str, started_at_ms: int,
        finished_at_ms: int, sanitized: Mapping[str, Any] | None, error_code: str | None,
    ) -> None:
        success = status == "SUCCESS" and sanitized is not None
        self.db.execute(
            """UPDATE v058_joined_requests SET book_status=?,book_started_at_ms=?,book_finished_at_ms=?,
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
            """UPDATE v058_totals SET book_fetch_completed=book_fetch_completed+1,
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
            raise V058StorageLimitError("V058_DATABASE_SIZE_LIMIT")

    def finish(self, run_id: int, status: str, error: BaseException | None, reconnects: int) -> None:
        self.flush(False)
        self.db.execute(
            "UPDATE v058_runs SET finished_at_ms=?,status=?,error_class=?,error_code=?,reconnects=? WHERE run_id=?",
            (
                time.time_ns() // 1_000_000, status,
                type(error).__name__ if error else None, str(error)[:128] if error else None,
                reconnects, run_id,
            ),
        )
        self.db.execute("UPDATE v058_meta SET value=? WHERE key='completion_reason'", (_compact(status),))
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


async def run_v058_async(
    *,
    prereg_path: str | Path,
    database_path: str | Path,
    project_root: str | Path = ROOT,
    environ: Mapping[str, str] | None = None,
    connect_factory: Callable[..., Any] = connect,
    book_fetcher: Callable[[str, list[str], float], Any] = fetch_public_books,
    mapping_fetcher: Callable[..., tuple[dict[str, dict[str, Any]], dict[str, Any]]] = fetch_position_map,
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
    mapping_contract = prereg["contract"]["mapping"]
    try:
        position_seed, seed_summary = load_position_seed(root / str(mapping_contract["position_seed_path"]))
        if len(position_seed) < int(mapping_contract["minimum_seed_positions"]):
            raise V058MappingError("V058_POSITION_SEED_TOO_SMALL")
        position_map, mapping_summary = await asyncio.to_thread(
            mapping_fetcher,
            str(mapping_contract["endpoint"]), dict(mapping_contract["query"]),
            position_seed, str(mapping_contract["position_ids_parameter"]),
            int(mapping_contract["position_ids_per_batch"]), int(mapping_contract["maximum_batches"]),
            float(mapping_contract["request_timeout_seconds"]),
            int(mapping_contract["maximum_attempts_per_batch"]),
            list(mapping_contract["retry_backoff_seconds"]),
        )
        mapping_summary = {**mapping_summary, "seed": seed_summary}
        if int(mapping_summary["markets"]) < int(mapping_contract["minimum_markets"]):
            raise V058MappingError("GAMMA_MAPPING_TOO_SMALL")
        if float(mapping_summary["seed_resolution_rate"]) < float(mapping_contract["minimum_seed_resolution_rate"]):
            raise V058MappingError("GAMMA_SEED_RESOLUTION_RATE_TOO_LOW")
    except Exception as exc:
        return {
            "status": "BLOCKED_POSITION_MAPPING_PREFLIGHT",
            "mapping_error_class": type(exc).__name__, "mapping_error_code": str(exc)[:128],
            "database_created": False, "network_connection_attempted": True,
            "credentials_sent_to_mapping": False, "orders_created": 0, "paper_orders": 0,
            "transactions_created": 0, "real_money": "BLOQUEADO",
        }
    database = Path(database_path).resolve()
    store = V058Store(database, prereg["contract"]["storage"], position_map, mapping_summary)
    store.open_new(sha256_file(prereg_file), int(prereg["contract"]["transport"]["duration_seconds"]))
    run_id = store.start_run()
    try:
        result = await collect_joined_replay(
            endpoint=str(prereg["contract"]["transport"]["rfq_endpoint"]), credentials=credentials,
            store=store, run_id=run_id, contract=prereg["contract"], connect_factory=connect_factory,
            book_fetcher=book_fetcher, duration_seconds=duration_seconds,
        )
        error = V058ReplayError(str(result["terminal_error_code"])) if result.get("terminal_error_class") else None
        store.finish(run_id, str(result["status"]), error, int(result["reconnects"]))
        return {
            **result, "mapping": mapping_summary,
            "preflight": {**preflight, "database_created": True},
            "database_created": True, "network_connection_attempted": True,
            "database_path": str(database), "real_money": "BLOQUEADO",
        }
    except BaseException as exc:
        store.finish(run_id, "INTERRUPTED", exc, 0)
        raise
    finally:
        store.close()


def run_v058(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_v058_async(**kwargs))


__all__ = [
    "DDL", "POSITION_SEED_SCHEMA", "V058MappingError", "V058ReplayError", "V058StorageError",
    "V058StorageLimitError", "V058Store", "build_position_seed", "fetch_position_map",
    "load_position_seed", "mapped_request_job", "parse_mapping_page", "run_v058", "run_v058_async",
]
