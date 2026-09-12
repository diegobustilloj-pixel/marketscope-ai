from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import calibrar_v012_execution_ev as cal

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

PREREG = DATA / "prereg_v013_cheap_strict_forward.json"
THRESHOLDS = DATA / "prereg_v012_thresholds.json"
EVALUATOR = DATA / "prereg_v013_evaluator.json"
OUT_DB = DATA / "paper_trades_v013.db"

POLL_SECONDS = 2.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify_frozen() -> tuple[dict[str, Any], dict[str, Any]]:
    if not PREREG.exists():
        raise RuntimeError(f"Falta {PREREG}")
    if not THRESHOLDS.exists():
        raise RuntimeError(f"Falta {THRESHOLDS}")
    if not EVALUATOR.exists():
        raise RuntimeError(f"Falta {EVALUATOR}")

    p = load(PREREG)
    t = load(THRESHOLDS)
    e = load(EVALUATOR)

    if p.get("schema") != "prereg_v013_cheap_strict_forward":
        raise RuntimeError("Schema v0.13 inesperado")

    if p.get("source_thresholds_sha256") != sha(THRESHOLDS):
        raise RuntimeError("Los thresholds v0.12 cambiaron después de congelar v0.13")

    if e.get("v013_prereg_sha256") != sha(PREREG):
        raise RuntimeError("El prereg v0.13 cambió después de congelar el evaluador")

    if e.get("thresholds_sha256") != sha(THRESHOLDS):
        raise RuntimeError("El evaluador v0.13 ya no coincide con thresholds")

    return p, t


