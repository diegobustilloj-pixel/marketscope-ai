from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import calibrar_v012_execution_ev as cal

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

SHADOW_DB = DATA / "shadow_forward_twap_transfer_v094a.db"
PREREG = DATA / "prereg_v012_execution_ev.json"
IMPL = DATA / "prereg_v012_implementation.json"
DEV_SPEC = DATA / "prereg_v012_evaluator.json"
THRESHOLDS = DATA / "prereg_v012_thresholds.json"
DEV_RESULT = DATA / "resultado_v012_development100.json"
FORWARD_SPEC = DATA / "prereg_v012_forward100.json"
FORWARD_RESULT = DATA / "resultado_v012_forward100.json"

ORDER_SIZE = 5.0
DEV_ROWS = 100
FORWARD_ROWS = 100


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

    if not DEV_SPEC.exists():
        raise RuntimeError("Falta prereg_v012_evaluator.json")

    spec = {
        "schema": "prereg_v012_forward100",
        "created_at": now(),
        "parent_prereg_sha256": sha(PREREG),
        "implementation_sha256": sha(IMPL),
        "development_evaluator_sha256": sha(DEV_SPEC),
        "population": "exactamente elegibles 101-200 en orden cronologico; los primeros 100 quedan reservados a development",
        "forward_rows": FORWARD_ROWS,
        "candidate": "usar exclusivamente selected_candidate de resultado_v012_development100.json; prohibido seleccionar o retocar con forward",
        "thresholds": "usar exactamente prereg_v012_thresholds.json usado por development",
        "labels": "leer solo los 100 labels verificados del forward cuando se ejecute --evaluate-forward; no mirar parcialmente",
        "entry_cost": "chosen_side_cost_60 = VWAP real de 5 shares + fee real",
        "pnl_per_share": "si acierta: 1-entry_cost; si falla: -entry_cost",
        "pnl_total_trade": "pnl_per_share*5",
        "roi_on_cost": "sum(pnl_total_trade)/sum(entry_cost*5)",
        "halves": "trades forward del candidato en orden cronologico; first_half=floor(n/2), second_half=restantes",
        "positive_trade_share": "max trade positivo/suma de trades positivos; sin positivos=1.0",
        "gates": {
            "minimum_trades": 10,
            "net_pnl": ">0",
            "roi_on_cost": ">0",
            "first_half_net_pnl": ">=0",
            "second_half_net_pnl": ">=0",
            "maximum_single_positive_trade_share": "<=0.35",
        },
        "no_partial_peeking": True,
        "no_threshold_rescue": True,
        "no_candidate_rescue": True,
        "real_money": "BLOQUEADO",
    }

    if FORWARD_SPEC.exists():
        old = load(FORWARD_SPEC)
        if old.get("schema") != spec["schema"]:
            raise RuntimeError("Existe forward prereg incompatible.")
        print("FORWARD100 V0.12 YA CONGELADO - NO SE SOBRESCRIBE")
        print("SHA256:", sha(FORWARD_SPEC))
        return

    FORWARD_SPEC.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 90)
    print("FORWARD100 V0.12 CONGELADO")
    print("=" * 90)
    print("POBLACION: elegibles 101-200")
    print("CANDIDATO: solo el seleccionado por DEVELOPMENT100")
    print("PEEKING PARCIAL: PROHIBIDO")
    print("RETUNING: PROHIBIDO")
    print("LABELS FORWARD LEIDOS AHORA: False")
    print("ARCHIVO:", FORWARD_SPEC)
    print("SHA256:", sha(FORWARD_SPEC))
    print("DINERO REAL: BLOQUEADO")
    print("=" * 90)


def read_labels(condition_ids: list[str]) -> dict[str, str]:
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
        if int(r["label_verified"] or 0) != 1:
            continue
        lab = str(r["label"] or "").strip().upper()
        if lab in ("UP", "DOWN"):
            out[str(r["condition_id"])] = lab
    return out


