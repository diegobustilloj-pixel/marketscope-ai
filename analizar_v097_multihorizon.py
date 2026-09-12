from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

DB_DEFAULT = DATA / "shadow_forward_twap_transfer_v094a.db"
PREREG = DATA / "prereg_v097_multihorizon.json"
IMPL_FREEZE = DATA / "prereg_v097_implementation.json"
OUT_JSON = DATA / "analisis_v097_multihorizon.json"

CUTOFF_MS = 1786394100000
HORIZONS = (120, 60, 30, 15)
MIN_TRAIN = 80
BLOCK_ROWS = 20
EDGE_GRID = (0.00, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10)

FEATURES = [
    "twap_distance_to_open_bps",
    "twap_minus_chainlink_bps",
    "chainlink_return_5s_bps",
    "chainlink_return_15s_bps",
    "chainlink_return_30s_bps",
    "chainlink_return_60s_bps",
    "chainlink_vol_30s_bps",
    "chainlink_vol_60s_bps",
    "chainlink_up_fraction_30s",
    "chainlink_state",
    "chainlink_state_run_length",
    "binance_return_5s_bps",
    "binance_return_15s_bps",
    "binance_return_30s_bps",
    "binance_return_60s_bps",
    "volatility_regime_ratio",
    "market_probability",
    "market_logit",
    "market_uncertainty",
]

MODEL_C = 0.1
MAX_ITER = 2000
ANCHOR_SHRINK = 0.25
MIN_TRADES = 10
MAX_BRIER_DELTA = 0.005

IMPLEMENTATION = {
    "schema": "prereg_v097_implementation",
    "purpose": "Congelar detalles operativos antes de calcular resultados multi-horizonte.",
    "common_population": (
        "Usar la interseccion de condition_id validos presentes en los cuatro "
        "horizontes 120/60/30/15, label_verified=1 y market_start_ms<=cutoff. "
        "Esto evita que un horizonte gane por usar una poblacion distinta."
    ),
    "market_probability": (
        "Usar exclusivamente feature_json.implied_up_mid_probability de cada "
        "horizonte. Si falta o no es finita, esa fila no es valida."
    ),
    "oof": {
        "type": "expanding_walk_forward",
        "min_train_rows": MIN_TRAIN,
        "block_rows": BLOCK_ROWS,
        "same_common_rows_all_horizons": True,
    },
    "model": {
        "type": "market_anchored_logistic",
        "C": MODEL_C,
        "max_iter": MAX_ITER,
        "solver": "lbfgs",
        "anchor_logit_shrink": ANCHOR_SHRINK,
        "imputer": "median+indicator",
        "scaler": "standard",
    },
    "costs": (
        "Por horizonte: ask observado + slippage_per_share de shadow_meta; "
        "fee = fee_rate * fill * (1-fill)."
    ),
    "threshold_selection_within_horizon": (
        "Evaluar grid congelado. Un threshold es elegible solo si tiene >=10 trades, "
        "net_pnl>0 y correlacion edge-PnL>0. Entre elegibles seleccionar max net_pnl; "
        "desempate por mayor mean_pnl y luego threshold mas alto."
    ),
    "horizon_gate": (
        "Horizon elegible si Brier candidato - Brier mercado <=0.005 y existe "
        "threshold elegible."
    ),
    "horizon_ranking": (
        "Entre horizontes elegibles: primero menor delta Brier candidato-mercado; "
        "desempate por mayor net_pnl del threshold seleccionado; luego horizonte "
        "mas temprano (mayor numero de segundos)."
    ),
    "future_data": "No leer ni usar market_start_ms posteriores al cutoff en --analyze.",
    "real_money": "BLOQUEADO",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_float(v):
    if v is None:
        return np.nan
    try:
        x = float(v)
        return x if math.isfinite(x) else np.nan
    except Exception:
        return np.nan


def clip_prob(p):
    return np.clip(np.asarray(p, dtype=float), 0.001, 0.999)


def logit(p):
    p = clip_prob(p)
    return np.log(p / (1.0 - p))


def sigmoid(z):
    z = np.clip(np.asarray(z, dtype=float), -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-z))


