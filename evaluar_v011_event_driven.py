from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

DB_DEFAULT = DATA / "shadow_forward_twap_transfer_v094a.db"
PREREG = DATA / "prereg_v011_event_driven.json"
THRESHOLDS = DATA / "prereg_v011_event_thresholds.json"
EVALUATOR = DATA / "prereg_v011_evaluator.json"
RESULT = DATA / "resultado_v011_event_driven_development.json"

CUTOFF_MS = 1786394100000
EXPECTED_BASE_COMMON = 159
EXPECTED_CONSUMED_COMMON = 50
EXPECTED_DEV = 209
MIN_TRADES = 15
MAX_POS_SHARE = 0.35
EPS = 1e-9

FAMILIES = [
    "twap_shock_market_lag",
    "external_consensus_market_lag",
    "external_shock_orderbook_confirmation",
]
SEVERITIES = ["standard", "strict"]


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


def safe_float(v):
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
    if not finite(x) or float(x) == 0.0:
        return 0
    return 1 if float(x) > 0 else -1


def delta(a60, a120, key):
    x60 = safe_float(a60.get(key))
    x120 = safe_float(a120.get(key))
    if not (finite(x60) and finite(x120)):
        return np.nan
    return float(x60 - x120)


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


def verify_inputs():
    if not PREREG.exists():
        raise RuntimeError(f"Falta {PREREG}")
    if not THRESHOLDS.exists():
        raise RuntimeError(f"Falta {THRESHOLDS}")

    p = load_json(PREREG)
    t = load_json(THRESHOLDS)

    if p.get("schema") != "prereg_v011_event_driven":
        raise RuntimeError("Schema prereg v0.11 inesperado.")
    if t.get("schema") != "prereg_v011_event_thresholds":
        raise RuntimeError("Schema thresholds v0.11 inesperado.")
    if int(p.get("development_rows_expected", -1)) != EXPECTED_DEV:
        raise RuntimeError("Desarrollo esperado debe ser 209.")
    if int(t.get("development_rows", -1)) != EXPECTED_DEV:
        raise RuntimeError("Thresholds no fueron calibrados sobre 209 filas.")
    if t.get("parent_prereg_sha256") != sha256_file(PREREG):
        raise RuntimeError("El preregistro cambió después de congelar thresholds.")
    if list(p.get("event_families", [])) != FAMILIES:
        raise RuntimeError("Familias no coinciden con el diseño congelado.")
    return p, t


def freeze_evaluator():
    p, t = verify_inputs()

    if EVALUATOR.exists():
        print("=" * 86)
        print("EVALUADOR V0.11 YA CONGELADO - NO SE SOBRESCRIBE")
        print("=" * 86)
        print("ARCHIVO:", EVALUATOR)
        print("DINERO REAL: BLOQUEADO")
        print("=" * 86)
        return

    d = {
        "schema": "prereg_v011_evaluator",
        "created_at": now_utc(),
        "parent_prereg_sha256": sha256_file(PREREG),
        "thresholds_sha256": sha256_file(THRESHOLDS),
        "development_rows_expected": EXPECTED_DEV,
        "population_rule": (
            "Misma población congelada: 159 base common fresh 120s+60s + "
            "los mismos 50 mercados 60s fresh post-cutoff ya consumidos por v097, "
            "también common fresh 120s+60s. No reemplazar faltantes."
        ),
        "direction_rule": {
            "twap_shock_market_lag": (
                "UP si twap_move_bps>0; DOWN si twap_move_bps<0."
            ),
            "external_consensus_market_lag": (
                "UP si Binance y Chainlink 60s son ambos positivos; "
                "DOWN si ambos son negativos."
            ),
            "external_shock_orderbook_confirmation": (
                "Misma dirección de consenso Binance+Chainlink; orderbook solo confirma."
            ),
        },
        "execution": {
            "entry_horizon_seconds": 60,
            "fill_formula": "clip(best_ask_60 + slippage_per_share, 0.001, 0.999)",
            "fee_formula": "fee_rate * fill * (1-fill)",
            "cost_formula": "fill + fee",
            "realized_pnl_up": "1-label? no: y_up - up_cost",
            "realized_pnl_down": "(1-y_up) - down_cost",
            "unit": "PnL por share comprado",
        },
        "development_gate": {
            "minimum_trades": MIN_TRADES,
            "net_pnl": "positive",
            "roi_on_cost": "positive",
            "first_half_net_pnl": "nonnegative",
            "second_half_net_pnl": "nonnegative",
            "maximum_single_positive_trade_share": MAX_POS_SHARE,
        },
        "half_split": (
            "Mitades cronológicas de las 209 filas de desarrollo; "
            "cada trade pertenece a la mitad según market_start_ms."
        ),
        "evaluation_scope": "Evaluar exactamente las 6 reglas congeladas: 3 familias x 2 severidades.",
        "selection": (
            "Entre reglas que pasen todos los gates: mayor net PnL; "
            "desempate mayor ROI; después menor complejidad de familia "
            "(TWAP lag, consenso externo lag, orderbook confirmation); "
            "después standard antes que strict."
        ),
        "future_policy": (
            "Si ninguna regla pasa desarrollo, v0.11 termina y no se toca el test futuro. "
            "Si una regla pasa, congelar solo esa regla y esperar 100 mercados elegibles "
            "futuros antes de evaluar, sin peeking parcial."
        ),
        "real_money": "BLOQUEADO",
    }

    EVALUATOR.write_text(
        json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 86)
    print("EVALUADOR V0.11 CONGELADO ANTES DE MIRAR PNL")
    print("=" * 86)
    print("REGLAS A EVALUAR: 6")
    print("GATE MINIMO TRADES:", MIN_TRADES)
    print("MAX SHARE POSITIVO:", MAX_POS_SHARE)
    print("TEST FUTURO: NO SE TOCA EN ESTA ETAPA")
    print("ARCHIVO:", EVALUATOR)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 86)


