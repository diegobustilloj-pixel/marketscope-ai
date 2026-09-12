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
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

DB_DEFAULT = DATA / "shadow_forward_twap_transfer_v094a.db"
PREREG = DATA / "prereg_v096.json"
SPECS = DATA / "prereg_v096_model_specs.json"
CUTOFF_V095 = DATA / "corte_desarrollo_v095.json"

PREPARE_JSON = DATA / "prepare_v096.json"
MODEL_BUNDLE = DATA / "modelos_screen_v096.joblib"
SCREEN_JSON = DATA / "screen50_v096.json"

ROW_SOURCE_MODEL = "twap_transfer_strike_hgb"

BASE_ROWS_EXPECTED = 161
BASE_CUTOFF_EXPECTED = 1786394100000
SCREEN_ROWS = 50
FINAL_NEW_ROWS = 100

WF_MIN_TRAIN = 80
WF_BLOCK = 20

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

EDGE_GRID = (0.02, 0.04, 0.06, 0.08, 0.10)
MIN_DEV_TRADES = 10

RIDGE_ALPHA = 100.0
RIDGE_SHRINK = 0.25
RIDGE_CAP = 0.15

LOGISTIC_C = 0.1
ANCHOR_SHRINK = 0.25

HGB_PARAMS = dict(
    learning_rate=0.05,
    max_iter=100,
    max_depth=2,
    min_samples_leaf=20,
    l2_regularization=5.0,
    random_state=960,
)

BARRIER_BRIER_DELTA = 0.005
MIN_SCREEN_TRADES = 10


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def open_ro(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def clip_prob(p):
    a = np.asarray(p, dtype=float)
    return np.clip(a, 0.001, 0.999)


def logit(p):
    p = clip_prob(p)
    return np.log(p / (1.0 - p))


def sigmoid(z):
    z = np.asarray(z, dtype=float)
    z = np.clip(z, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-z))


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


def verify_prereg() -> tuple[dict, dict, dict]:
    for path in (PREREG, SPECS, CUTOFF_V095):
        if not path.exists():
            raise RuntimeError(f"Falta archivo preregistrado: {path}")

    prereg = load_json(PREREG)
    specs = load_json(SPECS)
    cutoff = load_json(CUTOFF_V095)

    if int(prereg["base_development_cutoff_v095"]) != BASE_CUTOFF_EXPECTED:
        raise RuntimeError("El cutoff de prereg_v096 no coincide con el cutoff esperado.")
    if int(prereg["base_development_rows"]) != BASE_ROWS_EXPECTED:
        raise RuntimeError("Las filas base preregistradas no coinciden con 161.")
    if int(prereg["screening_new_rows"]) != SCREEN_ROWS:
        raise RuntimeError("screening_new_rows no coincide con 50.")
    if int(prereg["final_development_new_rows"]) != FINAL_NEW_ROWS:
        raise RuntimeError("final_development_new_rows no coincide con 100.")
    if int(cutoff["development_cutoff_market_start_ms"]) != BASE_CUTOFF_EXPECTED:
        raise RuntimeError("corte_desarrollo_v095.json no coincide con el preregistro.")

    expected_features = list(prereg["fixed_features"])
    if expected_features != FEATURES:
        raise RuntimeError(
            "Las features del script no coinciden EXACTAMENTE con prereg_v096.json."
        )

    expected_grid = [float(x) for x in specs["threshold_selection"]["grid"]]
    if expected_grid != list(EDGE_GRID):
        raise RuntimeError("El grid de edges no coincide con las specs congeladas.")

    return prereg, specs, cutoff


def read_meta(con: sqlite3.Connection) -> dict:
    meta = {}
    for row in con.execute("SELECT key,value FROM shadow_meta"):
        try:
            meta[str(row["key"])] = json.loads(row["value"])
        except Exception:
            meta[str(row["key"])] = row["value"]
    return meta


