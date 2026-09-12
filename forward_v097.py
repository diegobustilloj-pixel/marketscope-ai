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
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

DB_DEFAULT = DATA / "shadow_forward_twap_transfer_v094a.db"

PREREG_MULTI = DATA / "prereg_v097_multihorizon.json"
PREREG_IMPL = DATA / "prereg_v097_implementation.json"
PREREG_FWD = DATA / "prereg_v097_forward.json"

MODEL_FILE = DATA / "modelo_forward_v097_60s.joblib"
MODEL_META = DATA / "modelo_forward_v097_60s.json"
FORWARD_RESULT = DATA / "forward50_v097.json"

CUTOFF_MS = 1786394100000
HORIZON = 60
FORWARD_ROWS = 50
THRESHOLD = 0.0

MODEL_C = 0.1
MAX_ITER = 2000
ANCHOR_SHRINK = 0.25

MAX_BRIER_DELTA = 0.005
MIN_TRADES = 10

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


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path):
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


def verify_prereg():
    for p in (PREREG_MULTI, PREREG_IMPL, PREREG_FWD):
        if not p.exists():
            raise RuntimeError(f"Falta archivo congelado: {p}")

    multi = load_json(PREREG_MULTI)
    impl = load_json(PREREG_IMPL)
    fwd = load_json(PREREG_FWD)

    if int(fwd["selected_horizon_seconds"]) != HORIZON:
        raise RuntimeError("Horizonte forward no coincide con 60s.")
    if str(fwd["selected_model"]) != "market_anchored_logistic":
        raise RuntimeError("Modelo forward no coincide.")
    if float(fwd["frozen_threshold"]) != THRESHOLD:
        raise RuntimeError("Threshold forward no coincide con 0.0.")
    if int(fwd["development_cutoff_market_start_ms"]) != CUTOFF_MS:
        raise RuntimeError("Cutoff forward no coincide.")
    if int(fwd["forward_rows"]) != FORWARD_ROWS:
        raise RuntimeError("Forward rows no coincide con 50.")
    if list(multi["features"]) != FEATURES:
        raise RuntimeError("Features no coinciden con prereg_v097_multihorizon.json.")

    return multi, impl, fwd


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


def taker_cost(ask: float, fee_rate: float, slippage: float):
    fill = min(0.999, max(0.001, float(ask) + float(slippage)))
    fee = float(fee_rate) * fill * (1.0 - fill)
    return fill + fee


def parse_feature_record(row, fee_rate, slippage):
    f = json.loads(row["feature_json"])

    if f.get("twap_30s_fresh") not in (1, True):
        return None
    if f.get("twap_open_fresh") not in (1, True):
        return None

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

    return {
        "condition_id": str(row["condition_id"]),
        "market_start_ms": int(row["market_start_ms"]),
        "y": 1 if str(row["label"]).lower() == "up" else 0,
        "p_market": pm,
        "x": [vals[k] for k in FEATURES],
        "up_cost": taker_cost(up_ask, fee_rate, slippage),
        "down_cost": taker_cost(down_ask, fee_rate, slippage),
    }


def load_valid_60s(db: Path):
    con = open_ro(db)
    try:
        meta = read_meta(con)
        fee_rate = float(meta.get("fee_rate", 0.07))
        slippage = float(meta.get("slippage_per_share", 0.005))

        rows = con.execute(
            """
            SELECT
                m.condition_id,
                m.market_start_ms,
                m.label,
                f.feature_json
            FROM shadow_features f
            JOIN shadow_markets m
              ON m.condition_id=f.condition_id
            WHERE f.horizon_seconds=60
              AND f.feature_json IS NOT NULL
              AND m.label_verified=1
            ORDER BY m.market_start_ms, m.condition_id
            """
        ).fetchall()
    finally:
        con.close()

    out = []
    for row in rows:
        rec = parse_feature_record(row, fee_rate, slippage)
        if rec is not None:
            out.append(rec)

    return out, {
        "fee_rate": fee_rate,
        "slippage_per_share": slippage,
    }


