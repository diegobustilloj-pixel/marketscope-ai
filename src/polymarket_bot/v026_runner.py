from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib

from polymarket_bot.config import Settings
from polymarket_bot.phase41 import ShadowStore, run_shadow_forward, shadow_status
from polymarket_bot.runtime_policy import enforce_forward_duration
from polymarket_bot.v018_runner import ROOT, _parse_utc, sha256_file
from polymarket_bot.v022_runner import V022ProcessLock
from polymarket_bot.v024_tournament import market_record
from polymarket_bot.v026_strategy import eligible_arm_ids, validate_arm_config


PREREG_SCHEMA = "prereg_v026_down_cost_adjusted_replication_1"
MAXIMUM_HOURS = 24.0
VARIANT = "V0.26_DOWN_COST_ADJUSTED_REPLICATION_24H"


class V026Error(RuntimeError):
    pass


def _resolve_project_file(relative_path: Any, *, label: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise V026Error(f"Ruta V0.26 ausente: {label}")
    path = (ROOT / relative_path).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise V026Error(f"Ruta V0.26 fuera del proyecto: {label}") from exc
    if not path.is_file():
        raise V026Error(f"Archivo V0.26 ausente: {label}")
    return path


def load_and_verify_prereg(path: str | Path) -> dict[str, Any]:
    prereg_path = Path(path).resolve()
    payload = json.loads(prereg_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != PREREG_SCHEMA:
        raise V026Error("Prerregistro V0.26 incompatible")
    if payload.get("status") != "FROZEN_DOWN_COST_ADJUSTED_REPLICATION":
        raise V026Error("V0.26 no esta congelado")
    if float(payload.get("maximum_hours", 0)) != MAXIMUM_HOURS:
        raise V026Error("V0.26 debe durar exactamente 24 horas")
    if payload.get("fixed_full_window") is not True:
        raise V026Error("V0.26 requiere ventana completa fija")
    validate_arm_config(payload.get("arms"))

    expected_decision = {
        "minimum_down_trades": 15,
        "minimum_down_trades_per_half": 5,
        "requires_positive_down_net_pnl": True,
        "requires_down_profit_factor_above_one": True,
        "requires_positive_first_half": True,
        "requires_positive_second_half": True,
        "requires_positive_raw_direction_contrast": True,
        "requires_positive_cost_matched_contrast": True,
        "requires_positive_down_lcb_for_statistical_pass": True,
        "requires_positive_direction_lcb_for_statistical_pass": True,
        "controls_can_be_selected": False,
    }
    decision = payload.get("decision_contract")
    if not isinstance(decision, dict):
        raise V026Error("Contrato de decision V0.26 ausente")
    for key, expected in expected_decision.items():
        if decision.get(key) != expected:
            raise V026Error(f"Decision V0.26 incompatible: {key}")

    expected_stopping = {
        "maximum_hours": 24.0,
        "fixed_full_window": True,
        "early_stop_by_observed_pnl": False,
        "final_result_only": True,
        "intermediate_scheduled_reports": False,
    }
    stopping = payload.get("stopping")
    if not isinstance(stopping, dict):
        raise V026Error("Contrato de parada V0.26 ausente")
    for key, expected in expected_stopping.items():
        if stopping.get(key) != expected:
            raise V026Error(f"Parada V0.26 incompatible: {key}")

    expected_safety = {
        "wallet_required": False,
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "real_money": "BLOQUEADO",
        "active_forward_modified": False,
        "maximum_hours": 24,
    }
    safety = payload.get("safety")
    if not isinstance(safety, dict):
        raise V026Error("Safety V0.26 ausente")
    for key, expected in expected_safety.items():
        if safety.get(key) != expected:
            raise V026Error(f"Safety V0.26 incumplido: {key}")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise V026Error("Artefactos V0.26 ausentes")
    for key in ("model", "phase4"):
        item = artifacts.get(key)
        if not isinstance(item, dict):
            raise V026Error(f"Artefacto V0.26 ausente: {key}")
        artifact_path = _resolve_project_file(item.get("relative_path"), label=key)
        if sha256_file(artifact_path) != item.get("sha256"):
            raise V026Error(f"Hash V0.26 no coincide: {key}")

    for key in ("design_evidence", "closed_diagnostic"):
        item = payload.get(key)
        if not isinstance(item, dict):
            raise V026Error(f"Evidencia V0.26 ausente: {key}")
        item_path = _resolve_project_file(item.get("relative_path"), label=key)
        if sha256_file(item_path) != item.get("sha256"):
            raise V026Error(f"Evidencia V0.26 no coincide: {key}")

    hashes = payload.get("code_hashes")
    if not isinstance(hashes, dict):
        raise V026Error("Hashes de codigo V0.26 ausentes")
    expected_hashes = {
        "strategy": sha256_file(ROOT / "src/polymarket_bot/v026_strategy.py"),
        "runner": sha256_file(Path(__file__)),
        "entrypoint": sha256_file(ROOT / "v026_monitor.py"),
        "auditor": sha256_file(ROOT / "src/polymarket_bot/v026_audit.py"),
        "diagnostic": sha256_file(ROOT / "src/polymarket_bot/v026_diagnostic.py"),
        "collector_and_model_runtime": sha256_file(
            ROOT / "src/polymarket_bot/phase41.py"
        ),
        "cost_runtime": sha256_file(ROOT / "src/polymarket_bot/phase4.py"),
        "duration_policy_runtime": sha256_file(
            ROOT / "src/polymarket_bot/runtime_policy.py"
        ),
    }
    for key, actual in expected_hashes.items():
        if hashes.get(key) != actual:
            raise V026Error(f"Hash V0.26 no coincide: {key}")
    return payload


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _bind_database(
    *, database: Path, prereg: dict[str, Any], prereg_path: Path
) -> None:
    existed = database.is_file()
    model_path = _resolve_project_file(
        prereg["artifacts"]["model"]["relative_path"], label="model"
    )
    artifact = joblib.load(model_path)
    if (
        not isinstance(artifact, dict)
        or artifact.get("artifact_type") != "polymarket_shadow_forward_models"
        or artifact.get("orders_enabled") is not False
    ):
        raise V026Error("Modelo V0.26 incompatible o inseguro")
    store = ShadowStore(database)
    store.open(target_hours=MAXIMUM_HOURS, artifact=artifact)
    try:
        meta = store.meta()
        prereg_hash = sha256_file(prereg_path)
        if existed and meta.get("v026_prereg_sha256") != prereg_hash:
            raise V026Error("Base V0.26 pertenece a otro prerregistro")
        if not existed:
            store.set_meta("v026_prereg_sha256", prereg_hash)
            store.set_meta("v026_variant", VARIANT)
            store.set_meta("v026_arms", prereg["arms"])
            store.set_meta("v026_completion_reason", None)
            store.set_meta("v026_real_money", "BLOQUEADO")
    finally:
        store.close()


def v026_arm_records(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    database = Path(path).resolve()
    connection = _open_read_only(database)
    try:
        meta = {
            str(key): json.loads(str(value))
            for key, value in connection.execute("SELECT key,value FROM shadow_meta")
        }
        arms = validate_arm_config(meta.get("v026_arms"))
        records = {str(arm["id"]): [] for arm in arms}
        for row in connection.execute(
            """
            SELECT f.condition_id,f.feature_json,m.market_start_ms,m.label,
             m.label_verified,s.probability_up
            FROM shadow_features AS f
            JOIN shadow_markets AS m USING(condition_id)
            LEFT JOIN shadow_signals AS s
              ON s.condition_id=f.condition_id
             AND s.model_name='twap_transfer_strike_hgb'
            ORDER BY m.market_start_ms
            """
        ):
            record = market_record(
                feature_json=str(row["feature_json"]),
                model_probability_up=(
                    float(row["probability_up"])
                    if row["probability_up"] is not None
                    else None
                ),
                label=str(row["label"]) if row["label"] is not None else None,
                market_start_ms=int(row["market_start_ms"]),
                condition_id=str(row["condition_id"]),
            )
            if record is None:
                continue
            implied = float(record["implied_up_mid_probability"])
            record["favorite_probability"] = max(implied, 1.0 - implied)
            record["label_verified"] = int(row["label_verified"] or 0)
            for arm_id in eligible_arm_ids(record, arms):
                records[arm_id].append(record)
        return records
    finally:
        connection.close()


def _mark_completion(database: Path, *, reason: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection = sqlite3.connect(database, timeout=60)
    try:
        values = {
            "v026_observation_ended_at": now,
            "v026_completion_reason": reason,
        }
        connection.executemany(
            "INSERT OR REPLACE INTO shadow_meta(key,value) VALUES(?,?)",
            [
                (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))
                for key, value in values.items()
            ],
        )
        connection.commit()
    finally:
        connection.close()


async def run_v026(
    *, settings: Settings, prereg_path: str | Path, output_db: str | Path
) -> dict[str, Any]:
    prereg_file = Path(prereg_path).resolve()
    prereg = load_and_verify_prereg(prereg_file)
    database = Path(output_db).resolve()
    enforce_forward_duration(MAXIMUM_HOURS, database)
    lock = V022ProcessLock(f"{database}.lock")
    lock.acquire()
    try:
        _bind_database(database=database, prereg=prereg, prereg_path=prereg_file)
        model_path = _resolve_project_file(
            prereg["artifacts"]["model"]["relative_path"], label="model"
        )
        result = await run_shadow_forward(
            settings=settings,
            model_file=model_path,
            output_db=database,
            target_hours=MAXIMUM_HOURS,
        )
        if result.get("status") == "COMPLETED":
            _mark_completion(database, reason="FIXED_24H_REACHED")
            result["completion_reason"] = "FIXED_24H_REACHED"
        result.update(
            {
                "variant": VARIANT,
                "maximum_hours": MAXIMUM_HOURS,
                "orders_created": False,
                "paper_orders": 0,
                "wallet_required": False,
                "real_money": "BLOQUEADO",
            }
        )
        return result
    finally:
        lock.release()


def v026_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).resolve()
    empty_counts = {
        "favorite_down_cap_090": 0,
        "favorite_up_cap_090_control": 0,
        "favorite_all_cap_090_control": 0,
    }
    if not database.is_file():
        return {
            "status": "NOT_STARTED",
            "variant": VARIANT,
            "database": str(database),
            "maximum_hours": MAXIMUM_HOURS,
            "captured_arm_counts": empty_counts,
            "resolved_arm_counts": empty_counts,
            "orders_created": False,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
    base = shadow_status(database)
    connection = _open_read_only(database)
    try:
        meta = {
            str(key): json.loads(str(value))
            for key, value in connection.execute("SELECT key,value FROM shadow_meta")
        }
    finally:
        connection.close()
    records = v026_arm_records(database)
    captured = {key: len(value) for key, value in records.items()}
    resolved = {
        key: sum(int(row["label_verified"]) == 1 for row in value)
        for key, value in records.items()
    }
    target_end = _parse_utc(str(meta["target_end_at"])).timestamp()
    completion_reason = meta.get("v026_completion_reason")
    base.update(
        {
            "status": "COMPLETED" if completion_reason else "RUNNING_OR_RESUMABLE",
            "variant": VARIANT,
            "maximum_hours": MAXIMUM_HOURS,
            "completion_reason": completion_reason,
            "remaining_hours": max(0.0, target_end - time.time()) / 3600,
            "captured_arm_counts": captured,
            "resolved_arm_counts": resolved,
            "orders_created": False,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
    )
    return base


__all__ = [
    "MAXIMUM_HOURS",
    "PREREG_SCHEMA",
    "VARIANT",
    "V026Error",
    "load_and_verify_prereg",
    "run_v026",
    "v026_arm_records",
    "v026_status",
]
