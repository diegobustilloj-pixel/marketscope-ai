from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import v014_monitor as protocol


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT_DB = DATA / "paper_trades_v014.db"
POLL_SECONDS = 2.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db() -> sqlite3.Connection:
    OUT_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(OUT_DB, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_orders(
            condition_id TEXT PRIMARY KEY,
            market_start_ms INTEGER NOT NULL,
            detected_at_utc TEXT NOT NULL,
            stratum TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_cost_per_share REAL NOT NULL,
            order_size_shares REAL NOT NULL,
            simulated_cash_outlay REAL NOT NULL,
            payout_if_win REAL NOT NULL,
            profit_if_win REAL NOT NULL,
            max_loss REAL NOT NULL,
            abs_twap_move_bps REAL NOT NULL,
            market_response_ratio REAL NOT NULL,
            status TEXT NOT NULL,
            real_money INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_meta(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def save_meta(connection: sqlite3.Connection, prereg: dict[str, Any]) -> None:
    meta = {
        "schema": "paper_trader_v014_1",
        "created_at": utc_now(),
        "v014_prereg_sha256": protocol.sha(protocol.PREREG),
        "evaluator_sha256": protocol.sha(protocol.SPEC),
        "cutoff_market_start_ms": prereg["forward_after_market_start_ms"],
        "target_per_stratum": prereg["target_per_stratum"],
        "real_money": "BLOQUEADO",
    }
    for key, value in meta.items():
        connection.execute(
            "INSERT OR REPLACE INTO paper_meta(key,value) VALUES(?,?)",
            (key, str(value)),
        )
    connection.commit()


def target_rows() -> list[tuple[str, dict[str, Any]]]:
    prereg, _, _ = protocol.verify_frozen()
    _, strata = protocol.postcut_population()
    target = int(prereg["target_per_stratum"])
    rows = [
        (stratum, row)
        for stratum in ("LOW", "MODERATE")
        for row in strata[stratum][:target]
    ]
    rows.sort(key=lambda item: (int(item[1]["market_start_ms"]), item[1]["condition_id"]))
    return rows


def already_saved(connection: sqlite3.Connection, condition_id: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM paper_orders WHERE condition_id=? LIMIT 1",
        (condition_id,),
    ).fetchone() is not None


def record_order(
    connection: sqlite3.Connection,
    stratum: str,
    row: dict[str, Any],
    prereg: dict[str, Any],
) -> None:
    side = "UP" if int(row["direction"]) > 0 else "DOWN"
    cost = float(row["chosen_side_cost_60"])
    size = float(prereg["order_size_shares"])
    cash = cost * size
    payout = size

    connection.execute(
        """
        INSERT OR IGNORE INTO paper_orders(
            condition_id,market_start_ms,detected_at_utc,stratum,side,
            entry_cost_per_share,order_size_shares,simulated_cash_outlay,
            payout_if_win,profit_if_win,max_loss,abs_twap_move_bps,
            market_response_ratio,status,real_money
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
        """,
        (
            row["condition_id"],
            int(row["market_start_ms"]),
            utc_now(),
            stratum,
            side,
            cost,
            size,
            cash,
            payout,
            payout - cash,
            cash,
            float(row["abs_twap_move_bps"]),
            float(row["market_response_ratio"]),
            "PAPER_FILLED",
        ),
    )
    connection.commit()
    print(
        f"[{utc_now()}] PAPER V0.14 {stratum} | {row['condition_id']} | "
        f"side={side} cost={cost:.6f} shares={size} real_money=0"
    )


def status(connection: sqlite3.Connection) -> None:
    counts = dict(
        connection.execute(
            "SELECT stratum,COUNT(*) FROM paper_orders GROUP BY stratum"
        ).fetchall()
    )
    real = connection.execute(
        "SELECT COALESCE(SUM(real_money),0) FROM paper_orders"
    ).fetchone()[0]
    print("=" * 80)
    print("STATUS PAPER TRADER V0.14")
    print("=" * 80)
    print("LOW:", counts.get("LOW", 0), "/6")
    print("MODERATE:", counts.get("MODERATE", 0), "/6")
    print("LABELS/OUTCOMES: NO SE LEEN")
    print("REAL MONEY SUM:", real)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 80)


def monitor(hours: float | None) -> None:
    prereg, _, _ = protocol.verify_frozen()
    connection = open_db()
    save_meta(connection, prereg)
    deadline = None if hours is None else time.time() + hours * 3600.0

    print("=" * 92)
    print("PAPER TRADER V0.14 - DISJOINT DEVELOPMENT")
    print("=" * 92)
    print("LOW [0.05,0.10): 6 trades")
    print("MODERATE (0.22,0.30]: 6 trades")
    print("NO lee outcomes. NO firma ni envia ordenes.")
    print("DINERO REAL: BLOQUEADO")
    print("=" * 92)

    try:
        while deadline is None or time.time() < deadline:
            for stratum, row in target_rows():
                if not already_saved(connection, row["condition_id"]):
                    record_order(connection, stratum, row, prereg)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nPaper trader v0.14 detenido")
    finally:
        status(connection)
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=None)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    prereg, _, _ = protocol.verify_frozen()
    connection = open_db()
    save_meta(connection, prereg)
    if args.status:
        status(connection)
        connection.close()
        return
    connection.close()
    monitor(args.hours)


if __name__ == "__main__":
    main()