def arrays(rows):
    X = np.asarray([r["x"] for r in rows], dtype=float)
    y = np.asarray([r["y"] for r in rows], dtype=int)
    pm = np.asarray([r["p_market"] for r in rows], dtype=float)
    return X, y, pm


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


def anchored_predict(model, rows):
    X, _, pm = arrays(rows)
    rawp = clip_prob(model.predict_proba(X)[:, 1])
    z = logit(pm) + ANCHOR_SHRINK * (logit(rawp) - logit(pm))
    return clip_prob(sigmoid(z))


def auc_safe(y, p):
    if len(np.unique(y)) < 2:
        return None
    return float(roc_auc_score(y, p))


def prob_metrics(y, p):
    return {
        "rows": int(len(y)),
        "brier": float(brier_score_loss(y, p)),
        "auc": auc_safe(y, p),
        "accuracy": float(np.mean((np.asarray(p) >= 0.5).astype(int) == y)),
    }


def make_trades(rows, probs):
    out = []
    for r, p in zip(rows, probs):
        p = float(p)
        up_edge = p - r["up_cost"]
        down_edge = 1.0 - p - r["down_cost"]
        edge = max(up_edge, down_edge)

        if edge < THRESHOLD:
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
                "condition_id": r["condition_id"],
                "market_start_ms": r["market_start_ms"],
                "side": side,
                "edge": float(edge),
                "cost": float(cost),
                "won": bool(won),
                "pnl": float(pnl),
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


def trade_metrics(trades):
    if not trades:
        return {
            "threshold": THRESHOLD,
            "trades": 0,
            "wins": 0,
            "win_rate": None,
            "net_pnl": 0.0,
            "roi_on_cost": None,
            "mean_pnl": None,
            "edge_pnl_correlation": None,
        }

    pnl = [t["pnl"] for t in trades]
    cost = [t["cost"] for t in trades]
    edge = [t["edge"] for t in trades]

    return {
        "threshold": THRESHOLD,
        "trades": len(trades),
        "wins": sum(int(t["won"]) for t in trades),
        "win_rate": float(np.mean([t["won"] for t in trades])),
        "net_pnl": float(sum(pnl)),
        "roi_on_cost": float(sum(pnl) / sum(cost)) if sum(cost) else None,
        "mean_pnl": float(np.mean(pnl)),
        "edge_pnl_correlation": corr_safe(edge, pnl),
    }


