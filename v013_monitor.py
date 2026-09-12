from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import calibrar_v012_execution_ev as cal

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

PREREG = DATA / "prereg_v013_cheap_strict_forward.json"
THRESHOLDS = DATA / "prereg_v012_thresholds.json"
SHADOW_DB = DATA / "shadow_forward_twap_transfer_v094a.db"

SPEC = DATA / "prereg_v013_evaluator.json"
RESULT = DATA / "resultado_v013_forward10.json"

POLL_SECONDS = 30


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


def verify_base() -> tuple[dict[str, Any], dict[str, Any]]:
    if not PREREG.exists():
        raise RuntimeError(f"Falta {PREREG}")
    if not THRESHOLDS.exists():
        raise RuntimeError(f"Falta {THRESHOLDS}")

    p = load(PREREG)
    t = load(THRESHOLDS)

    if p.get("schema") != "prereg_v013_cheap_strict_forward":
        raise RuntimeError("Schema v0.13 inesperado.")

    expected = p.get("source_thresholds_sha256")
    actual = sha(THRESHOLDS)
    if expected != actual:
        raise RuntimeError(
            "prereg_v012_thresholds.json cambió después de congelar v0.13."
        )

    if p.get("no_partial_peeking") is not True:
        raise RuntimeError("no_partial_peeking debe ser True.")
    if p.get("no_retuning") is not True:
        raise RuntimeError("no_retuning debe ser True.")

    return p, t


def freeze_spec() -> None:
    p, _ = verify_base()

    spec = {
        "schema": "prereg_v013_evaluator",
        "created_at": now(),
        "v013_prereg_sha256": sha(PREREG),
        "thresholds_sha256": sha(THRESHOLDS),
        "helper_calibrator_sha256": sha(ROOT / "calibrar_v012_execution_ev.py"),
        "population": (
            "mercados elegibles definidos por calibrar_v012_execution_ev.eligible(), "
            "con market_start_ms estrictamente mayor al cutoff v0.13"
        ),
        "candidate_rule": (
            "severity strict de prereg_v012_thresholds + "
            "chosen_side_cost_60 dentro de [0.10,0.22]"
        ),
        "stopping_rule": (
            "evaluar exactamente los primeros 10 trades calificables en orden cronologico; "
            "no leer outcomes antes de completar 10"
        ),
        "maximum_eligible_markets": int(p["max_eligible_markets"]),
        "insufficient_frequency_rule": (
            "si los primeros 300 mercados elegibles post-cutoff contienen menos de 10 "
            "trades calificables => FAIL_INSUFFICIENT_FREQUENCY sin leer labels"
        ),
        "label_scope": (
            "al llegar a 10, leer exclusivamente labels verificados de esos primeros "
            "10 trades calificables"
        ),
        "pnl_per_share": "win: 1-entry_cost; loss: -entry_cost",
        "pnl_total_trade": "pnl_per_share * 5 shares",
        "roi_on_cost": "sum(pnl_total_trade)/sum(entry_cost*5)",
        "halves": "primeros 5 trades vs ultimos 5 trades",
        "gates": p["forward_gates"],
        "no_partial_peeking": True,
        "no_retuning": True,
        "real_money": "BLOQUEADO",
    }

    if SPEC.exists():
        old = load(SPEC)
        if old.get("schema") != spec["schema"]:
            raise RuntimeError("Existe prereg_v013_evaluator incompatible.")
        print("EVALUADOR V0.13 YA CONGELADO - NO SE SOBRESCRIBE")
        print("SHA256:", sha(SPEC))
        return

    SPEC.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 92)
    print("EVALUADOR V0.13 CONGELADO")
    print("=" * 92)
    print("TARGET: primeros 10 trades calificables")
    print("MAX MERCADOS ELEGIBLES:", spec["maximum_eligible_markets"])
    print("LABELS LEIDOS AHORA: False")
    print("PEEKING PARCIAL: PROHIBIDO")
    print("RETUNING: PROHIBIDO")
    print("ARCHIVO:", SPEC)
    print("SHA256:", sha(SPEC))
    print("DINERO REAL: BLOQUEADO")
    print("=" * 92)


def verify_spec() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    p, t = verify_base()

    if not SPEC.exists():
        raise RuntimeError("Primero ejecuta --freeze-spec.")

    s = load(SPEC)

    if s.get("v013_prereg_sha256") != sha(PREREG):
        raise RuntimeError("El prereg v0.13 cambió después de congelar el evaluador.")
    if s.get("thresholds_sha256") != sha(THRESHOLDS):
        raise RuntimeError("Los thresholds cambiaron.")
    if s.get("helper_calibrator_sha256") != sha(ROOT / "calibrar_v012_execution_ev.py"):
        raise RuntimeError("calibrar_v012_execution_ev.py cambió después de congelar v0.13.")

    return p, t, s