def verify_main_prereg():
    if not PREREG.exists():
        raise RuntimeError(f"Falta {PREREG}")
    d = load_json(PREREG)
    if int(d["development_cutoff_market_start_ms"]) != CUTOFF_MS:
        raise RuntimeError("Cutoff del preregistro no coincide.")
    if tuple(int(x) for x in d["horizons_seconds"]) != HORIZONS:
        raise RuntimeError("Horizontes del preregistro no coinciden.")
    if list(d["features"]) != FEATURES:
        raise RuntimeError("Features del preregistro no coinciden.")
    if tuple(float(x) for x in d["edge_grid_diagnostic"]) != EDGE_GRID:
        raise RuntimeError("Edge grid del preregistro no coincide.")
    if int(d["walk_forward"]["min_train_rows"]) != MIN_TRAIN:
        raise RuntimeError("min_train_rows no coincide.")
    if int(d["walk_forward"]["block_rows"]) != BLOCK_ROWS:
        raise RuntimeError("block_rows no coincide.")
    return d


def freeze_implementation():
    verify_main_prereg()
    if IMPL_FREEZE.exists():
        existing = load_json(IMPL_FREEZE)
        print("=" * 72)
        print("IMPLEMENTACION V0.9.7 YA CONGELADA - NO SE SOBRESCRIBE")
        print("=" * 72)
        print("Archivo:", IMPL_FREEZE)
        print("Creado:", existing.get("created_at"))
        print("DINERO REAL: BLOQUEADO")
        print("=" * 72)
        return

    payload = dict(IMPLEMENTATION)
    payload["created_at"] = now_utc()
    payload["parent_prereg"] = str(PREREG)
    payload["parent_prereg_sha256"] = sha256_file(PREREG)
    IMPL_FREEZE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 72)
    print("IMPLEMENTACION V0.9.7 CONGELADA")
    print("=" * 72)
    print("Poblacion: interseccion comun 120/60/30/15")
    print("Market probability: implied_up_mid_probability por horizonte")
    print("Threshold: >=10 trades + PnL>0 + corr(edge,PnL)>0")
    print("Ranking: delta Brier; luego PnL")
    print("Archivo:", IMPL_FREEZE)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 72)


def verify_impl():
    verify_main_prereg()
    if not IMPL_FREEZE.exists():
        raise RuntimeError(
            "Primero ejecute --freeze-implementation antes de analizar."
        )
    d = load_json(IMPL_FREEZE)
    if d.get("parent_prereg_sha256") != sha256_file(PREREG):
        raise RuntimeError(
            "El preregistro principal cambio despues de congelar implementacion."
        )
    return d


def open_ro(path: Path):
    uri = f"{path.resolve().as_uri()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def read_meta(con):
    out = {}
    for r in con.execute("SELECT key,value FROM shadow_meta"):
        try:
            out[str(r["key"])] = json.loads(r["value"])
        except Exception:
            out[str(r["key"])] = r["value"]
    return out


def extract_record(condition_id, market_start_ms, label, horizon, feature_json,
                   fee_rate, slippage):
    f = json.loads(feature_json)

    pm = safe_float(f.get("implied_up_mid_probability"))
    up_ask = safe_float(f.get("up_best_ask"))
    down_ask = safe_float(f.get("down_best_ask"))
    if not (math.isfinite(pm) and math.isfinite(up_ask) and math.isfinite(down_ask)):
        return None
    if not (0.0 < pm < 1.0):
        return None

    pm = float(np.clip(pm, 0.001, 0.999))

    vals = {}
    for k in FEATURES:
        if k == "market_probability":
            vals[k] = pm
        elif k == "market_logit":
            vals[k] = float(logit(pm))
        elif k == "market_uncertainty":
            vals[k] = pm * (1.0 - pm)
        else:
            vals[k] = safe_float(f.get(k))

    # Requisito de TWAP fresco cuando esos campos estan disponibles.
    if f.get("twap_30s_fresh") not in (1, True):
        return None
    if f.get("twap_open_fresh") not in (1, True):
        return None

    def cost(ask):
        fill = min(0.999, max(0.001, float(ask) + float(slippage)))
        fee = float(fee_rate) * fill * (1.0 - fill)
        return fill + fee

    return {
        "condition_id": str(condition_id),
        "market_start_ms": int(market_start_ms),
        "label": str(label),
        "y": 1 if str(label).lower() == "up" else 0,
        "horizon": int(horizon),
        "p_market": pm,
        "x": [vals[k] for k in FEATURES],
        "up_cost": cost(up_ask),
        "down_cost": cost(down_ask),
    }


