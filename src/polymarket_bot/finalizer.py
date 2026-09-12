from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.analysis24 import OUTPUT as ANALYSIS24_OUTPUT
from polymarket_bot.analysis24 import analyze_last_24h, write_once as write_analysis24_once
from polymarket_bot.phase41 import audit_shadow_forward, shadow_status
from polymarket_bot import v015


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
SHADOW_DB = DATA / "shadow_forward_twap_transfer_v094a.db"
PHASE4_DB = DATA / "fase4_modelos.db"
FORWARD_AUDIT = DATA / "resultado_auditoria_twap_transfer_v094a.json"
FORWARD_REPORT = ROOT / "auditoria_forward_twap_transfer_v094a_7_dias.txt"
SUMMARY = DATA / "resumen_cierre_experimentos.json"
RESULT_V013 = DATA / "resultado_v013_forward10.json"
RESULT_V014 = DATA / "resultado_v014_development12.json"
RESULT_V015 = DATA / "resultado_v015_confirmation10.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON incompatible: {path}")
    return payload


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _result_summary(path: Path, kind: str) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "status": "PENDING"}
    payload = _load(path)
    common = {
        "exists": True,
        "status": payload.get("status"),
        "created_at": payload.get("created_at"),
        "labels_read": payload.get("labels_read"),
        "real_money": payload.get("real_money"),
    }
    if kind == "v013":
        common.update(
            {
                "eligible_markets": payload.get(
                    "eligible_markets_seen_at_stop",
                    payload.get("eligible_markets_examined"),
                ),
                "qualifying_trades": payload.get("qualifying_trades"),
                "net_pnl_5shares": payload.get("net_pnl_5shares"),
                "roi_on_cost": payload.get("roi_on_cost"),
                "failures": payload.get("failures"),
            }
        )
    elif kind == "v014":
        strata = payload.get("strata_results")
        summarized_strata: dict[str, Any] = {}
        if isinstance(strata, dict):
            for name in ("LOW", "MODERATE"):
                value = strata.get(name)
                if isinstance(value, dict):
                    summarized_strata[name] = {
                        "trades": value.get("trades"),
                        "wins": value.get("wins"),
                        "net_pnl_5shares": value.get("net_pnl_5shares"),
                        "roi_on_cost": value.get("roi_on_cost"),
                        "failures": value.get("failures"),
                        "qualifies_for_v015": value.get("qualifies_for_v015"),
                    }
        common.update(
            {
                "eligible_markets": payload.get(
                    "eligible_markets_seen_at_stop",
                    payload.get("eligible_markets_examined"),
                ),
                "selected_for_v015": payload.get("selected_for_v015"),
                "strata": summarized_strata,
                "v013_candidate_outcomes_read": payload.get(
                    "v013_candidate_outcomes_read"
                ),
            }
        )
    elif kind == "v015":
        common.update(
            {
                "selected_stratum": payload.get("selected_stratum"),
                "eligible_markets": payload.get(
                    "eligible_markets_seen_at_stop",
                    payload.get("eligible_markets_examined"),
                ),
                "qualifying_trades": payload.get("qualifying_trades"),
                "net_pnl_5shares": payload.get("net_pnl_5shares"),
                "roi_on_cost": payload.get("roi_on_cost"),
                "failures": payload.get("failures"),
            }
        )
    return common


def _forward_state() -> dict[str, Any]:
    if not SHADOW_DB.is_file():
        return {
            "database_exists": False,
            "experiment_complete": False,
            "status": "NO_DATABASE",
        }
    payload = shadow_status(SHADOW_DB)
    runs = payload.get("runs")
    current_run = runs[-1] if isinstance(runs, list) and runs else None
    return {
        "database_exists": True,
        "experiment_complete": bool(payload.get("experiment_completed_at")),
        "experiment_completed_at": payload.get("experiment_completed_at"),
        "target_end_at": payload.get("target_end_at"),
        "sqlite_quick_check": payload.get("sqlite_quick_check"),
        "current_run": current_run,
        "orders_created": payload.get("orders_created"),
        "wallet_required": payload.get("wallet_required"),
    }


def _write_forward_report(audit: dict[str, Any]) -> None:
    status = shadow_status(SHADOW_DB)
    report = (
        "FASE 4.2 v0.9.4a1 - AUDITORIA FORWARD AUTOMATICA\n"
        f"Fin: {utc_now()}\n\n"
        "AUDITORIA\n"
        + json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n\nESTADO FINAL\n"
        + json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n\nDINERO REAL: BLOQUEADO\n"
    )
    _atomic_text(FORWARD_REPORT, report)


def _audit_if_complete(forward: dict[str, Any], *, mutate: bool) -> dict[str, Any]:
    if FORWARD_AUDIT.is_file():
        payload = _load(FORWARD_AUDIT)
        return {
            "exists": True,
            "status": "AUDITED",
            "experiment_complete": payload.get("experiment_complete"),
            "technical_passed": payload.get("technical_passed"),
            "forward_candidate": payload.get("forward_candidate"),
            "twap_retraining_required": payload.get("twap_retraining_required"),
            "report_exists": FORWARD_REPORT.is_file(),
        }
    if not forward["experiment_complete"]:
        return {"exists": False, "status": "WAITING_FORWARD_COMPLETION"}
    if not mutate:
        return {"exists": False, "status": "READY_TO_AUDIT"}
    if forward.get("sqlite_quick_check") != "ok":
        raise RuntimeError("No se audita forward: sqlite_quick_check no es ok")
    result = audit_shadow_forward(
        shadow_db=SHADOW_DB,
        phase4_db=PHASE4_DB,
    )
    _atomic_json(FORWARD_AUDIT, result)
    _write_forward_report(result)
    return {
        "exists": True,
        "status": "AUDITED",
        "experiment_complete": result.get("experiment_complete"),
        "technical_passed": result.get("technical_passed"),
        "forward_candidate": result.get("forward_candidate"),
        "twap_retraining_required": result.get("twap_retraining_required"),
        "report_exists": True,
    }


