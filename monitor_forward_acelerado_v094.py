from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist

MODEL_NAME = "twap_transfer_strike_hgb"
THRESHOLDS = (0.06, 0.08, 0.10, 0.12, 0.15)

# Descubrimiento: posible congelación temprana tras 12h aprox. (144 mercados)
# y congelación obligatoria tras 24h aprox. (288 mercados).
DISCOVERY_LOOKS = (144, 288)
EARLY_SELECT_MIN_TRADES = 30
FINAL_SELECT_MIN_TRADES = 30
EARLY_FAIL_MIN_TRADES = 20

# Confirmación independiente DESPUÉS del bloque usado para escoger threshold.
CONFIRM_CHECKPOINTS = (50, 100, 150, 250)
MIN_PASS_TRADES = 100
MAX_BRIER_DEGRADATION = 0.01

# Corrección conservadora por múltiples miradas.
DISCOVERY_Z = NormalDist().inv_cdf(
    1.0 - 0.05 / (len(THRESHOLDS) * len(DISCOVERY_LOOKS))
)
CONFIRM_Z = NormalDist().inv_cdf(
    1.0 - 0.05 / (2 * len(CONFIRM_CHECKPOINTS))
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_ro(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def read_meta(con: sqlite3.Connection) -> dict:
    result = {}
    try:
        rows = con.execute("SELECT key,value FROM shadow_meta").fetchall()
    except sqlite3.Error:
        return result
    for row in rows:
        try:
            result[str(row["key"])] = json.loads(row["value"])
        except Exception:
            result[str(row["key"])] = row["value"]
    return result


def taker_cost(ask: float, fee_rate: float, slippage: float):
    fill = min(0.999, max(0.001, float(ask) + float(slippage)))
    fee = float(fee_rate) * fill * (1.0 - fill)
    return fill + fee, fill, fee


def sample_stats(pnls: list[float], costs: list[float], z: float) -> dict:
    n = len(pnls)
    if n == 0:
        return {
            "trades": 0,
            "wins": 0,
            "win_rate": None,
            "net_pnl": 0.0,
            "roi_on_cost": None,
            "mean_pnl": None,
            "std_pnl": None,
            "lcb_mean_pnl": None,
            "ucb_mean_pnl": None,
            "profit_factor": None,
        }
    mean = statistics.fmean(pnls)
    sd = statistics.stdev(pnls) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else 0.0
    net = sum(pnls)
    total_cost = sum(costs)
    wins = sum(1 for x in pnls if x > 0)
    gp = sum(x for x in pnls if x > 0)
    gl = -sum(x for x in pnls if x < 0)
    return {
        "trades": n,
        "wins": wins,
        "win_rate": wins / n,
        "net_pnl": net,
        "roi_on_cost": (net / total_cost) if total_cost else None,
        "mean_pnl": mean,
        "std_pnl": sd,
        "lcb_mean_pnl": mean - z * se,
        "ucb_mean_pnl": mean + z * se,
        "profit_factor": (gp / gl) if gl else None,
    }


def brier(rows: list[dict], cutoff_ms: int | None = None) -> dict:
    chosen = rows
    if cutoff_ms is not None:
        chosen = [r for r in rows if r["market_start_ms"] <= cutoff_ms]
    if not chosen:
        return {
            "rows": 0,
            "model": None,
            "market": None,
            "delta_model_minus_market": None,
        }
    model = statistics.fmean(
        (r["probability_up"] - r["y"]) ** 2 for r in chosen
    )
    market = statistics.fmean(
        (r["market_probability"] - r["y"]) ** 2 for r in chosen
    )
    return {
        "rows": len(chosen),
        "model": model,
        "market": market,
        "delta_model_minus_market": model - market,
    }


def load_rows(db: Path) -> tuple[list[dict], dict]:
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
                s.probability_up,
                f.feature_json,
                b.probability_up AS market_probability
            FROM shadow_signals AS s
            JOIN shadow_markets AS m
              ON m.condition_id=s.condition_id
            JOIN shadow_features AS f
              ON f.condition_id=s.condition_id
            JOIN shadow_signals AS b
              ON b.condition_id=s.condition_id
             AND b.model_name='market_implied'
            WHERE s.model_name=?
              AND m.label_verified=1
            ORDER BY m.market_start_ms, m.condition_id
            """,
            (MODEL_NAME,),
        ).fetchall()
    finally:
        con.close()

    out = []
    for row in rows:
        try:
            feature = json.loads(row["feature_json"])
            up_ask = float(feature["up_best_ask"])
            down_ask = float(feature["down_best_ask"])
        except Exception:
            continue
        probability_up = float(row["probability_up"])
        up_cost, _, _ = taker_cost(up_ask, fee_rate, slippage)
        down_cost, _, _ = taker_cost(down_ask, fee_rate, slippage)
        y = 1 if str(row["label"]) == "Up" else 0
        out.append(
            {
                "condition_id": str(row["condition_id"]),
                "market_start_ms": int(row["market_start_ms"]),
                "y": y,
                "probability_up": probability_up,
                "market_probability": float(row["market_probability"]),
                "up_cost": up_cost,
                "down_cost": down_cost,
                "up_edge": probability_up - up_cost,
                "down_edge": 1.0 - probability_up - down_cost,
            }
        )
    return out, {
        "fee_rate": fee_rate,
        "slippage_per_share": slippage,
        "shadow_meta": meta,
    }


def trades_for_threshold(rows: list[dict], threshold: float) -> list[dict]:
    trades = []
    for row in rows:
        up_edge = row["up_edge"]
        down_edge = row["down_edge"]
        if max(up_edge, down_edge) < threshold:
            continue
        if up_edge >= down_edge:
            side = "Up"
            cost = row["up_cost"]
            won = row["y"] == 1
        else:
            side = "Down"
            cost = row["down_cost"]
            won = row["y"] == 0
        pnl = (1.0 - cost) if won else -cost
        trades.append(
            {
                **row,
                "threshold": threshold,
                "side": side,
                "cost": cost,
                "won": won,
                "pnl": pnl,
            }
        )
    return trades


def threshold_metrics(rows: list[dict], threshold: float, z: float) -> dict:
    trades = trades_for_threshold(rows, threshold)
    stats = sample_stats(
        [t["pnl"] for t in trades],
        [t["cost"] for t in trades],
        z,
    )
    return {
        "threshold": threshold,
        **stats,
    }


def selection_path(db: Path) -> Path:
    return db.parent / "seleccion_threshold_twap_v094.json"


def result_path(db: Path) -> Path:
    return db.parent / "estado_forward_acelerado_v094.json"


def load_selection(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def freeze_selection(path: Path, payload: dict) -> dict:
    # Jamás sobrescribir una selección ya congelada.
    if path.exists():
        existing = load_selection(path)
        if existing:
            return existing
        raise RuntimeError(
            f"Existe {path} pero no se puede leer. No lo borre; revíselo manualmente."
        )
    temp = path.with_suffix(path.suffix + ".partial")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp.replace(path)
    return payload


def choose_threshold(discovery_rows: list[dict], look: int, early: bool) -> dict:
    metrics = [
        threshold_metrics(discovery_rows, t, DISCOVERY_Z)
        for t in THRESHOLDS
    ]
    min_trades = EARLY_SELECT_MIN_TRADES if early else FINAL_SELECT_MIN_TRADES
    eligible = [m for m in metrics if m["trades"] >= min_trades]

    if not eligible:
        # Determinístico: escoger el threshold con más trades para no bloquear
        # la confirmación, pero queda marcado como selección débil.
        chosen = max(metrics, key=lambda m: (m["trades"], -m["threshold"]))
        reason = "fallback_mas_trades_sin_minimo"
    else:
        # Se escoge exclusivamente con el bloque de descubrimiento.
        chosen = max(
            eligible,
            key=lambda m: (
                -1e99 if m["lcb_mean_pnl"] is None else m["lcb_mean_pnl"],
                -1e99 if m["mean_pnl"] is None else m["mean_pnl"],
                -m["threshold"],
            ),
        )
        reason = "max_lcb_mean_pnl"

    cutoff = discovery_rows[-1]["market_start_ms"]
    return {
        "schema": "sequential_threshold_selection_v094",
        "created_at": utc_now(),
        "model_name": MODEL_NAME,
        "threshold_grid": list(THRESHOLDS),
        "discovery_look_predictions": look,
        "discovery_cutoff_market_start_ms": cutoff,
        "selected_threshold": chosen["threshold"],
        "selection_reason": reason,
        "discovery_z": DISCOVERY_Z,
        "discovery_metrics": metrics,
        "warning": (
            "Esta selección usa SOLO el bloque de descubrimiento. "
            "La evidencia de confirmación debe usar mercados posteriores al cutoff."
        ),
    }


def analyze(db: Path) -> dict:
    if not db.exists():
        return {
            "status": "WAITING_DATABASE",
            "database": str(db),
            "message": "La base del forward todavía no existe.",
            "updated_at": utc_now(),
        }

    rows, config = load_rows(db)
    n_predictions = len(rows)
    sel_path = selection_path(db)
    selection = load_selection(sel_path)

    tournament = [
        threshold_metrics(rows, t, DISCOVERY_Z)
        for t in THRESHOLDS
    ]

    # Antes de selección: dos miradas pre-registradas.
    discovery_verdict = "ACUMULANDO_DESCUBRIMIENTO"
    if selection is None:
        if n_predictions >= DISCOVERY_LOOKS[0]:
            first = rows[: DISCOVERY_LOOKS[0]]
            first_metrics = [
                threshold_metrics(first, t, DISCOVERY_Z)
                for t in THRESHOLDS
            ]
            strong = [
                m for m in first_metrics
                if m["trades"] >= EARLY_SELECT_MIN_TRADES
                and m["lcb_mean_pnl"] is not None
                and m["lcb_mean_pnl"] > 0
                and m["net_pnl"] > 0
            ]
            bad = [
                m for m in first_metrics
                if m["trades"] >= EARLY_FAIL_MIN_TRADES
                and m["ucb_mean_pnl"] is not None
                and m["ucb_mean_pnl"] < 0
            ]
            sufficiently_observed = [
                m for m in first_metrics
                if m["trades"] >= EARLY_FAIL_MIN_TRADES
            ]

            if strong:
                selection = freeze_selection(
                    sel_path,
                    choose_threshold(first, DISCOVERY_LOOKS[0], early=True),
                )
                discovery_verdict = "THRESHOLD_CONGELADO_TEMPRANO"
            elif (
                len(sufficiently_observed) == len(THRESHOLDS)
                and len(bad) == len(THRESHOLDS)
            ):
                discovery_verdict = "FAIL_MODELO_TEMPRANO"
            elif n_predictions >= DISCOVERY_LOOKS[1]:
                final_block = rows[: DISCOVERY_LOOKS[1]]
                selection = freeze_selection(
                    sel_path,
                    choose_threshold(final_block, DISCOVERY_LOOKS[1], early=False),
                )
                discovery_verdict = "THRESHOLD_CONGELADO_24H"
            else:
                discovery_verdict = "CONTINUAR_HASTA_288_PREDICCIONES"

    result = {
        "status": "RUNNING",
        "updated_at": utc_now(),
        "database": str(db),
        "model_name": MODEL_NAME,
        "resolved_model_predictions": n_predictions,
        "threshold_grid": list(THRESHOLDS),
        "discovery_z": DISCOVERY_Z,
        "confirm_z": CONFIRM_Z,
        "discovery_verdict": discovery_verdict,
        "tournament_current_all_rows": tournament,
        "selection_file": str(sel_path),
        "selection": selection,
        "confirmation": None,
        "action": "CONTINUAR_FORWARD",
        "money_real": "BLOQUEADO",
    }

    if discovery_verdict == "FAIL_MODELO_TEMPRANO" and selection is None:
        result["action"] = "PUEDE_ABORTAR_CANDIDATO"
        result["status"] = "FAIL_EARLY"
        return result

    if selection is None:
        remaining_144 = max(0, DISCOVERY_LOOKS[0] - n_predictions)
        remaining_288 = max(0, DISCOVERY_LOOKS[1] - n_predictions)
        result["remaining_to_144"] = remaining_144
        result["remaining_to_288"] = remaining_288
        return result

    threshold = float(selection["selected_threshold"])
    cutoff = int(selection["discovery_cutoff_market_start_ms"])
    confirm_rows = [r for r in rows if r["market_start_ms"] > cutoff]
    confirm_trades = trades_for_threshold(confirm_rows, threshold)

    checkpoint_results = []
    verdict = "ACUMULANDO_CONFIRMACION"
    decisive = None

    for checkpoint in CONFIRM_CHECKPOINTS:
        if len(confirm_trades) < checkpoint:
            continue
        first_n = confirm_trades[:checkpoint]
        cutoff_trade_ms = first_n[-1]["market_start_ms"]
        stats = sample_stats(
            [t["pnl"] for t in first_n],
            [t["cost"] for t in first_n],
            CONFIRM_Z,
        )
        prob = brier(confirm_rows, cutoff_ms=cutoff_trade_ms)
        passed = (
            checkpoint >= MIN_PASS_TRADES
            and stats["lcb_mean_pnl"] is not None
            and stats["lcb_mean_pnl"] > 0
            and stats["net_pnl"] > 0
            and stats["roi_on_cost"] is not None
            and stats["roi_on_cost"] > 0
            and prob["model"] is not None
            and prob["market"] is not None
            and prob["model"] <= prob["market"] + MAX_BRIER_DEGRADATION
        )
        failed = (
            stats["ucb_mean_pnl"] is not None
            and stats["ucb_mean_pnl"] < 0
        )
        item = {
            "checkpoint_trades": checkpoint,
            "stats": stats,
            "brier": prob,
            "pass_development_gate": passed,
            "fail_fast_gate": failed,
        }
        checkpoint_results.append(item)

        if failed:
            verdict = f"FAIL_FAST_{checkpoint}_TRADES"
            decisive = item
            break
        if passed:
            verdict = f"PASS_DESARROLLO_{checkpoint}_TRADES"
            decisive = item
            break

    current_stats = sample_stats(
        [t["pnl"] for t in confirm_trades],
        [t["cost"] for t in confirm_trades],
        CONFIRM_Z,
    )
    current_brier = brier(confirm_rows)

    result["confirmation"] = {
        "selected_threshold": threshold,
        "confirmation_predictions": len(confirm_rows),
        "confirmation_trades": len(confirm_trades),
        "current_stats": current_stats,
        "current_brier": current_brier,
        "checkpoints": checkpoint_results,
        "verdict": verdict,
        "decisive_checkpoint": decisive,
    }

    if verdict.startswith("FAIL_FAST"):
        result["status"] = "FAIL_EARLY"
        result["action"] = "PUEDE_ABORTAR_CANDIDATO"
    elif verdict.startswith("PASS_DESARROLLO"):
        result["status"] = "PASS_DEVELOPMENT"
        result["action"] = (
            "ADELANTAR_SIGUIENTE_FASE_SIN_DETENER_FORWARD; "
            "NO_HABILITAR_DINERO_REAL"
        )
    else:
        result["remaining_to_next_confirmation_checkpoint"] = next(
            (
                cp - len(confirm_trades)
                for cp in CONFIRM_CHECKPOINTS
                if cp > len(confirm_trades)
            ),
            0,
        )

    return result


def fmt_pct(x):
    return "n/a" if x is None else f"{100*x:.2f}%"


def fmt_num(x, digits=5):
    return "n/a" if x is None else f"{x:.{digits}f}"


def print_human(result: dict) -> None:
    print("=" * 68)
    print("FORWARD ACELERADO v0.9.4 - GATE SECUENCIAL")
    print("=" * 68)
    print("Actualizado:", result.get("updated_at"))
    print("Estado:", result.get("status"))
    print("Accion:", result.get("action", ""))
    if result.get("status") == "WAITING_DATABASE":
        print(result.get("message"))
        return

    print("Predicciones resueltas:", result.get("resolved_model_predictions"))
    print("Descubrimiento:", result.get("discovery_verdict"))
    if result.get("selection"):
        s = result["selection"]
        print(
            "Threshold CONGELADO:",
            s["selected_threshold"],
            "| corte:",
            s["discovery_look_predictions"],
            "predicciones",
        )
    else:
        print(
            "Faltan para 144:",
            result.get("remaining_to_144", 0),
            "| para 288:",
            result.get("remaining_to_288", 0),
        )

    print("\nTORNEO DE THRESHOLDS (solo paper):")
    for m in result.get("tournament_current_all_rows", []):
        print(
            f"  edge {m['threshold']:.2f} | "
            f"trades {m['trades']:4d} | "
            f"PnL {fmt_num(m['net_pnl'])} | "
            f"ROI {fmt_pct(m['roi_on_cost'])} | "
            f"LCB {fmt_num(m['lcb_mean_pnl'])} | "
            f"UCB {fmt_num(m['ucb_mean_pnl'])}"
        )

    c = result.get("confirmation")
    if c:
        st = c["current_stats"]
        br = c["current_brier"]
        print("\nCONFIRMACION INDEPENDIENTE:")
        print("  Predicciones:", c["confirmation_predictions"])
        print("  Trades:", c["confirmation_trades"])
        print("  PnL:", fmt_num(st["net_pnl"]))
        print("  ROI:", fmt_pct(st["roi_on_cost"]))
        print("  Win rate:", fmt_pct(st["win_rate"]))
        print(
            "  Media PnL / LCB / UCB:",
            fmt_num(st["mean_pnl"]),
            "/",
            fmt_num(st["lcb_mean_pnl"]),
            "/",
            fmt_num(st["ucb_mean_pnl"]),
        )
        print(
            "  Brier modelo / mercado / delta:",
            fmt_num(br["model"]),
            "/",
            fmt_num(br["market"]),
            "/",
            fmt_num(br["delta_model_minus_market"]),
        )
        print("  VEREDICTO:", c["verdict"])
        if result.get("remaining_to_next_confirmation_checkpoint"):
            print(
                "  Faltan trades para proximo gate:",
                result["remaining_to_next_confirmation_checkpoint"],
            )

    print("\nDINERO REAL:", result.get("money_real", "BLOQUEADO"))
    print("=" * 68)


def save_result(db: Path, result: dict) -> None:
    path = result_path(db)
    temp = path.with_suffix(path.suffix + ".partial")
    temp.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        default=r"data\shadow_forward_twap_transfer_v093.db",
    )
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()

    db = Path(args.db).expanduser().resolve()

    while True:
        try:
            result = analyze(db)
            if db.exists():
                save_result(db, result)
            print_human(result)
        except KeyboardInterrupt:
            print("\nMonitor detenido por usuario. El forward puede seguir corriendo.")
            return
        except Exception as exc:
            print("=" * 68)
            print("MONITOR: error temporal:", repr(exc))
            print("Se reintentara; NO modifica la base del forward.")
            print("=" * 68)

        if not args.watch:
            return
        time.sleep(max(30, int(args.interval)))


if __name__ == "__main__":
    main()
