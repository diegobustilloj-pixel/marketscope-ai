from __future__ import annotations

import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import calibrar_v012_execution_ev as cal


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
TEMPLATE = DATA / "prereg_v015_confirmation_template.json"
ACTIVE = DATA / "prereg_v015_confirmation.json"
RESULT_V014 = DATA / "resultado_v014_development12.json"
RESULT = DATA / "resultado_v015_confirmation10.json"
THRESHOLDS = DATA / "prereg_v012_thresholds.json"
SHADOW_DB = DATA / "shadow_forward_twap_transfer_v094a.db"
PAPER_TRADER = ROOT / "paper_trader_v015.py"
POLL_SECONDS = 15
MARKET_INTERVAL_MS = 300_000

TEMPLATE_SCHEMA = "prereg_v015_confirmation10_template"
ACTIVE_SCHEMA = "prereg_v015_confirmation10"
RESULT_SCHEMA = "resultado_v015_confirmation10"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON incompatible: {path}")
    return payload


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_new_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _check_sources(sources: Any) -> None:
    if not isinstance(sources, dict) or not sources:
        raise RuntimeError("El template v0.15 no declara fuentes congeladas")
    for relative, expected in sorted(sources.items()):
        path = (ROOT / str(relative)).resolve()
        if not path.is_file():
            raise RuntimeError(f"Falta fuente congelada v0.15: {relative}")
        if sha(path) != str(expected):
            raise RuntimeError(f"Hash v0.15 no coincide: {relative}")


def verify_template() -> dict[str, Any]:
    if not TEMPLATE.is_file():
        raise RuntimeError(f"Falta template v0.15: {TEMPLATE}")
    template = load(TEMPLATE)
    if template.get("schema") != TEMPLATE_SCHEMA:
        raise RuntimeError("Schema del template v0.15 incompatible")
    if template.get("status") != "FROZEN_WAITING_V014":
        raise RuntimeError("Template v0.15 no esta congelado")
    if template.get("no_partial_peeking") is not True:
        raise RuntimeError("v0.15 requiere no_partial_peeking=True")
    if template.get("no_retuning") is not True:
        raise RuntimeError("v0.15 requiere no_retuning=True")
    if template.get("no_threshold_rescue") is not True:
        raise RuntimeError("v0.15 requiere no_threshold_rescue=True")
    if int(template.get("target_trades", 0)) != 10:
        raise RuntimeError("v0.15 debe evaluar exactamente 10 trades")
    if int(template.get("maximum_eligible_markets", 0)) != 500:
        raise RuntimeError("v0.15 debe detenerse en 500 elegibles")
    if float(template.get("order_size_shares", 0)) != 5.0:
        raise RuntimeError("v0.15 debe usar 5 shares paper")
    if template.get("real_money") != "BLOQUEADO":
        raise RuntimeError("Dinero real debe permanecer bloqueado")
    _check_sources(template.get("sources"))
    return template


def _v014_selection(result: dict[str, Any]) -> str | None:
    if result.get("schema") != "resultado_v014_development12":
        raise RuntimeError("Resultado v0.14 incompatible")
    if result.get("status") != "DEVELOPMENT_CANDIDATE_SELECTED":
        return None
    selected = result.get("selected_for_v015")
    if selected not in ("LOW", "MODERATE"):
        raise RuntimeError("v0.14 declaro seleccion v0.15 invalida")
    strata = result.get("strata_results")
    if not isinstance(strata, dict):
        raise RuntimeError("Resultado v0.14 sin strata_results")
    selected_result = strata.get(selected)
    if (
        not isinstance(selected_result, dict)
        or selected_result.get("qualifies_for_v015") is not True
    ):
        raise RuntimeError("La seleccion v0.14 no califico para v0.15")
    qualified = [
        name
        for name in ("LOW", "MODERATE")
        if isinstance(strata.get(name), dict)
        and strata[name].get("qualifies_for_v015") is True
    ]
    if selected not in qualified:
        raise RuntimeError("Seleccion v0.14 inconsistente")
    return str(selected)


