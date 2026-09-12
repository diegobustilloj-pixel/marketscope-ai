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
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

DB_DEFAULT = DATA / "shadow_forward_twap_transfer_v094a.db"
PREREG = DATA / "prereg_v010_microstructure.json"
IMPL = DATA / "prereg_v010_implementation.json"
PREPARE_JSON = DATA / "prepare_v010_microstructure.json"
MODEL_FILE = DATA / "modelo_v010_microstructure.joblib"

CUTOFF_MS = 1786394100000
DEV_EXPECTED = 209
CONSUMED_POST_CUTOFF_60S = 50

MIN_TRAIN = 100
BLOCK_ROWS = 25
GRID = (0.00, 0.01, 0.02, 0.03, 0.04, 0.05)

MIN_TRADES = 20
MAX_POS_SHARE = 0.35

FEATURES = [
    "delta_implied_up_probability",
    "delta_up_mid",
    "delta_up_best_bid",
    "delta_up_best_ask",
    "delta_up_spread",
    "delta_down_spread",
    "delta_up_bid_depth_1c",
    "delta_up_ask_depth_1c",
    "delta_down_bid_depth_1c",
    "delta_down_ask_depth_1c",
    "delta_up_order_imbalance_1c",
    "delta_down_order_imbalance_1c",
    "delta_polymarket_trade_count_60s",
    "delta_polymarket_trade_volume_60s",
    "delta_book_messages_60s",
    "delta_price_change_messages_60s",
    "delta_binance_trade_count_60s",
    "delta_binance_trade_volume_60s",
    "delta_twap_distance_to_open_bps",
    "delta_twap_minus_chainlink_bps",
    "market_ask_overround_60",
    "up_cost_60",
    "down_cost_60",
]


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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


def verify_prereg():
    if not PREREG.exists():
        raise RuntimeError(f"Falta {PREREG}")
    d = load_json(PREREG)

    if int(d["development_rows_expected"]) != DEV_EXPECTED:
        raise RuntimeError("development_rows_expected no coincide con 209.")
    if list(d["horizons"]) != [120, 60]:
        raise RuntimeError("Horizontes no coinciden con [120,60].")
    if list(d["dynamic_features"]) != FEATURES:
        raise RuntimeError("Features dinámicas no coinciden con preregistro.")
    if tuple(float(x) for x in d["predicted_pnl_threshold_grid"]) != GRID:
        raise RuntimeError("Grid de thresholds no coincide.")
    if int(d["walk_forward"]["min_train_rows"]) != MIN_TRAIN:
        raise RuntimeError("min_train_rows no coincide.")
    if int(d["walk_forward"]["block_rows"]) != BLOCK_ROWS:
        raise RuntimeError("block_rows no coincide.")
    return d


