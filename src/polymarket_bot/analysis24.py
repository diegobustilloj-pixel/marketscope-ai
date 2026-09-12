from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np

from polymarket_bot.phase4 import _probability_metrics
from polymarket_bot.phase41 import SHADOW_HYPOTHESES, _trade_forward_metrics


ROOT = Path(__file__).resolve().parents[2]
PREREG = ROOT / "data" / "prereg_analisis_forward_ultimas24h.json"
SHADOW_DB = ROOT / "data" / "shadow_forward_twap_transfer_v094a.db"
OUTPUT = ROOT / "data" / "resultado_analisis_forward_ultimas24h.json"
SCHEMA = "resultado_analisis_forward_ultimas24h_1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON incompatible: {path}")
    return payload


def verify_prereg(path: Path = PREREG) -> dict[str, Any]:
    payload = _load(path)
    if payload.get("schema") != "prereg_analisis_forward_ultimas24h_1":
        raise RuntimeError("Prerregistro de ultimas 24h incompatible")
    if float(payload.get("window_hours", 0)) != 24.0:
        raise RuntimeError("El holdout debe durar exactamente 24 horas")
    if payload.get("no_outcomes_before_full_completion") is not True:
        raise RuntimeError("El prerregistro debe prohibir outcomes parciales")
    if payload.get("real_money") != "BLOQUEADO":
        raise RuntimeError("Dinero real debe permanecer bloqueado")
    expected_module = payload.get("analysis_module_sha256")
    if expected_module != _sha256(Path(__file__).resolve()):
        raise RuntimeError("El analizador de 24h cambio despues del prerregistro")
    for relative, expected in payload.get("sources", {}).items():
        source = (ROOT / str(relative)).resolve()
        if not source.is_file() or _sha256(source) != str(expected):
            raise RuntimeError(f"Fuente congelada no coincide: {relative}")
    return payload


def _meta(connection: sqlite3.Connection) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, raw in connection.execute("SELECT key,value FROM shadow_meta"):
        try:
            values[str(key)] = json.loads(str(raw))
        except json.JSONDecodeError:
            values[str(key)] = raw
    return values


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RuntimeError("Timestamp sin zona horaria")
    return parsed.astimezone(timezone.utc)


