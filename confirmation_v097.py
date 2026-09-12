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
from sklearn.metrics import brier_score_loss, roc_auc_score

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

DB_DEFAULT = DATA / "shadow_forward_twap_transfer_v094a.db"

PREREG_MULTI = DATA / "prereg_v097_multihorizon.json"
PREREG_IMPL = DATA / "prereg_v097_implementation.json"
PREREG_FWD = DATA / "prereg_v097_forward.json"
PREREG_CONF = DATA / "prereg_v097_confirmation100.json"

MODEL_FILE = DATA / "modelo_forward_v097_60s.joblib"
MODEL_META = DATA / "modelo_forward_v097_60s.json"
FORWARD50_RESULT = DATA / "forward50_v097.json"
CONFIRM_RESULT = DATA / "confirmation100_v097.json"

CUTOFF_MS = 1786394100000
HORIZON = 60
THRESHOLD = 0.0

FORWARD50_ROWS = 50
CONFIRM_START_INDEX = 50   # Python slice: fila post-cutoff 51
CONFIRM_END_INDEX = 150    # exclusivo: incluye hasta fila post-cutoff 150
CONFIRM_ROWS = 100

MIN_TRADES = 20
MAX_BRIER_DELTA = 0.005
MAX_SINGLE_POSITIVE_PNL_SHARE = 0.50
ANCHOR_SHRINK = 0.25

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


def verify_files_and_prereg():
    for p in (
        PREREG_MULTI,
        PREREG_IMPL,
        PREREG_FWD,
        PREREG_CONF,
        MODEL_FILE,
        MODEL_META,
    ):
        if not p.exists():
            raise RuntimeError(f"Falta archivo requerido: {p}")

    conf = load_json(PREREG_CONF)
    if int(conf["horizon_seconds"]) != HORIZON:
        raise RuntimeError("Horizonte confirmation100 no coincide con 60s.")
    if float(conf["threshold"]) != THRESHOLD:
        raise RuntimeError("Threshold confirmation100 no coincide con 0.0.")
    if int(conf["development_cutoff_market_start_ms"]) != CUTOFF_MS:
        raise RuntimeError("Cutoff confirmation100 no coincide.")
    if list(conf["reserved_rows"]["forward50"]) != [1, 50]:
        raise RuntimeError("Reserva forward50 no coincide.")
    if list(conf["reserved_rows"]["confirmation100"]) != [51, 150]:
        raise RuntimeError("Reserva confirmation100 no coincide.")
    if int(conf["confirmation_rows"]) != CONFIRM_ROWS:
        raise RuntimeError("confirmation_rows no coincide con 100.")

    multi = load_json(PREREG_MULTI)
    if list(multi["features"]) != FEATURES:
        raise RuntimeError("Features no coinciden con el preregistro multi-horizonte.")

    meta = load_json(MODEL_META)
    if sha256_file(MODEL_FILE) != meta["model_sha256"]:
        raise RuntimeError("El modelo congelado fue modificado.")

    expected = meta["prereg_hashes"]
    current = {
        "multihorizon": sha256_file(PREREG_MULTI),
        "implementation": sha256_file(PREREG_IMPL),
        "forward": sha256_file(PREREG_FWD),
    }
    if expected != current:
        raise RuntimeError("Un preregistro base cambió después de congelar el modelo.")

    return conf, meta


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


def anchored_predict(model, rows):
    X, _, pm = arrays(rows)
    rawp = clip_prob(model.predict_proba(X)[:, 1])
    z = logit(pm) + ANCHOR_SHRINK * (logit(rawp) - logit(pm))
    return clip_prob(sigmoid(z))


def auc_safe(y, p):
    if len(np.unique(y)) < 2:
        return None
    return float(roc_auc_score(y, p))


def probability_metrics(y, p):
    return {
        "rows": int(len(y)),
        "brier": float(brier_score_loss(y, p)),
        "auc": auc_safe(y, p),
        "accuracy": float(np.mean((np.asarray(p) >= 0.5).astype(int) == y)),
    }


