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
PREREG_FINAL = DATA / "prereg_v097_final_robustness.json"

MODEL_FILE = DATA / "modelo_forward_v097_60s.joblib"
MODEL_META = DATA / "modelo_forward_v097_60s.json"
FORWARD50_RESULT = DATA / "forward50_v097.json"
CONFIRM_RESULT = DATA / "confirmation100_v097.json"
FINAL_RESULT = DATA / "final_robustness150_v097.json"

CUTOFF_MS = 1786394100000
HORIZON = 60
THRESHOLD = 0.0

FINAL_START_INDEX = 150   # post-cutoff fila 151
FINAL_END_INDEX = 300     # exclusivo; incluye fila 300
FINAL_ROWS = 150
HALF = 75

MIN_TRADES = 30
MAX_BRIER_DELTA = 0.005
MAX_SINGLE_POSITIVE_PNL_SHARE = 0.35
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


def verify():
    required = (
        PREREG_MULTI,
        PREREG_IMPL,
        PREREG_FWD,
        PREREG_CONF,
        PREREG_FINAL,
        MODEL_FILE,
        MODEL_META,
    )
    for p in required:
        if not p.exists():
            raise RuntimeError(f"Falta archivo requerido: {p}")

    final = load_json(PREREG_FINAL)
    if int(final["horizon_seconds"]) != HORIZON:
        raise RuntimeError("Horizonte final no coincide con 60s.")
    if float(final["threshold"]) != THRESHOLD:
        raise RuntimeError("Threshold final no coincide con 0.0.")
    if int(final["development_cutoff_market_start_ms"]) != CUTOFF_MS:
        raise RuntimeError("Cutoff final no coincide.")
    if list(final["reserved_post_cutoff_rows"]["final_robustness150"]) != [151, 300]:
        raise RuntimeError("Reserva final 151-300 no coincide.")
    if int(final["audit_rows"]) != FINAL_ROWS:
        raise RuntimeError("audit_rows no coincide con 150.")

    multi = load_json(PREREG_MULTI)
    if list(multi["features"]) != FEATURES:
        raise RuntimeError("Features no coinciden con prereg_v097_multihorizon.json.")

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

    return final, meta


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


def parse_record(row, fee_rate, slippage):
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
        rec = parse_record(row, fee_rate, slippage)
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


def prob_metrics(y, p):
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
    pos_total = float(positive.sum()) if len(positive) else 0.0
    largest_pos = float(positive.max()) if len(positive) else 0.0
    share = largest_pos / pos_total if pos_total > 0 else None

    return {
        "threshold": THRESHOLD,
        "trades": int(len(trades)),
        "wins": int(sum(int(t["won"]) for t in trades)),
        "win_rate": float(np.mean([t["won"] for t in trades])),
        "net_pnl": float(pnl.sum()),
        "roi_on_cost": float(pnl.sum() / cost.sum()) if cost.sum() else None,
        "mean_pnl": float(pnl.mean()),
        "edge_pnl_correlation": corr_safe(edge, pnl),
        "positive_pnl_total": pos_total,
        "largest_positive_trade_pnl": largest_pos,
        "largest_positive_trade_share": share,
    }


def status(db: Path):
    verify()
    rows, _ = load_valid_60s(db)
    future = [r for r in rows if r["market_start_ms"] > CUTOFF_MS]

    print("=" * 78)
    print("STATUS AUDITORIA FINAL V0.9.7")
    print("=" * 78)
    print("Mercados válidos post-cutoff:", len(future))
    print("Forward50 reservado:", min(len(future), 50), "/ 50")
    print("Confirmation100 reservado:", max(0, min(len(future), 150) - 50), "/ 100")
    print("Final150 disponibles:", max(0, min(len(future), 300) - 150), "/ 150")
    print("Faltan para completar fila 300:", max(0, 300 - len(future)))
    print("Forward50 resultado existe:", FORWARD50_RESULT.exists())
    print("Confirmation100 resultado existe:", CONFIRM_RESULT.exists())
    print("Auditoría final ejecutada:", FINAL_RESULT.exists())
    print("DINERO REAL: BLOQUEADO")
    print("=" * 78)