def load_development(db: Path):
    con = open_ro(db)
    try:
        meta = read_meta(con)
        fee_rate = float(meta.get("fee_rate", 0.07))
        slippage = float(meta.get("slippage_per_share", 0.005))

        markets = {
            str(r["condition_id"]): (
                int(r["market_start_ms"]),
                str(r["label"]),
            )
            for r in con.execute(
                """
                SELECT condition_id,market_start_ms,label
                FROM shadow_markets
                WHERE label_verified=1
                  AND market_start_ms<=?
                ORDER BY market_start_ms,condition_id
                """,
                (CUTOFF_MS,),
            )
        }

        per_h = {h: {} for h in HORIZONS}

        for r in con.execute(
            """
            SELECT condition_id,horizon_seconds,feature_json
            FROM shadow_features
            WHERE horizon_seconds=60
              AND feature_json IS NOT NULL
            """
        ):
            cid = str(r["condition_id"])
            if cid not in markets:
                continue
            ms, label = markets[cid]
            rec = extract_record(
                cid, ms, label, 60, r["feature_json"], fee_rate, slippage
            )
            if rec is not None:
                per_h[60][cid] = rec

        for r in con.execute(
            """
            SELECT condition_id,horizon_seconds,feature_json
            FROM shadow_diagnostics
            WHERE status='SAVED'
              AND horizon_seconds IN (120,30,15)
              AND feature_json IS NOT NULL
            """
        ):
            h = int(r["horizon_seconds"])
            cid = str(r["condition_id"])
            if cid not in markets:
                continue
            ms, label = markets[cid]
            rec = extract_record(
                cid, ms, label, h, r["feature_json"], fee_rate, slippage
            )
            if rec is not None:
                per_h[h][cid] = rec
    finally:
        con.close()

    common = set(per_h[HORIZONS[0]])
    for h in HORIZONS[1:]:
        common &= set(per_h[h])

    common_ids = sorted(
        common,
        key=lambda cid: (
            per_h[60][cid]["market_start_ms"],
            cid,
        ),
    )

    rows_by_h = {
        h: [per_h[h][cid] for cid in common_ids]
        for h in HORIZONS
    }

    counts = {
        str(h): {
            "valid_before_intersection": len(per_h[h]),
            "common_rows": len(common_ids),
        }
        for h in HORIZONS
    }

    return rows_by_h, counts, {
        "fee_rate": fee_rate,
        "slippage_per_share": slippage,
    }


def build_model():
    return Pipeline(
        [
            (
                "imputer",
                SimpleImputer(
                    strategy="median",
                    add_indicator=True,
                    keep_empty_features=True,
                ),
            ),
            ("scale", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    C=MODEL_C,
                    max_iter=MAX_ITER,
                    solver="lbfgs",
                    random_state=970,
                ),
            ),
        ]
    )


def arrays(rows):
    X = np.asarray([r["x"] for r in rows], dtype=float)
    y = np.asarray([r["y"] for r in rows], dtype=int)
    pm = np.asarray([r["p_market"] for r in rows], dtype=float)
    return X, y, pm


def fit_predict(train_rows, test_rows):
    Xtr, ytr, _ = arrays(train_rows)
    Xte, _, pm = arrays(test_rows)
    model = build_model()
    model.fit(Xtr, ytr)
    rawp = clip_prob(model.predict_proba(Xte)[:, 1])
    anchored = logit(pm) + ANCHOR_SHRINK * (logit(rawp) - logit(pm))
    return clip_prob(sigmoid(anchored))


