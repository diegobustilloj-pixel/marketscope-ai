from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

DB_DEFAULT = DATA / "shadow_forward_twap_transfer_v094a.db"
PREREG_FORWARD = DATA / "prereg_v011_forward100.json"
RESULT_FORWARD = DATA / "resultado_v011_forward100.json"

OUT_JSON = DATA / "postmortem_v011_forward100.json"
OUT_CSV = DATA / "postmortem_v011_forward100_trades.csv"

CUTOFF_MS = 1786394100000
CONSUMED_POST_CUTOFF_60S = 50
TEST_ROWS = 100
EPS = 1e-9


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sf(v):
    if v is None:
        return np.nan
    try:
        x = float(v)
        return x if math.isfinite(x) else np.nan
    except Exception:
        return np.nan


def finite(v):
    try:
        return math.isfinite(float(v))
    except Exception:
        return False


def fresh(d):
    return (
        d.get("twap_30s_fresh") in (1, True)
        and d.get("twap_open_fresh") in (1, True)
    )


def sign(x):
    if not finite(x) or float(x) == 0:
        return 0
    return 1 if float(x) > 0 else -1


def delta(a60, a120, key):
    x60 = sf(a60.get(key))
    x120 = sf(a120.get(key))
    if not (finite(x60) and finite(x120)):
        return np.nan
    return float(x60 - x120)


def taker_components(ask, fee_rate, slip):
    fill = min(0.999, max(0.001, float(ask) + float(slip)))
    fee = float(fee_rate) * fill * (1.0 - fill)
    return float(fill), float(fee), float(fill + fee)


def read_meta(con):
    out = {}
    for k, v in con.execute("SELECT key,value FROM shadow_meta"):
        try:
            out[str(k)] = json.loads(v)
        except Exception:
            out[str(k)] = v
    return out