def evaluate_forward() -> None:
    cal.verify()

    needed = [DEV_SPEC, FORWARD_SPEC, THRESHOLDS, DEV_RESULT]
    missing = [str(p.name) for p in needed if not p.exists()]
    if missing:
        print("NO SE PUEDE EVALUAR. FALTAN:", ", ".join(missing))
        return

    if FORWARD_RESULT.exists():
        print("RESULTADO FORWARD100 YA EXISTE - NO SE SOBRESCRIBE")
        print("ARCHIVO:", FORWARD_RESULT)
        return

    fs = load(FORWARD_SPEC)
    dev = load(DEV_RESULT)
    th = load(THRESHOLDS)

    if fs["parent_prereg_sha256"] != sha(PREREG):
        raise RuntimeError("Prereg principal cambió.")
    if fs["implementation_sha256"] != sha(IMPL):
        raise RuntimeError("Implementation cambió.")
    if fs["development_evaluator_sha256"] != sha(DEV_SPEC):
        raise RuntimeError("Development evaluator cambió.")

    if dev.get("status") != "PASS_DEVELOPMENT":
        print("DEVELOPMENT NO PASO. FORWARD100 NO SE EVALUA.")
        print("STATUS DEVELOPMENT:", dev.get("status"))
        return

    selected = dev.get("selected_candidate")
    if not selected:
        print("No hay candidato seleccionado. FORWARD100 NO SE EVALUA.")
        return

    if dev.get("thresholds_sha256") != sha(THRESHOLDS):
        raise RuntimeError("Thresholds actuales no coinciden con development.")
    if dev.get("forward_labels_read", 0) != 0:
        raise RuntimeError("Development report indica forward labels leídos.")

    eligible = cal.eligible()
    if len(eligible) < DEV_ROWS + FORWARD_ROWS:
        print("FORWARD100 TODAVIA NO COMPLETO:", max(0, len(eligible)-DEV_ROWS), "/100")
        print("ELEGIBLES TOTALES:", len(eligible), "/200")
        print("NO se leen labels forward hasta tener los 200.")
        return

    forward = eligible[DEV_ROWS:DEV_ROWS + FORWARD_ROWS]
    ids = [r["condition_id"] for r in forward]
    labels = read_labels(ids)

    if len(labels) != FORWARD_ROWS:
        missing_ids = [cid for cid in ids if cid not in labels]
        print("NO SE EVALUA: faltan labels verificados:", len(missing_ids))
        print("Primeros faltantes:", ", ".join(missing_ids[:5]))
        return

    rules = th["candidate_rules"]
    thresholds = th["thresholds"]
    bands = th["cost_bands"]

    severity, band_name = rules[selected]
    t = thresholds[severity]
    lo, hi = map(float, bands[band_name])

    trades = []
    for r in forward:
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
    roi = net / capital if capital > 0 else None

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
    if roi is None or not (roi > 0): failures.append("ROI")
    if not (first >= 0): failures.append("FIRST_HALF")
    if not (second >= 0): failures.append("SECOND_HALF")
    if not (pos_share <= 0.35): failures.append("POS_SHARE")

    status = "PASS_FORWARD100" if not failures else "FAIL_FORWARD100"

    out = {
        "schema": "resultado_v012_forward100",
        "created_at": now(),
        "candidate": selected,
        "forward_rows": FORWARD_ROWS,
        "trades": n,
        "wins": wins,
        "win_rate": wins / n if n else None,
        "net_pnl_5shares": net,
        "net_pnl_per_share_equivalent": net / ORDER_SIZE,
        "capital_deployed": capital,
        "roi_on_cost": roi,
        "first_half_net_pnl": first,
        "second_half_net_pnl": second,
        "maximum_single_positive_trade_share": pos_share,
        "failures": failures,
        "status": status,
        "development_rows_reused": 0,
        "forward_labels_read": FORWARD_ROWS,
        "trades_detail": trades,
        "real_money": "BLOQUEADO",
    }

    FORWARD_RESULT.write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 96)
    print("V0.12 FORWARD100 - OUT OF SAMPLE")
    print("=" * 96)
    print("CANDIDATO:", selected)
    print("TRADES:", n)
    print("WINS:", wins)
    print("WIN RATE:", out["win_rate"])
    print("PNL 5 SHARES:", f"{net:+.6f}")
    print("ROI:", roi)
    print("PNL MITAD1:", f"{first:+.6f}")
    print("PNL MITAD2:", f"{second:+.6f}")
    print("MAX POSITIVE TRADE SHARE:", f"{pos_share:.6f}")
    print("STATUS:", status)
    print("FALLOS:", ",".join(failures) if failures else "NINGUNO")
    print("ARCHIVO:", FORWARD_RESULT)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 96)


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--freeze-spec", action="store_true")
    g.add_argument("--evaluate-forward", action="store_true")
    args = ap.parse_args()

    if args.freeze_spec:
        freeze_spec()
    else:
        evaluate_forward()


if __name__ == "__main__":
    main()