def freeze_model(db: Path):
    verify_prereg()

    if MODEL_FILE.exists() or MODEL_META.exists():
        if MODEL_FILE.exists() and MODEL_META.exists():
            meta = load_json(MODEL_META)
            print("=" * 72)
            print("MODELO FORWARD V0.9.7 YA CONGELADO - NO SE SOBRESCRIBE")
            print("=" * 72)
            print("Filas entrenamiento:", meta["training_rows"])
            print("Cutoff:", meta["cutoff_market_start_ms"])
            print("SHA256 modelo:", meta["model_sha256"])
            print("DINERO REAL: BLOQUEADO")
            print("=" * 72)
            return
        raise RuntimeError(
            "Existe solo uno de MODEL_FILE/MODEL_META. No borrar nada; revisar."
        )

    rows, costs = load_valid_60s(db)
    train = [r for r in rows if r["market_start_ms"] <= CUTOFF_MS]
    future = [r for r in rows if r["market_start_ms"] > CUTOFF_MS]

    if len(train) < 100:
        raise RuntimeError(f"Muy pocas filas de entrenamiento: {len(train)}")

    X, y, pm = arrays(train)
    model = build_model()
    model.fit(X, y)

    bundle = {
        "schema": "modelo_forward_v097_60s",
        "created_at": now_utc(),
        "horizon_seconds": HORIZON,
        "threshold": THRESHOLD,
        "cutoff_market_start_ms": CUTOFF_MS,
        "feature_names": FEATURES,
        "anchor_logit_shrink": ANCHOR_SHRINK,
        "model": model,
        "prereg_hashes": {
            "multihorizon": sha256_file(PREREG_MULTI),
            "implementation": sha256_file(PREREG_IMPL),
            "forward": sha256_file(PREREG_FWD),
        },
    }
    joblib.dump(bundle, MODEL_FILE)

    meta = {
        "schema": "modelo_forward_v097_60s_meta",
        "created_at": now_utc(),
        "training_rows": len(train),
        "future_valid_rows_visible_but_NOT_used": len(future),
        "cutoff_market_start_ms": CUTOFF_MS,
        "horizon_seconds": HORIZON,
        "threshold": THRESHOLD,
        "feature_names": FEATURES,
        "model_sha256": sha256_file(MODEL_FILE),
        "prereg_hashes": bundle["prereg_hashes"],
        "costs": costs,
        "money_real": "BLOQUEADO",
    }
    MODEL_META.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 72)
    print("MODELO FORWARD V0.9.7 CONGELADO")
    print("=" * 72)
    print("Horizonte:", HORIZON, "s")
    print("Threshold:", THRESHOLD)
    print("Filas entrenamiento:", len(train))
    print("Filas futuras visibles pero NO usadas:", len(future))
    print("Modelo:", MODEL_FILE)
    print("Metadata:", MODEL_META)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 72)


def status(db: Path):
    verify_prereg()
    rows, _ = load_valid_60s(db)
    train = [r for r in rows if r["market_start_ms"] <= CUTOFF_MS]
    future = [r for r in rows if r["market_start_ms"] > CUTOFF_MS]

    print("=" * 72)
    print("STATUS FORWARD V0.9.7")
    print("=" * 72)
    print("Entrenamiento congelable <= cutoff:", len(train))
    print("Mercados validos post-cutoff:", len(future))
    print("Faltan para forward50:", max(0, FORWARD_ROWS - len(future)))
    print("Modelo congelado:", MODEL_FILE.exists() and MODEL_META.exists())
    print("Forward50 ejecutado:", FORWARD_RESULT.exists())
    print("DINERO REAL: BLOQUEADO")
    print("=" * 72)


