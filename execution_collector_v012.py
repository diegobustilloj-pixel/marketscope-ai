from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE_DB = ROOT / "data" / "shadow_forward_twap_transfer_v094a.db"
DEFAULT_OUT_DB = ROOT / "data" / "execution_forward_v012.db"

DEFAULT_CLOB_HTTP = os.getenv(
    "PM_CLOB_HTTP_URL",
    "https://clob.polymarket.com",
).rstrip("/")

USER_AGENT = "Mozilla/5.0 PolymarketBot-v0.12-paper-research"
SNAPSHOT_LATE_TOLERANCE_MS = 15_000

DDL = """
CREATE TABLE IF NOT EXISTS execution_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_snapshots(
    condition_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    market_start_ms INTEGER NOT NULL,
    market_end_ms INTEGER NOT NULL,
    horizon_seconds INTEGER NOT NULL,
    target_timestamp_ms INTEGER NOT NULL,
    snapshot_timestamp_ms INTEGER,
    lateness_ms INTEGER,
    status TEXT NOT NULL,
    error TEXT,

    up_token_id TEXT,
    down_token_id TEXT,
    tick_size REAL,
    min_order_size REAL,
    fee_rate REAL,
    taker_fee_enabled INTEGER,

    up_book_timestamp_ms INTEGER,
    up_best_bid REAL,
    up_best_ask REAL,
    up_best_ask_size REAL,
    up_total_ask_depth REAL,
    up_executable_size REAL,
    up_executable INTEGER,
    up_vwap_buy REAL,
    up_fee_per_share REAL,
    up_total_cost_per_share REAL,
    up_profit_if_win_per_share REAL,
    up_worst_ask_used REAL,
    up_levels_consumed INTEGER,
    up_asks_json TEXT,
    up_bids_json TEXT,

    down_book_timestamp_ms INTEGER,
    down_best_bid REAL,
    down_best_ask REAL,
    down_best_ask_size REAL,
    down_total_ask_depth REAL,
    down_executable_size REAL,
    down_executable INTEGER,
    down_vwap_buy REAL,
    down_fee_per_share REAL,
    down_total_cost_per_share REAL,
    down_profit_if_win_per_share REAL,
    down_worst_ask_used REAL,
    down_levels_consumed INTEGER,
    down_asks_json TEXT,
    down_bids_json TEXT,

    market_info_json TEXT,
    created_at TEXT NOT NULL,

    PRIMARY KEY(condition_id, horizon_seconds)
);

CREATE INDEX IF NOT EXISTS idx_execution_snapshots_time
ON execution_snapshots(market_start_ms, horizon_seconds);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def http_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def http_json_retry(
    url: str,
    timeout: float,
    attempts: int = 3,
    retry_delay_seconds: float = 0.12,
) -> Any:
    retryable_http = {429, 500, 502, 503, 504}
    last_exc: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return http_json(url, min(float(timeout), 2.0))
        except urllib.error.HTTPError as exc:
            if exc.code not in retryable_http:
                raise
            last_exc = exc
        except urllib.error.URLError as exc:
            last_exc = exc

        if attempt < attempts:
            print(
                f"[{utc_now()}] RETRY HTTP {attempt}/{attempts - 1} "
                f"para {url}"
            )
            time.sleep(retry_delay_seconds * attempt)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("http_json_retry fallo sin excepcion")


def open_output(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(DDL)
    con.execute(
        "INSERT OR IGNORE INTO execution_meta(key,value) VALUES(?,?)",
        ("schema_version", "v0.12-execution-sidecar-1"),
    )
    con.execute(
        "INSERT OR IGNORE INTO execution_meta(key,value) VALUES(?,?)",
        ("created_at", utc_now()),
    )
    con.execute(
        "INSERT OR REPLACE INTO execution_meta(key,value) VALUES(?,?)",
        ("real_money", "BLOQUEADO"),
    )
    con.commit()
    return con


def read_recent_markets(source_db: Path, now_ms: int) -> list[sqlite3.Row]:
    uri = f"{source_db.resolve().as_uri()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        # Solo mercados actuales / próximos; no lee label, outcome ni PnL.
        return con.execute(
            """
            SELECT condition_id,slug,market_start_ms,market_end_ms
            FROM shadow_markets
            WHERE market_end_ms >= ?
              AND market_start_ms <= ?
            ORDER BY market_start_ms,condition_id
            """,
            (now_ms - 30_000, now_ms + 600_000),
        ).fetchall()
    finally:
        con.close()


def parse_market_info(payload: dict[str, Any]) -> dict[str, Any]:
    tokens = payload.get("t")
    if not isinstance(tokens, list):
        raise RuntimeError("CLOB market info sin lista de tokens")

    by_outcome: dict[str, str] = {}
    for item in tokens:
        if not isinstance(item, dict):
            continue
        token = item.get("t")
        outcome = item.get("o")
        if token is not None and outcome is not None:
            by_outcome[str(outcome).lower()] = str(token)

    up = by_outcome.get("up")
    down = by_outcome.get("down")
    if not up or not down:
        raise RuntimeError("No se encontraron tokens Up/Down en CLOB market info")

    mts = payload.get("mts")
    mos = payload.get("mos")
    if mts is None or mos is None:
        raise RuntimeError("CLOB market info sin tick/min order")

    fee_details = payload.get("fd") if isinstance(payload.get("fd"), dict) else {}
    fee_rate = float(fee_details.get("r") or 0.0)
    taker_enabled = 1 if fee_details.get("to") is True else 0

    return {
        "up_token_id": up,
        "down_token_id": down,
        "tick_size": float(mts),
        "min_order_size": float(mos),
        "fee_rate": fee_rate,
        "taker_fee_enabled": taker_enabled,
        "raw": payload,
    }


def normalize_book(payload: dict[str, Any]) -> dict[str, Any]:
    def levels(name: str, reverse: bool) -> list[tuple[float, float]]:
        raw = payload.get(name, [])
        out: list[tuple[float, float]] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                try:
                    price = float(item.get("price"))
                    size = float(item.get("size"))
                except (TypeError, ValueError):
                    continue
                if price < 0 or size <= 0:
                    continue
                out.append((price, size))
        out.sort(key=lambda x: x[0], reverse=reverse)
        return out

    asks = levels("asks", reverse=False)
    bids = levels("bids", reverse=True)

    ts = payload.get("timestamp")
    try:
        book_timestamp_ms = int(ts) if ts is not None else None
    except (TypeError, ValueError):
        book_timestamp_ms = None

    return {
        "asks": asks,
        "bids": bids,
        "book_timestamp_ms": book_timestamp_ms,
    }


def execution_metrics(
    book: dict[str, Any],
    requested_size: float,
    fee_rate: float,
    taker_fee_enabled: int,
) -> dict[str, Any]:
    asks: list[tuple[float, float]] = list(book["asks"])
    bids: list[tuple[float, float]] = list(book["bids"])

    best_ask = asks[0][0] if asks else None
    best_bid = bids[0][0] if bids else None

    best_ask_size = None
    if asks:
        p0 = asks[0][0]
        best_ask_size = sum(size for price, size in asks if price == p0)

    total_ask_depth = sum(size for _, size in asks)

    remaining = requested_size
    notional = 0.0
    fee_dollars = 0.0
    levels_consumed = 0
    worst_ask_used = None

    for price, size in asks:
        if remaining <= 1e-12:
            break
        take = min(remaining, size)
        if take <= 0:
            continue

        notional += take * price
        if taker_fee_enabled:
            fee_dollars += take * fee_rate * price * (1.0 - price)

        remaining -= take
        levels_consumed += 1
        worst_ask_used = price

    executable = remaining <= 1e-9

    if executable and requested_size > 0:
        vwap = notional / requested_size
        fee_per_share = fee_dollars / requested_size
        total_cost_per_share = vwap + fee_per_share
        profit_if_win = 1.0 - total_cost_per_share

        # Safety invariant: simulated fill can NEVER be better than observed best ask.
        if best_ask is not None and vwap + 1e-12 < best_ask:
            raise RuntimeError(
                f"Invariante rota: VWAP {vwap} < best ask {best_ask}"
            )
    else:
        vwap = None
        fee_per_share = None
        total_cost_per_share = None
        profit_if_win = None
        levels_consumed = 0
        worst_ask_used = None

    return {
        "book_timestamp_ms": book["book_timestamp_ms"],
        "best_bid": best_bid,
        "best_ask": best_ask,
        "best_ask_size": best_ask_size,
        "total_ask_depth": total_ask_depth,
        "executable_size": requested_size,
        "executable": 1 if executable else 0,
        "vwap_buy": vwap,
        "fee_per_share": fee_per_share,
        "total_cost_per_share": total_cost_per_share,
        "profit_if_win_per_share": profit_if_win,
        "worst_ask_used": worst_ask_used,
        "levels_consumed": levels_consumed,
        "asks_json": json_compact(
            [{"price": p, "size": s} for p, s in asks]
        ),
        "bids_json": json_compact(
            [{"price": p, "size": s} for p, s in bids]
        ),
    }


def snapshot_exists(
    con: sqlite3.Connection,
    condition_id: str,
    horizon_seconds: int,
) -> bool:
    row = con.execute(
        """
        SELECT 1
        FROM execution_snapshots
        WHERE condition_id=? AND horizon_seconds=?
        LIMIT 1
        """,
        (condition_id, horizon_seconds),
    ).fetchone()
    return row is not None


def save_missed(
    con: sqlite3.Connection,
    market: sqlite3.Row,
    horizon: int,
    target_ms: int,
    lateness_ms: int,
) -> None:
    con.execute(
        """
        INSERT OR IGNORE INTO execution_snapshots(
            condition_id,slug,market_start_ms,market_end_ms,
            horizon_seconds,target_timestamp_ms,snapshot_timestamp_ms,
            lateness_ms,status,error,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            market["condition_id"],
            market["slug"],
            market["market_start_ms"],
            market["market_end_ms"],
            horizon,
            target_ms,
            int(time.time() * 1000),
            lateness_ms,
            "MISSED",
            "snapshot_late_beyond_tolerance",
            utc_now(),
        ),
    )
    con.commit()


