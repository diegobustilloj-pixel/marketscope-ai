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

CUTOFF_MS = 1786394100000
EXPECTED_BASE_COMMON = 159
EXPECTED_CONSUMED_COMMON = 50
EXPECTED_DEV = 209

EPS = 1e-9


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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


def q(vals, quantile):
    a = np.asarray([x for x in vals if finite(x)], dtype=float)
    if len(a) == 0:
        return None
    return float(np.quantile(a, quantile))


def sign(x):
    if not finite(x) or x == 0:
        return 0
    return 1 if x > 0 else -1


def delta(a60, a120, key):
    x60 = safe_float(a60.get(key))
    x120 = safe_float(a120.get(key))
    if not (finite(x60) and finite(x120)):
        return np.nan
    return float(x60 - x120)


def verify_prereg():
    if not PREREG.exists():
        raise RuntimeError(f"Falta preregistro: {PREREG}")
    d = json.loads(PREREG.read_text(encoding="utf-8"))
    if d.get("schema") != "prereg_v011_event_driven":
        raise RuntimeError("Schema de preregistro v0.11 inesperado.")
    if int(d.get("development_rows_expected", -1)) != EXPECTED_DEV:
        raise RuntimeError("development_rows_expected debe ser 209.")
    if int(d.get("horizon_entry_seconds", -1)) != 60:
        raise RuntimeError("horizon_entry_seconds debe ser 60.")
    if len(d.get("event_families", [])) != 3:
        raise RuntimeError("El preregistro debe tener exactamente 3 familias.")
    return d