def verify_evaluator():
    p, t = verify_inputs()
    if not EVALUATOR.exists():
        raise RuntimeError("Primero ejecute --freeze-evaluator.")
    e = load_json(EVALUATOR)
    if e.get("parent_prereg_sha256") != sha256_file(PREREG):
        raise RuntimeError("Preregistro cambió después de congelar evaluador.")
    if e.get("thresholds_sha256") != sha256_file(THRESHOLDS):
        raise RuntimeError("Thresholds cambiaron después de congelar evaluador.")
    return p, t, e


def taker_cost(ask, fee_rate, slip):
    fill = min(0.999, max(0.001, float(ask) + float(slip)))
    fee = float(fee_rate) * fill * (1.0 - fill)
    return float(fill + fee)


def get_selected_60s(con):
    base = []
    cur = con.execute(
        """
        SELECT m.condition_id,m.market_start_ms,m.label,f.feature_json
        FROM shadow_features f
        JOIN shadow_markets m ON m.condition_id=f.condition_id
        WHERE f.horizon_seconds=60
          AND f.feature_json IS NOT NULL
          AND m.label_verified=1
          AND m.market_start_ms<=?
        ORDER BY m.market_start_ms,m.condition_id
        """,
        (CUTOFF_MS,),
    )
    for r in cur:
        d = json.loads(r["feature_json"])
        if fresh(d):
            base.append((str(r["condition_id"]), int(r["market_start_ms"]), str(r["label"]), d))

    future = []
    cur = con.execute(
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
    )
    for r in cur:
        d = json.loads(r["feature_json"])
        if fresh(d):
            future.append((str(r["condition_id"]), int(r["market_start_ms"]), str(r["label"]), d))
            if len(future) == 50:
                break

    if len(future) != 50:
        raise RuntimeError(f"Se esperaban 50 mercados consumidos; hay {len(future)}.")
    return base, future


def fetch_120(con, ids):
    out = {}
    for i in range(0, len(ids), 400):
        block = ids[i:i+400]
        ph = ",".join("?" for _ in block)
        sql = f"""
            SELECT condition_id,feature_json
            FROM shadow_diagnostics
            WHERE horizon_seconds=120
              AND status='SAVED'
              AND feature_json IS NOT NULL
              AND condition_id IN ({ph})
        """
        for r in con.execute(sql, block):
            d = json.loads(r["feature_json"])
            if fresh(d):
                out[str(r["condition_id"])] = d
    return out