def activation_status() -> dict[str, Any]:
    verify_template()
    if ACTIVE.is_file():
        active = verify_active()
        return {
            "status": "ACTIVE",
            "selected_stratum": active["selected_stratum"],
            "minimum_market_start_ms": active["minimum_market_start_ms"],
        }
    if not RESULT_V014.is_file():
        return {"status": "WAITING_V014_RESULT"}
    result = load(RESULT_V014)
    selected = _v014_selection(result)
    if selected is None:
        return {
            "status": "NOT_APPLICABLE_V014_NO_CANDIDATE",
            "v014_status": result.get("status"),
        }
    return {"status": "READY_TO_ACTIVATE", "selected_stratum": selected}


def _result_timestamp_ms(result: dict[str, Any]) -> int:
    raw = result.get("created_at")
    if not isinstance(raw, str):
        raise RuntimeError("Resultado v0.14 sin created_at")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("created_at v0.14 invalido") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("created_at v0.14 debe incluir zona horaria")
    return int(parsed.timestamp() * 1000)


def activate_if_ready() -> dict[str, Any] | None:
    template = verify_template()
    if ACTIVE.is_file():
        return verify_active()
    if not RESULT_V014.is_file():
        return None
    result = load(RESULT_V014)
    selected = _v014_selection(result)
    if selected is None:
        return None

    result_timestamp_ms = _result_timestamp_ms(result)
    minimum_start_ms = (
        result_timestamp_ms // MARKET_INTERVAL_MS + 1
    ) * MARKET_INTERVAL_MS
    active = {
        "schema": ACTIVE_SCHEMA,
        "created_at": utc_now(),
        "status": "FROZEN_ACTIVE_CONFIRMATION",
        "template": str(TEMPLATE.relative_to(ROOT)).replace("\\", "/"),
        "template_sha256": sha(TEMPLATE),
        "source_v014_result": str(RESULT_V014.relative_to(ROOT)).replace("\\", "/"),
        "source_v014_result_sha256": sha(RESULT_V014),
        "source_v014_result_created_at": result["created_at"],
        "selected_stratum": selected,
        "minimum_market_start_ms": minimum_start_ms,
        "cutoff_rule": template["cutoff_rule"],
        "severity": template["severity"],
        "direction": template["direction"],
        "entry_cost_field": template["entry_cost_field"],
        "entry_cost_band": template["cost_strata"][selected],
        "order_size_shares": template["order_size_shares"],
        "target_trades": template["target_trades"],
        "maximum_eligible_markets": template["maximum_eligible_markets"],
        "confirmation_gates": template["confirmation_gates"],
        "stopping_rule": template["stopping_rule"],
        "if_less_than_target_at_maximum": template[
            "if_less_than_target_at_maximum"
        ],
        "no_partial_peeking": True,
        "no_retuning": True,
        "no_threshold_rescue": True,
        "monitor_module_sha256": sha(Path(__file__).resolve()),
        "paper_trader_sha256": sha(PAPER_TRADER),
        "sources": template["sources"],
        "real_money": "BLOQUEADO",
    }
    try:
        _write_new_json(ACTIVE, active)
    except FileExistsError:
        return verify_active()
    return verify_active()


def verify_active() -> dict[str, Any]:
    template = verify_template()
    if not ACTIVE.is_file():
        raise RuntimeError("v0.15 aun no esta activado")
    active = load(ACTIVE)
    if active.get("schema") != ACTIVE_SCHEMA:
        raise RuntimeError("Schema activo v0.15 incompatible")
    if active.get("status") != "FROZEN_ACTIVE_CONFIRMATION":
        raise RuntimeError("Preregistro v0.15 no esta congelado")
    if active.get("template_sha256") != sha(TEMPLATE):
        raise RuntimeError("Template v0.15 cambio despues de activar")
    if active.get("sources") != template.get("sources"):
        raise RuntimeError("Fuentes v0.15 cambiaron al activar")
    _check_sources(active.get("sources"))
    if not RESULT_V014.is_file():
        raise RuntimeError("Falta resultado v0.14 que activo v0.15")
    if active.get("source_v014_result_sha256") != sha(RESULT_V014):
        raise RuntimeError("Resultado v0.14 cambio despues de activar v0.15")
    selected = _v014_selection(load(RESULT_V014))
    if selected != active.get("selected_stratum"):
        raise RuntimeError("Seleccion activa v0.15 ya no coincide con v0.14")
    if active.get("monitor_module_sha256") != sha(Path(__file__).resolve()):
        raise RuntimeError("Monitor v0.15 cambio despues de activar")
    if active.get("paper_trader_sha256") != sha(PAPER_TRADER):
        raise RuntimeError("Paper trader v0.15 cambio despues de activar")
    if active.get("no_partial_peeking") is not True:
        raise RuntimeError("v0.15 requiere no_partial_peeking=True")
    if int(active.get("target_trades", 0)) != 10:
        raise RuntimeError("Target activo v0.15 incompatible")
    if int(active.get("maximum_eligible_markets", 0)) != 500:
        raise RuntimeError("Maximo activo v0.15 incompatible")
    if active.get("real_money") != "BLOQUEADO":
        raise RuntimeError("Dinero real debe permanecer bloqueado")
    return active