def analyze_last_24h(
    shadow_db: str | Path = SHADOW_DB,
    *,
    prereg_path: Path = PREREG,
) -> dict[str, Any]:
    prereg = verify_prereg(prereg_path)
    database = Path(shadow_db).expanduser().resolve()
    connection = sqlite3.connect(
        f"{database.as_uri()}?mode=ro",
        uri=True,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    try:
        meta = _meta(connection)
        completed_at = meta.get("experiment_completed_at")
        if not completed_at:
            return {
                "schema": SCHEMA,
                "status": "WAITING_FULL_FORWARD_COMPLETION",
                "outcomes_read": 0,
                "real_money": "BLOQUEADO",
            }
        if str(meta.get("target_end_at")) != str(prereg["window_end_at"]):
            raise RuntimeError("Fin del forward no coincide con el holdout congelado")
        end = _parse_utc(str(prereg["window_end_at"]))
        start = _parse_utc(str(prereg["window_start_at"]))
        if end - start != timedelta(hours=24):
            raise RuntimeError("Ventana congelada distinta de 24 horas")
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        counts = connection.execute(
            """
            SELECT COUNT(*) AS markets,
                   SUM(CASE WHEN feature_status='SAVED' THEN 1 ELSE 0 END) AS features,
                   SUM(CASE WHEN label_verified=1 THEN 1 ELSE 0 END) AS resolved
            FROM shadow_markets
            WHERE market_start_ms>=? AND market_start_ms<?
            """,
            (start_ms, end_ms),
        ).fetchone()
        markets = int(counts["markets"] or 0)
        features = int(counts["features"] or 0)
        resolved = int(counts["resolved"] or 0)
        expected_markets = 288
        market_coverage = markets / expected_markets
        feature_coverage = features / markets if markets else 0.0
        resolution_coverage = resolved / markets if markets else 0.0
        hypotheses = list(SHADOW_HYPOTHESES)
        confidence_z = NormalDist().inv_cdf(1.0 - 0.05 / max(1, len(hypotheses)))
        model_results: list[dict[str, Any]] = []
        for hypothesis in hypotheses:
            model_name = str(hypothesis["model_name"])
            rows = connection.execute(
                """
                SELECT s.*,m.label,m.market_start_ms
                FROM shadow_signals AS s
                JOIN shadow_markets AS m ON m.condition_id=s.condition_id
                WHERE s.model_name=? AND m.label_verified=1
                  AND m.market_start_ms>=? AND m.market_start_ms<?
                ORDER BY m.market_start_ms
                """,
                (model_name, start_ms, end_ms),
            ).fetchall()
            if not rows:
                model_results.append(
                    {
                        "model_name": model_name,
                        "resolved_predictions": 0,
                        "passes_24h_diagnostic": False,
                        "failures": ["NO_RESOLVED_PREDICTIONS"],
                    }
                )
                continue
            targets = np.asarray(
                [1 if str(row["label"]) == "Up" else 0 for row in rows],
                dtype=int,
            )
            probabilities = np.asarray(
                [float(row["probability_up"]) for row in rows], dtype=float
            )
            probability = _probability_metrics(targets, probabilities)
            benchmark_rows = connection.execute(
                """
                SELECT b.probability_up,m.label
                FROM shadow_signals AS b
                JOIN shadow_signals AS selected
                  ON selected.condition_id=b.condition_id
                JOIN shadow_markets AS m ON m.condition_id=b.condition_id
                WHERE b.model_name='market_implied'
                  AND selected.model_name=? AND m.label_verified=1
                  AND m.market_start_ms>=? AND m.market_start_ms<?
                ORDER BY m.market_start_ms
                """,
                (model_name, start_ms, end_ms),
            ).fetchall()
            benchmark_targets = np.asarray(
                [1 if str(row["label"]) == "Up" else 0 for row in benchmark_rows],
                dtype=int,
            )
            benchmark_probabilities = np.asarray(
                [float(row["probability_up"]) for row in benchmark_rows],
                dtype=float,
            )
            benchmark = _probability_metrics(
                benchmark_targets, benchmark_probabilities
            )
            trading = _trade_forward_metrics(rows, confidence_z=confidence_z)
            failures: list[str] = []
            if int(trading["trades"]) < int(prereg["gates"]["minimum_trades"]):
                failures.append("INSUFFICIENT_FREQUENCY_24H")
            lower = trading["lower_confidence_bound_mean_pnl"]
            if lower is None or float(lower) <= 0:
                failures.append("LCB_MEAN_PNL_NOT_POSITIVE")
            if (
                float(probability["brier"])
                > float(benchmark["brier"])
                + float(prereg["gates"]["maximum_brier_degradation"])
            ):
                failures.append("BRIER_WORSE_THAN_MARKET")
            model_results.append(
                {
                    "model_name": model_name,
                    "resolved_predictions": len(rows),
                    "probability": probability,
                    "market_implied_probability": benchmark,
                    "trading": trading,
                    "passes_24h_diagnostic": not failures,
                    "failures": failures,
                }
            )
    finally:
        connection.close()
    technical_passed = (
        quick_check == "ok"
        and market_coverage >= float(prereg["gates"]["minimum_market_coverage"])
        and feature_coverage >= float(prereg["gates"]["minimum_feature_coverage"])
        and resolution_coverage >= float(prereg["gates"]["minimum_resolution_coverage"])
    )
    return {
        "schema": SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "ANALYZED_AFTER_FULL_COMPLETION",
        "window_start_at": prereg["window_start_at"],
        "window_end_at": prereg["window_end_at"],
        "window_hours": 24.0,
        "technical_passed": technical_passed,
        "expected_markets": expected_markets,
        "markets": markets,
        "market_coverage": market_coverage,
        "features": features,
        "feature_coverage": feature_coverage,
        "resolved": resolved,
        "resolution_coverage": resolution_coverage,
        "model_results": model_results,
        "diagnostic_candidate": technical_passed
        and any(item.get("passes_24h_diagnostic") for item in model_results),
        "official_7d_result_remains_primary": True,
        "independence_note": (
            "Ventana temporal futura al prerregistro, pero es un subconjunto del "
            "forward oficial de siete dias; no habilita dinero real."
        ),
        "outcomes_read": resolved,
        "outcomes_read_only_after_experiment_completed_at": True,
        "orders_enabled": False,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


def write_once(payload: dict[str, Any], path: Path = OUTPUT) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


__all__ = ["OUTPUT", "analyze_last_24h", "verify_prereg", "write_once"]
