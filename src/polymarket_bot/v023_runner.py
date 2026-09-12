from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import joblib

from polymarket_bot.config import Settings
from polymarket_bot.phase41 import ShadowStore, run_shadow_forward, shadow_status
from polymarket_bot.v018_runner import ROOT, _parse_utc, sha256_file
from polymarket_bot.v022_runner import V022ProcessLock


PREREG_SCHEMA = "prereg_v023_filtered_forward_4h_1"
TARGET_HOURS = 4.0
MAXIMUM_HOURS = 4.0
VARIANT = "V0.23_FILTERED_FORWARD_4H"


class V023Error(RuntimeError):
    pass


def _resolve_project_file(relative_path: Any, *, label: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise V023Error(f"Ruta V0.23 ausente: {label}")
    path = (ROOT / relative_path).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise V023Error(f"Ruta V0.23 fuera del proyecto: {label}") from exc
    if not path.is_file():
        raise V023Error(f"Archivo V0.23 ausente: {label}")
    return path


def load_and_verify_prereg(path: str | Path) -> dict[str, Any]:
    prereg_path = Path(path).resolve()
    payload = json.loads(prereg_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != PREREG_SCHEMA:
        raise V023Error("Prerregistro V0.23 incompatible")
    if payload.get("status") != "FROZEN_FILTERED_DIRECTIONAL_FORWARD_4H":
        raise V023Error("V0.23 no esta congelado como forward filtrado")
    hours = float(payload.get("target_hours", 0))
    if hours != TARGET_HOURS or hours > MAXIMUM_HOURS:
        raise V023Error("V0.23 debe durar exactamente 4 horas")

    strategy = payload.get("strategy")
    expected_strategy = {
        "model_name": "twap_transfer_strike_hgb",
        "minimum_expected_edge_inclusive": 0.10,
        "maximum_expected_edge_exclusive": 0.15,
        "minimum_entry_cost_exclusive": 0.50,
        "paper_shares_per_signal": 5.0,
    }
    if not isinstance(strategy, dict):
        raise V023Error("Estrategia V0.23 ausente")
    for key, value in expected_strategy.items():
        if strategy.get(key) != value:
            raise V023Error(f"Estrategia V0.23 incompatible: {key}")

    expected_safety = {
        "wallet_required": False,
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "real_money": "BLOQUEADO",
        "active_forward_modified": False,
        "maximum_hours": 4,
    }
    safety = payload.get("safety")
    if not isinstance(safety, dict):
        raise V023Error("Safety V0.23 ausente")
    for key, value in expected_safety.items():
        if safety.get(key) != value:
            raise V023Error(f"Safety V0.23 incumplido: {key}")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise V023Error("Artefactos V0.23 ausentes")
    for key in ("model", "phase4"):
        item = artifacts.get(key)
        if not isinstance(item, dict):
            raise V023Error(f"Artefacto V0.23 ausente: {key}")
        artifact_path = _resolve_project_file(item.get("relative_path"), label=key)
        if sha256_file(artifact_path) != item.get("sha256"):
            raise V023Error(f"Hash V0.23 no coincide: {key}")

    design = payload.get("design_evidence")
    if not isinstance(design, dict):
        raise V023Error("Evidencia de diseno V0.23 ausente")
    evidence_path = _resolve_project_file(
        design.get("relative_path"), label="design_evidence"
    )
    if sha256_file(evidence_path) != design.get("sha256"):
        raise V023Error("Evidencia de diseno V0.23 no coincide")

    hashes = payload.get("code_hashes")
    if not isinstance(hashes, dict):
        raise V023Error("Hashes de codigo V0.23 ausentes")
    expected_hashes = {
        "runner": sha256_file(Path(__file__)),
        "entrypoint": sha256_file(ROOT / "v023_monitor.py"),
        "auditor": sha256_file(ROOT / "src/polymarket_bot/v023_audit.py"),
        "collector_and_model_runtime": sha256_file(
            ROOT / "src/polymarket_bot/phase41.py"
        ),
    }
    for key, actual in expected_hashes.items():
        if hashes.get(key) != actual:
            raise V023Error(f"Hash V0.23 no coincide: {key}")
    return payload


def _bind_database(
    *, database: Path, prereg: dict[str, Any], prereg_path: Path
) -> None:
    existed = database.is_file()
    model_item = prereg["artifacts"]["model"]
    model_path = _resolve_project_file(model_item["relative_path"], label="model")
    artifact = joblib.load(model_path)
    if (
        not isinstance(artifact, dict)
        or artifact.get("artifact_type") != "polymarket_shadow_forward_models"
        or artifact.get("orders_enabled") is not False
    ):
        raise V023Error("Modelo V0.23 incompatible o inseguro")
    store = ShadowStore(database)
    store.open(target_hours=TARGET_HOURS, artifact=artifact)
    try:
        meta = store.meta()
        expected_prereg_hash = sha256_file(prereg_path)
        bound_hash = meta.get("v023_prereg_sha256")
        if existed and bound_hash != expected_prereg_hash:
            raise V023Error("Base V0.23 pertenece a otro prerregistro")
        if not existed:
            store.set_meta("v023_prereg_sha256", expected_prereg_hash)
            store.set_meta("v023_variant", VARIANT)
            store.set_meta("v023_strategy", prereg["strategy"])
            store.set_meta("v023_real_money", "BLOQUEADO")
    finally:
        store.close()


async def run_v023(
    *, settings: Settings, prereg_path: str | Path, output_db: str | Path
) -> dict[str, Any]:
    prereg_file = Path(prereg_path).resolve()
    prereg = load_and_verify_prereg(prereg_file)
    database = Path(output_db).resolve()
    lock = V022ProcessLock(f"{database}.lock")
    lock.acquire()
    try:
        _bind_database(
            database=database, prereg=prereg, prereg_path=prereg_file
        )
        model_path = _resolve_project_file(
            prereg["artifacts"]["model"]["relative_path"], label="model"
        )
        result = await run_shadow_forward(
            settings=settings,
            model_file=model_path,
            output_db=database,
            target_hours=TARGET_HOURS,
        )
        result.update(
            {
                "variant": VARIANT,
                "filtered_strategy": prereg["strategy"],
                "orders_created": False,
                "paper_orders": 0,
                "wallet_required": False,
                "real_money": "BLOQUEADO",
            }
        )
        return result
    finally:
        lock.release()


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def v023_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).resolve()
    if not database.is_file():
        return {
            "status": "NOT_STARTED",
            "variant": VARIANT,
            "database": str(database),
            "target_hours": TARGET_HOURS,
            "expected_markets": 48,
            "orders_created": False,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
    result = shadow_status(database)
    connection = _open_read_only(database)
    try:
        meta = {
            str(key): json.loads(str(value))
            for key, value in connection.execute("SELECT key,value FROM shadow_meta")
        }
        row = connection.execute(
            """
            SELECT COUNT(*) AS filtered_signals,
             COALESCE(SUM(m.label_verified=1),0) AS resolved_filtered_signals
            FROM shadow_signals AS s
            JOIN shadow_markets AS m USING(condition_id)
            WHERE s.model_name='twap_transfer_strike_hgb'
              AND s.would_trade=1
              AND s.entry_cost>0.50
              AND s.expected_edge>=0.10
              AND s.expected_edge<0.15
            """
        ).fetchone()
    finally:
        connection.close()
    target_end = _parse_utc(str(meta["target_end_at"])).timestamp()
    result.update(
        {
            "status": (
                "COMPLETED"
                if meta.get("experiment_completed_at")
                else "RUNNING_OR_RESUMABLE"
            ),
            "variant": VARIANT,
            "target_hours": TARGET_HOURS,
            "expected_markets": 48,
            "remaining_hours": max(0.0, target_end - time.time()) / 3600.0,
            "filtered_signals": int(row["filtered_signals"] or 0),
            "resolved_filtered_signals": int(
                row["resolved_filtered_signals"] or 0
            ),
            "orders_created": False,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
    )
    return result


__all__ = [
    "MAXIMUM_HOURS",
    "PREREG_SCHEMA",
    "TARGET_HOURS",
    "VARIANT",
    "V023Error",
    "load_and_verify_prereg",
    "run_v023",
    "v023_status",
]