def _inside_band(cost: Any, band: dict[str, Any]) -> bool:
    try:
        value = float(cost)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(value):
        return False
    lower = float(band["cost_min"])
    upper = float(band["cost_max"])
    lower_ok = value >= lower if band["cost_min_inclusive"] else value > lower
    upper_ok = value <= upper if band["cost_max_inclusive"] else value < upper
    return lower_ok and upper_ok


def postcut_population() -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    active = verify_active()
    thresholds = load(THRESHOLDS)
    strict = thresholds["thresholds"]["strict"]
    shock_min = float(strict["shock_abs_bps_min"])
    response_max = float(strict["market_response_ratio_max"])
    minimum = int(active["minimum_market_start_ms"])
    maximum = int(active["maximum_eligible_markets"])

    scoped = [
        row
        for row in cal.eligible()
        if int(row["market_start_ms"]) >= minimum
    ][:maximum]
    candidates: list[dict[str, Any]] = []
    for row in scoped:
        if float(row["abs_twap_move_bps"]) < shock_min:
            continue
        if float(row["market_response_ratio"]) > response_max:
            continue
        if _inside_band(row.get("chosen_side_cost_60"), active["entry_cost_band"]):
            candidates.append(row)
    return active, scoped, candidates


def read_only_target_labels(ids: list[str]) -> dict[str, str]:
    if len(ids) != 10 or len(set(ids)) != 10:
        raise RuntimeError("Por diseno v0.15 solo lee exactamente 10 labels unicos")
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


def _write_frequency_failure(
    active: dict[str, Any],
    eligible_n: int,
    qualifying_n: int,
) -> None:
    if RESULT.exists():
        return
    payload = {
        "schema": RESULT_SCHEMA,
        "created_at": utc_now(),
        "status": "FAIL_INSUFFICIENT_FREQUENCY",
        "selected_stratum": active["selected_stratum"],
        "eligible_markets_examined": eligible_n,
        "qualifying_trades": qualifying_n,
        "labels_read": 0,
        "pnl_calculated": False,
        "failures": ["INSUFFICIENT_FREQUENCY"],
        "source_prereg_sha256": sha(ACTIVE),
        "real_money": "BLOQUEADO",
    }
    _write_new_json(RESULT, payload)


