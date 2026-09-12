from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import io
import json
import math
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
SCHEMA = "polyledger_sentinel_v001"
WALLET_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")
OFFICIAL_ACTIVITY_TYPES = (
    "TRADE",
    "SPLIT",
    "MERGE",
    "REDEEM",
    "REWARD",
    "CONVERSION",
    "MAKER_REBATE",
    "REFERRAL_REWARD",
)
REWARD_TYPES = {
    "REWARD",
    "MAKER_REBATE",
    "TAKER_REBATE",
    "REFERRAL_REWARD",
    "YIELD",
}
INTERNAL_TYPES = {"SPLIT", "MERGE", "CONVERSION"}
EXTERNAL_TYPES = {"DEPOSIT", "WITHDRAWAL"}


class PolyLedgerError(RuntimeError):
    """Raised when public evidence cannot be captured or interpreted safely."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def validate_wallet(wallet: str) -> str:
    normalized = str(wallet).strip().lower()
    if not WALLET_PATTERN.fullmatch(normalized):
        raise ValueError("La wallet debe ser una dirección 0x de 40 caracteres hexadecimales")
    return normalized


def build_url(base: str, path: str, params: Mapping[str, Any] | None = None) -> str:
    items: list[tuple[str, str]] = []
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, (tuple, list)):
            value = ",".join(str(item) for item in value)
        items.append((key, str(value).lower() if isinstance(value, bool) else str(value)))
    query = urllib.parse.urlencode(items)
    return f"{base}{path}" + (f"?{query}" if query else "")


class PublicDataClient:
    """Read-only client. It has no signing, private-key or order methods."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 45.0,
        retries: int = 5,
        user_agent: str = "polyledger-sentinel/0.1 read-only",
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.user_agent = user_agent
        self.requests = 0
        self._request_lock = threading.Lock()

    def get_bytes(
        self,
        url: str,
        accept: str = "application/json",
        *,
        attempts: int | None = None,
    ) -> bytes:
        last_error: Exception | None = None
        attempt_count = self.retries if attempts is None else max(1, attempts)
        for attempt in range(attempt_count):
            request = urllib.request.Request(
                url,
                headers={"Accept": accept, "User-Agent": self.user_agent},
                method="GET",
            )
            try:
                with self._request_lock:
                    self.requests += 1
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                    raise PolyLedgerError(f"HTTP {exc.code} al consultar {url}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
            if attempt + 1 < attempt_count:
                time.sleep(min(8.0, 0.5 * (2**attempt)))
        raise PolyLedgerError(f"No se pudo consultar {url}: {last_error}")

    def get_json(self, url: str) -> Any:
        try:
            return json.loads(self.get_bytes(url).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PolyLedgerError(f"Respuesta JSON inválida: {url}") from exc


def event_identity(row: Mapping[str, Any]) -> str:
    """Stable identity excluding mutable profile metadata."""
    identity = {
        "proxyWallet": str(row.get("proxyWallet") or "").lower(),
        "timestamp": int(number(row.get("timestamp"))),
        "transactionHash": str(row.get("transactionHash") or "").lower(),
        "type": str(row.get("type") or "").upper(),
        "conditionId": str(row.get("conditionId") or "").lower(),
        "asset": str(row.get("asset") or ""),
        "side": str(row.get("side") or "").upper(),
        "outcomeIndex": row.get("outcomeIndex"),
        "size": row.get("size"),
        "usdcSize": row.get("usdcSize"),
        "price": row.get("price"),
    }
    return hashlib.sha256(stable_json(identity).encode("utf-8")).hexdigest()


def normalize_event(row: Mapping[str, Any]) -> dict[str, Any]:
    event_type = str(row.get("type") or "UNKNOWN").upper()
    side = str(row.get("side") or "").upper()
    usdc = abs(number(row.get("usdcSize")))
    cash_delta: float | None = None
    bucket = "unclassified"
    pnl_known = False
    explanation = "Tipo no reconocido: se conserva sin atribuir PnL."

    if event_type == "TRADE" and side in {"BUY", "SELL"}:
        cash_delta = -usdc if side == "BUY" else usdc
        bucket = "trading"
        explanation = (
            "Compra: salida de pUSD; el PnL se conoce al vender o resolver."
            if side == "BUY"
            else "Venta: entrada de pUSD; su PnL requiere costo de adquisición."
        )
    elif event_type == "SPLIT":
        cash_delta = -usdc
        bucket = "internal_conversion"
        explanation = "Convierte colateral en un juego completo de outcome tokens; no es pérdida."
    elif event_type == "MERGE":
        cash_delta = usdc
        bucket = "internal_conversion"
        explanation = "Convierte un juego completo de outcome tokens en colateral; no es ganancia."
    elif event_type == "CONVERSION":
        cash_delta = 0.0
        bucket = "internal_conversion"
        explanation = "Movimiento interno entre representaciones; se excluye del PnL."
    elif event_type == "REDEEM":
        cash_delta = usdc
        bucket = "settlement"
        explanation = "Cobro por resolución; el PnL exige conocer el costo de los tokens cobrados."
    elif event_type in REWARD_TYPES:
        cash_delta = usdc
        bucket = "reward"
        pnl_known = True
        explanation = "Ingreso explícito por recompensa o rebate."
    elif event_type == "DEPOSIT":
        cash_delta = usdc
        bucket = "external_transfer"
        explanation = "Aporte externo de capital; no es PnL."
    elif event_type == "WITHDRAWAL":
        cash_delta = -usdc
        bucket = "external_transfer"
        explanation = "Retiro externo de capital; no es PnL."

    timestamp = int(number(row.get("timestamp")))
    return {
        "event_id": event_identity(row),
        "wallet": str(row.get("proxyWallet") or "").lower(),
        "timestamp": timestamp,
        "timestamp_utc": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        "event_type": event_type,
        "side": side or None,
        "condition_id": str(row.get("conditionId") or "").lower() or None,
        "asset": str(row.get("asset") or "") or None,
        "outcome_index": row.get("outcomeIndex"),
        "outcome": row.get("outcome"),
        "title": row.get("title"),
        "size": number(row.get("size")),
        "price": number(row.get("price")),
        "usdc_size": usdc,
        "cash_delta_usd": cash_delta,
        "economic_bucket": bucket,
        "pnl_directly_known": pnl_known,
        "explanation": explanation,
        "transaction_hash": str(row.get("transactionHash") or "").lower() or None,
        "raw": dict(row),
    }


class LedgerStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity_events(
                event_id TEXT PRIMARY KEY,
                wallet TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                transaction_hash TEXT,
                condition_id TEXT,
                asset TEXT,
                side TEXT,
                size REAL NOT NULL,
                price REAL NOT NULL,
                usdc_size REAL NOT NULL,
                cash_delta_usd REAL,
                economic_bucket TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS activity_wallet_time
                ON activity_events(wallet, timestamp, event_id);
            CREATE TABLE IF NOT EXISTS snapshots(
                snapshot_id TEXT PRIMARY KEY,
                wallet TEXT NOT NULL,
                source TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                row_count INTEGER,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS snapshots_wallet_source
                ON snapshots(wallet, source, captured_at);
            CREATE TABLE IF NOT EXISTS sync_runs(
                run_id TEXT PRIMARY KEY,
                wallet TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                status TEXT NOT NULL,
                requested_start INTEGER,
                fetched_rows INTEGER NOT NULL DEFAULT 0,
                inserted_rows INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            """
        )
        self.db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema', ?)", (SCHEMA,)
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def latest_timestamp(self, wallet: str) -> int | None:
        row = self.db.execute(
            "SELECT MAX(timestamp) AS value FROM activity_events WHERE wallet=?", (wallet,)
        ).fetchone()
        return int(row["value"]) if row and row["value"] is not None else None

    def upsert_events(self, rows: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
        observed = now_utc()
        fetched = inserted = 0
        with self.db:
            for raw in rows:
                fetched += 1
                event = normalize_event(raw)
                before = self.db.total_changes
                self.db.execute(
                    """
                    INSERT INTO activity_events(
                        event_id, wallet, timestamp, event_type, transaction_hash,
                        condition_id, asset, side, size, price, usdc_size,
                        cash_delta_usd, economic_bucket, raw_json,
                        first_seen_at, last_seen_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        raw_json=excluded.raw_json,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        event["event_id"], event["wallet"], event["timestamp"],
                        event["event_type"], event["transaction_hash"],
                        event["condition_id"], event["asset"], event["side"],
                        event["size"], event["price"], event["usdc_size"],
                        event["cash_delta_usd"], event["economic_bucket"],
                        stable_json(event["raw"]), observed, observed,
                    ),
                )
                if self.db.total_changes > before:
                    current = self.db.execute(
                        "SELECT first_seen_at FROM activity_events WHERE event_id=?",
                        (event["event_id"],),
                    ).fetchone()
                    if current and current["first_seen_at"] == observed:
                        inserted += 1
        return fetched, inserted

    def events(self, wallet: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT raw_json FROM activity_events WHERE wallet=? ORDER BY timestamp,event_id",
            (wallet,),
        )
        return [normalize_event(json.loads(row["raw_json"])) for row in rows]

    def analytics(self, wallet: str) -> dict[str, Any]:
        grouped = list(
            self.db.execute(
                """
                SELECT event_type, COUNT(*) AS event_count,
                       COALESCE(SUM(cash_delta_usd), 0) AS cash_delta,
                       COALESCE(SUM(usdc_size), 0) AS usdc_size
                FROM activity_events
                WHERE wallet=?
                GROUP BY event_type
                ORDER BY event_type
                """,
                (wallet,),
            )
        )
        bounds = self.db.execute(
            """SELECT COUNT(*) AS event_count, MIN(timestamp) AS first_ts,
                      MAX(timestamp) AS last_ts,
                      COALESCE(SUM(cash_delta_usd), 0) AS cash_delta
               FROM activity_events WHERE wallet=?""",
            (wallet,),
        ).fetchone()
        event_counts = {str(row["event_type"]): int(row["event_count"]) for row in grouped}
        cash_by_type = {str(row["event_type"]): float(row["cash_delta"]) for row in grouped}
        usdc_by_type = {str(row["event_type"]): float(row["usdc_size"]) for row in grouped}
        rewards_by_type = {
            kind: usdc_by_type[kind] for kind in sorted(REWARD_TYPES) if kind in usdc_by_type
        }
        trade_rows = (
            {
                "event_id": row["event_id"],
                "timestamp": row["timestamp"],
                "event_type": row["event_type"],
                "asset": row["asset"],
                "side": row["side"],
                "size": row["size"],
                "price": row["price"],
            }
            for row in self.db.execute(
                """SELECT event_id,timestamp,event_type,asset,side,size,price
                   FROM activity_events
                   WHERE wallet=? AND event_type='TRADE'
                   ORDER BY timestamp,event_id""",
                (wallet,),
            )
        )
        fifo = fifo_trade_analysis(
            trade_rows,
            presorted=True,
            has_internal_events=any(kind in event_counts for kind in INTERNAL_TYPES),
        )
        digest = hashlib.sha256()
        for row in self.db.execute(
            "SELECT event_id FROM activity_events WHERE wallet=? ORDER BY timestamp,event_id",
            (wallet,),
        ):
            digest.update(str(row["event_id"]).encode("ascii"))
            digest.update(b"\n")
        return {
            "events": int(bounds["event_count"]),
            "event_counts": event_counts,
            "first_event_timestamp": bounds["first_ts"],
            "last_event_timestamp": bounds["last_ts"],
            "observable_pusd_cash_movement_usd": float(bounds["cash_delta"]),
            "cash_movement_by_type_usd": cash_by_type,
            "explicit_rewards_usd": sum(rewards_by_type.values()),
            "rewards_by_type_usd": rewards_by_type,
            "internal_conversion_notional_usd": sum(
                usdc_by_type.get(kind, 0.0) for kind in INTERNAL_TYPES
            ),
            "external_net_funding_usd": sum(
                cash_by_type.get(kind, 0.0) for kind in EXTERNAL_TYPES
            ),
            "fifo_trade_only": fifo,
            "_source_evidence": {
                "rows": int(bounds["event_count"]),
                "sha256_event_id_stream": digest.hexdigest(),
            },
        }

    def save_snapshot(self, wallet: str, source: str, payload: Any) -> str:
        captured = now_utc()
        encoded = stable_json(payload)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        snapshot_id = hashlib.sha256(
            f"{wallet}|{source}|{captured}|{digest}".encode("utf-8")
        ).hexdigest()
        row_count = len(payload) if isinstance(payload, list) else None
        with self.db:
            self.db.execute(
                """
                INSERT INTO snapshots(snapshot_id,wallet,source,captured_at,
                                      payload_sha256,row_count,payload_json)
                VALUES(?,?,?,?,?,?,?)
                """,
                (snapshot_id, wallet, source, captured, digest, row_count, encoded),
            )
        return snapshot_id

    def latest_snapshot(self, wallet: str, source: str) -> Any:
        row = self.db.execute(
            """SELECT payload_json FROM snapshots
               WHERE wallet=? AND source=?
               ORDER BY captured_at DESC LIMIT 1""",
            (wallet, source),
        ).fetchone()
        if row is None:
            raise PolyLedgerError(f"No existe snapshot local para {source}")
        return json.loads(row["payload_json"])

    def start_run(self, wallet: str, requested_start: int | None) -> str:
        started = now_utc()
        run_id = hashlib.sha256(f"{wallet}|{started}".encode("utf-8")).hexdigest()
        with self.db:
            self.db.execute(
                """UPDATE sync_runs
                   SET ended_at=?, status='INTERRUPTED', error='Reemplazado por una nueva ejecución'
                   WHERE wallet=? AND status='RUNNING'""",
                (started, wallet),
            )
            self.db.execute(
                """INSERT INTO sync_runs(run_id,wallet,started_at,status,requested_start)
                   VALUES(?,?,?,?,?)""",
                (run_id, wallet, started, "RUNNING", requested_start),
            )
        return run_id

    def finish_run(
        self, run_id: str, status: str, fetched: int, inserted: int, error: str | None = None
    ) -> None:
        with self.db:
            self.db.execute(
                """UPDATE sync_runs SET ended_at=?,status=?,fetched_rows=?,inserted_rows=?,error=?
                   WHERE run_id=?""",
                (now_utc(), status, fetched, inserted, error, run_id),
            )

    def integrity(self) -> str:
        return str(self.db.execute("PRAGMA quick_check").fetchone()[0])


@dataclass(frozen=True)
class PageAudit:
    endpoint: str
    start: int | None
    end: int | None
    offset: int
    count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "start": self.start,
            "end": self.end,
            "offset": self.offset,
            "count": self.count,
        }


def _validate_api_rows(rows: Any, wallet: str, endpoint: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise PolyLedgerError(f"Respuesta inválida de {endpoint}")
    for row in rows:
        owner = str(row.get("proxyWallet") or row.get("user") or "").lower()
        if owner and owner != wallet:
            raise PolyLedgerError(f"{endpoint} devolvió datos de otra wallet")
    return rows


def fetch_offset_pages(
    client: PublicDataClient,
    path: str,
    params: Mapping[str, Any],
    *,
    wallet: str,
    limit: int,
    max_offset: int,
    workers: int = 8,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], list[PageAudit], bool]:
    result: list[dict[str, Any]] = []
    audits: list[PageAudit] = []

    def fetch(offset: int) -> tuple[int, list[dict[str, Any]]]:
        page = _validate_api_rows(
            client.get_json(build_url(DATA_API, path, {**params, "limit": limit, "offset": offset})),
            wallet,
            path,
        )
        return offset, page

    offset = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        while offset <= max_offset:
            offsets = [
                candidate
                for candidate in range(offset, offset + limit * max(1, workers), limit)
                if candidate <= max_offset
            ]
            for page_offset, page in executor.map(fetch, offsets):
                result.extend(page)
                audits.append(PageAudit(path, None, None, page_offset, len(page)))
                if len(page) < limit:
                    if progress_callback is not None:
                        progress_callback(path, len(audits), len(result))
                    return result, audits, True
            if progress_callback is not None:
                progress_callback(path, len(audits), len(result))
            offset = offsets[-1] + limit
    return result, audits, False


def _position_identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("conditionId") or "").lower(),
        str(row.get("asset") or ""),
        str(row.get("outcomeIndex") if row.get("outcomeIndex") is not None else ""),
    )


def deduplicate_positions(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        unique[_position_identity(row)] = dict(row)
    return list(unique.values())


def fetch_all_positions(
    client: PublicDataClient,
    wallet: str,
    *,
    limit: int = 500,
    max_offset: int = 10_000,
    workers: int = 8,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], list[PageAudit], bool]:
    """Join both ends of the TOKENS ordering to work around the position offset cap."""
    common = {"user": wallet, "sizeThreshold": 0, "sortBy": "TOKENS"}
    descending, desc_audit, desc_complete = fetch_offset_pages(
        client,
        "/positions",
        {**common, "sortDirection": "DESC"},
        wallet=wallet,
        limit=limit,
        max_offset=max_offset,
        workers=workers,
        progress_callback=progress_callback,
    )
    if desc_complete:
        return descending, desc_audit, True
    ascending, asc_audit, asc_complete = fetch_offset_pages(
        client,
        "/positions",
        {**common, "sortDirection": "ASC"},
        wallet=wallet,
        limit=limit,
        max_offset=max_offset,
        workers=workers,
        progress_callback=progress_callback,
    )
    descending_keys = {_position_identity(row) for row in descending}
    ascending_keys = {_position_identity(row) for row in ascending}
    merged = {_position_identity(row): dict(row) for row in descending}
    merged.update({_position_identity(row): dict(row) for row in ascending})
    audits = desc_audit + asc_audit
    complete = asc_complete or bool(descending_keys & ascending_keys)
    if complete or not ascending:
        return list(merged.values()), audits, complete

    threshold = max(number(row.get("size")) for row in ascending)
    for _ in range(20):
        middle, middle_audit, middle_complete = fetch_offset_pages(
            client,
            "/positions",
            {
                "user": wallet,
                "sizeThreshold": threshold,
                "sortBy": "TOKENS",
                "sortDirection": "ASC",
            },
            wallet=wallet,
            limit=limit,
            max_offset=max_offset,
            workers=workers,
            progress_callback=progress_callback,
        )
        audits.extend(middle_audit)
        middle_keys = {_position_identity(row) for row in middle}
        merged.update({_position_identity(row): dict(row) for row in middle})
        if middle_complete or bool(middle_keys & descending_keys):
            return list(merged.values()), audits, True
        next_threshold = max((number(row.get("size")) for row in middle), default=threshold)
        if next_threshold <= threshold:
            return list(merged.values()), audits, False
        threshold = next_threshold
    return list(merged.values()), audits, False


def find_activity_bounds(client: PublicDataClient, wallet: str, end: int) -> tuple[int | None, int | None]:
    common = {"user": wallet, "limit": 1, "sortBy": "TIMESTAMP", "end": end}
    first = _validate_api_rows(
        client.get_json(build_url(DATA_API, "/activity", {**common, "sortDirection": "ASC"})),
        wallet,
        "/activity",
    )
    last = _validate_api_rows(
        client.get_json(build_url(DATA_API, "/activity", {**common, "sortDirection": "DESC"})),
        wallet,
        "/activity",
    )
    if not first:
        return None, None
    if not last:
        raise PolyLedgerError("Los límites de activity son inconsistentes")
    return int(first[0]["timestamp"]), int(last[0]["timestamp"])


def fetch_activity_window(
    client: PublicDataClient,
    wallet: str,
    start: int,
    end: int,
    *,
    limit: int = 500,
    max_offset: int = 5_000,
) -> tuple[list[dict[str, Any]], list[PageAudit]]:
    """Fetch an inclusive time window and split it before the API offset cap."""
    if end < start:
        return [], []
    rows: list[dict[str, Any]] = []
    audits: list[PageAudit] = []
    offset = 0
    while True:
        params = {
            "user": wallet,
            "start": start,
            "end": end,
            "sortBy": "TIMESTAMP",
            "sortDirection": "ASC",
            "limit": limit,
            "offset": offset,
        }
        page = _validate_api_rows(
            client.get_json(build_url(DATA_API, "/activity", params)), wallet, "/activity"
        )
        for row in page:
            timestamp = int(number(row.get("timestamp"), -1))
            if not start <= timestamp <= end:
                raise PolyLedgerError("activity devolvió una fila fuera de la ventana")
        audits.append(PageAudit("/activity", start, end, offset, len(page)))
        rows.extend(page)
        if len(page) < limit:
            return rows, audits
        if offset == max_offset:
            if start == end:
                raise PolyLedgerError(
                    f"Más de {max_offset + limit} eventos comparten el segundo {start}"
                )
            middle = start + (end - start) // 2
            left, left_audit = fetch_activity_window(
                client, wallet, start, middle, limit=limit, max_offset=max_offset
            )
            right, right_audit = fetch_activity_window(
                client, wallet, middle + 1, end, limit=limit, max_offset=max_offset
            )
            return left + right, left_audit + right_audit
        offset += limit


def fetch_activity_range(
    client: PublicDataClient,
    wallet: str,
    start: int,
    end: int,
    *,
    window_seconds: int = 86_400,
    workers: int = 8,
    batch_callback: Callable[[int, int, Sequence[Mapping[str, Any]]], None] | None = None,
    collect_rows: bool = True,
) -> tuple[list[dict[str, Any]], list[PageAudit]]:
    """Capture a large range concurrently while preserving complete pagination."""
    if end < start:
        return [], []
    windows = [
        (window_start, min(end, window_start + window_seconds - 1))
        for window_start in range(start, end + 1, window_seconds)
    ]

    def fetch(bounds: tuple[int, int]) -> tuple[list[dict[str, Any]], list[PageAudit]]:
        return fetch_activity_window(client, wallet, bounds[0], bounds[1])

    rows: list[dict[str, Any]] = []
    audits: list[PageAudit] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(windows)))) as executor:
        for index, (window_rows, window_audits) in enumerate(executor.map(fetch, windows), 1):
            unique_window_rows = deduplicate_rows(window_rows)
            if batch_callback is not None:
                batch_callback(index, len(windows), unique_window_rows)
            if collect_rows:
                rows.extend(unique_window_rows)
            audits.extend(window_audits)
    return deduplicate_rows(rows), audits