def forward50(db: Path):
    verify_prereg()

    if FORWARD_RESULT.exists():
        report = load_json(FORWARD_RESULT)
        print("=" * 72)
        print("FORWARD50 V0.9.7 YA EJECUTADO - NO SE SOBRESCRIBE")
        print("=" * 72)
        print("VEREDICTO:", report["verdict"])
        print("Trades:", report["trading"]["trades"])
        print("PnL:", report["trading"]["net_pnl"])
        print("Archivo:", FORWARD_RESULT)
        print("DINERO REAL: BLOQUEADO")
        print("=" * 72)
        return

    if not MODEL_FILE.exists() or not MODEL_META.exists():
        raise RuntimeError("Primero ejecute --freeze-model.")

    meta = load_json(MODEL_META)
    if sha256_file(MODEL_FILE) != meta["model_sha256"]:
        raise RuntimeError("El modelo congelado fue modificado.")

    expected_hashes = meta["prereg_hashes"]
    current_hashes = {
        "multihorizon": sha256_file(PREREG_MULTI),
        "implementation": sha256_file(PREREG_IMPL),
        "forward": sha256_file(PREREG_FWD),
    }
    if current_hashes != expected_hashes:
        raise RuntimeError("Uno de los preregistros cambió despues de congelar modelo.")

    bundle = joblib.load(MODEL_FILE)
    model = bundle["model"]

    rows, costs = load_valid_60s(db)
    future = [r for r in rows if r["market_start_ms"] > CUTOFF_MS]

    if len(future) < FORWARD_ROWS:
        print("=" * 72)
        print("FORWARD50 V0.9.7 - ESPERANDO DATOS")
        print("=" * 72)
        print("Disponibles:", len(future))
        print("Faltan:", FORWARD_ROWS - len(future))
        print("NO se evaluó el forward.")
        print("DINERO REAL: BLOQUEADO")
        print("=" * 72)
        return

    test = future[:FORWARD_ROWS]
    y = np.asarray([r["y"] for r in test], dtype=int)
    pm = np.asarray([r["p_market"] for r in test], dtype=float)
    p = anchored_predict(model, test)

    cand = prob_metrics(y, p)
    market = prob_metrics(y, pm)
    delta = cand["brier"] - market["brier"]
    trades = make_trades(test, p)
    tm = trade_metrics(trades)

    reasons = []
    if delta > MAX_BRIER_DELTA:
        reasons.append("BRIER_WORSE_THAN_MARKET_PLUS_0.005")
    if tm["trades"] < MIN_TRADES:
        reasons.append("LESS_THAN_10_TRADES")
    if not (tm["net_pnl"] > 0):
        reasons.append("NET_PNL_NOT_POSITIVE")
    if tm["edge_pnl_correlation"] is None or not (tm["edge_pnl_correlation"] > 0):
        reasons.append("EDGE_PNL_CORRELATION_NOT_POSITIVE")

    passed = len(reasons) == 0
    verdict = "PASS_FORWARD50_DEVELOPMENT_GATE" if passed else "FAIL_FORWARD50"

    report = {
        "schema": "forward50_v097",
        "created_at": now_utc(),
        "rows_exactly": FORWARD_ROWS,
        "first_market_start_ms": test[0]["market_start_ms"],
        "last_market_start_ms": test[-1]["market_start_ms"],
        "candidate": cand,
        "market": market,
        "brier_delta_candidate_minus_market": float(delta),
        "trading": tm,
        "pass": passed,
        "fail_reasons": reasons,
        "verdict": verdict,
        "gate": {
            "minimum_trades": MIN_TRADES,
            "net_pnl": ">0",
            "brier_candidate_minus_market_max": MAX_BRIER_DELTA,
            "edge_pnl_correlation": ">0",
        },
        "warning": (
            "PASS_FORWARD50 no habilita dinero real. Se requiere evidencia adicional "
            "independiente y auditoria antes de considerar ejecucion real."
        ),
        "money_real": "BLOQUEADO",
        "model_sha256": meta["model_sha256"],
    }

    FORWARD_RESULT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 80)
    print("FORWARD50 V0.9.7 - RESULTADO")
    print("=" * 80)
    print("Brier candidato :", round(cand["brier"], 6))
    print("Brier mercado   :", round(market["brier"], 6))
    print("Delta Brier     :", round(delta, 6))
    print("AUC candidato   :", None if cand["auc"] is None else round(cand["auc"], 6))
    print("Accuracy cand.  :", round(cand["accuracy"], 4))
    print("Accuracy mercado:", round(market["accuracy"], 4))
    print()
    print("Trades          :", tm["trades"])
    print("PnL neto        :", round(tm["net_pnl"], 6))
    print("ROI sobre costo :", tm["roi_on_cost"])
    print("Corr edge/PnL   :", tm["edge_pnl_correlation"])
    print()
    print("VEREDICTO:", verdict)
    if reasons:
        print("FALLOS:", ", ".join(reasons))
    print("Reporte:", FORWARD_RESULT)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 80)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_DEFAULT))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--freeze-model", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--forward50", action="store_true")
    args = ap.parse_args()

    db = Path(args.db).expanduser().resolve()
    if not db.exists():
        raise SystemExit(f"No existe DB: {db}")

    if args.freeze_model:
        freeze_model(db)
    elif args.status:
        status(db)
    else:
        forward50(db)


if __name__ == "__main__":
    main()
