from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .polyledger import DATA_API, PublicDataClient, build_url, event_identity, number, validate_wallet


CLOB_API = "https://clob.polymarket.com"
DEFAULT_WALLET = "0x7c3db723f1d4d8cb9c550095203b686cb11e5c6b"
HORIZONS_SECONDS = (0, 1, 2, 5, 10, 15, 30, 60, 120, 300, 900, 3600, 21600, 86400)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS signals(
            event_id TEXT PRIMARY KEY,
            wallet TEXT NOT NULL,
            original_timestamp INTEGER NOT NULL,
            detected_at REAL NOT NULL,
            detection_latency_seconds REAL NOT NULL,
            transaction_hash TEXT,
            condition_id TEXT,
            token_id TEXT NOT NULL,
            event_slug TEXT,
            market TEXT,
            outcome TEXT,
            side TEXT NOT NULL,
            original_price REAL NOT NULL,
            original_shares REAL NOT NULL,
            original_notional_usd REAL NOT NULL,
            raw_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS signals_time ON signals(original_timestamp,event_id);
        CREATE TABLE IF NOT EXISTS observations(
            event_id TEXT NOT NULL,
            horizon_seconds INTEGER NOT NULL,
            observed_at REAL NOT NULL,
            actual_elapsed_seconds REAL NOT NULL,
            status TEXT NOT NULL,
            best_bid REAL,
            best_ask REAL,
            spread REAL,
            follower_vwap REAL,
            follower_filled_shares REAL,
            execution_rate REAL,
            book_timestamp TEXT,
            book_json TEXT,
            PRIMARY KEY(event_id,horizon_seconds),
            FOREIGN KEY(event_id) REFERENCES signals(event_id)
        );
        CREATE TABLE IF NOT EXISTS poll_runs(
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL,
            activity_rows INTEGER NOT NULL DEFAULT 0,
            inserted_signals INTEGER NOT NULL DEFAULT 0,
            observations INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        """
    )
    return connection


def _levels(book: Mapping[str, Any], side: str) -> list[tuple[float, float]]:
    levels = []
    for row in book.get(side) or []:
        if not isinstance(row, Mapping):
            continue
        price = number(row.get("price"), math.nan)
        size = number(row.get("size"), math.nan)
        if math.isfinite(price) and math.isfinite(size) and price >= 0 and size > 0:
            levels.append((price, size))
    return sorted(levels, key=lambda item: item[0], reverse=(side == "bids"))


def simulate_book_fill(book: Mapping[str, Any], side: str, requested_shares: float) -> dict[str, float | None]:
    """Walk the public book after detection; it never grants @car's original fill."""
    levels = _levels(book, "asks" if side.upper() == "BUY" else "bids")
    remaining = max(0.0, float(requested_shares))
    filled = cost = 0.0
    for price, available in levels:
        take = min(remaining, available)
        filled += take
        cost += take * price
        remaining -= take
        if remaining <= 1e-12:
            break
    return {
        "filled_shares": filled,
        "vwap": cost / filled if filled else None,
        "execution_rate": filled / requested_shares if requested_shares > 0 else None,
    }


def _book(client: PublicDataClient, token_id: str) -> dict[str, Any]:
    payload = client.get_json(CLOB_API + "/book?" + urllib.parse.urlencode({"token_id": token_id}))
    if not isinstance(payload, dict):
        raise RuntimeError("El CLOB devolvió un book no válido")
    return payload


def _activity(client: PublicDataClient, wallet: str, start: int, end: int) -> list[dict[str, Any]]:
    params = {
        "user": wallet,
        "start": start,
        "end": end,
        "type": "TRADE",
        "sortBy": "TIMESTAMP",
        "sortDirection": "ASC",
        "limit": 500,
        "offset": 0,
    }
    payload = client.get_json(build_url(DATA_API, "/activity", params))
    if not isinstance(payload, list):
        raise RuntimeError("La Data API devolvió actividad no válida")
    if len(payload) >= 500:
        raise RuntimeError("Más de 500 fills en la ventana: reduzca --lookback para evitar omisiones")
    return [dict(row) for row in payload if isinstance(row, Mapping) and str(row.get("type") or "").upper() == "TRADE"]


def _insert_signal(connection: sqlite3.Connection, wallet: str, row: Mapping[str, Any], detected_at: float) -> tuple[str, bool]:
    identity = event_identity(row)
    before = connection.total_changes
    connection.execute(
        """INSERT OR IGNORE INTO signals(
             event_id,wallet,original_timestamp,detected_at,detection_latency_seconds,
             transaction_hash,condition_id,token_id,event_slug,market,outcome,side,
             original_price,original_shares,original_notional_usd,raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            identity, wallet, int(number(row.get("timestamp"))), detected_at,
            max(0.0, detected_at - number(row.get("timestamp"))), row.get("transactionHash"),
            str(row.get("conditionId") or "").lower(), str(row.get("asset") or ""),
            row.get("eventSlug"), row.get("title"), row.get("outcome"), str(row.get("side") or "").upper(),
            number(row.get("price")), number(row.get("size")), number(row.get("usdcSize")),
            json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ),
    )
    return identity, connection.total_changes > before


def _save_observation(
    connection: sqlite3.Connection,
    signal: Mapping[str, Any],
    horizon: int,
    now: float,
    status: str,
    book: Mapping[str, Any] | None,
) -> bool:
    bids = _levels(book or {}, "bids")
    asks = _levels(book or {}, "asks")
    result = simulate_book_fill(book or {}, str(signal["side"]), number(signal["original_shares"])) if book else {
        "filled_shares": None, "vwap": None, "execution_rate": None
    }
    before = connection.total_changes
    connection.execute(
        """INSERT OR IGNORE INTO observations(
             event_id,horizon_seconds,observed_at,actual_elapsed_seconds,status,best_bid,
             best_ask,spread,follower_vwap,follower_filled_shares,execution_rate,
             book_timestamp,book_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            signal["event_id"], horizon, now, now - number(signal["original_timestamp"]), status,
            bids[0][0] if bids else None, asks[0][0] if asks else None,
            asks[0][0] - bids[0][0] if bids and asks else None, result["vwap"],
            result["filled_shares"], result["execution_rate"], (book or {}).get("timestamp"),
            json.dumps(book, sort_keys=True, separators=(",", ":")) if book else None,
        ),
    )
    return connection.total_changes > before


def poll_once(database: Path, wallet: str = DEFAULT_WALLET, *, lookback_seconds: int = 120, cadence_seconds: float = 2.0) -> dict[str, Any]:
    wallet = validate_wallet(wallet)
    client = PublicDataClient(timeout_seconds=30.0, retries=4, user_agent="car-shadow-tracker/0.1 read-only")
    connection = _connect(database)
    started = _now_iso()
    run_id = connection.execute(
        "INSERT INTO poll_runs(started_at,status) VALUES(?,?)", (started, "RUNNING")
    ).lastrowid
    connection.commit()
    inserted = observed = 0
    rows: list[dict[str, Any]] = []
    try:
        now = time.time()
        previous = connection.execute("SELECT MAX(original_timestamp) FROM signals").fetchone()[0]
        start = max(int(now) - max(1, lookback_seconds), int(previous or 0) - 5)
        rows = _activity(client, wallet, start, int(now) + 1)
        new_signals: list[str] = []
        with connection:
            for row in rows:
                identity, was_inserted = _insert_signal(connection, wallet, row, now)
                if was_inserted:
                    inserted += 1
                    new_signals.append(identity)

        due = connection.execute(
            """SELECT s.event_id,s.original_timestamp,s.token_id,s.side,s.original_shares
               FROM signals s
               WHERE s.original_timestamp>=?
               ORDER BY s.original_timestamp,s.event_id""",
            (int(now) - 86_500,),
        ).fetchall()
        books: dict[str, dict[str, Any] | Exception] = {}
        grace = max(3.0, cadence_seconds * 2.5)
        with connection:
            for event_id, original_timestamp, token_id, side, original_shares in due:
                signal = {
                    "event_id": event_id, "original_timestamp": original_timestamp, "token_id": token_id,
                    "side": side, "original_shares": original_shares,
                }
                existing = {
                    int(item[0])
                    for item in connection.execute(
                        "SELECT horizon_seconds FROM observations WHERE event_id=?", (event_id,)
                    )
                }
                for horizon in HORIZONS_SECONDS:
                    if horizon in existing or now < original_timestamp + horizon:
                        continue
                    lateness = now - (original_timestamp + horizon)
                    if horizon == 0 and lateness > grace:
                        observed += int(_save_observation(connection, signal, horizon, now, "STALE_SIGNAL_AT_DETECTION", None))
                        continue
                    if horizon != 0 and lateness > grace:
                        observed += int(_save_observation(connection, signal, horizon, now, "MISSED_LATE_START", None))
                        continue
                    if token_id not in books:
                        try:
                            books[token_id] = _book(client, token_id)
                        except Exception as exc:  # evidence is retained as an explicit failure
                            books[token_id] = exc
                    book_or_error = books[token_id]
                    if isinstance(book_or_error, Exception):
                        status = "BOOK_ERROR"
                        book_payload = None
                    else:
                        status = "OBSERVED_EXECUTABLE_BOOK"
                        book_payload = book_or_error
                    observed += int(_save_observation(connection, signal, horizon, now, status, book_payload))
        with connection:
            connection.execute(
                """UPDATE poll_runs SET ended_at=?,status='COMPLETE',activity_rows=?,
                          inserted_signals=?,observations=? WHERE run_id=?""",
                (_now_iso(), len(rows), inserted, observed, run_id),
            )
        result = {
            "status": "COMPLETE", "wallet": wallet, "activity_rows": len(rows),
            "new_signals": inserted, "new_observations": observed, "database": str(database.resolve()),
            "safety": "read_only_no_orders_no_keys",
        }
    except Exception as exc:
        with connection:
            connection.execute(
                "UPDATE poll_runs SET ended_at=?,status='ERROR',error=? WHERE run_id=?",
                (_now_iso(), str(exc), run_id),
            )
        raise
    finally:
        connection.close()
    print(json.dumps(result, indent=2))
    return result


def run_loop(database: Path, wallet: str, lookback_seconds: int, cadence_seconds: float) -> None:
    while True:
        try:
            poll_once(database, wallet, lookback_seconds=lookback_seconds, cadence_seconds=cadence_seconds)
        except Exception as exc:
            print(json.dumps({"status": "ERROR", "error": str(exc), "retry_in_seconds": cadence_seconds}), flush=True)
        time.sleep(max(0.5, cadence_seconds))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Shadow read-only de fills públicos de @car y book posterior")
    parser.add_argument("--wallet", default=DEFAULT_WALLET)
    parser.add_argument("--database", default="data/car_forensics/car_shadow.db")
    parser.add_argument("--lookback", type=int, default=120)
    parser.add_argument("--cadence", type=float, default=2.0)
    parser.add_argument("--loop", action="store_true", help="Repite hasta Ctrl+C; sin esta opción ejecuta un solo ciclo")
    args = parser.parse_args(argv)
    if args.loop:
        run_loop(Path(args.database), args.wallet, args.lookback, args.cadence)
    else:
        poll_once(Path(args.database), args.wallet, lookback_seconds=args.lookback, cadence_seconds=args.cadence)


if __name__ == "__main__":
    main()