def deduplicate_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique[event_identity(row)] = dict(row)
    return sorted(unique.values(), key=lambda row: (int(number(row.get("timestamp"))), event_identity(row)))


def fifo_trade_analysis(
    events: Iterable[Mapping[str, Any]],
    *,
    presorted: bool = False,
    has_internal_events: bool | None = None,
) -> dict[str, Any]:
    lots: dict[str, deque[list[float]]] = defaultdict(deque)
    bought_cost = sold_proceeds = realized = matched = unmatched = 0.0
    ordered_events: Iterable[Mapping[str, Any]] = (
        events
        if presorted
        else sorted(events, key=lambda row: (int(row["timestamp"]), str(row["event_id"])))
    )
    observed_internal = False
    for event in ordered_events:
        observed_internal = observed_internal or event["event_type"] in INTERNAL_TYPES
        if event["event_type"] != "TRADE" or not event.get("asset"):
            continue
        asset = str(event["asset"])
        size = abs(number(event["size"]))
        price = number(event["price"])
        if event.get("side") == "BUY":
            lots[asset].append([size, price])
            bought_cost += size * price
        elif event.get("side") == "SELL":
            sold_proceeds += size * price
            remaining = size
            while remaining > 1e-12 and lots[asset]:
                lot = lots[asset][0]
                quantity = min(remaining, lot[0])
                realized += quantity * (price - lot[1])
                matched += quantity
                lot[0] -= quantity
                remaining -= quantity
                if lot[0] <= 1e-12:
                    lots[asset].popleft()
            unmatched += remaining
    remaining_shares = sum(lot[0] for queue in lots.values() for lot in queue)
    remaining_cost = sum(lot[0] * lot[1] for queue in lots.values() for lot in queue)
    return {
        "scope": "TRADE_BUY_SELL_ONLY_FIFO",
        "matched_sell_shares": matched,
        "unmatched_sell_shares": unmatched,
        "matched_realized_pnl_usd": realized,
        "gross_buy_cost_usd": bought_cost,
        "gross_sell_proceeds_usd": sold_proceeds,
        "remaining_matched_lot_shares": remaining_shares,
        "remaining_matched_lot_cost_usd": remaining_cost,
        "is_complete_pnl": unmatched <= 1e-9 and not (
            observed_internal if has_internal_events is None else has_internal_events
        ),
        "warning": (
            "No equivale al PnL total: splits, merges, conversiones, redenciones y "
            "transferencias de outcome tokens pueden romper el costo FIFO observable."
        ),
    }