def freeze_impl():
    verify_prereg()

    if IMPL.exists():
        print("=" * 76)
        print("IMPLEMENTACION V0.10 YA CONGELADA - NO SE SOBRESCRIBE")
        print("=" * 76)
        print("ARCHIVO:", IMPL)
        print("DINERO REAL: BLOQUEADO")
        print("=" * 76)
        return

    definitions = {
        "delta_implied_up_probability": ["implied_up_mid_probability"],
        "delta_up_mid": ["up_mid"],
        "delta_up_best_bid": ["up_best_bid"],
        "delta_up_best_ask": ["up_best_ask"],
        "delta_up_spread": ["up_spread"],
        "delta_down_spread": ["down_spread"],
        "delta_up_bid_depth_1c": ["up_bid_depth_1c"],
        "delta_up_ask_depth_1c": ["up_ask_depth_1c"],
        "delta_down_bid_depth_1c": ["down_bid_depth_1c"],
        "delta_down_ask_depth_1c": ["down_ask_depth_1c"],
        "delta_up_order_imbalance_1c": ["up_order_imbalance_1c"],
        "delta_down_order_imbalance_1c": ["down_order_imbalance_1c"],
        "delta_polymarket_trade_count_60s": ["polymarket_trade_count_60s"],
        "delta_polymarket_trade_volume_60s": ["polymarket_trade_volume_60s"],
        "delta_book_messages_60s": ["book_messages_60s"],
        "delta_price_change_messages_60s": ["price_change_messages_60s"],
        "delta_binance_trade_count_60s": ["binance_trade_count_60s"],
        "delta_binance_trade_volume_60s": ["binance_trade_volume_60s"],
        "delta_twap_distance_to_open_bps": ["twap_distance_to_open_bps"],
        "delta_twap_minus_chainlink_bps": ["twap_minus_chainlink_bps"],
        "market_ask_overround_60": ["market_ask_overround"],
        "up_cost_60": ["up_best_ask"],
        "down_cost_60": ["down_best_ask"],
    }

    d = {
        "schema": "prereg_v010_implementation",
        "created_at": now_utc(),
        "parent_prereg_sha256": sha256_file(PREREG),
        "population_rule": (
            "1) construir población 60s fresh; 2) base <= cutoff; "
            "3) primeras 50 filas 60s fresh post-cutoff son exactamente las ya consumidas "
            "por Forward50 v097; 4) exigir además snapshot 120s SAVED y TWAP fresh en ambos "
            "horizontes; 5) NO reemplazar exclusiones con filas futuras nuevas."
        ),
        "dynamic_feature_formula": (
            "Para cada feature delta: valor_60s - valor_120s. "
            "market_ask_overround_60 y costos se toman solo a 60s."
        ),
        "feature_definitions": definitions,
        "cost_formula": (
            "fill=min(0.999,max(0.001,ask_60 + slippage_per_share)); "
            "fee=fee_rate*fill*(1-fill); cost=fill+fee"
        ),
        "targets": {
            "up_realized_pnl": "y_up - up_cost_60",
            "down_realized_pnl": "(1-y_up) - down_cost_60",
        },
        "models": {
            "micro_ridge": {
                "two_independent_regressors": True,
                "alpha": 100.0,
                "imputer": "median+indicator+keep_empty_features",
                "scaler": "standard",
            },
            "micro_hgb": {
                "two_independent_regressors": True,
                "learning_rate": 0.05,
                "max_iter": 100,
                "max_depth": 2,
                "min_samples_leaf": 20,
                "l2_regularization": 5.0,
                "random_state": 1000,
            },
        },
        "walk_forward": {
            "type": "expanding",
            "min_train_rows": MIN_TRAIN,
            "block_rows": BLOCK_ROWS,
        },
        "decision_rule": (
            "Elegir UP si predicted_up_pnl >= predicted_down_pnl; si no DOWN. "
            "Operar solo si max predicted_pnl >= threshold."
        ),
        "threshold_gate": (
            ">=20 trades, net_pnl>0, ROI>0, corr(predicted_pnl,realized_pnl)>0, "
            "PnL primera mitad cronológica>=0, PnL segunda mitad cronológica>=0, "
            "largest_positive_trade_share<=0.35."
        ),
        "threshold_selection": (
            "Entre elegibles: mayor net_pnl; desempate mayor mean_pnl; "
            "después threshold más alto."
        ),
        "candidate_selection": (
            "Entre candidatos elegibles: mayor net_pnl; desempate mayor mean_pnl; "
            "después Ridge por simplicidad."
        ),
        "future_test": (
            "Primeros 100 mercados elegibles 120s+60s fresh posteriores a los 50 "
            "ya consumidos; prohibido evaluarlos parcialmente antes de congelar modelo."
        ),
        "real_money": "BLOQUEADO",
    }

    IMPL.write_text(
        json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 76)
    print("IMPLEMENTACION V0.10 CONGELADA")
    print("=" * 76)
    print("POBLACION DESARROLLO ESPERADA:", DEV_EXPECTED)
    print("FEATURES: DELTAS 120s -> 60s")
    print("MODELOS: micro_ridge + micro_hgb")
    print("TEST FUTURO: 100 MERCADOS ELEGIBLES, SIN PEEKING")
    print("ARCHIVO:", IMPL)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 76)


def verify_impl():
    verify_prereg()
    if not IMPL.exists():
        raise RuntimeError("Primero ejecute --freeze-implementation.")
    d = load_json(IMPL)
    if d["parent_prereg_sha256"] != sha256_file(PREREG):
        raise RuntimeError("Preregistro cambió después de congelar implementación.")


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