def open_ro(path: Path):
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def get_selected_60s(con):
    # IMPORTANTE:
    # - No lee m.label ni resultados/PnL.
    # - label_verified solo reproduce la población previamente congelada.
    base = []
    cur = con.execute(
        """
        SELECT m.condition_id,m.market_start_ms,f.feature_json
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
            base.append((str(r["condition_id"]), int(r["market_start_ms"]), d))

    consumed = []
    cur = con.execute(
        """
        SELECT m.condition_id,m.market_start_ms,f.feature_json
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
            consumed.append((str(r["condition_id"]), int(r["market_start_ms"]), d))
            if len(consumed) == 50:
                break

    if len(consumed) != 50:
        raise RuntimeError(f"Se esperaban 50 mercados consumidos 60s fresh; hay {len(consumed)}.")
    return base, consumed


def fetch_120_for_ids(con, ids):
    if not ids:
        return {}
    out = {}
    step = 400
    for i in range(0, len(ids), step):
        block = ids[i:i+step]
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


def load_dev_features(db: Path):
    con = open_ro(db)
    try:
        base60, consumed60 = get_selected_60s(con)
        ids = [x[0] for x in base60] + [x[0] for x in consumed60]
        f120 = fetch_120_for_ids(con, ids)
    finally:
        con.close()

    base_common = [r for r in base60 if r[0] in f120]
    consumed_common = [r for r in consumed60 if r[0] in f120]

    if len(base_common) != EXPECTED_BASE_COMMON:
        raise RuntimeError(
            f"Base común esperada {EXPECTED_BASE_COMMON}; obtenida {len(base_common)}."
        )
    if len(consumed_common) != EXPECTED_CONSUMED_COMMON:
        raise RuntimeError(
            f"Forward50 común esperado {EXPECTED_CONSUMED_COMMON}; obtenido {len(consumed_common)}."
        )

    rows = []
    for cid, ms, d60 in base_common + consumed_common:
        d120 = f120[cid]

        p60 = safe_float(d60.get("implied_up_mid_probability"))
        p120 = safe_float(d120.get("implied_up_mid_probability"))
        market_move_bps = (
            float((p60 - p120) * 10000.0)
            if finite(p60) and finite(p120) else np.nan
        )

        twap_move = delta(d60, d120, "twap_distance_to_open_bps")

        bin_ret = safe_float(d60.get("binance_return_60s_bps"))
        cl_ret = safe_float(d60.get("chainlink_return_60s_bps"))

        bsgn = sign(bin_ret)
        csgn = sign(cl_ret)
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
        book_raw = (
            float(dui - ddi)
            if finite(dui) and finite(ddi) else np.nan
        )
        directional_book_pressure = (
            float(consensus_dir * book_raw)
            if consensus_dir != 0 and finite(book_raw) else np.nan
        )

        row = {
            "condition_id": cid,
            "market_start_ms": ms,
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
            "price_change_activity_acceleration": delta(
                d60, d120, "price_change_messages_60s"
            ),
            "book_activity_acceleration": delta(
                d60, d120, "book_messages_60s"
            ),
            "trade_volume_acceleration": delta(
                d60, d120, "polymarket_trade_volume_60s"
            ),
            "trade_count_acceleration": delta(
                d60, d120, "polymarket_trade_count_60s"
            ),
            "depth_change_up_bid_1c": delta(
                d60, d120, "up_bid_depth_1c"
            ),
            "depth_change_up_ask_1c": delta(
                d60, d120, "up_ask_depth_1c"
            ),
            "depth_change_down_bid_1c": delta(
                d60, d120, "down_bid_depth_1c"
            ),
            "depth_change_down_ask_1c": delta(
                d60, d120, "down_ask_depth_1c"
            ),
            "up_best_ask_60": safe_float(d60.get("up_best_ask")),
            "down_best_ask_60": safe_float(d60.get("down_best_ask")),
            "up_spread_60": safe_float(d60.get("up_spread")),
            "down_spread_60": safe_float(d60.get("down_spread")),
            "market_ask_overround_60": safe_float(d60.get("market_ask_overround")),
        }
        rows.append(row)

    if len(rows) != EXPECTED_DEV:
        raise RuntimeError(f"Desarrollo esperado 209; obtenido {len(rows)}.")
    return rows


def coverage(rows, key):
    n = sum(1 for r in rows if finite(r.get(key)))
    return {"n": n, "pct": 100.0 * n / len(rows)}


def activity_medians(rows):
    return {
        "price_change_activity_acceleration": q(
            [r["price_change_activity_acceleration"] for r in rows], 0.50
        ),
        "book_activity_acceleration": q(
            [r["book_activity_acceleration"] for r in rows], 0.50
        ),
        "trade_volume_acceleration": q(
            [r["trade_volume_acceleration"] for r in rows], 0.50
        ),
    }


def build_thresholds(rows):
    twap_abs = [abs(r["twap_move_bps"]) for r in rows if finite(r["twap_move_bps"])]
    twap_resp = [
        r["twap_response_ratio"]
        for r in rows if finite(r["twap_response_ratio"])
    ]

    ext_shock = [
        r["external_consensus_shock_bps"]
        for r in rows if finite(r["external_consensus_shock_bps"])
    ]
    ext_resp = [
        r["external_response_ratio"]
        for r in rows if finite(r["external_response_ratio"])
    ]
    book_pos = [
        r["directional_book_pressure"]
        for r in rows
        if finite(r["directional_book_pressure"]) and r["directional_book_pressure"] > 0
    ]

    act = activity_medians(rows)

    thresholds = {
        "twap_shock_market_lag": {
            "standard": {
                "shock_abs_bps_min": q(twap_abs, 0.75),
                "market_response_ratio_max": q(twap_resp, 0.50),
            },
            "strict": {
                "shock_abs_bps_min": q(twap_abs, 0.90),
                "market_response_ratio_max": q(twap_resp, 0.35),
            },
        },
        "external_consensus_market_lag": {
            "standard": {
                "consensus_shock_bps_min": q(ext_shock, 0.75),
                "market_response_ratio_max": q(ext_resp, 0.50),
            },
            "strict": {
                "consensus_shock_bps_min": q(ext_shock, 0.90),
                "market_response_ratio_max": q(ext_resp, 0.35),
            },
        },
        "external_shock_orderbook_confirmation": {
            "standard": {
                "consensus_shock_bps_min": q(ext_shock, 0.75),
                "market_response_ratio_max": q(ext_resp, 0.65),
                "directional_book_pressure_min": q(book_pos, 0.60),
                "minimum_activity_confirmations": 1,
            },
            "strict": {
                "consensus_shock_bps_min": q(ext_shock, 0.90),
                "market_response_ratio_max": q(ext_resp, 0.50),
                "directional_book_pressure_min": q(book_pos, 0.75),
                "minimum_activity_confirmations": 2,
            },
        },
        "activity_reference_medians": act,
    }
    return thresholds


def activity_confirmations(r, med):
    cnt = 0
    for k, thr in med.items():
        v = r.get(k)
        if finite(v) and finite(thr) and v >= thr:
            cnt += 1
    return cnt


def matches_rule(r, family, severity, thresholds):
    t = thresholds[family][severity]

    if family == "twap_shock_market_lag":
        return bool(
            r["twap_direction"] != 0
            and finite(r["twap_move_bps"])
            and abs(r["twap_move_bps"]) >= t["shock_abs_bps_min"]
            and finite(r["twap_response_ratio"])
            and r["twap_response_ratio"] <= t["market_response_ratio_max"]
            and finite(r["up_best_ask_60"])
            and finite(r["down_best_ask_60"])
        )

    if family == "external_consensus_market_lag":
        return bool(
            r["external_consensus_direction"] != 0
            and finite(r["external_consensus_shock_bps"])
            and r["external_consensus_shock_bps"] >= t["consensus_shock_bps_min"]
            and finite(r["external_response_ratio"])
            and r["external_response_ratio"] <= t["market_response_ratio_max"]
            and finite(r["up_best_ask_60"])
            and finite(r["down_best_ask_60"])
        )

    if family == "external_shock_orderbook_confirmation":
        return bool(
            r["external_consensus_direction"] != 0
            and finite(r["external_consensus_shock_bps"])
            and r["external_consensus_shock_bps"] >= t["consensus_shock_bps_min"]
            and finite(r["external_response_ratio"])
            and r["external_response_ratio"] <= t["market_response_ratio_max"]
            and finite(r["directional_book_pressure"])
            and r["directional_book_pressure"] >= t["directional_book_pressure_min"]
            and activity_confirmations(
                r, thresholds["activity_reference_medians"]
            ) >= t["minimum_activity_confirmations"]
            and finite(r["up_best_ask_60"])
            and finite(r["down_best_ask_60"])
        )

    raise ValueError(family)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_DEFAULT))
    ap.add_argument("--freeze-thresholds", action="store_true")
    args = ap.parse_args()

    if not args.freeze_thresholds:
        raise SystemExit("Use --freeze-thresholds")

    prereg = verify_prereg()

    if THRESHOLDS.exists():
        d = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
        print("=" * 86)
        print("UMBRALES V0.11 YA CONGELADOS - NO SE SOBRESCRIBEN")
        print("=" * 86)
        print("ARCHIVO:", THRESHOLDS)
        print("DESARROLLO:", d["development_rows"])
        print("DINERO REAL: BLOQUEADO")
        print("=" * 86)
        return

    db = Path(args.db).expanduser().resolve()
    if not db.exists():
        raise SystemExit(f"No existe DB: {db}")

    rows = load_dev_features(db)
    thresholds = build_thresholds(rows)

    important = [
        "twap_move_bps",
        "market_probability_move_bps",
        "twap_response_ratio",
        "binance_return_60s_bps",
        "chainlink_return_60s_bps",
        "external_consensus_shock_bps",
        "external_response_ratio",
        "directional_book_pressure",
        "price_change_activity_acceleration",
        "book_activity_acceleration",
        "trade_volume_acceleration",
    ]
    cov = {k: coverage(rows, k) for k in important}

    counts = {}
    for family in prereg["event_families"]:
        counts[family] = {}
        for severity in ("standard", "strict"):
            counts[family][severity] = sum(
                1 for r in rows
                if matches_rule(r, family, severity, thresholds)
            )

    out = {
        "schema": "prereg_v011_event_thresholds",
        "created_at": now_utc(),
        "parent_prereg_sha256": sha256_file(PREREG),
        "development_rows": len(rows),
        "population": {
            "base_common_120_60": EXPECTED_BASE_COMMON,
            "consumed_forward50_common": EXPECTED_CONSUMED_COMMON,
            "future_new_rows_used": 0,
        },
        "label_or_pnl_usage": (
            "NO se leyó m.label, outcome ni PnL. label_verified=1 se usa únicamente "
            "para reproducir la población ya congelada."
        ),
        "calibration_method": {
            "standard_shock_quantile": 0.75,
            "strict_shock_quantile": 0.90,
            "standard_lag_response_quantile": 0.50,
            "strict_lag_response_quantile": 0.35,
            "orderbook_standard_positive_quantile": 0.60,
            "orderbook_strict_positive_quantile": 0.75,
            "activity_reference_quantile": 0.50,
        },
        "signal_definitions": {
            "twap_move_bps": (
                "twap_distance_to_open_bps@60 - twap_distance_to_open_bps@120"
            ),
            "market_probability_move_bps": (
                "(implied_up_mid_probability@60 - implied_up_mid_probability@120)*10000"
            ),
            "twap_response_ratio": (
                "sign(twap_move)*market_probability_move_bps/abs(twap_move)"
            ),
            "external_consensus_shock_bps": (
                "min(abs(binance_return_60s_bps),abs(chainlink_return_60s_bps)) "
                "solo cuando ambos tienen el mismo signo"
            ),
            "external_response_ratio": (
                "external_direction*market_probability_move_bps/"
                "external_consensus_shock_bps"
            ),
            "directional_book_pressure": (
                "external_direction * (delta_up_order_imbalance_1c - "
                "delta_down_order_imbalance_1c)"
            ),
        },
        "thresholds": thresholds,
        "development_event_counts_without_outcomes": counts,
        "coverage": cov,
        "rule_limit": "3 familias x 2 severidades = 6 reglas máximas",
        "next_step": (
            "Congelar evaluador y recién después medir outcomes/PnL en estas 209 filas. "
            "Los mercados futuros nuevos siguen intactos."
        ),
        "real_money": "BLOQUEADO",
    }

    THRESHOLDS.write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 96)
    print("V0.11 EVENT-DRIVEN - UMBRALES CONGELADOS SIN LABELS/PNL")
    print("=" * 96)
    print("DESARROLLO:", len(rows))
    print("FUTUROS NUEVOS USADOS: 0")
    print()
    print("COBERTURA:")
    for k in important:
        print(f"  {k:40s} {cov[k]['n']:3d}/{len(rows)} = {cov[k]['pct']:5.1f}%")
    print()
    print("EVENTOS DETECTADOS EN DESARROLLO (SIN MIRAR RESULTADOS):")
    for family in prereg["event_families"]:
        print(
            f"  {family:42s} "
            f"standard={counts[family]['standard']:3d} "
            f"strict={counts[family]['strict']:3d}"
        )
    print()
    print("ARCHIVO:", THRESHOLDS)
    print("SIGUIENTE PASO: congelar evaluador antes de mirar PnL.")
    print("DINERO REAL: BLOQUEADO")
    print("=" * 96)


if __name__ == "__main__":
    main()