def expanding_oof(rows):
    n = len(rows)
    preds = np.full(n, np.nan, dtype=float)
    start = MIN_TRAIN
    while start < n:
        end = min(n, start + BLOCK_ROWS)
        preds[start:end] = fit_predict(rows[:start], rows[start:end])
        start = end
    mask = np.isfinite(preds)
    return preds, mask


def auc_safe(y, p):
    y = np.asarray(y, dtype=int)
    if len(np.unique(y)) < 2:
        return None
    return float(roc_auc_score(y, p))


def prob_metrics(y, p):
    y = np.asarray(y, dtype=int)
    p = clip_prob(p)
    return {
        "rows": int(len(y)),
        "brier": float(brier_score_loss(y, p)),
        "auc": auc_safe(y, p),
        "accuracy": float(np.mean((p >= 0.5).astype(int) == y)),
    }


def make_trades(rows, probs, threshold):
    out = []
    for r, p in zip(rows, probs):
        p = float(p)
        up_edge = p - r["up_cost"]
        down_edge = 1.0 - p - r["down_cost"]
        edge = max(up_edge, down_edge)
        if edge < threshold:
            continue

        if up_edge >= down_edge:
            won = r["y"] == 1
            cost = r["up_cost"]
            side = "Up"
        else:
            won = r["y"] == 0
            cost = r["down_cost"]
            side = "Down"

        pnl = (1.0 - cost) if won else -cost
        out.append(
            {
                "side": side,
                "edge": float(edge),
                "cost": float(cost),
                "pnl": float(pnl),
                "won": bool(won),
            }
        )
    return out


def corr_safe(x, y):
    if len(x) < 2:
        return None
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if float(np.std(x)) == 0.0 or float(np.std(y)) == 0.0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def trade_metrics(trades, threshold):
    if not trades:
        return {
            "threshold": float(threshold),
            "trades": 0,
            "wins": 0,
            "win_rate": None,
            "net_pnl": 0.0,
            "roi_on_cost": None,
            "mean_pnl": None,
            "edge_pnl_correlation": None,
            "eligible": False,
        }
    pnl = [t["pnl"] for t in trades]
    cost = [t["cost"] for t in trades]
    edge = [t["edge"] for t in trades]
    corr = corr_safe(edge, pnl)
    m = {
        "threshold": float(threshold),
        "trades": len(trades),
        "wins": sum(int(t["won"]) for t in trades),
        "win_rate": float(np.mean([t["won"] for t in trades])),
        "net_pnl": float(sum(pnl)),
        "roi_on_cost": float(sum(pnl) / sum(cost)) if sum(cost) else None,
        "mean_pnl": float(np.mean(pnl)),
        "edge_pnl_correlation": corr,
    }
    m["eligible"] = bool(
        m["trades"] >= MIN_TRADES
        and m["net_pnl"] > 0
        and corr is not None
        and corr > 0
    )
    return m


