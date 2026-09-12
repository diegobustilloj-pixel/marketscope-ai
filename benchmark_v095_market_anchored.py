from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path

import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

MODEL_NAME = "twap_transfer_strike_hgb"
FEATURES = [
    "twap_distance_to_open_bps",
    "twap_minus_chainlink_bps",
    "chainlink_return_5s_bps",
    "chainlink_return_15s_bps",
    "chainlink_return_30s_bps",
    "chainlink_return_60s_bps",
    "binance_return_5s_bps",
    "binance_return_15s_bps",
    "binance_return_30s_bps",
    "binance_return_60s_bps",
    "volatility_regime_ratio",
    "chainlink_up_fraction_30s",
    "chainlink_state",
    "chainlink_state_run_length",
]
ALPHAS = (1.0, 10.0, 100.0)
SHRINKS = (0.25, 0.50, 1.00)
CORRECTION_CAP = 0.15
EDGE_GRID = (0.02, 0.04, 0.06, 0.08, 0.10)


def open_ro(path: Path):
    uri = f"{path.resolve().as_uri()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def load_cutoff(path: Path) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return int(payload["development_cutoff_market_start_ms"])


def safe_float(v):
    if v is None:
        return np.nan
    try:
        x = float(v)
        return x if math.isfinite(x) else np.nan
    except Exception:
        return np.nan


def taker_cost(ask: float, fee_rate: float, slippage: float) -> float:
    fill = min(0.999, max(0.001, float(ask) + float(slippage)))
    fee = float(fee_rate) * fill * (1.0 - fill)
    return fill + fee


def load_rows(db: Path, cutoff_ms: int):
    con = open_ro(db)
    try:
        meta = {}
        for r in con.execute("SELECT key,value FROM shadow_meta").fetchall():
            try:
                meta[r["key"]] = json.loads(r["value"])
            except Exception:
                meta[r["key"]] = r["value"]
        fee_rate = float(meta.get("fee_rate", 0.07))
        slippage = float(meta.get("slippage_per_share", 0.005))
        rows = con.execute(
            """
            SELECT m.condition_id,m.market_start_ms,m.label,
                   s.probability_up AS old_model_probability,
                   b.probability_up AS market_probability,
                   f.feature_json
            FROM shadow_signals s
            JOIN shadow_markets m ON m.condition_id=s.condition_id
            JOIN shadow_features f ON f.condition_id=s.condition_id
            JOIN shadow_signals b ON b.condition_id=s.condition_id
                                 AND b.model_name='market_implied'
            WHERE s.model_name=? AND m.label_verified=1 AND m.market_start_ms<=?
            ORDER BY m.market_start_ms,m.condition_id
            """,
            (MODEL_NAME, cutoff_ms),
        ).fetchall()
    finally:
        con.close()

    out = []
    for r in rows:
        f = json.loads(r["feature_json"])
        pm = min(0.999, max(0.001, float(r["market_probability"])))
        vec = [safe_float(f.get(k)) for k in FEATURES]
        vec.extend([math.log(pm / (1.0 - pm)), pm * (1.0 - pm)])
        out.append({
            "condition_id": str(r["condition_id"]),
            "market_start_ms": int(r["market_start_ms"]),
            "y": 1 if str(r["label"]) == "Up" else 0,
            "p_market": pm,
            "p_old": float(r["old_model_probability"]),
            "x": vec,
            "twap_distance": safe_float(f.get("twap_distance_to_open_bps")),
            "up_cost": taker_cost(float(f["up_best_ask"]), fee_rate, slippage),
            "down_cost": taker_cost(float(f["down_best_ask"]), fee_rate, slippage),
        })
    return out, fee_rate, slippage