def make_trades(rows, probs):
    trades = []
    for r, p in zip(rows, probs):
        p = float(p)
        up_edge = p - r["up_cost"]
        down_edge = 1.0 - p - r["down_cost"]
        edge = max(up_edge, down_edge)

        if edge < THRESHOLD:
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
        trades.append(
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
    return trades


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
            "positive_pnl_total": 0.0,
            "largest_positive_trade_pnl": 0.0,
            "largest_positive_trade_share": None,
        }

    pnl = np.asarray([t["pnl"] for t in trades], dtype=float)
    cost = np.asarray([t["cost"] for t in trades], dtype=float)
    edge = np.asarray([t["edge"] for t in trades], dtype=float)

    positive = pnl[pnl > 0]
    positive_total = float(positive.sum()) if len(positive) else 0.0
    largest_positive = float(positive.max()) if len(positive) else 0.0
    share = (
        largest_positive / positive_total
        if positive_total > 0
        else None
    )

    return {
        "threshold": THRESHOLD,
        "trades": int(len(trades)),
        "wins": int(sum(int(t["won"]) for t in trades)),
        "win_rate": float(np.mean([t["won"] for t in trades])),
        "net_pnl": float(pnl.sum()),
        "roi_on_cost": float(pnl.sum() / cost.sum()) if cost.sum() else None,
        "mean_pnl": float(pnl.mean()),
        "edge_pnl_correlation": corr_safe(edge, pnl),
        "positive_pnl_total": positive_total,
        "largest_positive_trade_pnl": largest_positive,
        "largest_positive_trade_share": share,
    }


def status(db: Path):
    verify_files_and_prereg()
    rows, _ = load_valid_60s(db)
    future = [r for r in rows if r["market_start_ms"] > CUTOFF_MS]

    print("=" * 76)
    print("STATUS CONFIRMATION100 V0.9.7")
    print("=" * 76)
    print("Mercados válidos post-cutoff:", len(future))
    print("Forward50 reservado:", min(len(future), FORWARD50_ROWS), "/", FORWARD50_ROWS)
    print(
        "Confirmation100 disponibles:",
        max(0, min(len(future), CONFIRM_END_INDEX) - CONFIRM_START_INDEX),
        "/",
        CONFIRM_ROWS,
    )
    print("Faltan para poder confirmar:", max(0, CONFIRM_END_INDEX - len(future)))
    print("Forward50 resultado existe:", FORWARD50_RESULT.exists())
    if FORWARD50_RESULT.exists():
        try:
            print("Forward50 veredicto:", load_json(FORWARD50_RESULT).get("verdict"))
        except Exception:
            print("Forward50 veredicto: ERROR leyendo archivo")
    print("Confirmation100 ejecutada:", CONFIRM_RESULT.exists())
    print("DINERO REAL: BLOQUEADO")
    print("=" * 76)