def _analysis24_if_complete(
    forward_audit: dict[str, Any],
    *,
    mutate: bool,
) -> dict[str, Any]:
    if ANALYSIS24_OUTPUT.is_file():
        payload = _load(ANALYSIS24_OUTPUT)
        return {
            "exists": True,
            "status": payload.get("status"),
            "window_hours": payload.get("window_hours"),
            "technical_passed": payload.get("technical_passed"),
            "diagnostic_candidate": payload.get("diagnostic_candidate"),
            "outcomes_read": payload.get("outcomes_read"),
        }
    if forward_audit.get("status") != "AUDITED":
        return {
            "exists": False,
            "status": "WAITING_OFFICIAL_7D_AUDIT",
            "partial_outcomes_read": 0,
        }
    if not mutate:
        return {
            "exists": False,
            "status": "READY_AFTER_OFFICIAL_7D_AUDIT",
            "partial_outcomes_read": 0,
        }
    payload = analyze_last_24h(SHADOW_DB)
    if payload.get("status") != "ANALYZED_AFTER_FULL_COMPLETION":
        raise RuntimeError("El analisis 24h no encontro el forward completo")
    write_analysis24_once(payload)
    return {
        "exists": True,
        "status": payload["status"],
        "window_hours": payload["window_hours"],
        "technical_passed": payload["technical_passed"],
        "diagnostic_candidate": payload["diagnostic_candidate"],
        "outcomes_read": payload["outcomes_read"],
    }


def _next_actions(
    v013_result: dict[str, Any],
    v014_result: dict[str, Any],
    v015_activation: dict[str, Any],
    v015_result: dict[str, Any],
    forward_audit: dict[str, Any],
    analysis24: dict[str, Any],
) -> list[str]:
    actions: list[str] = []
    if not v013_result["exists"]:
        actions.append("WAIT_V013_RESULT")
    if not v014_result["exists"]:
        actions.append("WAIT_V014_RESULT")
    if forward_audit["status"] in (
        "WAITING_FORWARD_COMPLETION",
        "READY_TO_AUDIT",
    ):
        actions.append(forward_audit["status"])
    activation_state = v015_activation["status"]
    if activation_state == "WAITING_V014_RESULT":
        actions.append("WAIT_V015_SELECTION")
    elif activation_state == "READY_TO_ACTIVATE":
        actions.append("ACTIVATE_V015")
    elif activation_state == "ACTIVE" and not v015_result["exists"]:
        actions.append("COLLECT_V015_CONFIRMATION10")
    elif activation_state == "NOT_APPLICABLE_V014_NO_CANDIDATE":
        actions.append("V015_NOT_APPLICABLE")
    if v015_result.get("status") == "PASS_V015_CONFIRMATION10":
        actions.append("DESIGN_EXTENDED_CONFIRMATION_AND_FREEZE_RISK_LIMITS")
    if analysis24["status"] == "WAITING_OFFICIAL_7D_AUDIT":
        actions.append("WAIT_PREREGISTERED_FINAL_24H_ANALYSIS")
    elif analysis24["status"] == "READY_AFTER_OFFICIAL_7D_AUDIT":
        actions.append("RUN_PREREGISTERED_FINAL_24H_ANALYSIS")
    return actions or ["ALL_CURRENT_WORKFLOWS_TERMINAL"]


class ExperimentFinalizer:
    def __init__(self, root: Path = ROOT) -> None:
        if root.resolve() != ROOT.resolve():
            raise ValueError("ExperimentFinalizer solo admite el proyecto configurado")

    def reconcile(self, *, mutate: bool = True) -> dict[str, Any]:
        v015_activation = v015.activation_status()
        if mutate and v015_activation["status"] == "READY_TO_ACTIVATE":
            active = v015.activate_if_ready()
            if active is None:
                raise RuntimeError("v0.15 estaba listo pero no se activo")
            v015_activation = v015.activation_status()

        forward = _forward_state()
        forward_audit = _audit_if_complete(forward, mutate=mutate)
        analysis24 = _analysis24_if_complete(forward_audit, mutate=mutate)
        v013_result = _result_summary(RESULT_V013, "v013")
        v014_result = _result_summary(RESULT_V014, "v014")
        v015_result = _result_summary(RESULT_V015, "v015")
        payload = {
            "schema": "resumen_cierre_experimentos_1",
            "updated_at": utc_now(),
            "forward": forward,
            "forward_audit": forward_audit,
            "final_24h_analysis": analysis24,
            "v013": v013_result,
            "v014": v014_result,
            "v015_activation": v015_activation,
            "v015": v015_result,
            "next_actions": _next_actions(
                v013_result,
                v014_result,
                v015_activation,
                v015_result,
                forward_audit,
                analysis24,
            ),
            "raw_labels_or_outcomes_read_by_finalizer": 0,
            "partial_labels_or_outcomes_read": 0,
            "forward_audit_outcomes_policy": (
                "solo despues de experiment_completed_at"
            ),
            "orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
        if mutate:
            _atomic_json(SUMMARY, payload)
        return payload


__all__ = [
    "ExperimentFinalizer",
    "FORWARD_AUDIT",
    "FORWARD_REPORT",
    "SUMMARY",
]