def postcut_population() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    p, t, _ = verify_spec()

    cutoff = int(p["forward_after_market_start_ms"])
    max_markets = int(p["max_eligible_markets"])
    lo, hi = map(float, p["entry_cost_band"])

    strict = t["thresholds"]["strict"]
    shock_min = float(strict["shock_abs_bps_min"])
    response_max = float(strict["market_response_ratio_max"])

    all_eligible = [
        r for r in cal.eligible()
        if int(r["market_start_ms"]) > cutoff
    ]

    # El preregistro limita la prueba a los primeros N elegibles.
    scoped = all_eligible[:max_markets]

    candidates: list[dict[str, Any]] = []
    for r in scoped:
        cost = r["chosen_side_cost_60"]
        if cost is None:
            continue

        event = (
            float(r["abs_twap_move_bps"]) >= shock_min
            and float(r["market_response_ratio"]) <= response_max
        )
        cheap = lo <= float(cost) <= hi

        if event and cheap:
            candidates.append(r)

    return scoped, candidates


def read_only_target_labels(ids: list[str]) -> dict[str, str]:
    if len(ids) != 10:
        raise RuntimeError("Por diseño solo se pueden leer exactamente 10 labels.")

    c = cal.ro(SHADOW_DB)
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = c.execute(
            f"""
            SELECT condition_id,label,label_verified
            FROM shadow_markets
            WHERE condition_id IN ({placeholders})
            """,
            ids,
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


def write_insufficient(eligible_n: int, qualifying_n: int) -> None:
    if RESULT.exists():
        return

    out = {
        "schema": "resultado_v013_forward10",
        "created_at": now(),
        "status": "FAIL_INSUFFICIENT_FREQUENCY",
        "eligible_markets_examined": eligible_n,
        "qualifying_trades": qualifying_n,
        "labels_read": 0,
        "pnl_calculated": False,
        "failures": ["INSUFFICIENT_FREQUENCY"],
        "real_money": "BLOQUEADO",
    }
    RESULT.write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def evaluate_if_ready() -> bool:
    p, _, _ = verify_spec()

    if RESULT.exists():
        r = load(RESULT)
        print("RESULTADO V0.13 YA EXISTE:", r.get("status"))
        print("ARCHIVO:", RESULT)
        return True

    scoped, candidates = postcut_population()
    max_markets = int(p["max_eligible_markets"])

    if len(candidates) < 10:
        if len(scoped) >= max_markets:
            write_insufficient(len(scoped), len(candidates))
            print("=" * 92)
            print("V0.13 FAIL_INSUFFICIENT_FREQUENCY")
            print("=" * 92)
            print("ELEGIBLES EXAMINADOS:", len(scoped))
            print("TRADES CALIFICABLES:", len(candidates), "/10")
            print("LABELS LEIDOS: 0")
            print("PNL CALCULADO: False")
            print("DINERO REAL: BLOQUEADO")
            print("=" * 92)
            return True
        return False

    target = candidates[:10]
    ids = [r["condition_id"] for r in target]

    # ÚNICO punto del programa donde se leen labels.
    labels = read_only_target_labels(ids)

    missing = [cid for cid in ids if cid not in labels]
    if missing:
        print(
            "[WAIT] Ya hay 10 trades, pero faltan",
            len(missing),
            "labels verificados. No se evalua aun."
        )
        return False

    order_size = float(p["order_size_shares"])
    trades = []

    for r in target:
        side = "UP" if int(r["direction"]) > 0 else "DOWN"
        outcome = labels[r["condition_id"]]
        cost = float(r["chosen_side_cost_60"])
        win = side == outcome
        pnl_ps = (1.0 - cost) if win else (-cost)
        pnl = pnl_ps * order_size

        trades.append({
            "condition_id": r["condition_id"],
            "market_start_ms": int(r["market_start_ms"]),
            "side": side,
            "outcome": outcome,
            "win": win,
            "entry_cost_per_share": cost,
            "pnl_per_share": pnl_ps,
            "pnl_total": pnl,
            "abs_twap_move_bps": float(r["abs_twap_move_bps"]),
            "market_response_ratio": float(r["market_response_ratio"]),
        })

    n = len(trades)
    wins = sum(1 for x in trades if x["win"])
    net = sum(x["pnl_total"] for x in trades)
    capital = sum(x["entry_cost_per_share"] * order_size for x in trades)
    roi = net / capital if capital > 0 else None

    first = sum(x["pnl_total"] for x in trades[:5])
    second = sum(x["pnl_total"] for x in trades[5:])

    positives = [x["pnl_total"] for x in trades if x["pnl_total"] > 0]
    gross_positive = sum(positives)
    pos_share = (
        max(positives) / gross_positive
        if positives and gross_positive > 0
        else 1.0
    )

    gates = p["forward_gates"]
    failures: list[str] = []

    if n < int(gates["minimum_trades"]):
        failures.append("MIN_TRADES")
    if not (net > 0):
        failures.append("NET_PNL")
    if roi is None or not (roi > 0):
        failures.append("ROI")
    if not (first >= 0):
        failures.append("FIRST_HALF")
    if not (second >= 0):
        failures.append("SECOND_HALF")
    if not (pos_share <= float(gates["maximum_single_positive_trade_share"])):
        failures.append("POS_SHARE")

    status = "PASS_V013_FORWARD10" if not failures else "FAIL_V013_FORWARD10"

    out = {
        "schema": "resultado_v013_forward10",
        "created_at": now(),
        "status": status,
        "eligible_markets_seen_at_stop": len(scoped),
        "qualifying_trades": n,
        "wins": wins,
        "win_rate": wins / n,
        "net_pnl_5shares": net,
        "net_pnl_per_share_equivalent": net / order_size,
        "capital_deployed": capital,
        "roi_on_cost": roi,
        "first_half_net_pnl": first,
        "second_half_net_pnl": second,
        "maximum_single_positive_trade_share": pos_share,
        "failures": failures,
        "labels_read": 10,
        "trades_detail": trades,
        "real_money": "BLOQUEADO",
    }

    RESULT.write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 100)
    print("V0.13 FORWARD10 - OUT OF SAMPLE")
    print("=" * 100)
    print("TRADES:", n)
    print("WINS:", wins)
    print("WIN RATE:", out["win_rate"])
    print("PNL 5 SHARES:", f"{net:+.6f}")
    print("ROI:", roi)
    print("MITAD1:", f"{first:+.6f}")
    print("MITAD2:", f"{second:+.6f}")
    print("MAX POSITIVE TRADE SHARE:", f"{pos_share:.6f}")
    print("STATUS:", status)
    print("FALLOS:", ",".join(failures) if failures else "NINGUNO")
    print("ARCHIVO:", RESULT)
    print("DINERO REAL: BLOQUEADO")
    print("=" * 100)
    return True