def final150(db: Path):
    final_prereg, meta = verify()

    if FINAL_RESULT.exists():
        report = load_json(FINAL_RESULT)
        print("=" * 78)
        print("AUDITORIA FINAL V0.9.7 YA EJECUTADA - NO SE SOBRESCRIBE")
        print("=" * 78)
        print("VEREDICTO:", report["verdict"])
        print("Trades:", report["trading"]["trades"])
        print("PnL:", report["trading"]["net_pnl"])
        print("Archivo:", FINAL_RESULT)
        print("DINERO REAL: BLOQUEADO")
        print("=" * 78)
        return

    if not FORWARD50_RESULT.exists():
        raise RuntimeError("Falta forward50_v097.json.")
    if not CONFIRM_RESULT.exists():
        raise RuntimeError("Falta confirmation100_v097.json.")

    fwd = load_json(FORWARD50_RESULT)
    conf = load_json(CONFIRM_RESULT)

    if fwd.get("verdict") != "PASS_FORWARD50_DEVELOPMENT_GATE":
        raise RuntimeError("Forward50 no pasó; auditoría final no debe ejecutarse.")
    if conf.get("verdict") != "PASS_CONFIRMATION100":
        raise RuntimeError("Confirmation100 no pasó; auditoría final no debe ejecutarse.")

    rows, costs = load_valid_60s(db)
    future = [r for r in rows if r["market_start_ms"] > CUTOFF_MS]

    if len(future) < FINAL_END_INDEX:
        print("=" * 78)
        print("AUDITORIA FINAL V0.9.7 - ESPERANDO DATOS")
        print("=" * 78)
        print("Post-cutoff disponibles:", len(future))
        print("Final150 disponibles:", max(0, len(future) - FINAL_START_INDEX))
        print("Faltan para filas 151-300:", FINAL_END_INDEX - len(future))
        print("NO se evaluó ningún resultado parcial.")
        print("DINERO REAL: BLOQUEADO")
        print("=" * 78)
        return

    test = future[FINAL_START_INDEX:FINAL_END_INDEX]
    if len(test) != FINAL_ROWS:
        raise RuntimeError(f"Se esperaban 150 filas y hay {len(test)}.")

    bundle = joblib.load(MODEL_FILE)
    model = bundle["model"]

    y = np.asarray([r["y"] for r in test], dtype=int)
    pm = np.asarray([r["p_market"] for r in test], dtype=float)
    p = anchored_predict(model, test)

    cand = prob_metrics(y, p)
    market = prob_metrics(y, pm)
    delta = cand["brier"] - market["brier"]

    trades = make_trades(test, p)
    tm = trade_metrics(trades)

    first_rows = test[:HALF]
    second_rows = test[HALF:]
    first_probs = p[:HALF]
    second_probs = p[HALF:]

    first_tm = trade_metrics(make_trades(first_rows, first_probs))
    second_tm = trade_metrics(make_trades(second_rows, second_probs))

    reasons = []
    if delta > MAX_BRIER_DELTA:
        reasons.append("BRIER_WORSE_THAN_MARKET_PLUS_0.005")
    if tm["trades"] < MIN_TRADES:
        reasons.append("LESS_THAN_30_TRADES")
    if not (tm["net_pnl"] > 0):
        reasons.append("NET_PNL_NOT_POSITIVE")
    if tm["roi_on_cost"] is None or not (tm["roi_on_cost"] > 0):
        reasons.append("ROI_NOT_POSITIVE")
    if tm["edge_pnl_correlation"] is None or not (tm["edge_pnl_correlation"] > 0):
        reasons.append("EDGE_PNL_CORRELATION_NOT_POSITIVE")
    if (
        tm["largest_positive_trade_share"] is None
        or tm["largest_positive_trade_share"] > MAX_SINGLE_POSITIVE_PNL_SHARE
    ):
        reasons.append("SINGLE_TRADE_POSITIVE_PNL_SHARE_ABOVE_0.35")
    if first_tm["net_pnl"] < 0:
        reasons.append("FIRST_HALF_NET_PNL_NEGATIVE")
    if second_tm["net_pnl"] < 0:
        reasons.append("SECOND_HALF_NET_PNL_NEGATIVE")

    passed = len(reasons) == 0
    verdict = "PASS_FINAL_ROBUSTNESS" if passed else "FAIL_FINAL_ROBUSTNESS"

    report = {
        "schema": "final_robustness150_v097",
        "created_at": now_utc(),
        "rows_exactly": FINAL_ROWS,
        "reserved_post_cutoff_rows": [151, 300],
        "first_market_start_ms": test[0]["market_start_ms"],
        "last_market_start_ms": test[-1]["market_start_ms"],
        "candidate": cand,
        "market": market,
        "brier_delta_candidate_minus_market": float(delta),
        "trading": tm,
        "first_half_75": first_tm,
        "second_half_75": second_tm,
        "pass": passed,
        "fail_reasons": reasons,
        "verdict": verdict,
        "gate": {
            "minimum_trades": MIN_TRADES,
            "net_pnl": ">0",
            "roi_on_cost": ">0",
            "brier_candidate_minus_market_max": MAX_BRIER_DELTA,
            "edge_pnl_correlation": ">0",
            "maximum_single_trade_share_of_positive_pnl": MAX_SINGLE_POSITIVE_PNL_SHARE,
            "first_half_net_pnl": ">=0",
            "second_half_net_pnl": ">=0",
        },
        "model_sha256": meta["model_sha256"],
        "prereg_final_sha256": sha256_file(PREREG_FINAL),
        "warning": (
            "PASS_FINAL_ROBUSTNESS NO habilita automáticamente dinero real. "
            "Solo permite discutir una fase posterior paper/live-small controlada."
        ),
        "money_real": "BLOQUEADO",
    }

    FINAL_RESULT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 88)
    print("AUDITORIA FINAL V0.9.7 - RESULTADO")
    print("=" * 88)
    print("Filas evaluadas       :", len(test), "(post-cutoff 151-300)")
    print("Brier candidato       :", round(cand["brier"], 6))
    print("Brier mercado         :", round(market["brier"], 6))
    print("Delta Brier           :", round(delta, 6))
    print("AUC candidato         :", None if cand["auc"] is None else round(cand["auc"], 6))
    print("Accuracy candidato    :", round(cand["accuracy"], 4))
    print("Accuracy mercado      :", round(market["accuracy"], 4))
    print()
    print("Trades                :", tm["trades"])
    print("PnL neto              :", round(tm["net_pnl"], 6))
    print("ROI sobre costo       :", tm["roi_on_cost"])
    print("Corr edge/PnL         :", tm["edge_pnl_correlation"])
    print("Mayor trade / PnL +   :", tm["largest_positive_trade_share"])
    print("PnL primera mitad (75):", round(first_tm["net_pnl"], 6))
    print("PnL segunda mitad (75):", round(second_tm["net_pnl"], 6))
    print()
    print("VEREDICTO:", verdict)
    if reasons:
        print("FALLOS:", ", ".join(reasons))
    print("Reporte:", FINAL_RESULT)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 88)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_DEFAULT))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--final150", action="store_true")
    args = ap.parse_args()

    db = Path(args.db).expanduser().resolve()
    if not db.exists():
        raise SystemExit(f"No existe DB: {db}")

    if args.status:
        status(db)
    else:
        final150(db)


if __name__ == "__main__":
    main()