def fresh_feature(d):
    return (
        d.get("twap_30s_fresh") in (1, True)
        and d.get("twap_open_fresh") in (1, True)
    )


def taker_cost(ask, fee_rate, slip):
    fill = min(0.999, max(0.001, float(ask) + float(slip)))
    fee = float(fee_rate) * fill * (1.0 - fill)
    return fill + fee


def delta(a60, a120, key):
    x60 = sf(a60.get(key))
    x120 = sf(a120.get(key))
    if not (finite(x60) and finite(x120)):
        return np.nan
    return float(x60 - x120)


def make_record(cid, ms, label, f120, f60, fee_rate, slip):
    a = json.loads(f120)
    b = json.loads(f60)

    if not fresh_feature(a) or not fresh_feature(b):
        return None

    ua = sf(b.get("up_best_ask"))
    da = sf(b.get("down_best_ask"))
    if not (finite(ua) and finite(da)):
        return None

    uc = taker_cost(ua, fee_rate, slip)
    dc = taker_cost(da, fee_rate, slip)
    y = 1 if str(label).lower() == "up" else 0

    vals = {
        "delta_implied_up_probability": delta(b, a, "implied_up_mid_probability"),
        "delta_up_mid": delta(b, a, "up_mid"),
        "delta_up_best_bid": delta(b, a, "up_best_bid"),
        "delta_up_best_ask": delta(b, a, "up_best_ask"),
        "delta_up_spread": delta(b, a, "up_spread"),
        "delta_down_spread": delta(b, a, "down_spread"),
        "delta_up_bid_depth_1c": delta(b, a, "up_bid_depth_1c"),
        "delta_up_ask_depth_1c": delta(b, a, "up_ask_depth_1c"),
        "delta_down_bid_depth_1c": delta(b, a, "down_bid_depth_1c"),
        "delta_down_ask_depth_1c": delta(b, a, "down_ask_depth_1c"),
        "delta_up_order_imbalance_1c": delta(b, a, "up_order_imbalance_1c"),
        "delta_down_order_imbalance_1c": delta(b, a, "down_order_imbalance_1c"),
        "delta_polymarket_trade_count_60s": delta(b, a, "polymarket_trade_count_60s"),
        "delta_polymarket_trade_volume_60s": delta(b, a, "polymarket_trade_volume_60s"),
        "delta_book_messages_60s": delta(b, a, "book_messages_60s"),
        "delta_price_change_messages_60s": delta(b, a, "price_change_messages_60s"),
        "delta_binance_trade_count_60s": delta(b, a, "binance_trade_count_60s"),
        "delta_binance_trade_volume_60s": delta(b, a, "binance_trade_volume_60s"),
        "delta_twap_distance_to_open_bps": delta(b, a, "twap_distance_to_open_bps"),
        "delta_twap_minus_chainlink_bps": delta(b, a, "twap_minus_chainlink_bps"),
        "market_ask_overround_60": sf(b.get("market_ask_overround")),
        "up_cost_60": float(uc),
        "down_cost_60": float(dc),
    }

    return {
        "condition_id": str(cid),
        "market_start_ms": int(ms),
        "y": y,
        "x": [vals[k] for k in FEATURES],
        "up_cost": float(uc),
        "down_cost": float(dc),
        "up_realized_pnl": float(y - uc),
        "down_realized_pnl": float((1 - y) - dc),
    }


