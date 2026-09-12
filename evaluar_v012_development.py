from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import calibrar_v012_execution_ev as cal

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

SHADOW_DB = DATA / "shadow_forward_twap_transfer_v094a.db"
PREREG = DATA / "prereg_v012_execution_ev.json"
IMPL = DATA / "prereg_v012_implementation.json"
THRESHOLDS = DATA / "prereg_v012_thresholds.json"
SPEC = DATA / "prereg_v012_evaluator.json"
RESULT = DATA / "resultado_v012_development100.json"

ORDER_SIZE = 5.0
DEV_ROWS = 100
SIMPLICITY_ORDER = [
    "standard_wide",
    "standard_central",
    "strict_wide",
    "strict_central",
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def freeze_spec() -> None:
    cal.verify()

    spec = {
        "schema": "prereg_v012_evaluator",
        "created_at": now(),
        "parent_prereg_sha256": sha(PREREG),
        "implementation_sha256": sha(IMPL),
        "development_rows": DEV_ROWS,
        "evaluation_population": "exactamente primeros 100 elegibles cronologicos definidos por prereg_v012_implementation",
        "labels": "leer solo label y label_verified para esos 100 condition_id; si falta un label verificado, NO evaluar",
        "outcome_normalization": {"UP": "UP", "DOWN": "DOWN"},
        "prediction_side": "UP si direction>0; DOWN si direction<0",
        "entry_cost": "chosen_side_cost_60 = VWAP real 5 shares + fee real",
        "pnl_per_share": "si acierta: 1-entry_cost; si falla: -entry_cost",
        "pnl_total_trade": "pnl_per_share * 5",
        "roi_on_cost": "sum(pnl_total_trade)/sum(entry_cost*5)",
        "halves": "trades del candidato en orden cronologico; first_half = primeros floor(n/2), second_half = restantes",
        "positive_trade_share": "max(pnl positivo individual)/sum(pnl positivos); si no hay positivos = 1.0",
        "gates": {
            "minimum_trades": 10,
            "net_pnl": ">0",
            "roi_on_cost": ">0",
            "first_half_net_pnl": ">=0",
            "second_half_net_pnl": ">=0",
            "maximum_single_positive_trade_share": "<=0.35",
        },
        "candidate_selection": "solo candidatos que pasan todos los gates; mayor net_pnl; desempate mayor ROI; luego simplicity_order",
        "simplicity_order": SIMPLICITY_ORDER,
        "no_threshold_rescue": True,
        "future_forward_labels_read": False,
        "real_money": "BLOQUEADO",
    }

    if SPEC.exists():
        old = load(SPEC)
        if old.get("schema") != spec["schema"]:
            raise RuntimeError("Existe evaluator prereg incompatible; no se sobrescribe.")
        print("EVALUADOR V0.12 YA CONGELADO - NO SE SOBRESCRIBE")
        print("SHA256:", sha(SPEC))
        return

    SPEC.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print("=" * 88)
    print("EVALUADOR V0.12 DEVELOPMENT100 CONGELADO")
    print("=" * 88)
    print("DEVELOPMENT:", DEV_ROWS)
    print("ORDER SIZE:", ORDER_SIZE)
    print("CANDIDATOS:", len(SIMPLICITY_ORDER))
    print("LABELS LEIDOS AHORA: False")
    print("FORWARD LABELS LEIDOS: False")
    print("ARCHIVO:", SPEC)
    print("SHA256:", sha(SPEC))
    print("DINERO REAL: BLOQUEADO")
    print("=" * 88)


def read_labels(condition_ids: list[str]) -> dict[str, str]:
    if not condition_ids:
        return {}

    c = cal.ro(SHADOW_DB)
    try:
        placeholders = ",".join("?" for _ in condition_ids)
        rows = c.execute(
            f"""
            SELECT condition_id,label,label_verified
            FROM shadow_markets
            WHERE condition_id IN ({placeholders})
            """,
            condition_ids,
        ).fetchall()
    finally:
        c.close()

    out: dict[str, str] = {}
    for r in rows:
        cid = str(r["condition_id"])
        if int(r["label_verified"] or 0) != 1:
            continue
        lab = str(r["label"] or "").strip().upper()
        if lab in ("UP", "DOWN"):
            out[cid] = lab
    return out


def evaluate() -> None:
    cal.verify()

    if not SPEC.exists():
        raise RuntimeError("Primero ejecuta --freeze-spec.")
    if not THRESHOLDS.exists():
        print("THRESHOLDS TODAVIA NO EXISTEN.")
        print("Primero espera 100 elegibles y ejecuta:")
        print(r".\.venv\Scripts\python.exe calibrar_v012_execution_ev.py --freeze-thresholds")
        return
    if RESULT.exists():
        print("RESULTADO DEVELOPMENT100 YA EXISTE - NO SE SOBRESCRIBE")
        print("ARCHIVO:", RESULT)
        return

    spec = load(SPEC)
    th = load(THRESHOLDS)

    if spec.get("parent_prereg_sha256") != sha(PREREG):
        raise RuntimeError("Prereg principal cambió después de congelar evaluador.")
    if spec.get("implementation_sha256") != sha(IMPL):
        raise RuntimeError("Implementation cambió después de congelar evaluador.")
    if th.get("parent_prereg_sha256") != sha(PREREG):
        raise RuntimeError("Thresholds no corresponden al prereg actual.")
    if th.get("implementation_sha256") != sha(IMPL):
        raise RuntimeError("Thresholds no corresponden a implementation actual.")
    if th.get("labels_read") is not False or th.get("pnl_read") is not False:
        raise RuntimeError("Thresholds no son label-free.")

    eligible = cal.eligible()
    if len(eligible) < DEV_ROWS:
        print("DEVELOPMENT100 TODAVIA NO LISTO:", len(eligible), "/100")
        print("FALTAN:", DEV_ROWS - len(eligible))
        return

    dev = eligible[:DEV_ROWS]
    ids = [r["condition_id"] for r in dev]
    labels = read_labels(ids)

    missing = [cid for cid in ids if cid not in labels]
    if missing:
        print("NO SE EVALUA: faltan labels verificados para", len(missing), "de 100 mercados.")
        print("Primeros faltantes:", ", ".join(missing[:5]))
        return

    thresholds = th["thresholds"]
    bands = th["cost_bands"]
    rules = th["candidate_rules"]

    results: dict[str, Any] = {}

    for name in SIMPLICITY_ORDER:
        severity, band_name = rules[name]
        t = thresholds[severity]
        lo, hi = map(float, bands[band_name])

        trades = []
        for r in dev:
            cost = r["chosen_side_cost_60"]
            event = (
                r["abs_twap_move_bps"] >= float(t["shock_abs_bps_min"])
                and r["market_response_ratio"] <= float(t["market_response_ratio_max"])
            )
            if not event or cost is None or not (lo <= cost <= hi):
                continue

            side = "UP" if r["direction"] > 0 else "DOWN"
            outcome = labels[r["condition_id"]]
            win = side == outcome
            pnl_ps = (1.0 - cost) if win else (-cost)
            pnl = pnl_ps * ORDER_SIZE
            trades.append({
                "condition_id": r["condition_id"],
                "market_start_ms": r["market_start_ms"],
                "side": side,
                "outcome": outcome,
                "win": win,
                "entry_cost_per_share": cost,
                "pnl_per_share": pnl_ps,
                "pnl_total": pnl,
            })

        n = len(trades)
        wins = sum(1 for x in trades if x["win"])
        net = sum(x["pnl_total"] for x in trades)
        capital = sum(x["entry_cost_per_share"] * ORDER_SIZE for x in trades)
        roi = net / capital if capital > 0 else float("-inf")

        cut = n // 2
        first = sum(x["pnl_total"] for x in trades[:cut])
        second = sum(x["pnl_total"] for x in trades[cut:])

        positives = [x["pnl_total"] for x in trades if x["pnl_total"] > 0]
        gross_positive = sum(positives)
        pos_share = (
            max(positives) / gross_positive
            if positives and gross_positive > 0
            else 1.0
        )

        failures = []
        if n < 10: failures.append("MIN_TRADES")
        if not (net > 0): failures.append("NET_PNL")
        if not (roi > 0): failures.append("ROI")
        if not (first >= 0): failures.append("FIRST_HALF")
        if not (second >= 0): failures.append("SECOND_HALF")
        if not (pos_share <= 0.35): failures.append("POS_SHARE")

        results[name] = {
            "trades": n,
            "wins": wins,
            "win_rate": wins / n if n else None,
            "net_pnl_5shares": net,
            "net_pnl_per_share_equivalent": net / ORDER_SIZE,
            "capital_deployed": capital,
            "roi_on_cost": roi if math.isfinite(roi) else None,
            "first_half_net_pnl": first,
            "second_half_net_pnl": second,
            "maximum_single_positive_trade_share": pos_share,
            "pass": len(failures) == 0,
            "failures": failures,
            "trades_detail": trades,
        }

    passing = [name for name in SIMPLICITY_ORDER if results[name]["pass"]]
    rank_index = {name: i for i, name in enumerate(SIMPLICITY_ORDER)}

    if passing:
        selected = sorted(
            passing,
            key=lambda name: (
                -results[name]["net_pnl_5shares"],
                -results[name]["roi_on_cost"],
                rank_index[name],
            ),
        )[0]
        status = "PASS_DEVELOPMENT"
    else:
        selected = None
        status = "FAIL_DEVELOPMENT"

    out = {
        "schema": "resultado_v012_development100",
        "created_at": now(),
        "development_rows": DEV_ROWS,
        "thresholds_sha256": sha(THRESHOLDS),
        "evaluator_spec_sha256": sha(SPEC),
        "labels_read": DEV_ROWS,
        "forward_labels_read": 0,
        "candidate_results": results,
        "selected_candidate": selected,
        "status": status,
        "real_money": "BLOQUEADO",
    }

    RESULT.write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 100)
    print("V0.12 DEVELOPMENT100 - EJECUCION REAL 5 SHARES")
    print("=" * 100)
    for name in SIMPLICITY_ORDER:
        r = results[name]
        print(
            f"{name:20s} trades={r['trades']:3d} "
            f"win={str(round(r['win_rate'],4)) if r['win_rate'] is not None else 'NA':>6s} "
            f"PnL5={r['net_pnl_5shares']:+.6f} "
            f"ROI={str(round(r['roi_on_cost'],6)) if r['roi_on_cost'] is not None else 'NA':>9s} "
            f"h1={r['first_half_net_pnl']:+.6f} "
            f"h2={r['second_half_net_pnl']:+.6f} "
            f"share={r['maximum_single_positive_trade_share']:.4f} "
            f"{'PASS' if r['pass'] else 'FAIL'}"
        )
        if r["failures"]:
            print(" " * 22 + "FALLOS:", ",".join(r["failures"]))
    print("-" * 100)
    print("STATUS:", status)
    print("SELECCIONADO:", selected)
    print("FORWARD LABELS LEIDOS: 0")
    print("ARCHIVO:", RESULT)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 100)


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--freeze-spec", action="store_true")
    g.add_argument("--evaluate", action="store_true")
    args = ap.parse_args()

    if args.freeze_spec:
        freeze_spec()
    else:
        evaluate()


if __name__ == "__main__":
    main()
