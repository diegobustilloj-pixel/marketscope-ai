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
from polymarket_bot.v024_tournament import (
    eligible_strategy_ids,
    market_record,
    validate_strategy_config,
)


PREREG_SCHEMA = "prereg_v024_parallel_tournament_4h_1"
TARGET_HOURS = 4.0
MAXIMUM_HOURS = 4.0
VARIANT = "V0.24_PARALLEL_TOURNAMENT_4H"


class V024Error(RuntimeError):
    pass


def _resolve_project_file(relative_path: Any, *, label: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise V024Error(f"Ruta V0.24 ausente: {label}")
    path = (ROOT / relative_path).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise V024Error(f"Ruta V0.24 fuera del proyecto: {label}") from exc
    if not path.is_file():
        raise V024Error(f"Archivo V0.24 ausente: {label}")
    return path


def load_and_verify_prereg(path: str | Path) -> dict[str, Any]:
    prereg_path = Path(path).resolve()
    payload = json.loads(prereg_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != PREREG_SCHEMA:
        raise V024Error("Prerregistro V0.24 incompatible")
    if payload.get("status") != "FROZEN_PARALLEL_TOURNAMENT_4H":
        raise V024Error("V0.24 no esta congelado como torneo paralelo")
    hours = float(payload.get("target_hours", 0))
    if hours != TARGET_HOURS or hours > MAXIMUM_HOURS:
        raise V024Error("V0.24 debe durar exactamente 4 horas")
    validate_strategy_config(payload.get("strategies"))

    selection = payload.get("selection")
    expected_selection = {
        "family_size": 3,
        "family_alpha": 0.05,
        "bonferroni_one_sided_z": 2.128045234184984,
        "minimum_trades_per_strategy": 10,
        "requires_positive_first_half": True,
        "requires_positive_second_half": True,
    }
    if not isinstance(selection, dict):
        raise V024Error("Seleccion V0.24 ausente")
    for key, value in expected_selection.items():
        if selection.get(key) != value:
            raise V024Error(f"Seleccion V0.24 incompatible: {key}")

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
        raise V024Error("Safety V0.24 ausente")
    for key, value in expected_safety.items():
        if safety.get(key) != value:
            raise V024Error(f"Safety V0.24 incumplido: {key}")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise V024Error("Artefactos V0.24 ausentes")
    for key in ("model", "phase4"):
        item = artifacts.get(key)
        if not isinstance(item, dict):
            raise V024Error(f"Artefacto V0.24 ausente: {key}")
        artifact_path = _resolve_project_file(item.get("relative_path"), label=key)
        if sha256_file(artifact_path) != item.get("sha256"):
            raise V024Error(f"Hash V0.24 no coincide: {key}")

    design = payload.get("design_evidence")
    if not isinstance(design, dict):
        raise V024Error("Evidencia de diseno V0.24 ausente")
    evidence_path = _resolve_project_file(
        design.get("relative_path"), label="design_evidence"
    )
    if sha256_file(evidence_path) != design.get("sha256"):
        raise V024Error("Evidencia de diseno V0.24 no coincide")

    hashes = payload.get("code_hashes")
    if not isinstance(hashes, dict):
        raise V024Error("Hashes de codigo V0.24 ausentes")
    expected_hashes = {
        "engine": sha256_file(ROOT / "src/polymarket_bot/v024_tournament.py"),
        "runner": sha256_file(Path(__file__)),
        "entrypoint": sha256_file(ROOT / "v024_monitor.py"),
        "auditor": sha256_file(ROOT / "src/polymarket_bot/v024_audit.py"),
        "collector_and_model_runtime": sha256_file(
            ROOT / "src/polymarket_bot/phase41.py"
        ),
        "cost_runtime": sha256_file(ROOT / "src/polymarket_bot/phase4.py"),
    }
    for key, actual in expected_hashes.items():
        if hashes.get(key) != actual:
            raise V024Error(f"Hash V0.24 no coincide: {key}")
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
        raise V024Error("Modelo V0.24 incompatible o inseguro")
    store = ShadowStore(database)
    store.open(target_hours=TARGET_HOURS, artifact=artifact)
    try:
        meta = store.meta()
        expected_hash = sha256_file(prereg_path)
        bound_hash = meta.get("v024_prereg_sha256")
        if existed and bound_hash != expected_hash:
            raise V024Error("Base V0.24 pertenece a otro prerregistro")
        if not existed:
            store.set_meta("v024_prereg_sha256", expected_hash)
            store.set_meta("v024_variant", VARIANT)
            store.set_meta("v024_strategies", prereg["strategies"])
            store.set_meta("v024_selection", prereg["selection"])
            store.set_meta("v024_real_money", "BLOQUEADO")
    finally:
        store.close()


async def run_v024(
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
                "parallel_strategies": [item["id"] for item in prereg["strategies"]],
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


def v024_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).resolve()
    if not database.is_file():
        return {
            "status": "NOT_STARTED",
            "variant": VARIANT,
            "database": str(database),
            "target_hours": TARGET_HOURS,
            "expected_markets": 48,
            "strategy_signal_counts": {key: 0 for key in (
                "agreement_cap_090", "favorite_band_060_080", "favorite_cap_090"
            )},
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
        strategies = validate_strategy_config(meta.get("v024_strategies"))
        counts = {str(item["id"]): 0 for item in strategies}
        for row in connection.execute(
            """
            SELECT f.condition_id,f.feature_json,s.probability_up
            FROM shadow_features AS f
            LEFT JOIN shadow_signals AS s
              ON s.condition_id=f.condition_id
             AND s.model_name='twap_transfer_strike_hgb'
            """
        ):
            record = market_record(
                feature_json=str(row["feature_json"]),
                model_probability_up=(
                    float(row["probability_up"])
                    if row["probability_up"] is not None
                    else None
                ),
                condition_id=str(row["condition_id"]),
            )
            if record is None:
                continue
            for strategy_id in eligible_strategy_ids(record, strategies):
                counts[strategy_id] += 1
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
            "strategy_signal_counts": counts,
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
    "V024Error",
    "load_and_verify_prereg",
    "run_v024",
    "v024_status",
]
