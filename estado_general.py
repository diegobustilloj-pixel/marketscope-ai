from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import v013_monitor as v013
import v014_monitor as v014
from polymarket_bot import v015
from polymarket_bot.finalizer import ExperimentFinalizer
from polymarket_bot.phase41 import shadow_status
from polymarket_bot.runtime_policy import load_duration_policy


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SUPERVISOR_STATUS = DATA / "supervisor_quantbot_status.json"
SHADOW_DB = DATA / "shadow_forward_twap_transfer_v094a.db"


def _iso_local(moment: datetime) -> str:
    return moment.astimezone().isoformat(timespec="minutes")


def _rate_and_eta(rows: list[dict[str, Any]], maximum: int) -> dict[str, Any]:
    timestamps = sorted(int(row["market_start_ms"]) for row in rows)
    rate = None
    if len(timestamps) >= 2 and timestamps[-1] > timestamps[0]:
        span_hours = (timestamps[-1] - timestamps[0]) / 3_600_000
        rate = (len(timestamps) - 1) / span_hours
    remaining = max(0, maximum - len(rows))
    hours = remaining / rate if rate and rate > 0 else None
    eta = datetime.now(timezone.utc) + timedelta(hours=hours) if hours is not None else None
    return {
        "eligible_per_hour": round(rate, 3) if rate is not None else None,
        "remaining_to_limit": remaining,
        "estimated_hours_to_limit": round(hours, 2) if hours is not None else None,
        "estimated_limit_at_local": _iso_local(eta) if eta is not None else None,
    }


def _v013_state() -> dict[str, Any]:
    scoped, candidates = v013.postcut_population()
    maximum = 300
    target = 10
    progress = _rate_and_eta(scoped, maximum)
    observed_rate = len(candidates) / len(scoped) if scoped else 0.0
    expected_more = (
        (target - len(candidates)) / observed_rate
        if observed_rate > 0 and len(candidates) < target
        else 0.0 if len(candidates) >= target else None
    )
    likely = (
        "TARGET_COMPLETE"
        if len(candidates) >= target
        else "ELIGIBLE_LIMIT_MORE_LIKELY"
        if expected_more is None or expected_more > progress["remaining_to_limit"]
        else "TRADE_TARGET_MORE_LIKELY"
    )
    result_path = DATA / "resultado_v013_forward10.json"
    sealed_result = (
        json.loads(result_path.read_text(encoding="utf-8"))
        if result_path.is_file()
        else None
    )
    return {
        "eligible": len(scoped),
        "eligible_limit": maximum,
        "qualifying_trades": len(candidates),
        "trade_target": target,
        "missing_trades": max(0, target - len(candidates)),
        "observed_trade_rate_per_eligible": round(observed_rate, 6),
        "expected_more_eligible_to_target_at_observed_rate": (
            round(expected_more, 2) if expected_more is not None else None
        ),
        "likely_stopping_path": likely,
        "result_exists": result_path.is_file(),
        "sealed_result_status": (
            sealed_result.get("status") if isinstance(sealed_result, dict) else None
        ),
        "labels_read_by_panel": 0,
        **progress,
    }


def _v014_state() -> dict[str, Any]:
    scoped, strata = v014.postcut_population()
    maximum = 250
    target = 6
    progress = _rate_and_eta(scoped, maximum)
    projections: dict[str, Any] = {}
    target_likely = True
    for name in ("LOW", "MODERATE"):
        count = len(strata[name])
        observed_rate = count / len(scoped) if scoped else 0.0
        expected_more = (
            (target - count) / observed_rate
            if observed_rate > 0 and count < target
            else 0.0 if count >= target else None
        )
        if expected_more is None or expected_more > progress["remaining_to_limit"]:
            target_likely = False
        projections[name] = {
            "trades": count,
            "target": target,
            "missing": max(0, target - count),
            "observed_rate_per_eligible": round(observed_rate, 6),
            "expected_more_eligible_to_target_at_observed_rate": (
                round(expected_more, 2) if expected_more is not None else None
            ),
        }
    result_path = DATA / "resultado_v014_development12.json"
    sealed_result = (
        json.loads(result_path.read_text(encoding="utf-8"))
        if result_path.is_file()
        else None
    )
    return {
        "eligible": len(scoped),
        "eligible_limit": maximum,
        "strata": projections,
        "likely_stopping_path": (
            "BOTH_TARGETS_COMPLETE"
            if all(len(strata[name]) >= target for name in ("LOW", "MODERATE"))
            else "BOTH_TARGETS_MORE_LIKELY"
            if target_likely
            else "ELIGIBLE_LIMIT_MORE_LIKELY"
        ),
        "result_exists": result_path.is_file(),
        "sealed_result_status": (
            sealed_result.get("status") if isinstance(sealed_result, dict) else None
        ),
        "labels_read_by_panel": 0,
        **progress,
    }


def _forward_state() -> dict[str, Any]:
    payload = shadow_status(SHADOW_DB)
    target_raw = payload.get("target_end_at")
    target = (
        datetime.fromisoformat(str(target_raw).replace("Z", "+00:00"))
        if target_raw
        else None
    )
    remaining = max(0.0, (target - datetime.now(timezone.utc)).total_seconds() / 3600) if target else None
    latest = payload.get("latest_health")
    connections = latest.get("connections") if isinstance(latest, dict) else {}
    return {
        "run_status": (
            payload["runs"][-1].get("status")
            if isinstance(payload.get("runs"), list) and payload["runs"]
            else None
        ),
        "experiment_completed_at": payload.get("experiment_completed_at"),
        "target_end_at_utc": target_raw,
        "target_end_at_local": _iso_local(target) if target else None,
        "estimated_hours_remaining": round(remaining, 2) if remaining is not None else None,
        "sqlite_quick_check": payload.get("sqlite_quick_check"),
        "connections": connections,
        "orders_created": payload.get("orders_created"),
        "wallet_required": payload.get("wallet_required"),
    }


