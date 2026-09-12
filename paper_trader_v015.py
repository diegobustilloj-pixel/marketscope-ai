from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot import v015


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT_DB = DATA / "paper_trades_v015.db"
POLL_SECONDS = 15

DDL = """
CREATE TABLE IF NOT EXISTS paper_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_orders(
    condition_id TEXT PRIMARY KEY,
    market_start_ms INTEGER NOT NULL,
    selected_stratum TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_cost_per_share REAL NOT NULL,
    shares REAL NOT NULL,
    cash_outlay REAL NOT NULL,
    status TEXT NOT NULL,
    real_money INTEGER NOT NULL CHECK(real_money=0),
    created_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db() -> sqlite3.Connection:
    OUT_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(OUT_DB, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(DDL)
    connection.commit()
    return connection


def save_meta(connection: sqlite3.Connection, active: dict[str, Any]) -> None:
    values = {
        "schema": "paper_trader_v015_1",
        "created_at": utc_now(),
        "v015_prereg_sha256": v015.sha(v015.ACTIVE),
        "selected_stratum": str(active["selected_stratum"]),
        "minimum_market_start_ms": str(active["minimum_market_start_ms"]),
        "target_trades": str(active["target_trades"]),
        "real_money": "BLOQUEADO",
    }
    for key, value in values.items():
        connection.execute(
            "INSERT OR REPLACE INTO paper_meta(key,value) VALUES(?,?)",
            (key, value),
        )
    connection.commit()


def target_rows() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    active, _, candidates = v015.postcut_population()
    return active, candidates[: int(active["target_trades"])]


def already_saved(connection: sqlite3.Connection, condition_id: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM paper_orders WHERE condition_id=?",
        (condition_id,),
    ).fetchone()
    return row is not None


def record_order(
    connection: sqlite3.Connection,
    active: dict[str, Any],
    row: dict[str, Any],
) -> None:
    condition_id = str(row["condition_id"])
    side = "UP" if int(row["direction"]) > 0 else "DOWN"
    cost = float(row["chosen_side_cost_60"])
    shares = float(active["order_size_shares"])
    cash_outlay = cost * shares
    connection.execute(
        """
        INSERT INTO paper_orders(
            condition_id,market_start_ms,selected_stratum,side,
            entry_cost_per_share,shares,cash_outlay,status,real_money,created_at
        ) VALUES(?,?,?,?,?,?,?,?,0,?)
        """,
        (
            condition_id,
            int(row["market_start_ms"]),
            str(active["selected_stratum"]),
            side,
            cost,
            shares,
            cash_outlay,
            "PAPER_FILLED",
            utc_now(),
        ),
    )
    connection.commit()
    print(
        f"[{utc_now()}] PAPER V0.15 {active['selected_stratum']} | "
        f"{condition_id} | side={side} cost={cost:.6f} "
        f"shares={shares} real_money=0"
    )


def status() -> dict[str, Any]:
    activation = v015.activation_status()
    payload: dict[str, Any] = {
        "schema": "paper_trader_v015_status",
        "activation": activation,
        "labels_or_outcomes_read": 0,
        "real_money": "BLOQUEADO",
    }
    if activation["status"] != "ACTIVE":
        return payload
    connection = open_db()
    try:
        count = connection.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
        real = connection.execute(
            "SELECT COALESCE(SUM(real_money),0) FROM paper_orders"
        ).fetchone()[0]
    finally:
        connection.close()
    payload.update(
        {
            "selected_stratum": activation["selected_stratum"],
            "paper_orders": int(count),
            "target_orders": 10,
            "real_money_sum": int(real),
        }
    )
    return payload


def monitor(hours: float | None) -> None:
    active = v015.activate_if_ready()
    if active is None:
        print(json.dumps(status(), ensure_ascii=False, indent=2, sort_keys=True))
        return
    connection = open_db()
    save_meta(connection, active)
    deadline = None if hours is None else time.time() + hours * 3600.0
    print("=" * 88)
    print("PAPER TRADER V0.15 - CONFIRMACION INDEPENDIENTE")
    print("STRATUM:", active["selected_stratum"])
    print("TARGET: primeros 10 trades futuros")
    print("NO lee outcomes. NO firma ni envia ordenes.")
    print("DINERO REAL: BLOQUEADO")
    print("=" * 88)
    try:
        while deadline is None or time.time() < deadline:
            if v015.RESULT.is_file():
                return
            active, rows = target_rows()
            for row in rows:
                condition_id = str(row["condition_id"])
                if not already_saved(connection, condition_id):
                    record_order(connection, active, row)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nPaper trader v0.15 detenido")
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=None)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2, sort_keys=True))
        return
    monitor(args.hours)


if __name__ == "__main__":
    main()