def make_feature_record(cid, ms, label, d120, d60, fee_rate, slip):
    p60 = safe_float(d60.get("implied_up_mid_probability"))
    p120 = safe_float(d120.get("implied_up_mid_probability"))
    market_move_bps = (
        float((p60 - p120) * 10000.0)
        if finite(p60) and finite(p120) else np.nan
    )

    twap_move = delta(d60, d120, "twap_distance_to_open_bps")
    bin_ret = safe_float(d60.get("binance_return_60s_bps"))
    cl_ret = safe_float(d60.get("chainlink_return_60s_bps"))

    bsgn, csgn = sign(bin_ret), sign(cl_ret)
    consensus_dir = bsgn if bsgn != 0 and bsgn == csgn else 0
    consensus_shock = (
        float(min(abs(bin_ret), abs(cl_ret)))
        if consensus_dir != 0 and finite(bin_ret) and finite(cl_ret)
        else np.nan
    )

    twap_dir = sign(twap_move)
    twap_response_ratio = (
        float(twap_dir * market_move_bps / (abs(twap_move) + EPS))
        if twap_dir != 0 and finite(market_move_bps) else np.nan
    )
    ext_response_ratio = (
        float(consensus_dir * market_move_bps / (consensus_shock + EPS))
        if consensus_dir != 0 and finite(market_move_bps) and finite(consensus_shock)
        else np.nan
    )

    dui = delta(d60, d120, "up_order_imbalance_1c")
    ddi = delta(d60, d120, "down_order_imbalance_1c")
    book_raw = float(dui - ddi) if finite(dui) and finite(ddi) else np.nan
    directional_book_pressure = (
        float(consensus_dir * book_raw)
        if consensus_dir != 0 and finite(book_raw) else np.nan
    )

    up_ask = safe_float(d60.get("up_best_ask"))
    down_ask = safe_float(d60.get("down_best_ask"))
    if not (finite(up_ask) and finite(down_ask)):
        return None

    y = 1 if label.lower() == "up" else 0
    up_cost = taker_cost(up_ask, fee_rate, slip)
    down_cost = taker_cost(down_ask, fee_rate, slip)

    return {
        "condition_id": cid,
        "market_start_ms": ms,
        "y": y,
        "twap_move_bps": twap_move,
        "market_probability_move_bps": market_move_bps,
        "twap_direction": twap_dir,
        "twap_response_ratio": twap_response_ratio,
        "binance_return_60s_bps": bin_ret,
        "chainlink_return_60s_bps": cl_ret,
        "external_consensus_direction": consensus_dir,
        "external_consensus_shock_bps": consensus_shock,
        "external_response_ratio": ext_response_ratio,
        "directional_book_pressure": directional_book_pressure,
        "price_change_activity_acceleration": delta(d60, d120, "price_change_messages_60s"),
        "book_activity_acceleration": delta(d60, d120, "book_messages_60s"),
        "trade_volume_acceleration": delta(d60, d120, "polymarket_trade_volume_60s"),
        "up_cost": up_cost,
        "down_cost": down_cost,
        "up_realized_pnl": float(y - up_cost),
        "down_realized_pnl": float((1 - y) - down_cost),
    }


def load_development(db: Path):
    con = open_ro(db)
    try:
        meta = read_meta(con)
        fee_rate = float(meta.get("fee_rate", 0.07))
        slip = float(meta.get("slippage_per_share", 0.005))
        base60, consumed60 = get_selected_60s(con)
        ids = [r[0] for r in base60] + [r[0] for r in consumed60]
        d120 = fetch_120(con, ids)
    finally:
        con.close()

    base_common = [r for r in base60 if r[0] in d120]
    consumed_common = [r for r in consumed60 if r[0] in d120]

    if len(base_common) != EXPECTED_BASE_COMMON:
        raise RuntimeError(f"Base común esperada 159; obtenida {len(base_common)}.")
    if len(consumed_common) != EXPECTED_CONSUMED_COMMON:
        raise RuntimeError(f"Consumidos common esperados 50; obtenidos {len(consumed_common)}.")

    rows = []
    for cid, ms, label, d60 in base_common + consumed_common:
        r = make_feature_record(cid, ms, label, d120[cid], d60, fee_rate, slip)
        if r is None:
            raise RuntimeError("Fila common sin ask observable.")
        rows.append(r)

    if len(rows) != EXPECTED_DEV:
        raise RuntimeError(f"Desarrollo esperado 209; obtenido {len(rows)}.")
    return rows, fee_rate, slip