def load_all_eligible(db: Path) -> tuple[list[dict], dict]:
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
                b.probability_up AS market_probability,
                f.feature_json
            FROM shadow_signals src
            JOIN shadow_markets m
              ON m.condition_id=src.condition_id
            JOIN shadow_features f
              ON f.condition_id=src.condition_id
            JOIN shadow_signals b
              ON b.condition_id=src.condition_id
             AND b.model_name='market_implied'
            WHERE src.model_name=?
              AND m.label_verified=1
            ORDER BY m.market_start_ms, m.condition_id
            """,
            (ROW_SOURCE_MODEL,),
        ).fetchall()
    finally:
        con.close()

    out = []
    for row in rows:
        f = json.loads(row["feature_json"])
        pm = float(row["market_probability"])
        pm = float(clip_prob(pm))
        vals = {
            "twap_distance_to_open_bps": safe_float(f.get("twap_distance_to_open_bps")),
            "twap_minus_chainlink_bps": safe_float(f.get("twap_minus_chainlink_bps")),
            "chainlink_return_5s_bps": safe_float(f.get("chainlink_return_5s_bps")),
            "chainlink_return_15s_bps": safe_float(f.get("chainlink_return_15s_bps")),
            "chainlink_return_30s_bps": safe_float(f.get("chainlink_return_30s_bps")),
            "chainlink_return_60s_bps": safe_float(f.get("chainlink_return_60s_bps")),
            "chainlink_vol_30s_bps": safe_float(f.get("chainlink_vol_30s_bps")),
            "chainlink_vol_60s_bps": safe_float(f.get("chainlink_vol_60s_bps")),
            "chainlink_up_fraction_30s": safe_float(f.get("chainlink_up_fraction_30s")),
            "chainlink_state": safe_float(f.get("chainlink_state")),
            "chainlink_state_run_length": safe_float(f.get("chainlink_state_run_length")),
            "binance_return_5s_bps": safe_float(f.get("binance_return_5s_bps")),
            "binance_return_15s_bps": safe_float(f.get("binance_return_15s_bps")),
            "binance_return_30s_bps": safe_float(f.get("binance_return_30s_bps")),
            "binance_return_60s_bps": safe_float(f.get("binance_return_60s_bps")),
            "volatility_regime_ratio": safe_float(f.get("volatility_regime_ratio")),
            "market_probability": pm,
            "market_logit": float(logit(pm)),
            "market_uncertainty": pm * (1.0 - pm),
        }
        x = [vals[k] for k in FEATURES]

        twap_current_fresh = int(f.get("twap_30s_fresh", 0)) == 1
        twap_open_fresh = int(f.get("twap_open_fresh", 0)) == 1
        if not (twap_current_fresh and twap_open_fresh):
            raise RuntimeError(
                "Se encontró una fila fuente del transfer model sin TWAP fresh; "
                "detener para revisar consistencia."
            )

        out.append(
            {
                "condition_id": str(row["condition_id"]),
                "market_start_ms": int(row["market_start_ms"]),
                "y": 1 if str(row["label"]) == "Up" else 0,
                "p_market": pm,
                "x": x,
                "up_cost": taker_cost(float(f["up_best_ask"]), fee_rate, slippage),
                "down_cost": taker_cost(float(f["down_best_ask"]), fee_rate, slippage),
            }
        )

    return out, {
        "fee_rate": fee_rate,
        "slippage_per_share": slippage,
        "shadow_meta": meta,
    }


def split_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    base = [r for r in rows if r["market_start_ms"] <= BASE_CUTOFF_EXPECTED]
    future = [r for r in rows if r["market_start_ms"] > BASE_CUTOFF_EXPECTED]
    if len(base) != BASE_ROWS_EXPECTED:
        raise RuntimeError(
            f"Se esperaban exactamente {BASE_ROWS_EXPECTED} filas base y hay {len(base)}."
        )
    return base, future


def arrays(rows: list[dict]):
    X = np.asarray([r["x"] for r in rows], dtype=float)
    y = np.asarray([r["y"] for r in rows], dtype=int)
    pm = np.asarray([r["p_market"] for r in rows], dtype=float)
    return X, y, pm


def build_ridge():
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
            ("scale", StandardScaler()),
            ("ridge", Ridge(alpha=RIDGE_ALPHA)),
        ]
    )


def build_logistic():
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
            ("scale", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    C=LOGISTIC_C,
                    penalty="l2",
                    max_iter=2000,
                    solver="lbfgs",
                    random_state=960,
                ),
            ),
        ]
    )


def build_hgb():
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
            ("model", HistGradientBoostingClassifier(**HGB_PARAMS)),
        ]
    )


def fit_candidate(name: str, train_rows: list[dict]):
    X, y, pm = arrays(train_rows)

    if name == "market_baseline":
        return {"name": name, "model": None}

    if name == "residual_ridge":
        model = build_ridge()
        model.fit(X, y.astype(float) - pm)
        return {"name": name, "model": model}

    if name == "market_anchored_logistic":
        model = build_logistic()
        model.fit(X, y)
        return {"name": name, "model": model}

    if name == "market_anchored_hist_gradient_boosting":
        model = build_hgb()
        model.fit(X, y)
        return {"name": name, "model": model}

    raise ValueError(f"Candidato desconocido: {name}")


def predict_candidate(bundle: dict, rows: list[dict]) -> np.ndarray:
    name = bundle["name"]
    X, _, pm = arrays(rows)

    if name == "market_baseline":
        return clip_prob(pm)

    model = bundle["model"]

    if name == "residual_ridge":
        raw = np.asarray(model.predict(X), dtype=float)
        correction = np.clip(RIDGE_SHRINK * raw, -RIDGE_CAP, RIDGE_CAP)
        return clip_prob(pm + correction)

    rawp = clip_prob(model.predict_proba(X)[:, 1])
    anchored_logit = logit(pm) + ANCHOR_SHRINK * (logit(rawp) - logit(pm))
    return clip_prob(sigmoid(anchored_logit))


def expanding_oof(name: str, rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    n = len(rows)
    preds = np.full(n, np.nan, dtype=float)

    start = WF_MIN_TRAIN
    while start < n:
        end = min(n, start + WF_BLOCK)
        train = rows[:start]
        test = rows[start:end]
        model = fit_candidate(name, train)
        preds[start:end] = predict_candidate(model, test)
        start = end

    mask = np.isfinite(preds)
    return preds, mask


def auc_safe(y, p):
    y = np.asarray(y, dtype=int)
    if len(np.unique(y)) < 2:
        return None
    return float(roc_auc_score(y, p))


def probability_metrics(y, p) -> dict:
    y = np.asarray(y, dtype=int)
    p = clip_prob(p)
    return {
        "rows": int(len(y)),
        "brier": float(brier_score_loss(y, p)),
        "auc": auc_safe(y, p),
        "accuracy": float(np.mean((p >= 0.5).astype(int) == y)),
    }


def trades_for_probs(rows: list[dict], probs: np.ndarray, threshold: float) -> list[dict]:
    out = []
    for r, p in zip(rows, probs):
        p = float(p)
        up_edge = p - r["up_cost"]
        down_edge = 1.0 - p - r["down_cost"]
        chosen_edge = max(up_edge, down_edge)
        if chosen_edge < threshold:
            continue

        if up_edge >= down_edge:
            side = "Up"
            cost = r["up_cost"]
            won = r["y"] == 1
        else:
            side = "Down"
            cost = r["down_cost"]
            won = r["y"] == 0

        pnl = (1.0 - cost) if won else -cost
        out.append(
            {
                "condition_id": r["condition_id"],
                "market_start_ms": r["market_start_ms"],
                "side": side,
                "edge": float(chosen_edge),
                "cost": float(cost),
                "won": bool(won),
                "pnl": float(pnl),
            }
        )
    return out


def corr_safe(a, b):
    if len(a) < 2:
        return None
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if float(np.std(a)) == 0.0 or float(np.std(b)) == 0.0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def trade_metrics(trades: list[dict], threshold) -> dict:
    if not trades:
        return {
            "threshold": threshold,
            "trades": 0,
            "wins": 0,
            "win_rate": None,
            "net_pnl": 0.0,
            "roi_on_cost": None,
            "mean_pnl": None,
            "edge_pnl_correlation": None,
        }
    pnls = [t["pnl"] for t in trades]
    costs = [t["cost"] for t in trades]
    edges = [t["edge"] for t in trades]
    return {
        "threshold": threshold,
        "trades": len(trades),
        "wins": sum(int(t["won"]) for t in trades),
        "win_rate": float(np.mean([t["won"] for t in trades])),
        "net_pnl": float(sum(pnls)),
        "roi_on_cost": float(sum(pnls) / sum(costs)) if sum(costs) else None,
        "mean_pnl": float(np.mean(pnls)),
        "edge_pnl_correlation": corr_safe(edges, pnls),
    }


def choose_threshold(rows: list[dict], probs: np.ndarray) -> tuple[float | None, list[dict], str]:
    metrics = []
    for t in EDGE_GRID:
        trades = trades_for_probs(rows, probs, t)
        metrics.append(trade_metrics(trades, float(t)))

    eligible = [m for m in metrics if m["trades"] >= MIN_DEV_TRADES]
    if not eligible:
        return None, metrics, "FAIL_NO_THRESHOLD_WITH_MIN_10_DEV_TRADES"

    chosen = max(
        eligible,
        key=lambda m: (
            m["net_pnl"],
            -1e99 if m["mean_pnl"] is None else m["mean_pnl"],
            m["threshold"],
        ),
    )
    return float(chosen["threshold"]), metrics, "MAX_NET_PNL_OOF_BASE"


def prepare(db: Path):
    prereg, specs, cutoff = verify_prereg()

    if PREPARE_JSON.exists() or MODEL_BUNDLE.exists():
        if PREPARE_JSON.exists() and MODEL_BUNDLE.exists():
            report = load_json(PREPARE_JSON)
            print("=" * 72)
            print("V0.9.6 PREPARE YA CONGELADO - NO SE SOBRESCRIBE")
            print("=" * 72)
            for name, item in report["candidates"].items():
                print(
                    f"{name:42s} threshold={item['frozen_threshold']} "
                    f"status={item['threshold_status']}"
                )
            print("Prepare:", PREPARE_JSON)
            print("Modelos:", MODEL_BUNDLE)
            print("DINERO REAL: BLOQUEADO")
            print("=" * 72)
            return
        raise RuntimeError(
            "Existe solo uno de los artefactos PREPARE/MODEL_BUNDLE. "
            "No borrar nada; revisar antes de continuar."
        )

    rows, config = load_all_eligible(db)
    base, future = split_rows(rows)

    candidate_names = list(prereg["candidates"])
    candidate_results = {}
    final_models = {}

    for name in candidate_names:
        oof, mask = expanding_oof(name, base)
        oof_rows = [r for r, keep in zip(base, mask) if keep]
        oof_probs = oof[mask]
        y_oof = np.asarray([r["y"] for r in oof_rows], dtype=int)
        pm_oof = np.asarray([r["p_market"] for r in oof_rows], dtype=float)

        threshold, threshold_grid_metrics, threshold_status = choose_threshold(
            oof_rows, oof_probs
        )

        candidate_results[name] = {
            "oof_scheme": {
                "min_train_rows": WF_MIN_TRAIN,
                "block_rows": WF_BLOCK,
                "oof_rows": int(mask.sum()),
            },
            "probability_metrics": probability_metrics(y_oof, oof_probs),
            "market_same_rows": probability_metrics(y_oof, pm_oof),
            "brier_delta_candidate_minus_market": float(
                brier_score_loss(y_oof, oof_probs)
                - brier_score_loss(y_oof, pm_oof)
            ),
            "threshold_grid_metrics": threshold_grid_metrics,
            "frozen_threshold": threshold,
            "threshold_status": threshold_status,
        }

        final_models[name] = fit_candidate(name, base)

    bundle = {
        "schema": "v096_screen50_models",
        "created_at": now_utc(),
        "base_cutoff_market_start_ms": BASE_CUTOFF_EXPECTED,
        "base_rows": len(base),
        "feature_names": FEATURES,
        "prereg_sha256": sha256_file(PREREG),
        "specs_sha256": sha256_file(SPECS),
        "models": final_models,
    }
    joblib.dump(bundle, MODEL_BUNDLE)

    report = {
        "schema": "v096_prepare",
        "created_at": now_utc(),
        "development_only": True,
        "base_cutoff_market_start_ms": BASE_CUTOFF_EXPECTED,
        "base_rows": len(base),
        "future_rows_visible_at_prepare_but_NOT_used": len(future),
        "important": (
            "Las filas posteriores al cutoff NO se usan para fit, OOF ni selección "
            "de threshold. El screen50 se evalúa después con modelos y thresholds "
            "congelados aquí."
        ),
        "features": FEATURES,
        "edge_grid": list(EDGE_GRID),
        "threshold_selection_rule": (
            "Entre thresholds con >=10 trades OOF base: max net_pnl; "
            "desempate max mean_pnl; luego threshold mayor."
        ),
        "candidates": candidate_results,
        "artifacts": {
            "model_bundle": str(MODEL_BUNDLE),
            "model_bundle_sha256": sha256_file(MODEL_BUNDLE),
            "prereg_sha256": sha256_file(PREREG),
            "specs_sha256": sha256_file(SPECS),
        },
        "money_real": "BLOQUEADO",
    }

    PREPARE_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 72)
    print("V0.9.6 PREPARE CONGELADO")
    print("=" * 72)
    print("Base:", len(base), "filas")
    print("OOF: desde fila", WF_MIN_TRAIN + 1, "| bloques:", WF_BLOCK)
    print("Filas futuras existentes al preparar:", len(future), "(NO USADAS)")
    print()
    for name, item in candidate_results.items():
        pm = item["probability_metrics"]
        print(
            f"{name:42s} "
            f"Brier={pm['brier']:.6f} "
            f"delta_vs_market={item['brier_delta_candidate_minus_market']:+.6f} "
            f"threshold={item['frozen_threshold']}"
        )
    print()
    print("Prepare:", PREPARE_JSON)
    print("Modelos:", MODEL_BUNDLE)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 72)


def status(db: Path):
    verify_prereg()
    rows, _ = load_all_eligible(db)
    base, future = split_rows(rows)
    print("=" * 72)
    print("V0.9.6 STATUS")
    print("=" * 72)
    print("Base congelada:", len(base))
    print("Nuevos post-cutoff:", len(future))
    print("Faltan para SCREEN50:", max(0, SCREEN_ROWS - len(future)))
    print("Faltan para FREEZE100:", max(0, FINAL_NEW_ROWS - len(future)))
    print("Prepare congelado:", PREPARE_JSON.exists() and MODEL_BUNDLE.exists())
    print("Screen50 ejecutado:", SCREEN_JSON.exists())
    print("DINERO REAL: BLOQUEADO")
    print("=" * 72)


def screen50(db: Path):
    verify_prereg()

    if SCREEN_JSON.exists():
        report = load_json(SCREEN_JSON)
        print("=" * 72)
        print("SCREEN50 V0.9.6 YA EJECUTADO - NO SE SOBRESCRIBE")
        print("=" * 72)
        print("Veredicto:", report["overall_verdict"])
        for name, item in report["candidates"].items():
            print(
                f"{name:42s} pass={item['pass_screen50']} "
                f"trades={item['trade_metrics']['trades']} "
                f"PnL={item['trade_metrics']['net_pnl']:.5f}"
            )
        print("Archivo:", SCREEN_JSON)
        print("DINERO REAL: BLOQUEADO")
        print("=" * 72)
        return

    if not PREPARE_JSON.exists() or not MODEL_BUNDLE.exists():
        raise RuntimeError("Primero ejecute: screen_v096.py --prepare")

    prepare_report = load_json(PREPARE_JSON)
    model_bundle = joblib.load(MODEL_BUNDLE)

    expected_hash = prepare_report["artifacts"]["model_bundle_sha256"]
    current_hash = sha256_file(MODEL_BUNDLE)
    if current_hash != expected_hash:
        raise RuntimeError("El bundle de modelos cambió después de PREPARE.")

    if sha256_file(PREREG) != prepare_report["artifacts"]["prereg_sha256"]:
        raise RuntimeError("prereg_v096.json cambió después de PREPARE.")
    if sha256_file(SPECS) != prepare_report["artifacts"]["specs_sha256"]:
        raise RuntimeError("prereg_v096_model_specs.json cambió después de PREPARE.")

    rows, config = load_all_eligible(db)
    base, future = split_rows(rows)

    if len(future) < SCREEN_ROWS:
        print("=" * 72)
        print("SCREEN50 V0.9.6 - ESPERANDO DATOS")
        print("=" * 72)
        print("Nuevos disponibles:", len(future))
        print("Faltan:", SCREEN_ROWS - len(future))
        print("No se evaluó ningún candidato.")
        print("DINERO REAL: BLOQUEADO")
        print("=" * 72)
        return

    screen_rows = future[:SCREEN_ROWS]
    y = np.asarray([r["y"] for r in screen_rows], dtype=int)
    pm = np.asarray([r["p_market"] for r in screen_rows], dtype=float)
    market_metrics = probability_metrics(y, pm)

    results = {}
    survivors = []

    for name, bundle in model_bundle["models"].items():
        p = predict_candidate(bundle, screen_rows)
        prob_metrics = probability_metrics(y, p)
        delta_brier = prob_metrics["brier"] - market_metrics["brier"]

        frozen_threshold = prepare_report["candidates"][name]["frozen_threshold"]
        if frozen_threshold is None:
            tm = trade_metrics([], None)
            pass_gate = False
            reasons = ["NO_FROZEN_THRESHOLD"]
        else:
            trades = trades_for_probs(screen_rows, p, float(frozen_threshold))
            tm = trade_metrics(trades, float(frozen_threshold))
            reasons = []
            if delta_brier > BARRIER_BRIER_DELTA:
                reasons.append("BRIER_WORSE_THAN_MARKET_PLUS_0.005")
            if tm["trades"] < MIN_SCREEN_TRADES:
                reasons.append("LESS_THAN_10_TRADES")
            if not (tm["net_pnl"] > 0):
                reasons.append("NET_PNL_NOT_POSITIVE")
            if tm["edge_pnl_correlation"] is None or not (
                tm["edge_pnl_correlation"] > 0
            ):
                reasons.append("EDGE_PNL_CORRELATION_NOT_POSITIVE")
            pass_gate = len(reasons) == 0

        if pass_gate and name != "market_baseline":
            survivors.append(name)

        results[name] = {
            "probability_metrics": prob_metrics,
            "market_same_50": market_metrics,
            "brier_delta_candidate_minus_market": float(delta_brier),
            "frozen_threshold": frozen_threshold,
            "trade_metrics": tm,
            "pass_screen50": pass_gate,
            "fail_reasons": reasons,
        }

    overall = "PASS_AT_LEAST_ONE_ARCHITECTURE" if survivors else "FAIL_ALL_MODEL_ARCHITECTURES"

    report = {
        "schema": "v096_screen50",
        "created_at": now_utc(),
        "screen_rows_exactly": SCREEN_ROWS,
        "screen_first_market_start_ms": screen_rows[0]["market_start_ms"],
        "screen_last_market_start_ms": screen_rows[-1]["market_start_ms"],
        "future_rows_available_at_evaluation": len(future),
        "market_baseline": market_metrics,
        "candidates": results,
        "model_survivors": survivors,
        "overall_verdict": overall,
        "gate": {
            "brier_candidate_minus_market_max": BARRIER_BRIER_DELTA,
            "minimum_trades": MIN_SCREEN_TRADES,
            "net_pnl": ">0",
            "edge_pnl_correlation": ">0",
        },
        "warning": (
            "Screen50 es screening preregistrado, NO prueba final de rentabilidad. "
            "Dinero real sigue bloqueado."
        ),
        "money_real": "BLOQUEADO",
    }

    SCREEN_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 72)
    print("SCREEN50 V0.9.6 - RESULTADO CONGELADO")
    print("=" * 72)
    print("Mercados screen:", SCREEN_ROWS)
    print(
        f"Mercado baseline: Brier={market_metrics['brier']:.6f} "
        f"AUC={market_metrics['auc']} Acc={market_metrics['accuracy']:.4f}"
    )
    print()
    for name, item in results.items():
        tm = item["trade_metrics"]
        corr = tm["edge_pnl_correlation"]
        corr_txt = "n/a" if corr is None else f"{corr:+.4f}"
        print(
            f"{name:42s} "
            f"pass={str(item['pass_screen50']):5s} "
            f"BrierDelta={item['brier_delta_candidate_minus_market']:+.6f} "
            f"thr={item['frozen_threshold']} "
            f"trades={tm['trades']:2d} "
            f"PnL={tm['net_pnl']:+.5f} "
            f"corr={corr_txt}"
        )
        if item["fail_reasons"]:
            print("   ->", ", ".join(item["fail_reasons"]))
    print()
    print("VEREDICTO:", overall)
    print("Supervivientes:", survivors if survivors else "NINGUNO")
    print("Archivo:", SCREEN_JSON)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 72)


def main():
    ap = argparse.ArgumentParser(
        description="Pipeline preregistrado v0.9.6: prepare + status + screen50"
    )
    ap.add_argument("--db", default=str(DB_DEFAULT))
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--screen50", action="store_true")
    args = ap.parse_args()

    db = Path(args.db).expanduser().resolve()
    if not db.exists():
        raise SystemExit(f"No existe base: {db}")

    if args.prepare:
        prepare(db)
    elif args.status:
        status(db)
    else:
        screen50(db)


if __name__ == "__main__":
    main()