def load_population(db: Path):
    con = open_ro(db)
    try:
        meta = read_meta(con)
        fee_rate = float(meta.get("fee_rate", 0.07))
        slip = float(meta.get("slippage_per_share", 0.005))

        f60_rows = con.execute(
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

        d120 = {
            str(r["condition_id"]): r["feature_json"]
            for r in con.execute(
                """
                SELECT condition_id,feature_json
                FROM shadow_diagnostics
                WHERE horizon_seconds=120
                  AND status='SAVED'
                  AND feature_json IS NOT NULL
                """
            )
        }
    finally:
        con.close()

    # Primero define exactamente la población consumida por v097 usando 60s fresh.
    valid60 = []
    for r in f60_rows:
        d = json.loads(r["feature_json"])
        if fresh_feature(d):
            valid60.append(r)

    base60 = [r for r in valid60 if int(r["market_start_ms"]) <= CUTOFF_MS]
    future60 = [r for r in valid60 if int(r["market_start_ms"]) > CUTOFF_MS]

    consumed50 = future60[:CONSUMED_POST_CUTOFF_60S]

    def common_record(r):
        cid = str(r["condition_id"])
        if cid not in d120:
            return None
        return make_record(
            cid,
            r["market_start_ms"],
            r["label"],
            d120[cid],
            r["feature_json"],
            fee_rate,
            slip,
        )

    base_common = []
    for r in base60:
        rec = common_record(r)
        if rec is not None:
            base_common.append(rec)

    consumed_common = []
    for r in consumed50:
        rec = common_record(r)
        if rec is not None:
            consumed_common.append(rec)

    if len(consumed_common) != 50:
        raise RuntimeError(
            f"Los 50 consumidos por v097 deberían ser common fresh; hay {len(consumed_common)}."
        )

    dev = base_common + consumed_common
    if len(dev) != DEV_EXPECTED:
        raise RuntimeError(
            f"Desarrollo esperado 209; obtenido {len(dev)} "
            f"(base_common={len(base_common)}, consumed_common={len(consumed_common)})."
        )

    # Solo contar futuros reservados sin usarlos para fit/selección.
    reserved_future_common_count = 0
    for r in future60[CONSUMED_POST_CUTOFF_60S:]:
        rec = common_record(r)
        if rec is not None:
            reserved_future_common_count += 1

    return dev, {
        "base60_fresh": len(base60),
        "base_common_120_60": len(base_common),
        "consumed50_60s": len(consumed50),
        "consumed50_common": len(consumed_common),
        "reserved_future_common_count": reserved_future_common_count,
        "fee_rate": fee_rate,
        "slippage_per_share": slip,
    }


def arrays(rows):
    X = np.asarray([r["x"] for r in rows], dtype=float)
    up = np.asarray([r["up_realized_pnl"] for r in rows], dtype=float)
    down = np.asarray([r["down_realized_pnl"] for r in rows], dtype=float)
    return X, up, down


def build_pair(name):
    if name == "micro_ridge":
        def one():
            return Pipeline([
                ("imputer", SimpleImputer(
                    strategy="median",
                    add_indicator=True,
                    keep_empty_features=True,
                )),
                ("scale", StandardScaler()),
                ("model", Ridge(alpha=100.0)),
            ])
    elif name == "micro_hgb":
        def one():
            return Pipeline([
                ("imputer", SimpleImputer(
                    strategy="median",
                    add_indicator=True,
                    keep_empty_features=True,
                )),
                ("model", HistGradientBoostingRegressor(
                    learning_rate=0.05,
                    max_iter=100,
                    max_depth=2,
                    min_samples_leaf=20,
                    l2_regularization=5.0,
                    random_state=1000,
                )),
            ])
    else:
        raise ValueError(name)
    return one(), one()


def fit_pair(name, rows):
    X, up, down = arrays(rows)
    um, dm = build_pair(name)
    um.fit(X, up)
    dm.fit(X, down)
    return {"name": name, "up_model": um, "down_model": dm}


def predict_pair(bundle, rows):
    X, _, _ = arrays(rows)
    return (
        np.asarray(bundle["up_model"].predict(X), dtype=float),
        np.asarray(bundle["down_model"].predict(X), dtype=float),
    )


def expanding_oof(name, rows):
    n = len(rows)
    pu = np.full(n, np.nan, dtype=float)
    pd = np.full(n, np.nan, dtype=float)
    start = MIN_TRAIN
    while start < n:
        end = min(n, start + BLOCK_ROWS)
        b = fit_pair(name, rows[:start])
        u, d = predict_pair(b, rows[start:end])
        pu[start:end] = u
        pd[start:end] = d
        start = end
    mask = np.isfinite(pu) & np.isfinite(pd)
    return pu, pd, mask


def corr_safe(a, b):
    if len(a) < 2:
        return None
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if float(np.std(a)) == 0.0 or float(np.std(b)) == 0.0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def make_trades(rows, pu, pd, threshold):
    out = []
    for r, u, d in zip(rows, pu, pd):
        if u >= d:
            side = "Up"
            predicted = float(u)
            realized = float(r["up_realized_pnl"])
            cost = float(r["up_cost"])
        else:
            side = "Down"
            predicted = float(d)
            realized = float(r["down_realized_pnl"])
            cost = float(r["down_cost"])

        if predicted < threshold:
            continue

        out.append({
            "market_start_ms": r["market_start_ms"],
            "side": side,
            "predicted_pnl": predicted,
            "realized_pnl": realized,
            "cost": cost,
        })
    return out


def trade_metrics(all_oof_rows, trades, threshold):
    if not trades:
        return {
            "threshold": float(threshold),
            "trades": 0,
            "wins": 0,
            "net_pnl": 0.0,
            "roi_on_cost": None,
            "mean_pnl": None,
            "predicted_realized_pnl_correlation": None,
            "first_half_net_pnl": 0.0,
            "second_half_net_pnl": 0.0,
            "largest_positive_trade_share": None,
            "eligible": False,
        }

    pnl = np.asarray([t["realized_pnl"] for t in trades], dtype=float)
    pred = np.asarray([t["predicted_pnl"] for t in trades], dtype=float)
    costs = np.asarray([t["cost"] for t in trades], dtype=float)

    positive = pnl[pnl > 0]
    positive_total = float(positive.sum()) if len(positive) else 0.0
    largest_positive = float(positive.max()) if len(positive) else 0.0
    share = largest_positive / positive_total if positive_total > 0 else None

    midpoint_ms = all_oof_rows[len(all_oof_rows) // 2]["market_start_ms"]
    first = float(sum(
        t["realized_pnl"] for t in trades
        if t["market_start_ms"] < midpoint_ms
    ))
    second = float(sum(
        t["realized_pnl"] for t in trades
        if t["market_start_ms"] >= midpoint_ms
    ))

    corr = corr_safe(pred, pnl)
    roi = float(pnl.sum() / costs.sum()) if costs.sum() else None

    eligible = bool(
        len(trades) >= MIN_TRADES
        and float(pnl.sum()) > 0
        and roi is not None and roi > 0
        and corr is not None and corr > 0
        and first >= 0
        and second >= 0
        and share is not None and share <= MAX_POS_SHARE
    )

    return {
        "threshold": float(threshold),
        "trades": int(len(trades)),
        "wins": int(np.sum(pnl > 0)),
        "win_rate": float(np.mean(pnl > 0)),
        "net_pnl": float(pnl.sum()),
        "roi_on_cost": roi,
        "mean_pnl": float(pnl.mean()),
        "predicted_realized_pnl_correlation": corr,
        "first_half_net_pnl": first,
        "second_half_net_pnl": second,
        "largest_positive_trade_share": share,
        "eligible": eligible,
    }


def prepare(db: Path):
    verify_impl()

    if PREPARE_JSON.exists() or MODEL_FILE.exists():
        if PREPARE_JSON.exists() and MODEL_FILE.exists():
            r = load_json(PREPARE_JSON)
            print("=" * 82)
            print("V0.10 PREPARE YA CONGELADO - NO SE SOBRESCRIBE")
            print("=" * 82)
            print("SELECCIONADO:", r.get("selected_candidate"))
            print("THRESHOLD:", r.get("selected_threshold"))
            print("VEREDICTO:", r.get("verdict"))
            print("DINERO REAL: BLOQUEADO")
            print("=" * 82)
            return
        raise RuntimeError("Existe solo uno de PREPARE/MODEL; revisar sin borrar.")

    dev, pop = load_population(db)

    names = ["micro_ridge", "micro_hgb"]
    results = {}
    final_models = {}

    for name in names:
        pu, pd, mask = expanding_oof(name, dev)
        oof_rows = [r for r, keep in zip(dev, mask) if keep]
        pu_oof = pu[mask]
        pd_oof = pd[mask]

        grid_results = []
        for t in GRID:
            ts = make_trades(oof_rows, pu_oof, pd_oof, float(t))
            grid_results.append(
                trade_metrics(oof_rows, ts, float(t))
            )

        eligible = [m for m in grid_results if m["eligible"]]
        chosen = (
            max(
                eligible,
                key=lambda m: (
                    m["net_pnl"],
                    m["mean_pnl"],
                    m["threshold"],
                ),
            )
            if eligible else None
        )

        results[name] = {
            "oof_rows": int(mask.sum()),
            "threshold_grid": grid_results,
            "selected_threshold": None if chosen is None else chosen["threshold"],
            "selected_metrics": chosen,
            "candidate_eligible": chosen is not None,
        }

        final_models[name] = fit_pair(name, dev)

    eligible_models = [
        (name, r)
        for name, r in results.items()
        if r["candidate_eligible"]
    ]

    selected = None
    selected_threshold = None
    if eligible_models:
        simplicity = {"micro_ridge": 0, "micro_hgb": 1}
        selected, selected_result = max(
            eligible_models,
            key=lambda x: (
                x[1]["selected_metrics"]["net_pnl"],
                x[1]["selected_metrics"]["mean_pnl"],
                -simplicity[x[0]],
            ),
        )
        selected_threshold = selected_result["selected_threshold"]

    verdict = (
        "CANDIDATE_FOUND_DEVELOPMENT_ONLY"
        if selected is not None
        else "FAIL_ALL_MICROSTRUCTURE_CANDIDATES"
    )

    bundle = {
        "schema": "modelo_v010_microstructure",
        "created_at": now_utc(),
        "development_rows": len(dev),
        "feature_names": FEATURES,
        "selected_candidate": selected,
        "selected_threshold": selected_threshold,
        "models": final_models,
        "prereg_sha256": sha256_file(PREREG),
        "implementation_sha256": sha256_file(IMPL),
    }
    joblib.dump(bundle, MODEL_FILE)

    report = {
        "schema": "prepare_v010_microstructure",
        "created_at": now_utc(),
        "development_rows": len(dev),
        "population": pop,
        "oof_scheme": {
            "min_train_rows": MIN_TRAIN,
            "block_rows": BLOCK_ROWS,
            "oof_rows": len(dev) - MIN_TRAIN,
        },
        "features": FEATURES,
        "threshold_grid": list(GRID),
        "results": results,
        "selected_candidate": selected,
        "selected_threshold": selected_threshold,
        "verdict": verdict,
        "model_file": str(MODEL_FILE),
        "model_sha256": sha256_file(MODEL_FILE),
        "prereg_sha256": sha256_file(PREREG),
        "implementation_sha256": sha256_file(IMPL),
        "warning": (
            "Solo desarrollo. Los mercados futuros posteriores a los 50 consumidos "
            "no se usaron para entrenamiento ni selección."
        ),
        "money_real": "BLOQUEADO",
    }
    PREPARE_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 98)
    print("V0.10 MICROSTRUCTURE 120s -> 60s - PREPARE OOF")
    print("=" * 98)
    print("Desarrollo:", len(dev))
    print("Base común 120+60:", pop["base_common_120_60"])
    print("Forward50 consumidos comunes:", pop["consumed50_common"])
    print("OOF:", len(dev) - MIN_TRAIN)
    print("Futuros elegibles reservados existentes:", pop["reserved_future_common_count"])
    print()
    for name in names:
        r = results[name]
        m = r["selected_metrics"]
        if m is None:
            print(f"{name:28s} -> SIN THRESHOLD ELEGIBLE")
        else:
            print(
                f"{name:28s} -> thr={m['threshold']:.2f} "
                f"trades={m['trades']:3d} PnL={m['net_pnl']:+.5f} "
                f"ROI={m['roi_on_cost']:+.4f} "
                f"corr={m['predicted_realized_pnl_correlation']:+.4f} "
                f"h1={m['first_half_net_pnl']:+.5f} "
                f"h2={m['second_half_net_pnl']:+.5f}"
            )
    print()
    print("SELECCIONADO:", selected)
    print("THRESHOLD:", selected_threshold)
    print("VEREDICTO:", verdict)
    print("Modelo:", MODEL_FILE)
    print("Reporte:", PREPARE_JSON)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 98)


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