def capture_snapshot(
    con: sqlite3.Connection,
    market: sqlite3.Row,
    horizon: int,
    requested_size: float,
    timeout: float,
    clob_http: str,
) -> None:
    condition_id = str(market["condition_id"])
    target_ms = int(market["market_end_ms"]) - horizon * 1000
    snap_ms = int(time.time() * 1000)
    lateness_ms = snap_ms - target_ms

    try:
        info_url = (
            f"{clob_http}/clob-markets/"
            f"{urllib.parse.quote(condition_id, safe='')}"
        )
        info_raw = http_json_retry(info_url, timeout)
        if not isinstance(info_raw, dict):
            raise RuntimeError("Respuesta market info inválida")
        info = parse_market_info(info_raw)

        up_url = (
            f"{clob_http}/book?token_id="
            f"{urllib.parse.quote(info['up_token_id'], safe='')}"
        )
        down_url = (
            f"{clob_http}/book?token_id="
            f"{urllib.parse.quote(info['down_token_id'], safe='')}"
        )

        up_raw = http_json_retry(up_url, timeout)
        down_raw = http_json_retry(down_url, timeout)
        if not isinstance(up_raw, dict) or not isinstance(down_raw, dict):
            raise RuntimeError("Respuesta de book inválida")

        up = execution_metrics(
            normalize_book(up_raw),
            requested_size,
            info["fee_rate"],
            info["taker_fee_enabled"],
        )
        down = execution_metrics(
            normalize_book(down_raw),
            requested_size,
            info["fee_rate"],
            info["taker_fee_enabled"],
        )

        # Verificación adicional: tick/min order del propio book si están presentes.
        for raw, side in ((up_raw, "UP"), (down_raw, "DOWN")):
            book_tick = raw.get("tick_size")
            book_min = raw.get("min_order_size")
            if book_tick is not None and abs(float(book_tick) - info["tick_size"]) > 1e-12:
                # Polymarket puede cambiar dinamicamente el tick (p.ej. 0.01 -> 0.001)
                # cuando el precio entra en zonas extremas. El /book es el snapshot
                # contemporaneo, asi que una diferencia de tick NO invalida el book.
                print(
                    f"[{utc_now()}] INFO {side}: tick dinamico "
                    f"book={book_tick} market_info={info['tick_size']}"
                )
            if book_min is not None and abs(float(book_min) - info["min_order_size"]) > 1e-12:
                raise RuntimeError(f"{side}: min order del book no coincide con market info")

        status = "SAVED"
        error = None

        con.execute(
            """
            INSERT OR REPLACE INTO execution_snapshots(
                condition_id,slug,market_start_ms,market_end_ms,
                horizon_seconds,target_timestamp_ms,snapshot_timestamp_ms,
                lateness_ms,status,error,

                up_token_id,down_token_id,tick_size,min_order_size,
                fee_rate,taker_fee_enabled,

                up_book_timestamp_ms,up_best_bid,up_best_ask,up_best_ask_size,
                up_total_ask_depth,up_executable_size,up_executable,
                up_vwap_buy,up_fee_per_share,up_total_cost_per_share,
                up_profit_if_win_per_share,up_worst_ask_used,
                up_levels_consumed,up_asks_json,up_bids_json,

                down_book_timestamp_ms,down_best_bid,down_best_ask,down_best_ask_size,
                down_total_ask_depth,down_executable_size,down_executable,
                down_vwap_buy,down_fee_per_share,down_total_cost_per_share,
                down_profit_if_win_per_share,down_worst_ask_used,
                down_levels_consumed,down_asks_json,down_bids_json,

                market_info_json,created_at
            ) VALUES(
                ?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                ?,?
            )
            """,
            (
                condition_id,
                market["slug"],
                market["market_start_ms"],
                market["market_end_ms"],
                horizon,
                target_ms,
                snap_ms,
                lateness_ms,
                status,
                error,

                info["up_token_id"],
                info["down_token_id"],
                min(
                    float(up_raw.get("tick_size") or info["tick_size"]),
                    float(down_raw.get("tick_size") or info["tick_size"]),
                ),
                info["min_order_size"],
                info["fee_rate"],
                info["taker_fee_enabled"],

                up["book_timestamp_ms"],
                up["best_bid"],
                up["best_ask"],
                up["best_ask_size"],
                up["total_ask_depth"],
                up["executable_size"],
                up["executable"],
                up["vwap_buy"],
                up["fee_per_share"],
                up["total_cost_per_share"],
                up["profit_if_win_per_share"],
                up["worst_ask_used"],
                up["levels_consumed"],
                up["asks_json"],
                up["bids_json"],

                down["book_timestamp_ms"],
                down["best_bid"],
                down["best_ask"],
                down["best_ask_size"],
                down["total_ask_depth"],
                down["executable_size"],
                down["executable"],
                down["vwap_buy"],
                down["fee_per_share"],
                down["total_cost_per_share"],
                down["profit_if_win_per_share"],
                down["worst_ask_used"],
                down["levels_consumed"],
                down["asks_json"],
                down["bids_json"],

                json_compact(info_raw),
                utc_now(),
            ),
        )
        con.commit()

        print(
            f"[{utc_now()}] SAVED {market['slug']} H={horizon}s "
            f"late={lateness_ms}ms | "
            f"UP ask={up['best_ask']} vwap5={up['vwap_buy']} exe={up['executable']} | "
            f"DOWN ask={down['best_ask']} vwap5={down['vwap_buy']} exe={down['executable']}"
        )

    except Exception as exc:
        con.execute(
            """
            INSERT OR REPLACE INTO execution_snapshots(
                condition_id,slug,market_start_ms,market_end_ms,
                horizon_seconds,target_timestamp_ms,snapshot_timestamp_ms,
                lateness_ms,status,error,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                condition_id,
                market["slug"],
                market["market_start_ms"],
                market["market_end_ms"],
                horizon,
                target_ms,
                snap_ms,
                lateness_ms,
                "FAILED",
                f"{type(exc).__name__}: {exc}",
                utc_now(),
            ),
        )
        con.commit()
        print(
            f"[{utc_now()}] FAILED {market['slug']} H={horizon}s: "
            f"{type(exc).__name__}: {exc}"
        )


def print_status(out_db: Path) -> None:
    if not out_db.exists():
        print("DB v0.12 todavía no existe:", out_db)
        return

    con = sqlite3.connect(out_db)
    try:
        rows = con.execute(
            """
            SELECT horizon_seconds,status,COUNT(*)
            FROM execution_snapshots
            GROUP BY horizon_seconds,status
            ORDER BY horizon_seconds DESC,status
            """
        ).fetchall()
        print("=" * 72)
        print("STATUS EXECUTION COLLECTOR v0.12")
        print("=" * 72)
        for horizon, status, n in rows:
            print(f"H={horizon:3d}s | {status:8s} | {n}")
        saved = con.execute(
            """
            SELECT COUNT(*)
            FROM execution_snapshots
            WHERE status='SAVED'
            """
        ).fetchone()[0]
        exe = con.execute(
            """
            SELECT COUNT(*)
            FROM execution_snapshots
            WHERE status='SAVED'
              AND up_executable=1
              AND down_executable=1
            """
        ).fetchone()[0]
        print("SAVED TOTAL:", saved)
        print("AMBOS LADOS EJECUTABLES:", exe)
        print("DB:", out_db)
        print("DINERO REAL: BLOQUEADO")
        print("=" * 72)
    finally:
        con.close()


def run(
    source_db: Path,
    out_db: Path,
    hours: float,
    requested_size: float,
    horizons: tuple[int, ...],
    poll_seconds: float,
    timeout: float,
    clob_http: str,
) -> None:
    if not source_db.exists():
        raise SystemExit(f"No existe source DB: {source_db}")
    if requested_size <= 0:
        raise SystemExit("--size debe ser positivo")
    if hours <= 0:
        raise SystemExit("--hours debe ser positivo")

    out = open_output(out_db)
    out.execute(
        "INSERT OR REPLACE INTO execution_meta(key,value) VALUES(?,?)",
        ("source_db", str(source_db.resolve())),
    )
    out.execute(
        "INSERT OR REPLACE INTO execution_meta(key,value) VALUES(?,?)",
        ("requested_size", str(requested_size)),
    )
    out.execute(
        "INSERT OR REPLACE INTO execution_meta(key,value) VALUES(?,?)",
        ("horizons", json_compact(list(horizons))),
    )
    out.execute(
        "INSERT OR REPLACE INTO execution_meta(key,value) VALUES(?,?)",
        ("clob_http", clob_http),
    )
    out.commit()

    end_at = time.time() + hours * 3600.0

    print("=" * 92)
    print("POLYMARKET EXECUTION COLLECTOR v0.12 - PAPER ONLY")
    print("=" * 92)
    print("SOURCE DB:", source_db)
    print("OUTPUT DB:", out_db)
    print("HORIZONS:", horizons)
    print("ORDER SIZE:", requested_size, "shares")
    print("CLOB HTTP:", clob_http)
    print("REGLA DE SEGURIDAD: VWAP nunca puede ser menor que best ask")
    print("NO lee labels ni PnL. NO envía órdenes. DINERO REAL BLOQUEADO.")
    print("Ctrl+C para detener; puede reanudarse con el mismo comando.")
    print("=" * 92)

    try:
        while time.time() < end_at:
            now_ms = int(time.time() * 1000)
            try:
                markets = read_recent_markets(source_db, now_ms)
            except Exception as exc:
                print(f"[{utc_now()}] SOURCE DB ERROR: {type(exc).__name__}: {exc}")
                time.sleep(max(1.0, poll_seconds))
                continue

            for market in markets:
                for horizon in horizons:
                    if snapshot_exists(out, str(market["condition_id"]), horizon):
                        continue

                    target_ms = int(market["market_end_ms"]) - horizon * 1000
                    lateness = now_ms - target_ms

                    if lateness < 0:
                        continue

                    if lateness > SNAPSHOT_LATE_TOLERANCE_MS:
                        # Solo marca missed para mercados que el sidecar vio en vivo.
                        save_missed(out, market, horizon, target_ms, lateness)
                        print(
                            f"[{utc_now()}] MISSED {market['slug']} "
                            f"H={horizon}s late={lateness}ms"
                        )
                        continue

                    capture_snapshot(
                        out,
                        market,
                        horizon,
                        requested_size,
                        timeout,
                        clob_http,
                    )

            time.sleep(poll_seconds)

    except KeyboardInterrupt:
        print("\nDetenido por usuario. Datos guardados siguen válidos.")
    finally:
        out.close()
        print_status(out_db)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-db", default=str(DEFAULT_SOURCE_DB))
    ap.add_argument("--output-db", default=str(DEFAULT_OUT_DB))
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--size", type=float, default=5.0)
    ap.add_argument("--poll-seconds", type=float, default=0.5)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--clob-http", default=DEFAULT_CLOB_HTTP)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    source_db = Path(args.source_db).expanduser()
    out_db = Path(args.output_db).expanduser()

    if args.status:
        print_status(out_db)
        return

    horizons = (120, 60)
    run(
        source_db=source_db,
        out_db=out_db,
        hours=args.hours,
        requested_size=args.size,
        horizons=horizons,
        poll_seconds=args.poll_seconds,
        timeout=args.timeout,
        clob_http=args.clob_http.rstrip("/"),
    )


if __name__ == "__main__":
    main()