def evaluate_if_ready() -> bool:
    active, scoped, candidates = postcut_population()
    if RESULT.exists():
        return True
    target_n = int(active["target_trades"])
    maximum = int(active["maximum_eligible_markets"])
    if len(candidates) < target_n:
        if len(scoped) >= maximum:
            _write_frequency_failure(active, len(scoped), len(candidates))
            return True
        return False

    target = candidates[:target_n]
    ids = [str(row["condition_id"]) for row in target]
    labels = read_only_target_labels(ids)
    missing = [condition_id for condition_id in ids if condition_id not in labels]
    if missing:
        print("[WAIT] v0.15 tiene 10 trades; faltan", len(missing), "labels")
        return False

    size = float(active["order_size_shares"])
    trades: list[dict[str, Any]] = []
    for row in target:
        side = "UP" if int(row["direction"]) > 0 else "DOWN"
        outcome = labels[str(row["condition_id"])]
        cost = float(row["chosen_side_cost_60"])
        won = side == outcome
        pnl_per_share = (1.0 - cost) if won else -cost
        trades.append(
            {
                "condition_id": str(row["condition_id"]),
                "market_start_ms": int(row["market_start_ms"]),
                "selected_stratum": active["selected_stratum"],
                "side": side,
                "outcome": outcome,
                "win": won,
                "entry_cost_per_share": cost,
                "pnl_per_share": pnl_per_share,
                "pnl_total": pnl_per_share * size,
                "abs_twap_move_bps": float(row["abs_twap_move_bps"]),
                "market_response_ratio": float(row["market_response_ratio"]),
            }
        )

    wins = sum(1 for trade in trades if trade["win"])
    net = sum(float(trade["pnl_total"]) for trade in trades)
    capital = sum(float(trade["entry_cost_per_share"]) * size for trade in trades)
    roi = net / capital if capital > 0 else None
    first = sum(float(trade["pnl_total"]) for trade in trades[:5])
    second = sum(float(trade["pnl_total"]) for trade in trades[5:])
    positives = [float(trade["pnl_total"]) for trade in trades if trade["pnl_total"] > 0]
    gross_positive = sum(positives)
    positive_share = (
        max(positives) / gross_positive
        if positives and gross_positive > 0
        else 1.0
    )
    gates = active["confirmation_gates"]
    failures: list[str] = []
    if len(trades) < int(gates["minimum_trades"]):
        failures.append("MIN_TRADES")
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

    status = "PASS_V015_CONFIRMATION10" if not failures else "FAIL_V015_CONFIRMATION10"
    payload = {
        "schema": RESULT_SCHEMA,
        "created_at": utc_now(),
        "status": status,
        "selected_stratum": active["selected_stratum"],
        "eligible_markets_seen_at_stop": len(scoped),
        "qualifying_trades": len(trades),
        "wins": wins,
        "win_rate": wins / len(trades),
        "net_pnl_5shares": net,
        "capital_deployed": capital,
        "roi_on_cost": roi,
        "first_half_net_pnl": first,
        "second_half_net_pnl": second,
        "maximum_single_positive_trade_share": positive_share,
        "failures": failures,
        "labels_read": 10,
        "trades_detail": trades,
        "source_prereg_sha256": sha(ACTIVE),
        "real_money": "BLOQUEADO",
    }
    _write_new_json(RESULT, payload)
    return True


def status() -> dict[str, Any]:
    activation = activation_status()
    payload: dict[str, Any] = {
        "schema": "status_v015_confirmation10",
        "activation": activation,
        "labels_read_by_status": 0,
        "real_money": "BLOQUEADO",
    }
    if activation["status"] != "ACTIVE":
        return payload
    active, scoped, candidates = postcut_population()
    payload.update(
        {
            "selected_stratum": active["selected_stratum"],
            "eligible_markets": len(scoped),
            "maximum_eligible_markets": int(active["maximum_eligible_markets"]),
            "qualifying_trades": len(candidates),
            "target_trades": int(active["target_trades"]),
            "missing_trades": max(0, int(active["target_trades"]) - len(candidates)),
            "result_exists": RESULT.is_file(),
        }
    )
    return payload


def monitor() -> None:
    active = activate_if_ready()
    if active is None:
        print(json.dumps(status(), ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("=" * 92)
    print("V0.15 CONFIRMATION10 - PAPER ONLY")
    print("STRATUM:", active["selected_stratum"])
    print("TARGET: 10 trades; MAXIMO: 500 elegibles")
    print("No lee outcomes antes de completar 10 trades.")
    print("DINERO REAL: BLOQUEADO")
    print("=" * 92)
    last: tuple[int, int] | None = None
    try:
        while True:
            _, scoped, candidates = postcut_population()
            state = (len(scoped), len(candidates))
            if state != last:
                print(
                    f"[STATUS] elegibles={state[0]}/500 | trades={state[1]}/10 | "
                    f"faltan={max(0, 10-state[1])}"
                )
                last = state
            if evaluate_if_ready():
                return
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nMonitor v0.15 detenido; collectors siguen independientes")


__all__ = [
    "ACTIVE",
    "RESULT",
    "RESULT_V014",
    "TEMPLATE",
    "activate_if_ready",
    "activation_status",
    "evaluate_if_ready",
    "monitor",
    "postcut_population",
    "read_only_target_labels",
    "sha",
    "status",
    "verify_active",
    "verify_template",
]
