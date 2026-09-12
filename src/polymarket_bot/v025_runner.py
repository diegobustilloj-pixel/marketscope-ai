from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib

from polymarket_bot.config import Settings
from polymarket_bot.phase41 import ShadowStore, run_shadow_forward, shadow_status
from polymarket_bot.v018_runner import ROOT, _parse_utc, sha256_file
from polymarket_bot.v022_runner import V022ProcessLock
from polymarket_bot.v024_tournament import market_record
from polymarket_bot.v025_strategy import (
    PRIMARY_ARM_ID,
    eligible_arm_ids,
    validate_arm_config,
)


PREREG_SCHEMA = "prereg_v025_down_asymmetry_1"
TARGET_PRIMARY_TRADES = 10
MAXIMUM_HOURS = 12.0
POLL_SECONDS = 5.0
VARIANT = "V0.25_DOWN_ASYMMETRY_SEQUENTIAL_10_OR_12H"


class V025Error(RuntimeError):
    pass


def _resolve_project_file(relative_path: Any, *, label: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise V025Error(f"Ruta V0.25 ausente: {label}")
    path = (ROOT / relative_path).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise V025Error(f"Ruta V0.25 fuera del proyecto: {label}") from exc
    if not path.is_file():
        raise V025Error(f"Archivo V0.25 ausente: {label}")
    return path


def load_and_verify_prereg(path: str | Path) -> dict[str, Any]:
    prereg_path = Path(path).resolve()
    payload = json.loads(prereg_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != PREREG_SCHEMA:
        raise V025Error("Prerregistro V0.25 incompatible")
    if payload.get("status") != "FROZEN_DOWN_ASYMMETRY_SEQUENTIAL":
        raise V025Error("V0.25 no esta congelado")
    if float(payload.get("maximum_hours", 0)) != MAXIMUM_HOURS:
        raise V025Error("V0.25 debe tener maximo exactamente 12 horas")
    if int(payload.get("target_resolved_primary_trades", 0)) != TARGET_PRIMARY_TRADES:
        raise V025Error("V0.25 debe parar en diez operaciones primarias resueltas")
    validate_arm_config(payload.get("arms"))

    stopping = payload.get("stopping")
    expected_stopping = {
        "primary_arm": PRIMARY_ARM_ID,
        "stop_when_target_resolved": True,
        "evaluation_uses_first_target_primary_trades": True,
        "controls_cut_off_at_tenth_primary_market": True,
        "final_result_only": True,
        "intermediate_scheduled_reports": False,
    }
    if not isinstance(stopping, dict):
        raise V025Error("Contrato de parada V0.25 ausente")
    for key, expected in expected_stopping.items():
        if stopping.get(key) != expected:
            raise V025Error(f"Parada V0.25 incompatible: {key}")

    selection = payload.get("primary_selection")
    expected_selection = {
        "minimum_trades": 10,
        "one_sided_95_z": 1.6448536269514715,
        "requires_positive_net_pnl": True,
        "requires_positive_lcb": True,
        "requires_profit_factor_above_one": True,
        "requires_positive_first_five": True,
        "requires_positive_last_five": True,
        "controls_can_be_selected": False,
    }
    if not isinstance(selection, dict):
        raise V025Error("Seleccion primaria V0.25 ausente")
    for key, expected in expected_selection.items():
        if selection.get(key) != expected:
            raise V025Error(f"Seleccion V0.25 incompatible: {key}")

    safety = payload.get("safety")
    expected_safety = {
        "wallet_required": False,
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "real_money": "BLOQUEADO",
        "active_forward_modified": False,
        "maximum_hours": 12,
    }
    if not isinstance(safety, dict):
        raise V025Error("Safety V0.25 ausente")
    for key, expected in expected_safety.items():
        if safety.get(key) != expected:
            raise V025Error(f"Safety V0.25 incumplido: {key}")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise V025Error("Artefactos V0.25 ausentes")
    for key in ("model", "phase4"):
        item = artifacts.get(key)
        if not isinstance(item, dict):
            raise V025Error(f"Artefacto V0.25 ausente: {key}")
        artifact_path = _resolve_project_file(item.get("relative_path"), label=key)
        if sha256_file(artifact_path) != item.get("sha256"):
            raise V025Error(f"Hash V0.25 no coincide: {key}")

    design = payload.get("design_evidence")
    if not isinstance(design, dict):
        raise V025Error("Evidencia de diseno V0.25 ausente")
    design_path = _resolve_project_file(
        design.get("relative_path"), label="design_evidence"
    )
    if sha256_file(design_path) != design.get("sha256"):
        raise V025Error("Evidencia de diseno V0.25 no coincide")

    hashes = payload.get("code_hashes")
    if not isinstance(hashes, dict):
        raise V025Error("Hashes de codigo V0.25 ausentes")
    expected_hashes = {
        "strategy": sha256_file(ROOT / "src/polymarket_bot/v025_strategy.py"),
        "runner": sha256_file(Path(__file__)),
        "entrypoint": sha256_file(ROOT / "v025_monitor.py"),
        "auditor": sha256_file(ROOT / "src/polymarket_bot/v025_audit.py"),
        "collector_and_model_runtime": sha256_file(
            ROOT / "src/polymarket_bot/phase41.py"
        ),
        "cost_runtime": sha256_file(ROOT / "src/polymarket_bot/phase4.py"),
    }
    for key, actual in expected_hashes.items():
        if hashes.get(key) != actual:
            raise V025Error(f"Hash V0.25 no coincide: {key}")
    return payload


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
        raise V025Error("Modelo V0.25 incompatible o inseguro")
    store = ShadowStore(database)
    store.open(target_hours=MAXIMUM_HOURS, artifact=artifact)
    try:
        meta = store.meta()
        prereg_hash = sha256_file(prereg_path)
        if existed and meta.get("v025_prereg_sha256") != prereg_hash:
            raise V025Error("Base V0.25 pertenece a otro prerregistro")
        if not existed:
            store.set_meta("v025_prereg_sha256", prereg_hash)
            store.set_meta("v025_variant", VARIANT)
            store.set_meta("v025_arms", prereg["arms"])
            store.set_meta("v025_target_resolved_primary_trades", TARGET_PRIMARY_TRADES)
            store.set_meta("v025_completion_reason", None)
            store.set_meta("v025_real_money", "BLOQUEADO")
    finally:
        store.close()


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def v025_arm_records(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    database = Path(path).resolve()
    connection = _open_read_only(database)
    try:
        meta = {
            str(key): json.loads(str(value))
            for key, value in connection.execute("SELECT key,value FROM shadow_meta")
        }
        arms = validate_arm_config(meta.get("v025_arms"))
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
            record["label_verified"] = int(row["label_verified"] or 0)
            for arm_id in eligible_arm_ids(record, arms):
                records[arm_id].append(record)
        return records
    finally:
        connection.close()


def _mark_completion(
    database: Path,
    *,
    reason: str,
    completed_primary_trades: int,
    cutoff_market_start_ms: int | None,
    replace_last_run_status: bool,
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection = sqlite3.connect(database, timeout=60)
    try:
        values = {
            "experiment_completed_at": now,
            "v025_observation_ended_at": now,
            "v025_completion_reason": reason,
            "v025_completed_primary_trades": completed_primary_trades,
            "v025_effective_cutoff_market_start_ms": cutoff_market_start_ms,
        }
        connection.executemany(
            "INSERT OR REPLACE INTO shadow_meta(key,value) VALUES(?,?)",
            [
                (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))
                for key, value in values.items()
            ],
        )
        if replace_last_run_status:
            connection.execute(
                """
                UPDATE shadow_runs
                SET status='COMPLETED',error=NULL
                WHERE run_id=(SELECT MAX(run_id) FROM shadow_runs)
                """
            )
        connection.commit()
    finally:
        connection.close()


def _primary_progress(database: Path) -> tuple[int, int, int | None]:
    records = v025_arm_records(database)[PRIMARY_ARM_ID]
    captured = len(records)
    resolved = [record for record in records if record["label_verified"] == 1]
    cutoff = (
        int(resolved[TARGET_PRIMARY_TRADES - 1]["market_start_ms"])
        if len(resolved) >= TARGET_PRIMARY_TRADES
        else None
    )
    return captured, len(resolved), cutoff


def _cutoff_window_is_fully_resolved(database: Path, cutoff: int) -> bool:
    connection = _open_read_only(database)
    try:
        unresolved = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM shadow_markets
                WHERE market_start_ms<=? AND label_verified<>1
                """,
                (cutoff,),
            ).fetchone()[0]
        )
        return unresolved == 0
    finally:
        connection.close()


async def run_v025(
    *, settings: Settings, prereg_path: str | Path, output_db: str | Path
) -> dict[str, Any]:
    prereg_file = Path(prereg_path).resolve()
    prereg = load_and_verify_prereg(prereg_file)
    database = Path(output_db).resolve()
    lock = V022ProcessLock(f"{database}.lock")
    lock.acquire()
    try:
        _bind_database(database=database, prereg=prereg, prereg_path=prereg_file)
        model_path = _resolve_project_file(
            prereg["artifacts"]["model"]["relative_path"], label="model"
        )
        collector = asyncio.create_task(
            run_shadow_forward(
                settings=settings,
                model_file=model_path,
                output_db=database,
                target_hours=MAXIMUM_HOURS,
            )
        )
        early_completion = False
        while not collector.done():
            await asyncio.sleep(POLL_SECONDS)
            _, resolved, cutoff = _primary_progress(database)
            if (
                resolved >= TARGET_PRIMARY_TRADES
                and cutoff is not None
                and _cutoff_window_is_fully_resolved(database, cutoff)
            ):
                early_completion = True
                collector.cancel()
                try:
                    await collector
                except asyncio.CancelledError:
                    pass
                _mark_completion(
                    database,
                    reason="TARGET_PRIMARY_TRADES_REACHED",
                    completed_primary_trades=resolved,
                    cutoff_market_start_ms=cutoff,
                    replace_last_run_status=True,
                )
                break
        if early_completion:
            result: dict[str, Any] = {
                "status": "COMPLETED",
                "completion_reason": "TARGET_PRIMARY_TRADES_REACHED",
            }
        else:
            result = await collector
            if result.get("status") == "COMPLETED":
                _, resolved, cutoff = _primary_progress(database)
                connection = _open_read_only(database)
                try:
                    meta = {
                        str(key): json.loads(str(value))
                        for key, value in connection.execute(
                            "SELECT key,value FROM shadow_meta"
                        )
                    }
                    maximum_cutoff = int(
                        _parse_utc(str(meta["target_end_at"])).timestamp() * 1000
                    )
                finally:
                    connection.close()
                _mark_completion(
                    database,
                    reason="MAXIMUM_HOURS_REACHED",
                    completed_primary_trades=resolved,
                    cutoff_market_start_ms=cutoff or maximum_cutoff,
                    replace_last_run_status=False,
                )
                result["completion_reason"] = "MAXIMUM_HOURS_REACHED"
        result.update(
            {
                "variant": VARIANT,
                "target_resolved_primary_trades": TARGET_PRIMARY_TRADES,
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


def v025_status(path: str | Path) -> dict[str, Any]:
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
            "target_resolved_primary_trades": TARGET_PRIMARY_TRADES,
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
    records = v025_arm_records(database)
    captured = {key: len(value) for key, value in records.items()}
    resolved = {
        key: sum(int(row["label_verified"]) == 1 for row in value)
        for key, value in records.items()
    }
    target_end = _parse_utc(str(meta["target_end_at"])).timestamp()
    completion_reason = meta.get("v025_completion_reason")
    base.update(
        {
            "status": "COMPLETED" if completion_reason else "RUNNING_OR_RESUMABLE",
            "variant": VARIANT,
            "maximum_hours": MAXIMUM_HOURS,
            "target_resolved_primary_trades": TARGET_PRIMARY_TRADES,
            "completion_reason": completion_reason,
            "remaining_hours_to_maximum": max(0.0, target_end - time.time()) / 3600,
            "captured_arm_counts": captured,
            "resolved_arm_counts": resolved,
            "primary_trades_remaining": max(
                0, TARGET_PRIMARY_TRADES - resolved.get(PRIMARY_ARM_ID, 0)
            ),
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
    "TARGET_PRIMARY_TRADES",
    "VARIANT",
    "V025Error",
    "load_and_verify_prereg",
    "run_v025",
    "v025_arm_records",
    "v025_status",
]