def analyze(db: Path):
    verify_impl()
    if OUT_JSON.exists():
        report = load_json(OUT_JSON)
        print("=" * 72)
        print("ANALISIS V0.9.7 YA EXISTE - NO SE SOBRESCRIBE")
        print("=" * 72)
        print("Ganador:", report.get("selected_horizon"))
        print("Veredicto:", report.get("verdict"))
        print("Archivo:", OUT_JSON)
        print("DINERO REAL: BLOQUEADO")
        print("=" * 72)
        return

    rows_by_h, population, costs = load_development(db)
    common_n = len(rows_by_h[60])
    if common_n <= MIN_TRAIN:
        raise RuntimeError(
            f"Solo hay {common_n} filas comunes; se necesitan mas de {MIN_TRAIN}."
        )

    results = {}

    for h in HORIZONS:
        rows = rows_by_h[h]
        pred, mask = expanding_oof(rows)
        oof_rows = [r for r, keep in zip(rows, mask) if keep]
        p = pred[mask]
        y = np.asarray([r["y"] for r in oof_rows], dtype=int)
        pm = np.asarray([r["p_market"] for r in oof_rows], dtype=float)

        cand = prob_metrics(y, p)
        market = prob_metrics(y, pm)
        delta = cand["brier"] - market["brier"]

        grid = []
        for t in EDGE_GRID:
            grid.append(
                trade_metrics(make_trades(oof_rows, p, float(t)), float(t))
            )

        eligible_thresholds = [m for m in grid if m["eligible"]]
        chosen = None
        if eligible_thresholds:
            chosen = max(
                eligible_thresholds,
                key=lambda m: (
                    m["net_pnl"],
                    m["mean_pnl"],
                    m["threshold"],
                ),
            )

        horizon_eligible = bool(
            delta <= MAX_BRIER_DELTA and chosen is not None
        )

        results[str(h)] = {
            "horizon_seconds": h,
            "common_development_rows": common_n,
            "oof_rows": int(mask.sum()),
            "candidate": cand,
            "market": market,
            "brier_delta_candidate_minus_market": float(delta),
            "threshold_grid": grid,
            "selected_threshold": None if chosen is None else chosen["threshold"],
            "selected_trade_metrics": chosen,
            "horizon_eligible": horizon_eligible,
        }

    eligible_h = [r for r in results.values() if r["horizon_eligible"]]
    selected = None
    if eligible_h:
        selected = min(
            eligible_h,
            key=lambda r: (
                r["brier_delta_candidate_minus_market"],
                -r["selected_trade_metrics"]["net_pnl"],
                -r["horizon_seconds"],
            ),
        )["horizon_seconds"]

    verdict = (
        "CANDIDATE_HORIZON_FOUND_DEVELOPMENT_ONLY"
        if selected is not None
        else "FAIL_ALL_HORIZONS_DEVELOPMENT"
    )

    report = {
        "schema": "analisis_v097_multihorizon",
        "created_at": now_utc(),
        "development_only": True,
        "cutoff_market_start_ms": CUTOFF_MS,
        "common_population_rows": common_n,
        "population_counts": population,
        "costs": costs,
        "results": results,
        "selected_horizon": selected,
        "verdict": verdict,
        "warning": (
            "Resultado exclusivamente de desarrollo OOF hasta el cutoff. "
            "No usar como evidencia forward ni habilitar dinero real."
        ),
        "money_real": "BLOQUEADO",
        "prereg_sha256": sha256_file(PREREG),
        "implementation_sha256": sha256_file(IMPL_FREEZE),
    }

    OUT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 88)
    print("V0.9.7 MULTI-HORIZON - DESARROLLO OOF")
    print("=" * 88)
    print("Filas comunes:", common_n)
    print("OOF por horizonte:", common_n - MIN_TRAIN)
    print()
    print(" H | Brier cand | Brier mkt | Delta    | AUC cand | Acc cand | Thr | Trades | PnL")
    print("-" * 88)
    for h in HORIZONS:
        r = results[str(h)]
        tm = r["selected_trade_metrics"]
        if tm is None:
            thr, trades, pnl = "None", 0, 0.0
        else:
            thr, trades, pnl = f"{tm['threshold']:.2f}", tm["trades"], tm["net_pnl"]
        auc = r["candidate"]["auc"]
        auc_txt = "n/a" if auc is None else f"{auc:.4f}"
        print(
            f"{h:3d} | "
            f"{r['candidate']['brier']:.6f}  | "
            f"{r['market']['brier']:.6f} | "
            f"{r['brier_delta_candidate_minus_market']:+.6f} | "
            f"{auc_txt:8s} | "
            f"{r['candidate']['accuracy']:.4f}   | "
            f"{thr:4s} | "
            f"{trades:6d} | "
            f"{pnl:+.5f}"
        )
    print()
    print("HORIZONTE SELECCIONADO:", selected)
    print("VEREDICTO:", verdict)
    print("Reporte:", OUT_JSON)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 88)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_DEFAULT))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--freeze-implementation", action="store_true")
    g.add_argument("--analyze", action="store_true")
    args = ap.parse_args()

    if args.freeze_implementation:
        freeze_implementation()
        return

    db = Path(args.db).expanduser().resolve()
    if not db.exists():
        raise SystemExit(f"No existe DB: {db}")
    analyze(db)


if __name__ == "__main__":
    main()