def _source_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "rows": len(rows),
        "sha256": hashlib.sha256(stable_json(list(rows)).encode("utf-8")).hexdigest(),
    }


def _sum(rows: Iterable[Mapping[str, Any]], field: str) -> float:
    return sum(number(row.get(field)) for row in rows)


def summarize_closed_performance(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    row_pnls = [number(row.get("realizedPnl")) for row in rows]
    grouped: dict[str, dict[str, float]] = defaultdict(lambda: {"pnl": 0.0, "bought": 0.0})
    for row in rows:
        key = str(row.get("eventSlug") or row.get("conditionId") or row.get("asset") or "UNKNOWN")
        grouped[key]["pnl"] += number(row.get("realizedPnl"))
        grouped[key]["bought"] += number(row.get("totalBought"))
    event_pnls = [item["pnl"] for item in grouped.values()]

    def outcome_counts(values: Sequence[float]) -> tuple[int, int, int]:
        wins = sum(value > 1e-9 for value in values)
        losses = sum(value < -1e-9 for value in values)
        return wins, losses, len(values) - wins - losses

    row_wins, row_losses, row_flat = outcome_counts(row_pnls)
    event_wins, event_losses, event_flat = outcome_counts(event_pnls)
    positive_bought = sorted(
        item["bought"] for item in grouped.values() if item["pnl"] > 1e-9 and item["bought"] > 0
    )
    median_winning_bought = (
        None
        if not positive_bought
        else (
            positive_bought[len(positive_bought) // 2]
            if len(positive_bought) % 2
            else (
                positive_bought[len(positive_bought) // 2 - 1]
                + positive_bought[len(positive_bought) // 2]
            )
            / 2
        )
    )
    return {
        "closed_rows": len(rows),
        "row_profit_count": row_wins,
        "row_loss_count": row_losses,
        "row_flat_count": row_flat,
        "row_profit_fraction": row_wins / len(rows) if rows else None,
        "grouping": "eventSlug; fallback conditionId/asset",
        "grouped_events": len(event_pnls),
        "profitable_events": event_wins,
        "losing_events": event_losses,
        "flat_events": event_flat,
        "profitable_event_fraction": event_wins / len(event_pnls) if event_pnls else None,
        "gross_positive_realized_pnl_usd": sum(value for value in event_pnls if value > 0),
        "gross_negative_realized_pnl_usd": sum(value for value in event_pnls if value < 0),
        "net_closed_row_realized_pnl_usd": sum(row_pnls),
        "median_total_bought_on_profitable_event_usd": median_winning_bought,
        "warning": (
            "La fracción rentable no es porcentaje de acierto: conversiones negative-risk, cierres "
            "parciales y múltiples outcomes pueden crear varias filas por una sola decisión."
        ),
    }


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    fields = sorted({str(key) for row in rows for key in row})
    with partial.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: stable_json(value) if isinstance(value, (dict, list)) else value
                    for key, value in row.items()
                }
            )
    partial.replace(path)


def analyze_ledger(
    *,
    wallet: str,
    events: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    closed_positions: Sequence[Mapping[str, Any]],
    value_rows: Sequence[Mapping[str, Any]],
    leaderboard_rows: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any] | None,
    sync: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None = None,
    ledger_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    activity_source_evidence: dict[str, Any]
    if ledger_override is None:
        event_counts = Counter(str(row["event_type"]) for row in events)
        cash_by_type: dict[str, float] = defaultdict(float)
        for event in events:
            if event.get("cash_delta_usd") is not None:
                cash_by_type[str(event["event_type"])] += number(event["cash_delta_usd"])

        reward_by_type = {
            kind: sum(number(row["usdc_size"]) for row in events if row["event_type"] == kind)
            for kind in sorted(REWARD_TYPES)
            if event_counts.get(kind)
        }
        rewards_total = sum(reward_by_type.values())
        internal_notional = sum(
            number(row["usdc_size"]) for row in events if row["event_type"] in INTERNAL_TYPES
        )
        external_net = sum(
            number(row.get("cash_delta_usd"))
            for row in events
            if row["event_type"] in EXTERNAL_TYPES
        )
        observable_cash = sum(
            number(row.get("cash_delta_usd"))
            for row in events
            if row.get("cash_delta_usd") is not None
        )
        fifo = fifo_trade_analysis(events)
        first_ts = min((int(row["timestamp"]) for row in events), default=None)
        last_ts = max((int(row["timestamp"]) for row in events), default=None)
        ledger_metrics = {
            "events": len(events),
            "event_counts": dict(sorted(event_counts.items())),
            "first_event_timestamp": first_ts,
            "last_event_timestamp": last_ts,
            "observable_pusd_cash_movement_usd": observable_cash,
            "cash_movement_by_type_usd": dict(sorted(cash_by_type.items())),
            "explicit_rewards_usd": rewards_total,
            "rewards_by_type_usd": reward_by_type,
            "internal_conversion_notional_usd": internal_notional,
            "external_net_funding_usd": external_net,
            "fifo_trade_only": fifo,
        }
        activity_source_evidence = _source_summary([row["raw"] for row in events])
    else:
        activity_source_evidence = dict(ledger_override.get("_source_evidence") or {})
        ledger_metrics = {
            key: value for key, value in ledger_override.items() if not str(key).startswith("_")
        }
        fifo = ledger_metrics["fifo_trade_only"]

    open_value = _sum(positions, "currentValue")
    open_cash_pnl = _sum(positions, "cashPnl")
    open_realized = _sum(positions, "realizedPnl")
    closed_realized = _sum(closed_positions, "realizedPnl")
    position_pnl = open_cash_pnl + open_realized + closed_realized
    value_api = _sum(value_rows, "value") if value_rows else None
    leaderboard = leaderboard_rows[0] if leaderboard_rows else None
    leaderboard_pnl = number(leaderboard.get("pnl")) if leaderboard else None
    leaderboard_volume = number(leaderboard.get("vol")) if leaderboard else None
    tolerance_value = max(0.01, abs(open_value) * 0.001)
    discrepancies: list[dict[str, Any]] = []

    if value_api is None:
        discrepancies.append(
            {
                "code": "VALUE_SOURCE_MISSING",
                "severity": "MEDIUM",
                "difference_usd": None,
                "explanation": "El endpoint /value no devolvió una cifra comparable.",
            }
        )
    elif abs(open_value - value_api) > tolerance_value:
        discrepancies.append(
            {
                "code": "OPEN_VALUE_MISMATCH",
                "severity": "HIGH",
                "difference_usd": open_value - value_api,
                "explanation": (
                    "La suma de currentValue no coincide con /value por encima de la tolerancia; "
                    "puede ser desfase temporal, umbral o cobertura de posiciones."
                ),
            }
        )

    if leaderboard_pnl is None:
        discrepancies.append(
            {
                "code": "LEADERBOARD_PNL_MISSING",
                "severity": "LOW",
                "difference_usd": None,
                "explanation": "La wallet no tiene una fila ALL/OVERALL disponible en leaderboard.",
            }
        )
    else:
        difference = position_pnl - leaderboard_pnl
        tolerance_pnl = max(1.0, abs(leaderboard_pnl) * 0.01)
        if abs(difference) > tolerance_pnl:
            discrepancies.append(
                {
                    "code": "PNL_DEFINITION_OR_TIMING_MISMATCH",
                    "severity": "MEDIUM",
                    "difference_usd": difference,
                    "explanation": (
                        "El total de snapshots de posiciones difiere del leaderboard. No se suma ni "
                        "corrige automáticamente porque las definiciones y tiempos pueden diferir."
                    ),
                }
            )

    if not bool(sync.get("positions_complete", True)):
        discrepancies.append(
            {
                "code": "POSITIONS_API_CAPPED",
                "severity": "HIGH",
                "difference_usd": None,
                "explanation": (
                    "Los segmentos TOKENS ASC/DESC y por umbral no lograron cubrir todo el rango; "
                    "las sumas de posiciones abiertas son parciales por el límite público de offset."
                ),
            }
        )
    if not bool(sync.get("closed_positions_complete", True)):
        discrepancies.append(
            {
                "code": "CLOSED_POSITIONS_API_CAPPED",
                "severity": "HIGH",
                "difference_usd": None,
                "explanation": "Las posiciones cerradas alcanzaron el máximo público de paginación.",
            }
        )
    if snapshot and not snapshot.get("captured"):
        discrepancies.append(
            {
                "code": "ACCOUNTING_SNAPSHOT_UNAVAILABLE",
                "severity": "LOW",
                "difference_usd": None,
                "explanation": str(snapshot.get("error") or "El ZIP contable oficial no estuvo disponible."),
            }
        )

    if fifo["unmatched_sell_shares"] > 1e-9:
        discrepancies.append(
            {
                "code": "FIFO_UNMATCHED_SELLS",
                "severity": "MEDIUM",
                "difference_usd": None,
                "explanation": (
                    "Hay ventas sin una compra pública previa del mismo asset; normalmente provienen "
                    "de splits, transferencias o historial anterior al corte."
                ),
            }
        )

    top_gains = sorted(
        closed_positions, key=lambda row: number(row.get("realizedPnl")), reverse=True
    )[:10]
    top_losses = sorted(closed_positions, key=lambda row: number(row.get("realizedPnl")))[:10]

    return {
        "schema": SCHEMA,
        "generated_at_utc": now_utc(),
        "wallet": wallet,
        "profile": dict(profile or {}),
        "status": "REVIEW_REQUIRED" if discrepancies else "CONSISTENT_WITHIN_TESTED_TOLERANCES",
        "official_metrics": {
            "open_positions": len(positions),
            "open_positions_complete": bool(sync.get("positions_complete", True)),
            "closed_positions": len(closed_positions),
            "closed_positions_complete": bool(sync.get("closed_positions_complete", True)),
            "open_current_value_usd": open_value,
            "value_endpoint_usd": value_api,
            "open_unrealized_cash_pnl_usd": open_cash_pnl,
            "open_realized_pnl_usd": open_realized,
            "closed_realized_pnl_usd": closed_realized,
            "position_snapshot_pnl_sum_usd": position_pnl,
            "leaderboard_all_overall_pnl_usd": leaderboard_pnl,
            "leaderboard_all_overall_volume_usd": leaderboard_volume,
        },
        "ledger_metrics": ledger_metrics,
        "closed_performance": summarize_closed_performance(closed_positions),
        "top_closed_gains": [dict(row) for row in top_gains],
        "top_closed_losses": [dict(row) for row in top_losses],
        "discrepancies": discrepancies,
        "source_evidence": {
            "activity": activity_source_evidence,
            "positions": _source_summary(positions),
            "closed_positions": _source_summary(closed_positions),
            "value": _source_summary(value_rows),
            "leaderboard": _source_summary(leaderboard_rows),
            "accounting_snapshot": dict(snapshot or {}),
            "sync": dict(sync),
        },
        "interpretation_rules": {
            "trade": "BUY/SELL mueve pUSD; el PnL requiere costo de adquisición.",
            "split_merge_conversion": "Movimientos internos, nunca ganancias o pérdidas por sí solos.",
            "redeem": "Cobro bruto; no se trata como beneficio sin costo base.",
            "rewards": "Ingresos explícitos separados del trading.",
            "transfers": "Depósitos/retiros son capital externo y se separan del PnL.",
        },
        "limitations": [
            "El API público no expone saldo histórico completo de pUSD ni todas las transferencias de outcome tokens.",
            "observable_pusd_cash_movement_usd es flujo visible, no PnL.",
            "El FIFO es diagnóstico y solo usa TRADE BUY/SELL del mismo asset; no reemplaza el PnL oficial.",
            "Leaderboard y posiciones pueden usar ventanas, umbrales y horas de actualización distintas.",
            "El sistema es exclusivamente de lectura: no firma, no opera y no solicita claves privadas.",
        ],
        "safety": {
            "read_only": True,
            "trading_enabled": False,
            "private_keys_used": False,
            "automatic_copying_enabled": False,
        },
    }


def _format_money(value: Any) -> str:
    return "N/D" if value is None else f"US${number(value):,.2f}"


def render_markdown(report: Mapping[str, Any]) -> str:
    official = report["official_metrics"]
    ledger = report["ledger_metrics"]
    lines = [
        "# PolyLedger Sentinel — informe de auditoría",
        "",
        f"- Wallet: `{report['wallet']}`",
        f"- Generado (UTC): {report['generated_at_utc']}",
        f"- Estado: **{report['status']}**",
        "- Modo: **solo lectura; no realiza operaciones**",
        "",
        "## Cifras oficiales separadas",
        "",
        "| Métrica | Resultado |",
        "|---|---:|",
        f"| Valor de posiciones abiertas (suma) | {_format_money(official['open_current_value_usd'])} |",
        f"| Valor según `/value` | {_format_money(official['value_endpoint_usd'])} |",
        f"| PnL no realizado abierto | {_format_money(official['open_unrealized_cash_pnl_usd'])} |",
        f"| PnL realizado dentro de posiciones abiertas | {_format_money(official['open_realized_pnl_usd'])} |",
        f"| PnL realizado en posiciones cerradas | {_format_money(official['closed_realized_pnl_usd'])} |",
        f"| Suma de componentes de posiciones | {_format_money(official['position_snapshot_pnl_sum_usd'])} |",
        f"| PnL leaderboard ALL/OVERALL | {_format_money(official['leaderboard_all_overall_pnl_usd'])} |",
        f"| Volumen leaderboard ALL/OVERALL | {_format_money(official['leaderboard_all_overall_volume_usd'])} |",
        "",
        "La suma de componentes y el leaderboard se muestran por separado: **no se fuerzan a coincidir**.",
        f"Cobertura de posiciones abiertas: **{'completa' if official.get('open_positions_complete') else 'parcial'}**. "
        f"Cobertura de posiciones cerradas: **{'completa' if official.get('closed_positions_complete') else 'parcial'}**.",
        "",
        "## Ledger de actividad",
        "",
        f"- Eventos únicos conservados: **{ledger['events']:,}**",
        f"- Movimiento pUSD observable: **{_format_money(ledger['observable_pusd_cash_movement_usd'])}** (flujo, no PnL)",
        f"- Recompensas/rebates explícitos: **{_format_money(ledger['explicit_rewards_usd'])}**",
        f"- Notional de conversiones internas: **{_format_money(ledger['internal_conversion_notional_usd'])}**",
        f"- PnL FIFO solo BUY/SELL emparejado: **{_format_money(ledger['fifo_trade_only']['matched_realized_pnl_usd'])}**",
        "",
        "### Conteo por tipo",
        "",
        "| Tipo | Eventos | Movimiento pUSD observable |",
        "|---|---:|---:|",
    ]
    for kind, count in ledger["event_counts"].items():
        lines.append(
            f"| {kind} | {count:,} | {_format_money(ledger['cash_movement_by_type_usd'].get(kind))} |"
        )

    lines.extend(["", "## Bandeja de discrepancias", ""])
    if report["discrepancies"]:
        for item in report["discrepancies"]:
            difference = (
                "" if item.get("difference_usd") is None else f" Diferencia: {_format_money(item['difference_usd'])}."
            )
            lines.append(
                f"- **{item['severity']} · {item['code']}** — {item['explanation']}{difference}"
            )
    else:
        lines.append("- No se detectaron diferencias por encima de las tolerancias probadas.")

    closed_summary = report["closed_performance"]
    lines.extend(
        [
            "",
            "## Resultado de cierres (diagnóstico)",
            "",
            f"- Eventos agrupados: **{closed_summary['grouped_events']:,}**",
            f"- Eventos con resultado positivo: **{closed_summary['profitable_events']:,}**",
            f"- Eventos con resultado negativo: **{closed_summary['losing_events']:,}**",
            f"- Fracción de eventos positivos: **{closed_summary['profitable_event_fraction']:.2%}**"
            if closed_summary["profitable_event_fraction"] is not None
            else "- Fracción de eventos positivos: **N/D**",
            f"- Compra total mediana en eventos positivos: **{_format_money(closed_summary['median_total_bought_on_profitable_event_usd'])}**",
            "",
            f"> {closed_summary['warning']}",
        ]
    )

    def append_positions(title: str, rows: Sequence[Mapping[str, Any]]) -> None:
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| Mercado | Outcome | PnL realizado | Total comprado |",
                "|---|---|---:|---:|",
            ]
        )
        for row in rows:
            market = str(row.get("title") or row.get("slug") or "N/D").replace("|", "\\|")
            outcome = str(row.get("outcome") or "N/D").replace("|", "\\|")
            lines.append(
                f"| {market} | {outcome} | {_format_money(row.get('realizedPnl'))} | {_format_money(row.get('totalBought'))} |"
            )
        if not rows:
            lines.append("| Sin datos | — | — | — |")

    append_positions("Mayores ganancias cerradas", report["top_closed_gains"])
    append_positions("Mayores pérdidas cerradas", report["top_closed_losses"])
    lines.extend(
        [
            "",
            "## Cómo interpretar el resultado",
            "",
            "- `SPLIT`, `MERGE` y `CONVERSION` son movimientos internos; no se presentan como rentabilidad.",
            "- `REDEEM` es cobro bruto. Sin costo base no se llama ganancia.",
            "- Rewards y rebates se presentan aparte del resultado de trading.",
            "- El FIFO mostrado es parcial si existen ventas creadas por splits, transferencias o historia no visible.",
            "",
            "## Límites",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in report["limitations"])
    artifacts = report.get("artifacts") or {}
    if artifacts:
        lines.extend(["", "## Archivos exportados", ""])
        lines.extend(f"- {name}: `{path}`" for name, path in artifacts.items())
    return "\n".join(lines) + "\n"


def write_report_artifacts(
    report: dict[str, Any],
    *,
    positions: Sequence[Mapping[str, Any]],
    closed_positions: Sequence[Mapping[str, Any]],
    output_json: str | Path,
    output_markdown: str | Path,
) -> dict[str, Any]:
    output_json = Path(output_json)
    output_markdown = Path(output_markdown)
    open_csv = output_json.with_name(f"{output_json.stem}_open_positions.csv")
    closed_csv = output_json.with_name(f"{output_json.stem}_closed_positions.csv")
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_markdown.parent.mkdir(parents=True, exist_ok=True)
    write_csv(open_csv, positions)
    write_csv(closed_csv, closed_positions)
    report["artifacts"] = {
        "ledger_sqlite": str(Path(report["source_evidence"]["sync"]["database"]).resolve()),
        "open_positions_csv": str(open_csv.resolve()),
        "closed_positions_csv": str(closed_csv.resolve()),
        "report_json": str(output_json.resolve()),
        "report_markdown": str(output_markdown.resolve()),
    }
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_markdown.write_text(render_markdown(report), encoding="utf-8")
    return report


def accounting_snapshot_summary(payload: bytes, output_zip: Path | None = None) -> dict[str, Any]:
    digest = hashlib.sha256(payload).hexdigest()
    if output_zip is not None:
        output_zip.parent.mkdir(parents=True, exist_ok=True)
        output_zip.write_bytes(payload)
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise PolyLedgerError("El accounting snapshot no es un ZIP válido") from exc
    files: list[dict[str, Any]] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = Path(info.filename).name
        if name not in {"positions.csv", "equity.csv"}:
            raise PolyLedgerError(f"Archivo inesperado en accounting snapshot: {info.filename}")
        raw = archive.read(info)
        try:
            rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        except UnicodeDecodeError as exc:
            raise PolyLedgerError(f"CSV inválido en accounting snapshot: {name}") from exc
        files.append(
            {
                "name": name,
                "rows": len(rows),
                "columns": list(rows[0].keys()) if rows else [],
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return {"captured": True, "zip_sha256": digest, "files": files}


def run_sentinel(
    *,
    wallet: str,
    database: str | Path,
    output_json: str | Path,
    output_markdown: str | Path,
    client: PublicDataClient | None = None,
    include_accounting_snapshot: bool = True,
) -> dict[str, Any]:
    wallet = validate_wallet(wallet)
    client = client or PublicDataClient()
    store = LedgerStore(database)
    latest = store.latest_timestamp(wallet)
    requested_start = latest if latest is not None else None
    run_id = store.start_run(wallet, requested_start)
    fetched = inserted = 0
    try:
        end = int(datetime.now(timezone.utc).timestamp())

        def persist_activity_batch(
            completed: int, total: int, rows: Sequence[Mapping[str, Any]]
        ) -> None:
            nonlocal fetched, inserted
            batch_fetched, batch_inserted = store.upsert_events(rows)
            fetched += batch_fetched
            inserted += batch_inserted
            if completed == 1 or completed % 10 == 0 or completed == total:
                print(
                    stable_json(
                        {
                            "status": "ACTIVITY_PROGRESS",
                            "windows": f"{completed}/{total}",
                            "events_seen": fetched,
                            "events_new": inserted,
                        }
                    ),
                    flush=True,
                )

        if requested_start is None:
            first, last = find_activity_bounds(client, wallet, end)
            if first is None or last is None:
                activity_rows: list[dict[str, Any]] = []
                activity_audit: list[PageAudit] = []
            else:
                activity_rows, activity_audit = fetch_activity_range(
                    client,
                    wallet,
                    first,
                    last,
                    batch_callback=persist_activity_batch,
                    collect_rows=False,
                )
        else:
            activity_rows, activity_audit = fetch_activity_range(
                client,
                wallet,
                requested_start,
                end,
                batch_callback=persist_activity_batch,
                collect_rows=False,
            )

        def endpoint_progress(endpoint: str, pages: int, rows: int) -> None:
            if pages > 8 and pages % 80 != 0:
                return
            print(
                stable_json(
                    {
                        "status": "ENDPOINT_PROGRESS",
                        "endpoint": endpoint,
                        "pages": pages,
                        "rows": rows,
                    }
                ),
                flush=True,
            )

        print(stable_json({"status": "PHASE", "name": "POSITIONS"}), flush=True)

        positions, positions_audit, positions_complete = fetch_all_positions(
            client,
            wallet,
            progress_callback=endpoint_progress,
        )
        print(
            stable_json(
                {
                    "status": "ENDPOINT_COMPLETE",
                    "endpoint": "/positions",
                    "rows": len(positions),
                    "complete": positions_complete,
                }
            ),
            flush=True,
        )
        print(stable_json({"status": "PHASE", "name": "CLOSED_POSITIONS"}), flush=True)
        closed, closed_audit, closed_complete = fetch_offset_pages(
            client,
            "/closed-positions",
            {"user": wallet, "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
            wallet=wallet,
            limit=50,
            max_offset=100_000,
            progress_callback=endpoint_progress,
        )
        closed = deduplicate_positions(closed)
        print(
            stable_json(
                {
                    "status": "ENDPOINT_COMPLETE",
                    "endpoint": "/closed-positions",
                    "rows": len(closed),
                    "complete": closed_complete,
                }
            ),
            flush=True,
        )
        print(stable_json({"status": "PHASE", "name": "OFFICIAL_TOTALS"}), flush=True)
        value_rows = _validate_api_rows(
            client.get_json(build_url(DATA_API, "/value", {"user": wallet})), wallet, "/value"
        )
        leaderboard_rows = _validate_api_rows(
            client.get_json(
                build_url(
                    DATA_API,
                    "/v1/leaderboard",
                    {"user": wallet, "category": "OVERALL", "timePeriod": "ALL", "limit": 1},
                )
            ),
            wallet,
            "/v1/leaderboard",
        )
        profile_payload = client.get_json(
            build_url(GAMMA_API, "/public-profile", {"address": wallet})
        )
        profile = profile_payload if isinstance(profile_payload, dict) else {}
        snapshot: dict[str, Any] = {"captured": False}
        store.save_snapshot(wallet, "positions", positions)
        store.save_snapshot(wallet, "closed_positions", closed)
        store.save_snapshot(wallet, "value", value_rows)
        store.save_snapshot(wallet, "leaderboard_all_overall", leaderboard_rows)
        store.save_snapshot(wallet, "public_profile", profile)
        if include_accounting_snapshot:
            print(stable_json({"status": "PHASE", "name": "ACCOUNTING_SNAPSHOT"}), flush=True)
            try:
                raw_zip = client.get_bytes(
                    build_url(DATA_API, "/v1/accounting/snapshot", {"user": wallet}),
                    accept="application/zip",
                    attempts=1,
                )
                snapshot_path = Path(output_json).with_name("accounting_snapshot.zip")
                snapshot = accounting_snapshot_summary(raw_zip, snapshot_path)
                snapshot["path"] = str(snapshot_path.resolve())
            except PolyLedgerError as exc:
                snapshot = {"captured": False, "error": f"Fuente oficial respondió con error: {exc}"}
        store.save_snapshot(wallet, "accounting_snapshot_summary", snapshot)
        print(stable_json({"status": "PHASE", "name": "STREAMING_ANALYSIS"}), flush=True)
        sync = {
            "run_id": run_id,
            "mode": "INCREMENTAL" if requested_start is not None else "FULL_HISTORY",
            "database": str(Path(database).resolve()),
            "requested_start": requested_start,
            "fetched_rows": fetched,
            "inserted_rows": inserted,
            "activity_pages": [row.as_dict() for row in activity_audit],
            "positions_pages": [row.as_dict() for row in positions_audit],
            "closed_positions_pages": [row.as_dict() for row in closed_audit],
            "positions_complete": positions_complete,
            "closed_positions_complete": closed_complete,
            "api_requests": client.requests,
            "database_quick_check": store.integrity(),
        }
        ledger_metrics = store.analytics(wallet)
        print(stable_json({"status": "PHASE", "name": "REPORT"}), flush=True)
        report = analyze_ledger(
            wallet=wallet,
            events=(),
            positions=positions,
            closed_positions=closed,
            value_rows=value_rows,
            leaderboard_rows=leaderboard_rows,
            profile=profile,
            sync=sync,
            snapshot=snapshot,
            ledger_override=ledger_metrics,
        )
        write_report_artifacts(
            report,
            positions=positions,
            closed_positions=closed,
            output_json=output_json,
            output_markdown=output_markdown,
        )
        store.finish_run(run_id, "COMPLETE", fetched, inserted)
        return report
    except BaseException as exc:
        status = "INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "FAILED"
        store.finish_run(run_id, status, fetched, inserted, str(exc))
        raise
    finally:
        store.close()


def reanalyze_local(
    *,
    wallet: str,
    database: str | Path,
    output_json: str | Path,
    output_markdown: str | Path,
) -> dict[str, Any]:
    wallet = validate_wallet(wallet)
    store = LedgerStore(database)
    try:
        positions = deduplicate_positions(store.latest_snapshot(wallet, "positions"))
        closed = deduplicate_positions(store.latest_snapshot(wallet, "closed_positions"))
        value_rows = store.latest_snapshot(wallet, "value")
        leaderboard_rows = store.latest_snapshot(wallet, "leaderboard_all_overall")
        profile = store.latest_snapshot(wallet, "public_profile")
        try:
            snapshot = store.latest_snapshot(wallet, "accounting_snapshot_summary")
        except PolyLedgerError:
            snapshot = {"captured": False, "error": "No existe accounting snapshot local."}
        previous: dict[str, Any] = {}
        output_json_path = Path(output_json)
        if output_json_path.exists():
            try:
                previous = json.loads(output_json_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                previous = {}
        old_sync = ((previous.get("source_evidence") or {}).get("sync") or {})
        sync = {
            **old_sync,
            "mode": "LOCAL_REANALYSIS",
            "database": str(Path(database).resolve()),
            "database_quick_check": store.integrity(),
            "api_requests": 0,
        }
        report = analyze_ledger(
            wallet=wallet,
            events=(),
            positions=positions,
            closed_positions=closed,
            value_rows=value_rows,
            leaderboard_rows=leaderboard_rows,
            profile=profile,
            sync=sync,
            snapshot=snapshot,
            ledger_override=store.analytics(wallet),
        )
        return write_report_artifacts(
            report,
            positions=positions,
            closed_positions=closed,
            output_json=output_json,
            output_markdown=output_markdown,
        )
    finally:
        store.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polyledger-sentinel",
        description="Auditor público y de solo lectura para una wallet de Polymarket.",
    )
    parser.add_argument("--wallet", required=True, help="Proxy wallet pública 0x...")
    parser.add_argument("--database", default="data/polyledger/polyledger.db")
    parser.add_argument("--output-json", default="data/polyledger/ultimo_reporte.json")
    parser.add_argument("--output-markdown", default="data/polyledger/ultimo_reporte.md")
    parser.add_argument(
        "--sin-accounting-snapshot",
        action="store_true",
        help="No descargar el ZIP oficial positions.csv/equity.csv.",
    )
    parser.add_argument(
        "--solo-local",
        action="store_true",
        help="Regenerar CSV e informe desde los últimos snapshots, sin internet.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    runner = reanalyze_local if args.solo_local else run_sentinel
    kwargs = {
        "wallet": args.wallet,
        "database": args.database,
        "output_json": args.output_json,
        "output_markdown": args.output_markdown,
    }
    if args.solo_local:
        report = runner(**kwargs)
    else:
        report = runner(
            **kwargs,
            include_accounting_snapshot=not args.sin_accounting_snapshot,
        )
    official = report["official_metrics"]
    print(
        json.dumps(
            {
                "status": report["status"],
                "wallet": report["wallet"],
                "events": report["ledger_metrics"]["events"],
                "closed_positions": official["closed_positions"],
                "leaderboard_pnl_usd": official["leaderboard_all_overall_pnl_usd"],
                "discrepancies": len(report["discrepancies"]),
                "report": str(Path(args.output_markdown).resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()


__all__ = [
    "LedgerStore",
    "PolyLedgerError",
    "PublicDataClient",
    "accounting_snapshot_summary",
    "analyze_ledger",
    "deduplicate_rows",
    "event_identity",
    "fetch_activity_window",
    "fetch_activity_range",
    "fifo_trade_analysis",
    "main",
    "normalize_event",
    "render_markdown",
    "reanalyze_local",
    "run_sentinel",
    "validate_wallet",
]