def first100_eligible(db: Path):
    con = sqlite3.connect(db)
    try:
        meta = read_meta(con)
        fee_rate = float(meta.get("fee_rate", 0.07))
        slip = float(meta.get("slippage_per_share", 0.005))

        f60 = con.execute(
            """
            SELECT m.condition_id,m.market_start_ms,m.label,f.feature_json
            FROM shadow_features f
            JOIN shadow_markets m ON m.condition_id=f.condition_id
            WHERE f.horizon_seconds=60
              AND f.feature_json IS NOT NULL
              AND m.label_verified=1
              AND m.market_start_ms>?
            ORDER BY m.market_start_ms,m.condition_id
            """,
            (CUTOFF_MS,),
        ).fetchall()

        d120 = dict(
            con.execute(
                """
                SELECT condition_id,feature_json
                FROM shadow_diagnostics
                WHERE horizon_seconds=120
                  AND status='SAVED'
                  AND feature_json IS NOT NULL
                """
            )
        )
    finally:
        con.close()

    f60 = [r for r in f60 if fresh(json.loads(r[3]))]
    remaining = f60[CONSUMED_POST_CUTOFF_60S:]

    eligible = []
    for r in remaining:
        cid = r[0]
        if cid in d120:
            d = json.loads(d120[cid])
            if fresh(d):
                eligible.append((r, d))
                if len(eligible) == TEST_ROWS:
                    break

    if len(eligible) != TEST_ROWS:
        raise RuntimeError(f"Se esperaban 100 elegibles; hay {len(eligible)}")

    return eligible, fee_rate, slip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_DEFAULT))
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()

    if not args.run:
        raise SystemExit("Use --run")

    for p in (PREREG_FORWARD, RESULT_FORWARD):
        if not p.exists():
            raise RuntimeError(f"Falta {p}")

    frozen = load_json(PREREG_FORWARD)
    forward_result = load_json(RESULT_FORWARD)

    if forward_result.get("verdicto") != "FAIL_FORWARD100":
        raise RuntimeError("Este post-mortem fue diseñado para el Forward100 fallido de v0.11.")

    threshold = frozen["frozen_thresholds"]
    shock_min = float(threshold["shock_abs_bps_min"])
    response_max = float(threshold["market_response_ratio_max"])

    eligible, fee_rate, slip = first100_eligible(Path(args.db))

    trades = []

    for idx, ((cid, ms, label, raw60), d120) in enumerate(eligible, start=1):
        d60 = json.loads(raw60)

        twap_dist_120 = sf(d120.get("twap_distance_to_open_bps"))
        twap_dist_60 = sf(d60.get("twap_distance_to_open_bps"))
        twap_move = (
            float(twap_dist_60 - twap_dist_120)
            if finite(twap_dist_60) and finite(twap_dist_120)
            else np.nan
        )

        p120 = sf(d120.get("implied_up_mid_probability"))
        p60 = sf(d60.get("implied_up_mid_probability"))
        market_move_bps = (
            float((p60 - p120) * 10000.0)
            if finite(p60) and finite(p120)
            else np.nan
        )

        direction = sign(twap_move)
        response_ratio = (
            float(direction * market_move_bps / (abs(twap_move) + EPS))
            if direction and finite(market_move_bps)
            else np.nan
        )

        up_ask = sf(d60.get("up_best_ask"))
        down_ask = sf(d60.get("down_best_ask"))

        event = bool(
            direction != 0
            and finite(twap_move)
            and abs(twap_move) >= shock_min
            and finite(response_ratio)
            and response_ratio <= response_max
            and finite(up_ask)
            and finite(down_ask)
        )
        if not event:
            continue

        y = 1 if str(label).lower() == "up" else 0

        if direction > 0:
            side = "UP"
            raw_ask = up_ask
            fill, fee, total_cost = taker_components(raw_ask, fee_rate, slip)
            pnl = float(y - total_cost)
            correct = bool(y == 1)
        else:
            side = "DOWN"
            raw_ask = down_ask
            fill, fee, total_cost = taker_components(raw_ask, fee_rate, slip)
            pnl = float((1 - y) - total_cost)
            correct = bool(y == 0)

        breakeven_win_rate = float(total_cost)
        reward_if_win = float(1.0 - total_cost)
        loss_if_lose = float(total_cost)
        rr_win_loss = (
            reward_if_win / loss_if_lose
            if loss_if_lose > 0
            else None
        )

        trades.append({
            "test_row": idx,
            "condition_id": str(cid),
            "market_start_ms": int(ms),
            "side": side,
            "outcome": str(label).upper(),
            "correct_direction": correct,
            "raw_best_ask": float(raw_ask),
            "slippage_per_share": float(slip),
            "fill_price": float(fill),
            "fee_per_share": float(fee),
            "total_cost_per_share": float(total_cost),
            "pnl_per_share": float(pnl),
            "reward_if_win": reward_if_win,
            "loss_if_lose": loss_if_lose,
            "breakeven_win_rate": breakeven_win_rate,
            "reward_loss_ratio": rr_win_loss,
            "twap_distance_120_bps": float(twap_dist_120),
            "twap_distance_60_bps": float(twap_dist_60),
            "twap_move_120_to_60_bps": float(twap_move),
            "implied_up_120": float(p120),
            "implied_up_60": float(p60),
            "market_probability_move_bps": float(market_move_bps),
            "market_response_ratio": float(response_ratio),
            "up_spread_60": sf(d60.get("up_spread")),
            "down_spread_60": sf(d60.get("down_spread")),
            "market_ask_overround_60": sf(d60.get("market_ask_overround")),
        })

    if len(trades) != int(forward_result.get("events_traded", -1)):
        raise RuntimeError(
            f"Post-mortem reconstruyó {len(trades)} trades, "
            f"pero resultado Forward100 reporta {forward_result.get('events_traded')}."
        )

    pnls = np.asarray([t["pnl_per_share"] for t in trades], dtype=float)
    costs = np.asarray([t["total_cost_per_share"] for t in trades], dtype=float)
    winners = [t for t in trades if t["pnl_per_share"] > 0]
    losers = [t for t in trades if t["pnl_per_share"] <= 0]

    def avg(rows, key):
        vals = [float(r[key]) for r in rows if finite(r.get(key))]
        return float(np.mean(vals)) if vals else None

    summary = {
        "trades": len(trades),
        "wins": len(winners),
        "losses": len(losers),
        "net_pnl_per_share": float(pnls.sum()),
        "roi_on_cost": float(pnls.sum() / costs.sum()) if costs.sum() else None,
        "average_entry_cost_all": avg(trades, "total_cost_per_share"),
        "average_entry_cost_winners": avg(winners, "total_cost_per_share"),
        "average_entry_cost_losers": avg(losers, "total_cost_per_share"),
        "average_profit_winner": avg(winners, "pnl_per_share"),
        "average_loss_loser": avg(losers, "pnl_per_share"),
        "median_entry_cost": float(np.median(costs)) if len(costs) else None,
        "minimum_entry_cost": float(np.min(costs)) if len(costs) else None,
        "maximum_entry_cost": float(np.max(costs)) if len(costs) else None,
        "average_breakeven_win_rate": avg(trades, "breakeven_win_rate"),
        "average_twap_shock_abs_bps": (
            float(np.mean([abs(t["twap_move_120_to_60_bps"]) for t in trades]))
            if trades else None
        ),
        "average_market_response_ratio": avg(trades, "market_response_ratio"),
        "gross_profit_winners": float(sum(t["pnl_per_share"] for t in winners)),
        "gross_loss_losers": float(sum(t["pnl_per_share"] for t in losers)),
    }

    # Diagnóstico puramente descriptivo de lo ya consumido.
    if summary["average_entry_cost_all"] is not None:
        if summary["average_entry_cost_all"] >= 0.80:
            price_note = "ENTRADAS_MUY_CARAS"
        elif summary["average_entry_cost_all"] >= 0.65:
            price_note = "ENTRADAS_CARAS"
        else:
            price_note = "ENTRADAS_NO_EXTREMADAMENTE_CARAS"
    else:
        price_note = "SIN_DATOS"

    summary["descriptive_price_diagnosis"] = price_note

    out = {
        "schema": "postmortem_v011_forward100",
        "source_test_rows": 100,
        "future_rows_beyond_first_100_used": 0,
        "frozen_rule": "twap_shock_market_lag / standard",
        "frozen_thresholds": threshold,
        "summary": summary,
        "trades": trades,
        "note": (
            "Post-mortem descriptivo del Forward100 ya consumido. "
            "No modifica thresholds ni reutiliza mercados posteriores."
        ),
        "real_money": "BLOQUEADO",
    }

    OUT_JSON.write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    fields = list(trades[0].keys()) if trades else []
    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(trades)

    print("=" * 116)
    print("POST-MORTEM v0.11 FORWARD100 - SOLO LOS 7 TRADES YA CONSUMIDOS")
    print("=" * 116)
    print("Trades:", summary["trades"], "| Wins:", summary["wins"], "| Losses:", summary["losses"])
    print("PnL neto/share:", round(summary["net_pnl_per_share"], 6))
    print("ROI sobre costo:", summary["roi_on_cost"])
    print("Costo medio entrada:", summary["average_entry_cost_all"])
    print("Costo medio ganadoras:", summary["average_entry_cost_winners"])
    print("Costo medio perdedoras:", summary["average_entry_cost_losers"])
    print("Ganancia media ganadora:", summary["average_profit_winner"])
    print("Perdida media perdedora:", summary["average_loss_loser"])
    print("Win rate break-even medio:", summary["average_breakeven_win_rate"])
    print("Gross profit winners:", summary["gross_profit_winners"])
    print("Gross loss losers:", summary["gross_loss_losers"])
    print("Diagnostico descriptivo precio:", summary["descriptive_price_diagnosis"])
    print()
    print("DETALLE:")
    for i, t in enumerate(trades, 1):
        print(
            f"{i:02d} row={t['test_row']:3d} {t['side']:4s} "
            f"ask={t['raw_best_ask']:.4f} cost={t['total_cost_per_share']:.4f} "
            f"TWAPshock={t['twap_move_120_to_60_bps']:+.2f}bps "
            f"resp={t['market_response_ratio']:+.3f} "
            f"outcome={t['outcome']:4s} pnl={t['pnl_per_share']:+.5f}"
        )
    print()
    print("JSON:", OUT_JSON)
    print("CSV :", OUT_CSV)
    print("MERCADOS POSTERIORES AL FORWARD100 USADOS: 0")
    print("DINERO REAL: BLOQUEADO")
    print("=" * 116)


if __name__ == "__main__":
    main()