def status() -> None:
    verify_spec()
    scoped, candidates = postcut_population()

    print("=" * 84)
    print("STATUS V0.13 CHEAP-STRICT")
    print("=" * 84)
    print("ELEGIBLES POST-CUTOFF:", len(scoped), "/300")
    print("TRADES CALIFICABLES:", len(candidates), "/10")
    print("FALTAN TRADES:", max(0, 10 - len(candidates)))
    print("LABELS LEIDOS POR ESTE STATUS: 0")
    print("DINERO REAL: BLOQUEADO")
    print("=" * 84)


def monitor() -> None:
    verify_spec()

    print("=" * 94)
    print("V0.13 MONITOR - CHEAP STRICT - PAPER ONLY")
    print("=" * 94)
    print("Cuenta señales sin leer outcomes.")
    print("Al completar 10 trades, lee SOLO esos 10 labels y evalua una vez.")
    print("Si llega a 300 elegibles con <10 trades: FAIL_INSUFFICIENT_FREQUENCY.")
    print("Ctrl+C detiene solo este monitor; no afecta collectors.")
    print("DINERO REAL: BLOQUEADO")
    print("=" * 94)

    last = None

    try:
        while True:
            scoped, candidates = postcut_population()
            state = (len(scoped), len(candidates))

            if state != last:
                print(
                    f"[STATUS] elegibles_postcut={state[0]}/300 | "
                    f"trades={state[1]}/10 | "
                    f"faltan={max(0,10-state[1])}"
                )
                last = state

            if evaluate_if_ready():
                return

            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        print("\nMonitor v0.13 detenido. Los collectors siguen independientes.")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--freeze-spec", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--monitor", action="store_true")
    args = ap.parse_args()

    if args.freeze_spec:
        freeze_spec()
    elif args.status:
        status()
    else:
        monitor()


if __name__ == "__main__":
    main()