def expanding_oof(rows, alpha: float, shrink: float):
    n = len(rows)
    min_train = 80 if n >= 120 else max(40, n // 2)
    block = 20
    preds = np.full(n, np.nan)
    corr_out = np.full(n, np.nan)
    X = np.asarray([r["x"] for r in rows], dtype=float)
    y = np.asarray([r["y"] for r in rows], dtype=float)
    pm = np.asarray([r["p_market"] for r in rows], dtype=float)
    residual = y - pm

    start = min_train
    while start < n:
        end = min(n, start + block)
        pipe = Pipeline([
            ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
            ("scale", StandardScaler()),
            ("ridge", Ridge(alpha=alpha)),
        ])
        pipe.fit(X[:start], residual[:start])
        raw = pipe.predict(X[start:end])
        corr = np.clip(shrink * raw, -CORRECTION_CAP, CORRECTION_CAP)
        preds[start:end] = np.clip(pm[start:end] + corr, 0.001, 0.999)
        corr_out[start:end] = corr
        start = end
    mask = np.isfinite(preds)
    return preds, corr_out, mask


def auc_safe(y, p):
    return None if len(set(map(int, y))) < 2 else float(roc_auc_score(y, p))


def metric_block(y, p):
    y = np.asarray(y, dtype=int)
    p = np.asarray(p, dtype=float)
    return {
        "rows": int(len(y)),
        "brier": float(brier_score_loss(y, p)),
        "auc": auc_safe(y, p),
        "accuracy": float(np.mean((p >= 0.5).astype(int) == y)),
    }


def trade_metrics(rows, probs, mask, threshold):
    pnls, costs = [], []
    wins = 0
    for i, r in enumerate(rows):
        if not mask[i]:
            continue
        p = float(probs[i])
        ue = p - r["up_cost"]
        de = 1.0 - p - r["down_cost"]
        if max(ue, de) < threshold:
            continue
        if ue >= de:
            won, cost = r["y"] == 1, r["up_cost"]
        else:
            won, cost = r["y"] == 0, r["down_cost"]
        pnl = (1.0 - cost) if won else -cost
        pnls.append(pnl); costs.append(cost); wins += int(won)
    if not pnls:
        return {"threshold": threshold, "trades": 0, "wins": 0, "win_rate": None,
                "net_pnl": 0.0, "roi_on_cost": None, "mean_pnl": None}
    return {"threshold": threshold, "trades": len(pnls), "wins": wins,
            "win_rate": wins / len(pnls), "net_pnl": float(sum(pnls)),
            "roi_on_cost": float(sum(pnls) / sum(costs)) if sum(costs) else None,
            "mean_pnl": float(np.mean(pnls))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=r"data\snapshot_auditoria_v095.db")
    ap.add_argument("--cutoff", default=r"data\corte_desarrollo_v095.json")
    ap.add_argument("--out", default=r"data\benchmark_market_anchored_v095.json")
    args = ap.parse_args()

    cutoff_ms = load_cutoff(Path(args.cutoff))
    rows, fee_rate, slippage = load_rows(Path(args.db), cutoff_ms)
    if len(rows) < 100:
        raise SystemExit(f"ERROR: solo {len(rows)} filas; se esperaban ~161.")

    y_all = np.asarray([r["y"] for r in rows], dtype=int)
    pm_all = np.asarray([r["p_market"] for r in rows], dtype=float)
    po_all = np.asarray([r["p_old"] for r in rows], dtype=float)

    candidates = []
    for alpha in ALPHAS:
        for shrink in SHRINKS:
            p, corr, mask = expanding_oof(rows, alpha, shrink)
            y, pm, po, pc = y_all[mask], pm_all[mask], po_all[mask], p[mask]
            candidates.append({
                "alpha": alpha,
                "shrink": shrink,
                "oof_rows": int(mask.sum()),
                "candidate": metric_block(y, pc),
                "market_same_rows": metric_block(y, pm),
                "old_model_same_rows": metric_block(y, po),
                "brier_improvement_vs_market": float(brier_score_loss(y, pm) - brier_score_loss(y, pc)),
                "mean_abs_correction": float(np.mean(np.abs(corr[mask]))),
            })

    best = min(candidates, key=lambda z: (z["candidate"]["brier"], z["mean_abs_correction"], z["alpha"], z["shrink"]))
    p_best, corr_best, mask = expanding_oof(rows, best["alpha"], best["shrink"])
    y, pm, po, pc = y_all[mask], pm_all[mask], po_all[mask], p_best[mask]

    cand_dir, market_dir = pc >= 0.5, pm >= 0.5
    dis = cand_dir != market_dir
    disagreements = {
        "rows": int(dis.sum()),
        "candidate_accuracy": float(np.mean(cand_dir[dis] == y[dis])) if dis.any() else None,
        "market_accuracy": float(np.mean(market_dir[dis] == y[dis])) if dis.any() else None,
    }

    idx = np.where(mask)[0]
    twap_pairs = [(int(rows[i]["twap_distance"] >= 0), rows[i]["y"]) for i in idx if np.isfinite(rows[i]["twap_distance"])]
    twap_acc = float(np.mean([a == b for a, b in twap_pairs])) if twap_pairs else None
    trading = [trade_metrics(rows, p_best, mask, t) for t in EDGE_GRID]

    report = {
        "schema": "market_anchored_residual_benchmark_v095",
        "development_only": True,
        "warning": "No es forward independiente; solo diseño. Mercados posteriores al cutoff quedan reservados.",
        "development_cutoff_market_start_ms": cutoff_ms,
        "development_rows_total": len(rows),
        "feature_names": FEATURES + ["market_logit", "market_uncertainty"],
        "model_family": "Ridge residual: p_final = p_market + correccion",
        "correction_cap": CORRECTION_CAP,
        "candidate_grid": candidates,
        "selected_development_candidate": best,
        "selected_oof_metrics": {
            "candidate": metric_block(y, pc),
            "market": metric_block(y, pm),
            "old_transfer_model": metric_block(y, po),
            "brier_improvement_vs_market": float(brier_score_loss(y, pm) - brier_score_loss(y, pc)),
            "mean_abs_correction": float(np.mean(np.abs(corr_best[mask]))),
            "disagreement_candidate_vs_market": disagreements,
            "twap_sign_accuracy_same_oof_region": twap_acc,
        },
        "paper_trading_selected_candidate_oof": trading,
        "config": {"alphas": ALPHAS, "shrinks": SHRINKS, "edge_grid": EDGE_GRID,
                   "fee_rate": fee_rate, "slippage_per_share": slippage},
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    sm = report["selected_oof_metrics"]
    print("=" * 72)
    print("BENCHMARK v0.9.5 - MARKET ANCHORED RESIDUAL (DESARROLLO)")
    print("=" * 72)
    print("Filas desarrollo:", len(rows))
    print("Filas OOF:", int(mask.sum()))
    print("Alpha elegido:", best["alpha"])
    print("Shrink elegido:", best["shrink"])
    print()
    print("Brier candidato :", round(sm["candidate"]["brier"], 6))
    print("Brier mercado   :", round(sm["market"]["brier"], 6))
    print("Brier viejo     :", round(sm["old_transfer_model"]["brier"], 6))
    print("Mejora Brier vs mercado:", round(sm["brier_improvement_vs_market"], 6))
    print("AUC candidato   :", None if sm["candidate"]["auc"] is None else round(sm["candidate"]["auc"], 6))
    print("AUC mercado     :", None if sm["market"]["auc"] is None else round(sm["market"]["auc"], 6))
    print("Accuracy cand.  :", round(sm["candidate"]["accuracy"], 4))
    print("Accuracy mercado:", round(sm["market"]["accuracy"], 4))
    print("TWAP signo acc. :", None if sm["twap_sign_accuracy_same_oof_region"] is None else round(sm["twap_sign_accuracy_same_oof_region"], 4))
    print("Desacuerdos candidato/mercado:", disagreements["rows"])
    print("Acc candidato en desacuerdos:", disagreements["candidate_accuracy"])
    print("Acc mercado en desacuerdos  :", disagreements["market_accuracy"])
    print()
    print("PAPER OOF (solo desarrollo):")
    for t in trading:
        roi = "n/a" if t["roi_on_cost"] is None else f"{100*t['roi_on_cost']:.2f}%"
        print(f" edge {t['threshold']:.2f} | trades {t['trades']:3d} | PnL {t['net_pnl']:.5f} | ROI {roi}")
    print("Reporte:", out)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 72)


if __name__ == "__main__":
    main()