def open_db() -> sqlite3.Connection:
    OUT_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(OUT_DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_orders(
            condition_id TEXT PRIMARY KEY,
            market_start_ms INTEGER NOT NULL,
            detected_at_utc TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_cost_per_share REAL NOT NULL,
            order_size_shares REAL NOT NULL,
            simulated_cash_outlay REAL NOT NULL,
            payout_if_win REAL NOT NULL,
            profit_if_win REAL NOT NULL,
            max_loss REAL NOT NULL,
            abs_twap_move_bps REAL NOT NULL,
            market_response_ratio REAL NOT NULL,
            shock_threshold REAL NOT NULL,
            response_threshold REAL NOT NULL,
            cost_min REAL NOT NULL,
            cost_max REAL NOT NULL,
            status TEXT NOT NULL,
            real_money INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_meta(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    c.commit()
    return c


def save_meta(c: sqlite3.Connection, p: dict[str, Any]) -> None:
    meta = {
        "schema": "paper_trader_v013_1",
        "created_at": utc_now(),
        "v013_prereg_sha256": sha(PREREG),
        "thresholds_sha256": sha(THRESHOLDS),
        "evaluator_sha256": sha(EVALUATOR),
        "cutoff_market_start_ms": str(p["forward_after_market_start_ms"]),
        "real_money": "BLOQUEADO",
    }
    for k, v in meta.items():
        c.execute(
            "INSERT OR REPLACE INTO paper_meta(key,value) VALUES(?,?)",
            (k, str(v)),
        )
    c.commit()


def qualifying_rows(
    p: dict[str, Any],
    t: dict[str, Any],
) -> list[dict[str, Any]]:
    cutoff = int(p["forward_after_market_start_ms"])
    lo, hi = map(float, p["entry_cost_band"])

    strict = t["thresholds"]["strict"]
    shock_min = float(strict["shock_abs_bps_min"])
    response_max = float(strict["market_response_ratio_max"])

    out: list[dict[str, Any]] = []

    for r in cal.eligible():
        if int(r["market_start_ms"]) <= cutoff:
            continue

        cost = r["chosen_side_cost_60"]
        if cost is None:
            continue

        if float(r["abs_twap_move_bps"]) < shock_min:
            continue
        if float(r["market_response_ratio"]) > response_max:
            continue
        if not (lo <= float(cost) <= hi):
            continue

        out.append(r)

    out.sort(key=lambda x: (int(x["market_start_ms"]), x["condition_id"]))
    return out


def already_saved(c: sqlite3.Connection, condition_id: str) -> bool:
    return (
        c.execute(
            "SELECT 1 FROM paper_orders WHERE condition_id=? LIMIT 1",
            (condition_id,),
        ).fetchone()
        is not None
    )


def record_paper_order(
    c: sqlite3.Connection,
    r: dict[str, Any],
    p: dict[str, Any],
    t: dict[str, Any],
) -> None:
    side = "UP" if int(r["direction"]) > 0 else "DOWN"
    cost = float(r["chosen_side_cost_60"])
    size = float(p["order_size_shares"])

    cash = cost * size
    payout = size
    profit_if_win = payout - cash
    max_loss = cash

    strict = t["thresholds"]["strict"]
    lo, hi = map(float, p["entry_cost_band"])

    c.execute(
        """
        INSERT OR IGNORE INTO paper_orders(
            condition_id,market_start_ms,detected_at_utc,side,
            entry_cost_per_share,order_size_shares,
            simulated_cash_outlay,payout_if_win,profit_if_win,max_loss,
            abs_twap_move_bps,market_response_ratio,
            shock_threshold,response_threshold,cost_min,cost_max,
            status,real_money
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
        """,
        (
            r["condition_id"],
            int(r["market_start_ms"]),
            utc_now(),
            side,
            cost,
            size,
            cash,
            payout,
            profit_if_win,
            max_loss,
            float(r["abs_twap_move_bps"]),
            float(r["market_response_ratio"]),
            float(strict["shock_abs_bps_min"]),
            float(strict["market_response_ratio_max"]),
            lo,
            hi,
            "PAPER_FILLED",
        ),
    )
    c.commit()

    print("=" * 94)
    print("PAPER TRADE v0.13 DETECTADO")
    print("=" * 94)
    print("CONDITION:", r["condition_id"])
    print("SIDE:", side)
    print("COST/SHARE:", round(cost, 6))
    print("SHARES:", size)
    print("DESEMBOLSO SIMULADO:", round(cash, 6))
    print("GANANCIA SI ACIERTA:", round(profit_if_win, 6))
    print("PERDIDA MAXIMA:", round(max_loss, 6))
    print("ABS TWAP SHOCK BPS:", round(float(r["abs_twap_move_bps"]), 6))
    print("MARKET RESPONSE RATIO:", round(float(r["market_response_ratio"]), 6))
    print("STATUS: PAPER_FILLED")
    print("DINERO REAL: 0")
    print("=" * 94)


def status(c: sqlite3.Connection) -> None:
    n = c.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
    print("=" * 76)
    print("STATUS PAPER TRADER v0.13")
    print("=" * 76)
    print("PAPER ORDERS REGISTRADAS:", n)
    print("DB:", OUT_DB)
    print("LABELS/OUTCOMES: NO SE LEEN")
    print("DINERO REAL: BLOQUEADO")
    print("=" * 76)


def monitor(hours: float | None) -> None:
    p, t = verify_frozen()
    c = open_db()
    save_meta(c, p)

    deadline = None if hours is None else time.time() + hours * 3600.0

    print("=" * 94)
    print("PAPER TRADER v0.13 - SOLO SIMULACION")
    print("=" * 94)
    print("Regla: STRICT + coste ejecutable entre 0.10 y 0.22")
    print("Tamaño: 5 shares")
    print("Usa VWAP + fee ya capturados por execution_collector_v012")
    print("NO lee labels, outcomes ni PnL realizado")
    print("NO firma ni envia ordenes")
    print("DINERO REAL: BLOQUEADO")
    print("Ctrl+C detiene solo este paper trader")
    print("=" * 94)

    try:
        while deadline is None or time.time() < deadline:
            rows = qualifying_rows(p, t)
            for r in rows:
                if not already_saved(c, r["condition_id"]):
                    record_paper_order(c, r, p, t)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nPaper trader detenido.")
    finally:
        status(c)
        c.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    p, _ = verify_frozen()
    c = open_db()
    save_meta(c, p)

    if args.status:
        status(c)
        c.close()
        return

    c.close()
    monitor(args.hours)


if __name__ == "__main__":
    main()