def activity_confirmations(r, med):
    cnt = 0
    for k, thr in med.items():
        v = r.get(k)
        if finite(v) and finite(thr) and v >= float(thr):
            cnt += 1
    return cnt


def matches_rule(r, family, severity, thresholds):
    t = thresholds[family][severity]

    if family == "twap_shock_market_lag":
        return bool(
            r["twap_direction"] != 0
            and finite(r["twap_move_bps"])
            and abs(r["twap_move_bps"]) >= float(t["shock_abs_bps_min"])
            and finite(r["twap_response_ratio"])
            and r["twap_response_ratio"] <= float(t["market_response_ratio_max"])
        )

    if family == "external_consensus_market_lag":
        return bool(
            r["external_consensus_direction"] != 0
            and finite(r["external_consensus_shock_bps"])
            and r["external_consensus_shock_bps"] >= float(t["consensus_shock_bps_min"])
            and finite(r["external_response_ratio"])
            and r["external_response_ratio"] <= float(t["market_response_ratio_max"])
        )

    if family == "external_shock_orderbook_confirmation":
        return bool(
            r["external_consensus_direction"] != 0
            and finite(r["external_consensus_shock_bps"])
            and r["external_consensus_shock_bps"] >= float(t["consensus_shock_bps_min"])
            and finite(r["external_response_ratio"])
            and r["external_response_ratio"] <= float(t["market_response_ratio_max"])
            and finite(r["directional_book_pressure"])
            and r["directional_book_pressure"] >= float(t["directional_book_pressure_min"])
            and activity_confirmations(
                r, thresholds["activity_reference_medians"]
            ) >= int(t["minimum_activity_confirmations"])
        )

    raise ValueError(family)


def direction_for_rule(r, family):
    if family == "twap_shock_market_lag":
        return int(r["twap_direction"])
    return int(r["external_consensus_direction"])