def confirmation100(db: Path):
    conf, meta = verify_files_and_prereg()

    if CONFIRM_RESULT.exists():
        report = load_json(CONFIRM_RESULT)
        print("=" * 76)
        print("CONFIRMATION100 V0.9.7 YA EJECUTADA - NO SE SOBRESCRIBE")
        print("=" * 76)
        print("VEREDICTO:", report["verdict"])
        print("Trades:", report["trading"]["trades"])
        print("PnL:", report["trading"]["net_pnl"])
        print("Archivo:", CONFIRM_RESULT)
        print("DINERO REAL: BLOQUEADO")
        print("=" * 76)
        return

    if not FORWARD50_RESULT.exists():
        raise RuntimeError(
            "No existe forward50_v097.json. Primero debe completarse el Forward50."
        )

    fwd = load_json(FORWARD50_RESULT)
    if fwd.get("verdict") != "PASS_FORWARD50_DEVELOPMENT_GATE":
        raise RuntimeError(
            "Forward50 NO pasó. Por preregistro, Confirmation100 no debe ejecutarse."
        )

    rows, costs = load_valid_60s(db)
    future = [r for r in rows if r["market_start_ms"] > CUTOFF_MS]

    if len(future) < CONFIRM_END_INDEX:
        print("=" * 76)
        print("CONFIRMATION100 V0.9.7 - ESPERANDO DATOS")
        print("=" * 76)
        print("Post-cutoff disponibles:", len(future))
        print("Confirmation100 disponibles:", max(0, len(future) - FORWARD50_ROWS))
        print("Faltan para filas 51-150:", CONFIRM_END_INDEX - len(future))
        print("NO se evaluó ningún resultado parcial.")
        print("DINERO REAL: BLOQUEADO")
        print("=" * 76)
        return

    test = future[CONFIRM_START_INDEX:CONFIRM_END_INDEX]
    if len(test) != CONFIRM_ROWS:
        raise RuntimeError(f"Se esperaban 100 filas y hay {len(test)}.")

    bundle = joblib.load(MODEL_FILE)
    model = bundle["model"]

    y = np.asarray([r["y"] for r in test], dtype=int)
    pm = np.asarray([r["p_market"] for r in test], dtype=float)
    p = anchored_predict(model, test)

    cand = probability_metrics(y, p)
    market = probability_metrics(y, pm)
    delta = cand["brier"] - market["brier"]

    trades = make_trades(test, p)
    tm = trade_metrics(trades)

    reasons = []
    if delta > MAX_BRIER_DELTA:
        reasons.append("BRIER_WORSE_THAN_MARKET_PLUS_0.005")
    if tm["trades"] < MIN_TRADES:
        reasons.append("LESS_THAN_20_TRADES")
    if not (tm["net_pnl"] > 0):
        reasons.append("NET_PNL_NOT_POSITIVE")
    if tm["edge_pnl_correlation"] is None or not (tm["edge_pnl_correlation"] > 0):
        reasons.append("EDGE_PNL_CORRELATION_NOT_POSITIVE")
    if (
        tm["largest_positive_trade_share"] is None
        or tm["largest_positive_trade_share"] > MAX_SINGLE_POSITIVE_PNL_SHARE
    ):
        reasons.append("SINGLE_TRADE_POSITIVE_PNL_SHARE_ABOVE_0.50")

    passed = len(reasons) == 0
    verdict = "PASS_CONFIRMATION100" if passed else "FAIL_CONFIRMATION100"

    report = {
        "schema": "confirmation100_v097",
        "created_at": now_utc(),
        "rows_exactly": CONFIRM_ROWS,
        "reserved_post_cutoff_rows": [51, 150],
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
            "maximum_single_trade_share_of_positive_pnl": MAX_SINGLE_POSITIVE_PNL_SHARE,
        },
        "model_sha256": meta["model_sha256"],
        "prereg_confirmation_sha256": sha256_file(PREREG_CONF),
        "warning": (
            "PASS_CONFIRMATION100 permite avanzar a auditoría final de robustez. "
            "NO habilita dinero real."
        ),
        "money_real": "BLOQUEADO",
    }

    CONFIRM_RESULT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 84)
    print("CONFIRMATION100 V0.9.7 - RESULTADO")
    print("=" * 84)
    print("Filas evaluadas      :", len(test), "(post-cutoff 51-150)")
    print("Brier candidato      :", round(cand["brier"], 6))
    print("Brier mercado        :", round(market["brier"], 6))
    print("Delta Brier          :", round(delta, 6))
    print("AUC candidato        :", None if cand["auc"] is None else round(cand["auc"], 6))
    print("Accuracy candidato   :", round(cand["accuracy"], 4))
    print("Accuracy mercado     :", round(market["accuracy"], 4))
    print()
    print("Trades               :", tm["trades"])
    print("PnL neto             :", round(tm["net_pnl"], 6))
    print("ROI sobre costo      :", tm["roi_on_cost"])
    print("Corr edge/PnL        :", tm["edge_pnl_correlation"])
    print("Mayor trade / PnL +  :", tm["largest_positive_trade_share"])
    print()
    print("VEREDICTO:", verdict)
    if reasons:
        print("FALLOS:", ", ".join(reasons))
    print("Reporte:", CONFIRM_RESULT)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 84)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_DEFAULT))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--confirmation100", action="store_true")
    args = ap.parse_args()

    db = Path(args.db).expanduser().resolve()
    if not db.exists():
        raise SystemExit(f"No existe DB: {db}")

    if args.status:
        status(db)
    else:
        confirmation100(db)


if __name__ == "__main__":
    main()