def _supervisor_state() -> dict[str, Any]:
    if not SUPERVISOR_STATUS.is_file():
        return {"status": "NO_STATUS"}
    payload = json.loads(SUPERVISOR_STATUS.read_text(encoding="utf-8"))
    workers = payload.get("workers")
    statuses = {
        name: value.get("status")
        for name, value in workers.items()
        if isinstance(value, dict)
    } if isinstance(workers, dict) else {}
    updated_raw = payload.get("updated_at")
    try:
        updated = datetime.fromisoformat(str(updated_raw).replace("Z", "+00:00"))
        age_seconds = max(
            0.0,
            (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds(),
        )
    except (TypeError, ValueError):
        age_seconds = None
    fresh = age_seconds is not None and age_seconds <= 60.0
    healthy = fresh and bool(statuses) and all(
        status in (
            "RUNNING",
            "COMPLETED",
            "COMPLETED_PROCESS_STILL_RUNNING",
            "NOT_APPLICABLE",
            "WAITING_DEPENDENCY",
            "WAITING_ACTIVATION",
        )
        for status in statuses.values()
    )
    return {
        "healthy": healthy,
        "fresh": fresh,
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "updated_at": updated_raw,
        "unknown_python_processes": payload.get("unknown_python_processes"),
        "workers": statuses,
    }


def build_status() -> dict[str, Any]:
    finalizer = ExperimentFinalizer().reconcile(mutate=False)
    policy = load_duration_policy()
    return {
        "schema": "estado_general_quantbot_1",
        "created_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
        "supervisor": _supervisor_state(),
        "v013": _v013_state(),
        "v014": _v014_state(),
        "v015": v015.status(),
        "forward": _forward_state(),
        "closure": {
            "forward_audit": finalizer["forward_audit"],
            "final_24h_analysis": finalizer["final_24h_analysis"],
            "next_actions": finalizer["next_actions"],
        },
        "duration_policy": {
            "maximum_new_experiment_hours": policy["maximum_new_experiment_hours"],
            "maximum_backtest_runtime_hours": policy["maximum_backtest_runtime_hours"],
            "grandfathered_active_forward_hours": policy["grandfathered_experiments"][0]["target_hours"],
        },
        "labels_or_outcomes_read_by_panel": 0,
        "orders_enabled": False,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


def _hours(value: Any) -> str:
    return "sin estimacion" if value is None else f"{float(value):.1f} h"


def print_human(payload: dict[str, Any]) -> None:
    v13 = payload["v013"]
    v14 = payload["v014"]
    v15_state = payload["v015"]["activation"]["status"]
    forward = payload["forward"]
    supervisor = payload["supervisor"]
    print("=" * 92)
    print("POLYMARKER QUANTBOT - ESTADO GENERAL SEGURO")
    print("=" * 92)
    print(
        "SUPERVISOR:",
        "OK" if supervisor.get("healthy") else "REVISAR",
        "| desconocidos=",
        len(supervisor.get("unknown_python_processes") or []),
    )
    print("-")
    print(
        f"V0.13: elegibles {v13['eligible']}/300 | trades "
        f"{v13['qualifying_trades']}/10 | faltan {v13['missing_trades']}"
    )
    if v13.get("sealed_result_status"):
        print("       RESULTADO SELLADO:", v13["sealed_result_status"])
    print(
        "       ETA limite:",
        _hours(v13["estimated_hours_to_limit"]),
        "| hora local:",
        v13["estimated_limit_at_local"],
        "| ruta probable:",
        v13["likely_stopping_path"],
    )
    low = v14["strata"]["LOW"]
    moderate = v14["strata"]["MODERATE"]
    print(
        f"V0.14: elegibles {v14['eligible']}/250 | LOW {low['trades']}/6 | "
        f"MODERATE {moderate['trades']}/6"
    )
    if v14.get("sealed_result_status"):
        print("       RESULTADO SELLADO:", v14["sealed_result_status"])
    print(
        "       ETA limite:",
        _hours(v14["estimated_hours_to_limit"]),
        "| hora local:",
        v14["estimated_limit_at_local"],
        "| ruta probable:",
        v14["likely_stopping_path"],
    )
    print("V0.15:", v15_state)
    print("-")
    print(
        "FORWARD:",
        forward["run_status"],
        "| faltan:",
        _hours(forward["estimated_hours_remaining"]),
        "| fin local:",
        forward["target_end_at_local"],
    )
    print("CONEXIONES:", json.dumps(forward["connections"], ensure_ascii=False))
    print("AUDITORIA FINAL:", payload["closure"]["forward_audit"]["status"])
    print(
        "ANALISIS FINAL 24H:",
        payload["closure"]["final_24h_analysis"]["status"],
    )
    print(
        "POLITICA: nuevos experimentos/backtests maximo",
        payload["duration_policy"]["maximum_new_experiment_hours"],
        "horas",
    )
    print("PROXIMOS PASOS:", ", ".join(payload["closure"]["next_actions"]))
    print("-")
    print("ESTE PANEL LEYO LABELS/OUTCOMES: 0")
    print("DINERO REAL: BLOQUEADO")
    print("=" * 92)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = build_status()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_human(payload)


if __name__ == "__main__":
    main()