def rule_metrics(all_rows, family, severity, thresholds):
    trades = []
    for r in all_rows:
        if not matches_rule(r, family, severity, thresholds):
            continue

        direction = direction_for_rule(r, family)
        if direction > 0:
            side = "UP"
            pnl = r["up_realized_pnl"]
            cost = r["up_cost"]
        elif direction < 0:
            side = "DOWN"
            pnl = r["down_realized_pnl"]
            cost = r["down_cost"]
        else:
            continue

        trades.append({
            "market_start_ms": r["market_start_ms"],
            "side": side,
            "pnl": float(pnl),
            "cost": float(cost),
            "win": bool(pnl > 0),
        })

    if not trades:
        return {
            "family": family,
            "severity": severity,
            "trades": 0,
            "wins": 0,
            "win_rate": None,
            "net_pnl": 0.0,
            "roi_on_cost": None,
            "first_half_net_pnl": 0.0,
            "second_half_net_pnl": 0.0,
            "largest_positive_trade_share": None,
            "eligible": False,
            "failures": ["MIN_TRADES", "NET_PNL", "ROI", "HALVES", "POS_SHARE"],
        }

    pnls = np.asarray([t["pnl"] for t in trades], dtype=float)
    costs = np.asarray([t["cost"] for t in trades], dtype=float)
    wins = int(np.sum(pnls > 0))
    net = float(pnls.sum())
    roi = float(net / costs.sum()) if costs.sum() > 0 else None

    midpoint_ms = all_rows[len(all_rows) // 2]["market_start_ms"]
    first = float(sum(t["pnl"] for t in trades if t["market_start_ms"] < midpoint_ms))
    second = float(sum(t["pnl"] for t in trades if t["market_start_ms"] >= midpoint_ms))

    pos = pnls[pnls > 0]
    if len(pos) and float(pos.sum()) > 0:
        share = float(pos.max() / pos.sum())
    else:
        share = None

    failures = []
    if len(trades) < MIN_TRADES:
        failures.append("MIN_TRADES")
    if net <= 0:
        failures.append("NET_PNL")
    if roi is None or roi <= 0:
        failures.append("ROI")
    if first < 0:
        failures.append("FIRST_HALF")
    if second < 0:
        failures.append("SECOND_HALF")
    if share is None or share > MAX_POS_SHARE:
        failures.append("POS_SHARE")

    return {
        "family": family,
        "severity": severity,
        "trades": len(trades),
        "wins": wins,
        "win_rate": float(wins / len(trades)),
        "net_pnl": net,
        "roi_on_cost": roi,
        "first_half_net_pnl": first,
        "second_half_net_pnl": second,
        "largest_positive_trade_share": share,
        "eligible": len(failures) == 0,
        "failures": failures,
    }


def evaluate_development(db: Path):
    p, t, e = verify_evaluator()

    if RESULT.exists():
        r = load_json(RESULT)
        print("=" * 96)
        print("V0.11 DEVELOPMENT YA EVALUADO - NO SE SOBRESCRIBE")
        print("=" * 96)
        print("SELECCIONADA:", r.get("selected_rule"))
        print("VEREDICTO:", r.get("verdict"))
        print("ARCHIVO:", RESULT)
        print("DINERO REAL: BLOQUEADO")
        print("=" * 96)
        return

    rows, fee_rate, slip = load_development(db)
    thresholds = t["thresholds"]

    metrics = []
    for family in FAMILIES:
        for severity in SEVERITIES:
            metrics.append(rule_metrics(rows, family, severity, thresholds))

    family_complexity = {
        "twap_shock_market_lag": 0,
        "external_consensus_market_lag": 1,
        "external_shock_orderbook_confirmation": 2,
    }
    severity_complexity = {"standard": 0, "strict": 1}

    eligible = [m for m in metrics if m["eligible"]]
    selected = None
    if eligible:
        selected = max(
            eligible,
            key=lambda m: (
                m["net_pnl"],
                m["roi_on_cost"],
                -family_complexity[m["family"]],
                -severity_complexity[m["severity"]],
            ),
        )

    verdict = (
        "CANDIDATE_FOUND_DEVELOPMENT_ONLY"
        if selected is not None
        else "FAIL_ALL_EVENT_RULES_DEVELOPMENT"
    )

    out = {
        "schema": "resultado_v011_event_driven_development",
        "created_at": now_utc(),
        "development_rows": len(rows),
        "fee_rate": fee_rate,
        "slippage_per_share": slip,
        "rules": metrics,
        "selected_rule": None if selected is None else {
            "family": selected["family"],
            "severity": selected["severity"],
            "development_metrics": selected,
        },
        "verdict": verdict,
        "future_test_touched": False,
        "next_step": (
            "Si FAIL: cerrar v0.11 y no evaluar futuro. "
            "Si candidato: congelar regla seleccionada y esperar 100 futuros elegibles."
        ),
        "real_money": "BLOQUEADO",
        "prereg_sha256": sha256_file(PREREG),
        "thresholds_sha256": sha256_file(THRESHOLDS),
        "evaluator_sha256": sha256_file(EVALUATOR),
    }

    RESULT.write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 108)
    print("V0.11 EVENT-DRIVEN - EVALUACION DEVELOPMENT")
    print("=" * 108)
    print("Desarrollo:", len(rows))
    print("Futuros nuevos tocados: 0")
    print()
    for m in metrics:
        roi = "None" if m["roi_on_cost"] is None else f"{m['roi_on_cost']:+.4f}"
        wr = "None" if m["win_rate"] is None else f"{m['win_rate']:.3f}"
        share = (
            "None"
            if m["largest_positive_trade_share"] is None
            else f"{m['largest_positive_trade_share']:.3f}"
        )
        print(
            f"{m['family']:42s} {m['severity']:8s} "
            f"trades={m['trades']:3d} win={wr:>5s} "
            f"PnL={m['net_pnl']:+.5f} ROI={roi:>8s} "
            f"h1={m['first_half_net_pnl']:+.5f} "
            f"h2={m['second_half_net_pnl']:+.5f} "
            f"share={share:>5s} eligible={m['eligible']}"
        )
        if m["failures"]:
            print("   FAIL:", ",".join(m["failures"]))

    print()
    print("SELECCIONADA:", None if selected is None else f"{selected['family']} / {selected['severity']}")
    print("VEREDICTO:", verdict)
    print("RESULTADO:", RESULT)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 108)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_DEFAULT))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--freeze-evaluator", action="store_true")
    g.add_argument("--evaluate-development", action="store_true")
    args = ap.parse_args()

    if args.freeze_evaluator:
        freeze_evaluator()
        return

    db = Path(args.db).expanduser().resolve()
    if not db.exists():
        raise SystemExit(f"No existe DB: {db}")
    evaluate_development(db)


if __name__ == "__main__":
    main()
