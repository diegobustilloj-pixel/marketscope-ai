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
import v013_monitor as v013


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

THRESHOLDS = DATA / "prereg_v012_thresholds.json"
SHADOW_DB = DATA / "shadow_forward_twap_transfer_v094a.db"
PREREG = DATA / "prereg_v014_disjoint_development.json"
SPEC = DATA / "prereg_v014_evaluator.json"
RESULT = DATA / "resultado_v014_development12.json"
PAPER_TRADER = ROOT / "paper_trader_v014.py"

POLL_SECONDS = 30
FIVE_MINUTES_MS = 300_000


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _validate_base_files() -> None:
    if not THRESHOLDS.exists():
        raise RuntimeError(f"Falta {THRESHOLDS}")
    if not PAPER_TRADER.exists():
        raise RuntimeError(f"Falta {PAPER_TRADER}")

    thresholds = load(THRESHOLDS)
    if thresholds.get("schema") != "prereg_v012_thresholds":
        raise RuntimeError("Schema de thresholds inesperado")
    if "strict" not in thresholds.get("thresholds", {}):
        raise RuntimeError("Faltan thresholds STRICT")


def freeze_specs() -> None:
    _validate_base_files()

    if PREREG.exists() or SPEC.exists():
        if not PREREG.exists() or not SPEC.exists():
            raise RuntimeError("Freeze v0.14 incompleto: existe solo uno de los archivos")
        verify_frozen()
        print("V0.14 YA CONGELADO - NO SE SOBRESCRIBE")
        print("PREREG SHA256:", sha(PREREG))
        print("EVALUATOR SHA256:", sha(SPEC))
        return

    v013_scoped, _ = v013.postcut_population()
    if len(v013_scoped) < 50:
        raise RuntimeError(
            f"V0.14 solo puede congelarse despues de 50 elegibles v0.13; hay {len(v013_scoped)}"
        )

    freeze_ms = int(time.time() * 1000)
    cutoff = (freeze_ms // FIVE_MINUTES_MS) * FIVE_MINUTES_MS

    prereg = {
        "schema": "prereg_v014_disjoint_development",
        "created_at": now(),
        "purpose": "desarrollo paralelo disjunto; no es validacion final",
        "forward_after_market_start_ms": cutoff,
        "baseline_v013_eligible_at_freeze": len(v013_scoped),
        "source_thresholds": "data/prereg_v012_thresholds.json",
        "source_thresholds_sha256": sha(THRESHOLDS),
        "helper_calibrator_sha256": sha(ROOT / "calibrar_v012_execution_ev.py"),
        "monitor_sha256": sha(Path(__file__).resolve()),
        "paper_trader_sha256": sha(PAPER_TRADER),
        "hypothesis": (
            "STRICT TWAP shock + market lag fuera de la banda v0.13, "
            "comparando bajo coste contra coste moderado"
        ),
        "severity": "strict",
        "direction": "simetrica: UP si TWAP shock positivo, DOWN si negativo",
        "strata": {
            "LOW": {
                "cost_min": 0.05,
                "cost_min_inclusive": True,
                "cost_max": 0.10,
                "cost_max_inclusive": False,
            },
            "MODERATE": {
                "cost_min": 0.22,
                "cost_min_inclusive": False,
                "cost_max": 0.30,
                "cost_max_inclusive": True,
            },
        },
        "v013_excluded_band": {
            "cost_min": 0.10,
            "cost_max": 0.22,
            "both_inclusive": True,
        },
        "excluded_high_cost": "cost > 0.30",
        "entry_cost_field": (
            "chosen_side total_cost_per_share a 60s, VWAP real 5 shares + fee"
        ),
        "order_size_shares": 5.0,
        "target_per_stratum": 6,
        "total_labels_at_evaluation": 12,
        "maximum_eligible_markets": 250,
        "stopping_rule": (
            "primeros 6 LOW y primeros 6 MODERATE despues del cutoff; "
            "outcomes prohibidos hasta completar ambos estratos"
        ),
        "insufficient_frequency_rule": (
            "si los primeros 250 elegibles no contienen 6 LOW y 6 MODERATE, "
            "FAIL_INSUFFICIENT_FREQUENCY con cero labels"
        ),
        "development_gates_per_stratum": {
            "minimum_trades": 6,
            "minimum_wins": 2,
            "net_pnl": "positive",
            "roi_on_cost": "positive",
            "first_half_net_pnl": "nonnegative",
            "second_half_net_pnl": "nonnegative",
            "maximum_single_positive_trade_share": 0.60,
        },
        "selection_rule": (
            "entre estratos que pasen todos los gates elegir mayor net PnL; "
            "desempate mayor ROI y luego LOW; seleccionar como maximo uno para v0.15"
        ),
        "no_partial_peeking": True,
        "no_overlap_with_v013_candidates": True,
        "real_money": "BLOQUEADO",
    }

    PREREG.write_text(
        json.dumps(prereg, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    spec = {
        "schema": "prereg_v014_evaluator",
        "created_at": now(),
        "v014_prereg_sha256": sha(PREREG),
        "thresholds_sha256": sha(THRESHOLDS),
        "helper_calibrator_sha256": sha(ROOT / "calibrar_v012_execution_ev.py"),
        "monitor_sha256": sha(Path(__file__).resolve()),
        "paper_trader_sha256": sha(PAPER_TRADER),
        "population": (
            "calibrar_v012_execution_ev.eligible() despues del cutoff v0.14, "
            "limitada a los primeros 250 elegibles"
        ),
        "target": "primeros 6 LOW y primeros 6 MODERATE, 12 labels exactos",
        "label_scope": (
            "leer exclusivamente labels verificados de los 12 targets cuando "
            "ambos estratos esten completos"
        ),
        "halves": "primeros 3 vs ultimos 3 dentro de cada estrato",
        "pnl_per_share": "win: 1-entry_cost; loss: -entry_cost",
        "pnl_total_trade": "pnl_per_share * 5 shares",
        "roi_on_cost": "sum(pnl_total_trade)/sum(entry_cost*5)",
        "selection": "maximo un estrato para un futuro v0.15 independiente",
        "no_partial_peeking": True,
        "development_only": True,
        "real_money": "BLOQUEADO",
    }
    SPEC.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 96)
    print("V0.14 DISJOINT DEVELOPMENT CONGELADO")
    print("=" * 96)
    print("V0.13 ELEGIBLES AL FREEZE:", len(v013_scoped))
    print("CUTOFF MARKET_START_MS:", cutoff)
    print("LOW: [0.05,0.10) | MODERATE: (0.22,0.30]")
    print("TARGET: 6 LOW + 6 MODERATE")
    print("LABELS LEIDOS AHORA: 0")
    print("PREREG SHA256:", sha(PREREG))
    print("EVALUATOR SHA256:", sha(SPEC))
    print("DINERO REAL: BLOQUEADO")
    print("=" * 96)


def verify_frozen() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    _validate_base_files()
    if not PREREG.exists() or not SPEC.exists():
        raise RuntimeError("Primero ejecuta --freeze-spec")

    prereg = load(PREREG)
    thresholds = load(THRESHOLDS)
    spec = load(SPEC)

    if prereg.get("schema") != "prereg_v014_disjoint_development":
        raise RuntimeError("Schema prereg v0.14 inesperado")
    if spec.get("schema") != "prereg_v014_evaluator":
        raise RuntimeError("Schema evaluator v0.14 inesperado")

    checks = (
        (prereg.get("source_thresholds_sha256"), sha(THRESHOLDS), "thresholds"),
        (
            prereg.get("helper_calibrator_sha256"),
            sha(ROOT / "calibrar_v012_execution_ev.py"),
            "calibrador",
        ),
        (prereg.get("monitor_sha256"), sha(Path(__file__).resolve()), "monitor"),
        (prereg.get("paper_trader_sha256"), sha(PAPER_TRADER), "paper trader"),
        (spec.get("v014_prereg_sha256"), sha(PREREG), "prereg"),
        (spec.get("thresholds_sha256"), sha(THRESHOLDS), "spec thresholds"),
        (
            spec.get("helper_calibrator_sha256"),
            sha(ROOT / "calibrar_v012_execution_ev.py"),
            "spec calibrador",
        ),
        (spec.get("monitor_sha256"), sha(Path(__file__).resolve()), "spec monitor"),
        (spec.get("paper_trader_sha256"), sha(PAPER_TRADER), "spec paper trader"),
    )
    for expected, actual, name in checks:
        if expected != actual:
            raise RuntimeError(f"Hash v0.14 no coincide: {name}")

    if prereg.get("no_partial_peeking") is not True:
        raise RuntimeError("no_partial_peeking debe ser True")
    if prereg.get("no_overlap_with_v013_candidates") is not True:
        raise RuntimeError("v0.14 debe permanecer disjunto de v0.13")

    return prereg, thresholds, spec


def classify_cost(cost: Any, prereg: dict[str, Any]) -> str | None:
    try:
        value = float(cost)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None

    low = prereg["strata"]["LOW"]
    moderate = prereg["strata"]["MODERATE"]

    if float(low["cost_min"]) <= value < float(low["cost_max"]):
        return "LOW"
    if float(moderate["cost_min"]) < value <= float(moderate["cost_max"]):
        return "MODERATE"
    return None


def postcut_population() -> tuple[
    list[dict[str, Any]],
    dict[str, list[dict[str, Any]]],
]:
    prereg, thresholds, _ = verify_frozen()
    cutoff = int(prereg["forward_after_market_start_ms"])
    maximum = int(prereg["maximum_eligible_markets"])
    strict = thresholds["thresholds"]["strict"]
    shock_min = float(strict["shock_abs_bps_min"])
    response_max = float(strict["market_response_ratio_max"])

    scoped = [
        row
        for row in cal.eligible()
        if int(row["market_start_ms"]) > cutoff
    ][:maximum]

    strata: dict[str, list[dict[str, Any]]] = {"LOW": [], "MODERATE": []}
    for row in scoped:
        if float(row["abs_twap_move_bps"]) < shock_min:
            continue
        if float(row["market_response_ratio"]) > response_max:
            continue

        stratum = classify_cost(row.get("chosen_side_cost_60"), prereg)
        if stratum is not None:
            strata[stratum].append(row)

    return scoped, strata


def read_only_target_labels(ids: list[str]) -> dict[str, str]:
    if len(ids) != 12 or len(set(ids)) != 12:
        raise RuntimeError("Por diseno solo se pueden leer exactamente 12 labels unicos")

    connection = cal.ro(SHADOW_DB)
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"""
            SELECT condition_id,label,label_verified
            FROM shadow_markets
            WHERE condition_id IN ({placeholders})
            """,
            ids,
        ).fetchall()
    finally:
        connection.close()

    labels: dict[str, str] = {}
    for row in rows:
        if int(row["label_verified"] or 0) != 1:
            continue
        label = str(row["label"] or "").strip().upper()
        if label in ("UP", "DOWN"):
            labels[str(row["condition_id"])] = label
    return labels


def assert_no_v013_candidate_overlap(ids: list[str]) -> None:
    _, v013_candidates = v013.postcut_population()
    v013_ids = {str(row["condition_id"]) for row in v013_candidates}
    overlap = sorted(set(ids) & v013_ids)
    if overlap:
        raise RuntimeError(
            "Invariante rota: v0.14 intenta abrir outcomes candidatos de v0.13: "
            + ",".join(overlap)
        )


def _write_frequency_failure(
    eligible_n: int,
    low_n: int,
    moderate_n: int,
) -> None:
    if RESULT.exists():
        return
    payload = {
        "schema": "resultado_v014_development12",
        "created_at": now(),
        "status": "FAIL_INSUFFICIENT_FREQUENCY",
        "eligible_markets_examined": eligible_n,
        "low_trades": low_n,
        "moderate_trades": moderate_n,
        "labels_read": 0,
        "pnl_calculated": False,
        "selected_for_v015": None,
        "real_money": "BLOQUEADO",
    }
    RESULT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _metrics(
    stratum: str,
    rows: list[dict[str, Any]],
    labels: dict[str, str],
    prereg: dict[str, Any],
) -> dict[str, Any]:
    size = float(prereg["order_size_shares"])
    trades: list[dict[str, Any]] = []
    for row in rows:
        side = "UP" if int(row["direction"]) > 0 else "DOWN"
        outcome = labels[row["condition_id"]]
        cost = float(row["chosen_side_cost_60"])
        win = side == outcome
        pnl_per_share = (1.0 - cost) if win else -cost
        trades.append(
            {
                "condition_id": row["condition_id"],
                "market_start_ms": int(row["market_start_ms"]),
                "stratum": stratum,
                "side": side,
                "outcome": outcome,
                "win": win,
                "entry_cost_per_share": cost,
                "pnl_per_share": pnl_per_share,
                "pnl_total": pnl_per_share * size,
                "abs_twap_move_bps": float(row["abs_twap_move_bps"]),
                "market_response_ratio": float(row["market_response_ratio"]),
            }
        )

    wins = sum(1 for trade in trades if trade["win"])
    net = sum(trade["pnl_total"] for trade in trades)
    capital = sum(trade["entry_cost_per_share"] * size for trade in trades)
    roi = net / capital if capital > 0 else None
    first = sum(trade["pnl_total"] for trade in trades[:3])
    second = sum(trade["pnl_total"] for trade in trades[3:])
    positives = [trade["pnl_total"] for trade in trades if trade["pnl_total"] > 0]
    gross_positive = sum(positives)
    positive_share = (
        max(positives) / gross_positive
        if positives and gross_positive > 0
        else 1.0
    )

    gates = prereg["development_gates_per_stratum"]
    failures: list[str] = []
    if len(trades) < int(gates["minimum_trades"]):
        failures.append("MIN_TRADES")
    if wins < int(gates["minimum_wins"]):
        failures.append("MIN_WINS")
    if not net > 0:
        failures.append("NET_PNL")
    if roi is None or not roi > 0:
        failures.append("ROI")
    if not first >= 0:
        failures.append("FIRST_HALF")
    if not second >= 0:
        failures.append("SECOND_HALF")
    if not positive_share <= float(gates["maximum_single_positive_trade_share"]):
        failures.append("POS_SHARE")

    return {
        "stratum": stratum,
        "trades": len(trades),
        "wins": wins,
        "win_rate": wins / len(trades),
        "net_pnl_5shares": net,
        "capital_deployed": capital,
        "roi_on_cost": roi,
        "first_half_net_pnl": first,
        "second_half_net_pnl": second,
        "maximum_single_positive_trade_share": positive_share,
        "failures": failures,
        "qualifies_for_v015": not failures,
        "trades_detail": trades,
    }


def evaluate_if_ready() -> bool:
    prereg, _, _ = verify_frozen()
    if RESULT.exists():
        payload = load(RESULT)
        print("RESULTADO V0.14 YA EXISTE:", payload.get("status"))
        print("ARCHIVO:", RESULT)
        return True

    scoped, strata = postcut_population()
    target_n = int(prereg["target_per_stratum"])
    maximum = int(prereg["maximum_eligible_markets"])

    if len(strata["LOW"]) < target_n or len(strata["MODERATE"]) < target_n:
        if len(scoped) >= maximum:
            _write_frequency_failure(
                len(scoped),
                len(strata["LOW"]),
                len(strata["MODERATE"]),
            )
            print("V0.14 FAIL_INSUFFICIENT_FREQUENCY")
            print("ELEGIBLES:", len(scoped))
            print("LOW:", len(strata["LOW"]), "/6")
            print("MODERATE:", len(strata["MODERATE"]), "/6")
            print("LABELS LEIDOS: 0")
            return True
        return False

    low_target = strata["LOW"][:target_n]
    moderate_target = strata["MODERATE"][:target_n]
    target = sorted(
        low_target + moderate_target,
        key=lambda row: (int(row["market_start_ms"]), row["condition_id"]),
    )
    ids = [str(row["condition_id"]) for row in target]

    # UNICO punto donde v0.14 abre outcomes. Los IDs son disjuntos de v0.13.
    assert_no_v013_candidate_overlap(ids)
    labels = read_only_target_labels(ids)
    missing = [condition_id for condition_id in ids if condition_id not in labels]
    if missing:
        print("[WAIT] Targets completos; faltan", len(missing), "labels verificados")
        return False

    results = {
        "LOW": _metrics("LOW", low_target, labels, prereg),
        "MODERATE": _metrics("MODERATE", moderate_target, labels, prereg),
    }
    qualified = [value for value in results.values() if value["qualifies_for_v015"]]
    qualified.sort(
        key=lambda value: (
            -float(value["net_pnl_5shares"]),
            -float(value["roi_on_cost"]),
            0 if value["stratum"] == "LOW" else 1,
        )
    )
    selected = qualified[0]["stratum"] if qualified else None
    status = "DEVELOPMENT_CANDIDATE_SELECTED" if selected else "FAIL_DEVELOPMENT"

    payload = {
        "schema": "resultado_v014_development12",
        "created_at": now(),
        "status": status,
        "eligible_markets_seen_at_stop": len(scoped),
        "labels_read": 12,
        "strata_results": results,
        "selected_for_v015": selected,
        "v013_candidate_outcomes_read": 0,
        "development_only": True,
        "real_money": "BLOQUEADO",
    }
    RESULT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("=" * 100)
    print("V0.14 DISJOINT DEVELOPMENT - RESULTADO")
    print("=" * 100)
    for name in ("LOW", "MODERATE"):
        value = results[name]
        print(
            name,
            "trades=", value["trades"],
            "wins=", value["wins"],
            "pnl=", f"{value['net_pnl_5shares']:+.6f}",
            "roi=", value["roi_on_cost"],
            "failures=", value["failures"],
        )
    print("STATUS:", status)
    print("SELECCION PARA V0.15:", selected)
    print("OUTCOMES CANDIDATOS V0.13 LEIDOS: 0")
    print("DINERO REAL: BLOQUEADO")
    print("=" * 100)
    return True


def status() -> None:
    prereg, _, _ = verify_frozen()
    scoped, strata = postcut_population()
    target = int(prereg["target_per_stratum"])
    print("=" * 88)
    print("STATUS V0.14 DISJOINT DEVELOPMENT")
    print("=" * 88)
    print("ELEGIBLES POST-CUTOFF:", len(scoped), "/250")
    print("LOW [0.05,0.10):", len(strata["LOW"]), f"/{target}")
    print("MODERATE (0.22,0.30]:", len(strata["MODERATE"]), f"/{target}")
    print("TOTAL TARGETS PRESENTES:", min(target, len(strata["LOW"])) + min(target, len(strata["MODERATE"])), "/12")
    print("LABELS LEIDOS POR ESTE STATUS: 0")
    print("OUTCOMES CANDIDATOS V0.13 LEIDOS: 0")
    print("DINERO REAL: BLOQUEADO")
    print("=" * 88)


def monitor() -> None:
    verify_frozen()
    print("=" * 96)
    print("V0.14 MONITOR - DISJOINT DEVELOPMENT - PAPER ONLY")
    print("=" * 96)
    print("LOW [0.05,0.10): target 6")
    print("MODERATE (0.22,0.30]: target 6")
    print("No se solapa con candidatos v0.13 [0.10,0.22]")
    print("No lee outcomes hasta completar 6 + 6")
    print("DINERO REAL: BLOQUEADO")
    print("=" * 96)

    last: tuple[int, int, int] | None = None
    try:
        while True:
            scoped, strata = postcut_population()
            state = (len(scoped), len(strata["LOW"]), len(strata["MODERATE"]))
            if state != last:
                print(
                    f"[STATUS] elegibles={state[0]}/250 | "
                    f"low={state[1]}/6 | moderate={state[2]}/6"
                )
                last = state
            if evaluate_if_ready():
                return
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nMonitor v0.14 detenido; collectors y v0.13 siguen independientes")


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--freeze-spec", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--monitor", action="store_true")
    args = parser.parse_args()

    if args.freeze_spec:
        freeze_specs()
    elif args.status:
        status()
    else:
        monitor()


if __name__ == "__main__":
    main()
