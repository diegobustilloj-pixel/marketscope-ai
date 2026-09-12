from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

DB_DEFAULT = DATA / "shadow_forward_twap_transfer_v094a.db"
PREREG = DATA / "prereg_v099_meta_label.json"
IMPL = DATA / "prereg_v099_implementation.json"
PREPARE_JSON = DATA / "prepare_v099_meta_label.json"
MODEL_FILE = DATA / "modelo_v099_meta_label.joblib"

CUTOFF_MS = 1786394100000
DEV_POST = 50
DEV_EXPECTED = 211

GEN_MIN_TRAIN = 100
GEN_BLOCK = 25
META_MIN_TRAIN = 50
META_BLOCK = 20
META_GRID = (0.50, 0.55, 0.60, 0.65, 0.70)

MIN_TRADES = 15
MAX_POS_SHARE = 0.35

GEN_FEATURES = [
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
    "implied_up_mid_probability",
    "up_cost",
    "down_cost",
]

META_FEATURES = [
    "ridge_pred_up",
    "ridge_pred_down",
    "ridge_best_pnl",
    "ridge_margin",
    "hgb_pred_up",
    "hgb_pred_down",
    "hgb_best_pnl",
    "hgb_margin",
    "ridge_hgb_side_agreement",
    "implied_up_mid_probability",
    "chosen_side_cost",
    "twap_distance_to_open_bps",
    "twap_minus_chainlink_bps",
    "chainlink_return_15s_bps",
    "chainlink_return_30s_bps",
    "chainlink_vol_30s_bps",
]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def loadj(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sf(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else np.nan
    except Exception:
        return np.nan


def verify_prereg():
    if not PREREG.exists():
        raise RuntimeError(f"Falta {PREREG}")
    d = loadj(PREREG)
    if int(d["development_rows"]) != DEV_EXPECTED:
        raise RuntimeError("development_rows != 211")
    if list(d["future_test1_post_cutoff_rows"]) != [51, 150]:
        raise RuntimeError("TEST1 != 51-150")
    if int(d["future_test1_rows"]) != 100:
        raise RuntimeError("future_test1_rows != 100")
    if int(d["horizon_seconds"]) != 60:
        raise RuntimeError("horizon_seconds != 60")
    if list(d["meta_features"]) != META_FEATURES:
        raise RuntimeError("Meta features no coinciden")
    if tuple(float(x) for x in d["trade_probability_threshold_grid"]) != META_GRID:
        raise RuntimeError("Grid meta no coincide")
    if int(d["generator_walk_forward"]["min_train_rows"]) != GEN_MIN_TRAIN:
        raise RuntimeError("generator min_train no coincide")
    if int(d["generator_walk_forward"]["block_rows"]) != GEN_BLOCK:
        raise RuntimeError("generator block no coincide")
    if int(d["meta_walk_forward"]["minimum_meta_train_rows"]) != META_MIN_TRAIN:
        raise RuntimeError("meta min_train no coincide")
    if int(d["meta_walk_forward"]["block_rows"]) != META_BLOCK:
        raise RuntimeError("meta block no coincide")
    return d


def freeze_impl():
    verify_prereg()
    if IMPL.exists():
        print("=" * 74)
        print("IMPLEMENTACION V0.9.9 YA CONGELADA - NO SE SOBRESCRIBE")
        print("=" * 74)
        print("ARCHIVO:", IMPL)
        print("DINERO REAL: BLOQUEADO")
        print("=" * 74)
        return

    d = {
        "schema": "prereg_v099_implementation",
        "created_at": now(),
        "parent_prereg_sha256": sha(PREREG),
        "development_population": (
            "Exactamente 161 filas base <= cutoff + primeras 50 filas validas "
            "post-cutoff. Filas post-cutoff 51+ prohibidas en prepare."
        ),
        "generator_features": GEN_FEATURES,
        "generator_models": {
            "ridge": {
                "target_up": "y_up-up_cost",
                "target_down": "1-y_up-down_cost",
                "alpha": 100.0,
                "two_independent_regressors": True,
                "imputer": "median+indicator+keep_empty_features",
                "scaler": "standard",
            },
            "hgb": {
                "target_up": "y_up-up_cost",
                "target_down": "1-y_up-down_cost",
                "learning_rate": 0.05,
                "max_iter": 100,
                "max_depth": 2,
                "min_samples_leaf": 20,
                "l2_regularization": 5.0,
                "random_state": 990,
                "two_independent_regressors": True,
            },
        },
        "generator_oof": {
            "type": "expanding",
            "min_train_rows": GEN_MIN_TRAIN,
            "block_rows": GEN_BLOCK,
            "expected_generator_oof_rows": DEV_EXPECTED - GEN_MIN_TRAIN,
        },
        "ridge_side_rule": "Up si ridge_pred_up >= ridge_pred_down; si no Down",
        "meta_target": (
            "1 si realized_pnl del lado elegido por Ridge es >0; "
            "0 si realized_pnl<=0"
        ),
        "meta_features": META_FEATURES,
        "meta_model": {
            "type": "LogisticRegression",
            "C": 0.1,
            "solver": "lbfgs",
            "max_iter": 2000,
            "random_state": 990,
            "imputer": "median+indicator+keep_empty_features",
            "scaler": "standard",
        },
        "meta_oof": {
            "input_population": (
                "Solo filas con predicciones OOF de Ridge y HGB; "
                "ninguna prediccion in-sample entra al meta modelo."
            ),
            "type": "expanding",
            "minimum_meta_train_rows": META_MIN_TRAIN,
            "block_rows": META_BLOCK,
            "expected_meta_oof_rows": (DEV_EXPECTED - GEN_MIN_TRAIN) - META_MIN_TRAIN,
        },
        "threshold_selection": (
            "Sobre meta-OOF: operar lado elegido por Ridge si P(meta_target=1)>=threshold. "
            "Threshold elegible si trades>=15, net_pnl>0, ROI>0, PnL primera mitad "
            "cronologica>=0, PnL segunda mitad cronologica>=0 y "
            "largest_positive_trade_share<=0.35. Elegir mayor net_pnl; "
            "desempate mayor mean_pnl y luego threshold mayor."
        ),
        "final_fit_for_future_test": (
            "Despues de seleccionar threshold: entrenar Ridge/HGB generadores con las "
            "211 filas completas. Entrenar meta modelo usando exclusivamente las 111 "
            "filas de features generadas OOF por generadores. Congelar todo antes "
            "de TEST1 51-150."
        ),
        "real_money": "BLOQUEADO",
    }
    IMPL.write_text(
        json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print("=" * 74)
    print("IMPLEMENTACION V0.9.9 CONGELADA")
    print("=" * 74)
    print("GENERADORES: Ridge + HGB PnL OOF")
    print("META MODELO: LogisticRegression OPERAR / NO OPERAR")
    print("META OOF ESPERADO:", d["meta_oof"]["expected_meta_oof_rows"])
    print("TEST1 51-150: RESERVADO")
    print("ARCHIVO:", IMPL)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 74)


def verify_impl():
    verify_prereg()
    if not IMPL.exists():
        raise RuntimeError("Primero ejecute --freeze-implementation")
    if loadj(IMPL)["parent_prereg_sha256"] != sha(PREREG):
        raise RuntimeError("Preregistro cambió después de congelar implementación")


def open_ro(path: Path):
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
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


def taker_cost(ask, fee_rate, slip):
    fill = min(0.999, max(0.001, float(ask) + float(slip)))
    fee = float(fee_rate) * fill * (1.0 - fill)
    return fill + fee


def parse_row(row, fee_rate, slip):
    f = json.loads(row["feature_json"])
    if f.get("twap_30s_fresh") not in (1, True):
        return None
    if f.get("twap_open_fresh") not in (1, True):
        return None

    ua = sf(f.get("up_best_ask"))
    da = sf(f.get("down_best_ask"))
    pm = sf(f.get("implied_up_mid_probability"))
    if not all(math.isfinite(x) for x in (ua, da, pm)):
        return None

    uc = taker_cost(ua, fee_rate, slip)
    dc = taker_cost(da, fee_rate, slip)
    y = 1 if str(row["label"]).lower() == "up" else 0

    raw = {
        "implied_up_mid_probability": pm,
        "twap_distance_to_open_bps": sf(f.get("twap_distance_to_open_bps")),
        "twap_minus_chainlink_bps": sf(f.get("twap_minus_chainlink_bps")),
        "chainlink_return_15s_bps": sf(f.get("chainlink_return_15s_bps")),
        "chainlink_return_30s_bps": sf(f.get("chainlink_return_30s_bps")),
        "chainlink_vol_30s_bps": sf(f.get("chainlink_vol_30s_bps")),
    }

    gen_x = []
    for k in GEN_FEATURES:
        if k == "up_cost":
            gen_x.append(uc)
        elif k == "down_cost":
            gen_x.append(dc)
        else:
            gen_x.append(sf(f.get(k)))

    return {
        "condition_id": str(row["condition_id"]),
        "market_start_ms": int(row["market_start_ms"]),
        "y": y,
        "gen_x": gen_x,
        "raw": raw,
        "up_cost": float(uc),
        "down_cost": float(dc),
        "up_realized_pnl": float(y - uc),
        "down_realized_pnl": float((1 - y) - dc),
    }


def load_rows(db: Path):
    con = open_ro(db)
    try:
        m = read_meta(con)
        fee = float(m.get("fee_rate", 0.07))
        slip = float(m.get("slippage_per_share", 0.005))
        q = con.execute(
            """
            SELECT m.condition_id,m.market_start_ms,m.label,f.feature_json
            FROM shadow_features f
            JOIN shadow_markets m ON m.condition_id=f.condition_id
            WHERE f.horizon_seconds=60
              AND f.feature_json IS NOT NULL
              AND m.label_verified=1
            ORDER BY m.market_start_ms,m.condition_id
            """
        ).fetchall()
    finally:
        con.close()

    out = []
    for r in q:
        x = parse_row(r, fee, slip)
        if x is not None:
            out.append(x)
    return out, {"fee_rate": fee, "slippage_per_share": slip}


def dev_split(rows):
    base = [r for r in rows if r["market_start_ms"] <= CUTOFF_MS]
    future = [r for r in rows if r["market_start_ms"] > CUTOFF_MS]
    dev = base + future[:DEV_POST]
    if len(dev) != DEV_EXPECTED:
        raise RuntimeError(
            f"Esperadas 211 filas; hay {len(dev)} "
            f"(base={len(base)}, post50={min(50,len(future))})"
        )
    return dev, base, future


def gen_arrays(rows):
    X = np.asarray([r["gen_x"] for r in rows], float)
    up = np.asarray([r["up_realized_pnl"] for r in rows], float)
    dn = np.asarray([r["down_realized_pnl"] for r in rows], float)
    return X, up, dn


def build_gen_pair(kind):
    if kind == "ridge":
        def one():
            return Pipeline([
                ("imputer", SimpleImputer(
                    strategy="median", add_indicator=True, keep_empty_features=True
                )),
                ("scale", StandardScaler()),
                ("model", Ridge(alpha=100.0)),
            ])
    elif kind == "hgb":
        def one():
            return Pipeline([
                ("imputer", SimpleImputer(
                    strategy="median", add_indicator=True, keep_empty_features=True
                )),
                ("model", HistGradientBoostingRegressor(
                    learning_rate=0.05,
                    max_iter=100,
                    max_depth=2,
                    min_samples_leaf=20,
                    l2_regularization=5.0,
                    random_state=990,
                )),
            ])
    else:
        raise ValueError(kind)
    return one(), one()


def fit_gen(kind, rows):
    X, up, dn = gen_arrays(rows)
    um, dm = build_gen_pair(kind)
    um.fit(X, up)
    dm.fit(X, dn)
    return {"kind": kind, "up_model": um, "down_model": dm}


def pred_gen(bundle, rows):
    X, _, _ = gen_arrays(rows)
    return (
        np.asarray(bundle["up_model"].predict(X), float),
        np.asarray(bundle["down_model"].predict(X), float),
    )


def generator_oof(kind, rows):
    n = len(rows)
    pu = np.full(n, np.nan)
    pd = np.full(n, np.nan)
    start = GEN_MIN_TRAIN
    while start < n:
        end = min(n, start + GEN_BLOCK)
        b = fit_gen(kind, rows[:start])
        u, d = pred_gen(b, rows[start:end])
        pu[start:end] = u
        pd[start:end] = d
        start = end
    return pu, pd


def meta_row(base_row, ru, rd, hu, hd):
    ridge_side_up = ru >= rd
    hgb_side_up = hu >= hd
    chosen_cost = base_row["up_cost"] if ridge_side_up else base_row["down_cost"]
    realized = (
        base_row["up_realized_pnl"]
        if ridge_side_up
        else base_row["down_realized_pnl"]
    )
    mf = {
        "ridge_pred_up": float(ru),
        "ridge_pred_down": float(rd),
        "ridge_best_pnl": float(max(ru, rd)),
        "ridge_margin": float(abs(ru - rd)),
        "hgb_pred_up": float(hu),
        "hgb_pred_down": float(hd),
        "hgb_best_pnl": float(max(hu, hd)),
        "hgb_margin": float(abs(hu - hd)),
        "ridge_hgb_side_agreement": float(ridge_side_up == hgb_side_up),
        "implied_up_mid_probability": base_row["raw"]["implied_up_mid_probability"],
        "chosen_side_cost": float(chosen_cost),
        "twap_distance_to_open_bps": base_row["raw"]["twap_distance_to_open_bps"],
        "twap_minus_chainlink_bps": base_row["raw"]["twap_minus_chainlink_bps"],
        "chainlink_return_15s_bps": base_row["raw"]["chainlink_return_15s_bps"],
        "chainlink_return_30s_bps": base_row["raw"]["chainlink_return_30s_bps"],
        "chainlink_vol_30s_bps": base_row["raw"]["chainlink_vol_30s_bps"],
    }
    return {
        "condition_id": base_row["condition_id"],
        "market_start_ms": base_row["market_start_ms"],
        "x": [mf[k] for k in META_FEATURES],
        "target": int(realized > 0),
        "realized_pnl": float(realized),
        "cost": float(chosen_cost),
        "chosen_side": "Up" if ridge_side_up else "Down",
    }


def make_meta_oof_dataset(dev):
    ru, rd = generator_oof("ridge", dev)
    hu, hd = generator_oof("hgb", dev)
    rows = []
    for i in range(len(dev)):
        if not all(math.isfinite(float(x)) for x in (ru[i], rd[i], hu[i], hd[i])):
            continue
        rows.append(meta_row(dev[i], ru[i], rd[i], hu[i], hd[i]))
    return rows


def build_meta():
    return Pipeline([
        ("imputer", SimpleImputer(
            strategy="median", add_indicator=True, keep_empty_features=True
        )),
        ("scale", StandardScaler()),
        ("model", LogisticRegression(
            C=0.1,
            solver="lbfgs",
            max_iter=2000,
            random_state=990,
        )),
    ])


def meta_arrays(rows):
    X = np.asarray([r["x"] for r in rows], float)
    y = np.asarray([r["target"] for r in rows], int)
    return X, y


def meta_oof(rows):
    n = len(rows)
    p = np.full(n, np.nan)
    start = META_MIN_TRAIN
    while start < n:
        end = min(n, start + META_BLOCK)
        Xtr, ytr = meta_arrays(rows[:start])
        Xte, _ = meta_arrays(rows[start:end])
        if len(np.unique(ytr)) < 2:
            raise RuntimeError("Meta train quedó con una sola clase.")
        m = build_meta()
        m.fit(Xtr, ytr)
        p[start:end] = m.predict_proba(Xte)[:, 1]
        start = end
    mask = np.isfinite(p)
    return p, mask


def trade_metrics(rows, probs, threshold):
    selected = [
        (r, float(p))
        for r, p in zip(rows, probs)
        if float(p) >= threshold
    ]
    if not selected:
        return {
            "threshold": threshold,
            "trades": 0,
            "wins": 0,
            "net_pnl": 0.0,
            "roi_on_cost": None,
            "mean_pnl": None,
            "largest_positive_trade_share": None,
            "first_half_net_pnl": 0.0,
            "second_half_net_pnl": 0.0,
            "eligible": False,
        }

    pnl = np.asarray([r["realized_pnl"] for r, _ in selected], float)
    costs = np.asarray([r["cost"] for r, _ in selected], float)
    positive = pnl[pnl > 0]
    pos_total = float(positive.sum()) if len(positive) else 0.0
    largest = float(positive.max()) if len(positive) else 0.0
    share = largest / pos_total if pos_total > 0 else None

    # Mitades cronológicas sobre la ventana meta-OOF, no sobre número de trades.
    midpoint_ms = rows[len(rows) // 2]["market_start_ms"]
    first_pnl = sum(
        r["realized_pnl"]
        for r, p in selected
        if r["market_start_ms"] < midpoint_ms
    )
    second_pnl = sum(
        r["realized_pnl"]
        for r, p in selected
        if r["market_start_ms"] >= midpoint_ms
    )

    roi = float(pnl.sum() / costs.sum()) if costs.sum() else None
    eligible = bool(
        len(selected) >= MIN_TRADES
        and pnl.sum() > 0
        and roi is not None and roi > 0
        and first_pnl >= 0
        and second_pnl >= 0
        and share is not None
        and share <= MAX_POS_SHARE
    )
    return {
        "threshold": float(threshold),
        "trades": len(selected),
        "wins": int(np.sum(pnl > 0)),
        "win_rate": float(np.mean(pnl > 0)),
        "net_pnl": float(pnl.sum()),
        "roi_on_cost": roi,
        "mean_pnl": float(pnl.mean()),
        "largest_positive_trade_share": share,
        "first_half_net_pnl": float(first_pnl),
        "second_half_net_pnl": float(second_pnl),
        "eligible": eligible,
    }


def prepare(db: Path):
    verify_impl()

    if PREPARE_JSON.exists() or MODEL_FILE.exists():
        if PREPARE_JSON.exists() and MODEL_FILE.exists():
            r = loadj(PREPARE_JSON)
            print("=" * 78)
            print("V0.9.9 PREPARE YA CONGELADO - NO SE SOBRESCRIBE")
            print("=" * 78)
            print("THRESHOLD:", r.get("selected_threshold"))
            print("VEREDICTO:", r.get("verdict"))
            print("DINERO REAL: BLOQUEADO")
            print("=" * 78)
            return
        raise RuntimeError("Existe solo uno de PREPARE/MODEL; revisar sin borrar.")

    all_rows, costs = load_rows(db)
    dev, base, future = dev_split(all_rows)

    meta_dev = make_meta_oof_dataset(dev)
    if len(meta_dev) != DEV_EXPECTED - GEN_MIN_TRAIN:
        raise RuntimeError(
            f"Meta dataset OOF esperado 111; obtenido {len(meta_dev)}."
        )

    probs, mask = meta_oof(meta_dev)
    oof_rows = [r for r, keep in zip(meta_dev, mask) if keep]
    oof_probs = probs[mask]

    grid = [
        trade_metrics(oof_rows, oof_probs, float(t))
        for t in META_GRID
    ]
    eligible = [m for m in grid if m["eligible"]]
    chosen = (
        max(
            eligible,
            key=lambda m: (
                m["net_pnl"],
                m["mean_pnl"],
                m["threshold"],
            ),
        )
        if eligible
        else None
    )

    selected_threshold = None if chosen is None else chosen["threshold"]
    verdict = (
        "CANDIDATE_FOUND_DEVELOPMENT_ONLY"
        if chosen is not None
        else "FAIL_META_LABEL_DEVELOPMENT"
    )

    # Fit final para futuro: generadores con 211; meta con generator-OOF 111.
    final_ridge = fit_gen("ridge", dev)
    final_hgb = fit_gen("hgb", dev)
    Xmeta, ymeta = meta_arrays(meta_dev)
    if len(np.unique(ymeta)) < 2:
        raise RuntimeError("Meta target final tiene una sola clase.")
    final_meta = build_meta()
    final_meta.fit(Xmeta, ymeta)

    bundle = {
        "schema": "modelo_v099_meta_label",
        "created_at": now(),
        "development_rows": len(dev),
        "generator_oof_meta_rows": len(meta_dev),
        "selected_threshold": selected_threshold,
        "generator_features": GEN_FEATURES,
        "meta_features": META_FEATURES,
        "ridge_generator": final_ridge,
        "hgb_generator": final_hgb,
        "meta_model": final_meta,
        "prereg_sha256": sha(PREREG),
        "implementation_sha256": sha(IMPL),
    }
    joblib.dump(bundle, MODEL_FILE)

    report = {
        "schema": "prepare_v099_meta_label",
        "created_at": now(),
        "development_rows": len(dev),
        "base_rows": len(base),
        "post_cutoff_rows_used_for_development": 50,
        "future_rows_51plus_existing_but_not_used": max(0, len(future) - 50),
        "generator_oof_rows": len(meta_dev),
        "meta_oof_rows": int(mask.sum()),
        "meta_target_positive_rate_generator_oof": float(
            np.mean([r["target"] for r in meta_dev])
        ),
        "meta_target_positive_rate_meta_oof_region": float(
            np.mean([r["target"] for r in oof_rows])
        ),
        "threshold_grid": grid,
        "selected_threshold": selected_threshold,
        "selected_metrics": chosen,
        "verdict": verdict,
        "model_file": str(MODEL_FILE),
        "model_sha256": sha(MODEL_FILE),
        "costs": costs,
        "warning": (
            "Solo desarrollo. No se usaron filas post-cutoff 51+. "
            "Dinero real bloqueado."
        ),
        "money_real": "BLOQUEADO",
    }
    PREPARE_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 94)
    print("V0.9.9 META-LABEL - PREPARE OOF")
    print("=" * 94)
    print("Desarrollo:", len(dev), "(161 base + primeras 50 post-cutoff)")
    print("Generator OOF para meta:", len(meta_dev))
    print("Meta OOF evaluable:", int(mask.sum()))
    print("Filas futuras 51+ existentes pero NO usadas:", max(0, len(future) - 50))
    print()
    for m in grid:
        print(
            f"thr={m['threshold']:.2f} | trades={m['trades']:3d} | "
            f"PnL={m['net_pnl']:+.5f} | ROI={m['roi_on_cost']} | "
            f"h1={m['first_half_net_pnl']:+.5f} | "
            f"h2={m['second_half_net_pnl']:+.5f} | "
            f"share={m['largest_positive_trade_share']} | "
            f"eligible={m['eligible']}"
        )
    print()
    print("THRESHOLD SELECCIONADO:", selected_threshold)
    print("VEREDICTO:", verdict)
    print("Modelo:", MODEL_FILE)
    print("Reporte:", PREPARE_JSON)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 94)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_DEFAULT))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--freeze-implementation", action="store_true")
    g.add_argument("--prepare", action="store_true")
    args = ap.parse_args()

    if args.freeze_implementation:
        freeze_impl()
        return

    db = Path(args.db).expanduser().resolve()
    if not db.exists():
        raise SystemExit(f"No existe DB: {db}")
    prepare(db)


if __name__ == "__main__":
    main()
