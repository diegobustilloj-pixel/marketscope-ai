from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.config import Settings

from .backtest import wilson_interval
from .research import ROOT, sha256_file, utc_now
from .v002_shadow import (
    DATABASE_PATH as BASE_DATABASE_PATH,
    PREREG_PATH as BASE_PREREG_PATH,
    ProcessLock,
    ShadowError,
    _run_shadow,
    load_and_verify_prereg as load_and_verify_base_prereg,
    run_smoke,
    status as shadow_status,
)


PREREG_PATH = ROOT / "data" / "btc5m90_v002_confirmatory" / "prereg.json"
DATABASE_PATH = ROOT / "data" / "btc5m90_v002_confirmatory" / "shadow.db"
PREREG_SCHEMA = "btc5m90_v002_confirmatory_prereg_1"
TARGET_COMPLETE_MARKETS = 276
MAXIMUM_HOURS = 24.0
REFERENCE_MARKETS = 96
REFERENCE_SIGNALS = 33


class ConfirmatoryError(ShadowError):
    pass


def _base_capture_summary() -> dict[str, Any]:
    summary = shadow_status(BASE_DATABASE_PATH)
    if summary.get("status") != "COMPLETED":
        raise ConfirmatoryError("La cohorte base V002 no está completa")
    if int(summary.get("markets_complete", 0)) != REFERENCE_MARKETS:
        raise ConfirmatoryError("La cohorte base no conserva sus 96 mercados")
    if int(summary.get("approved_signals", 0)) != REFERENCE_SIGNALS:
        raise ConfirmatoryError("La cohorte base no conserva sus 33 señales")
    if any(
        int(summary.get(field, 0)) != 0
        for field in ("paper_orders", "orders_sent", "outcomes_read", "real_money_rows")
    ):
        raise ConfirmatoryError("La cohorte base incumple el contrato paper-only")
    if summary.get("sqlite_quick_check") != "ok":
        raise ConfirmatoryError("La base V002 no supera quick_check")
    return summary


def _last_base_market_end_ms() -> int:
    path = Path(BASE_DATABASE_PATH).resolve()
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        row = connection.execute("SELECT MAX(market_end_ms) FROM shadow_markets").fetchone()
    finally:
        connection.close()
    if row is None or row[0] is None:
        raise ConfirmatoryError("No se encontró el final de la cohorte base")
    return int(row[0])


def _code_paths() -> tuple[Path, ...]:
    return (
        ROOT / "src" / "polymarket_bot" / "btc_5m_90" / "v002.py",
        ROOT / "src" / "polymarket_bot" / "btc_5m_90" / "v002_shadow.py",
        Path(__file__).resolve(),
        ROOT / "btc5m90_v002_confirmatory.py",
    )


def build_prereg() -> dict[str, Any]:
    base_prereg = load_and_verify_base_prereg(BASE_PREREG_PATH)
    base_summary = _base_capture_summary()
    signal_frequency_low, signal_frequency_high = wilson_interval(
        REFERENCE_SIGNALS, REFERENCE_MARKETS
    )
    assert signal_frequency_low is not None and signal_frequency_high is not None
    minimum_signals = int(signal_frequency_low * TARGET_COMPLETE_MARKETS)
    return {
        "schema": PREREG_SCHEMA,
        "status": "FROZEN_CONFIRMATORY_SHADOW_ONLY",
        "created_at": utc_now(),
        "target_complete_markets": TARGET_COMPLETE_MARKETS,
        "expected_capture_hours": TARGET_COMPLETE_MARKETS * 5.0 / 60.0,
        "maximum_hours": MAXIMUM_HOURS,
        "network_recovery_reserve_hours": (
            MAXIMUM_HOURS - TARGET_COMPLETE_MARKETS * 5.0 / 60.0
        ),
        "not_before_market_end_ms": _last_base_market_end_ms(),
        "strategy": base_prereg["strategy"],
        "execution": base_prereg["execution"],
        "gates": {
            "minimum_complete_markets": TARGET_COMPLETE_MARKETS,
            "minimum_signals": minimum_signals,
            "minimum_execution_rate": 0.80,
            "minimum_realized_edge_per_share": 0.002,
            "positive_net_pnl": True,
            "pooled_wilson_95_low_above_pooled_break_even": True,
        },
        "reference_only": {
            "markets": int(base_summary["markets_complete"]),
            "signals": int(base_summary["approved_signals"]),
            "signal_frequency": REFERENCE_SIGNALS / REFERENCE_MARKETS,
            "signal_frequency_wilson_95_low": signal_frequency_low,
            "signal_frequency_wilson_95_high": signal_frequency_high,
            "minimum_signals_is_floor_of_lower_bound_times_target": True,
        },
        "evaluation": {
            "labels_blinded_during_capture": True,
            "evaluate_confirmatory_cohort_separately": True,
            "also_report_pooled_base_plus_confirmatory": True,
            "price_and_side_slices_are_diagnostic_only": True,
            "no_posthoc_rule_change": True,
            "no_automatic_live_promotion": True,
        },
        "safety": {
            "wallet_required": False,
            "orders_enabled": False,
            "paper_orders": False,
            "outcomes_during_capture": False,
            "real_money": "BLOQUEADO",
        },
        "sources": {
            "base_prereg_sha256": sha256_file(BASE_PREREG_PATH),
            "base_database_sha256": sha256_file(BASE_DATABASE_PATH),
        },
        "code_sha256": {
            str(path.relative_to(ROOT)).replace("\\", "/"): sha256_file(path)
            for path in _code_paths()
        },
    }


def freeze_prereg(path: str | Path = PREREG_PATH) -> dict[str, Any]:
    target = Path(path).resolve()
    if target.exists():
        raise ConfirmatoryError("El prerregistro confirmatorio ya existe")
    payload = build_prereg()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    return payload


def load_and_verify_prereg(path: str | Path = PREREG_PATH) -> dict[str, Any]:
    target = Path(path).resolve()
    payload = json.loads(target.read_text(encoding="utf-8"))
    if (
        payload.get("schema") != PREREG_SCHEMA
        or payload.get("status") != "FROZEN_CONFIRMATORY_SHADOW_ONLY"
    ):
        raise ConfirmatoryError("Prerregistro confirmatorio incompatible")
    if int(payload.get("target_complete_markets", 0)) != TARGET_COMPLETE_MARKETS:
        raise ConfirmatoryError("Cambió el objetivo confirmatorio")
    if float(payload.get("maximum_hours", 0.0)) != MAXIMUM_HOURS:
        raise ConfirmatoryError("Cambió el límite de 24 horas")
    base_prereg = load_and_verify_base_prereg(BASE_PREREG_PATH)
    if payload.get("strategy") != base_prereg.get("strategy"):
        raise ConfirmatoryError("La estrategia ya no coincide exactamente con V002")
    if payload.get("execution") != base_prereg.get("execution"):
        raise ConfirmatoryError("La ejecución ya no coincide exactamente con V002")
    expected_safety = {
        "wallet_required": False,
        "orders_enabled": False,
        "paper_orders": False,
        "outcomes_during_capture": False,
        "real_money": "BLOQUEADO",
    }
    if payload.get("safety") != expected_safety:
        raise ConfirmatoryError("Cambió el contrato de seguridad")
    expected_sources = {
        "base_prereg_sha256": sha256_file(BASE_PREREG_PATH),
        "base_database_sha256": sha256_file(BASE_DATABASE_PATH),
    }
    if payload.get("sources") != expected_sources:
        raise ConfirmatoryError("Cambió una fuente congelada V002")
    expected_files = {
        "src/polymarket_bot/btc_5m_90/v002.py",
        "src/polymarket_bot/btc_5m_90/v002_shadow.py",
        "src/polymarket_bot/btc_5m_90/v002_confirmatory.py",
        "btc5m90_v002_confirmatory.py",
    }
    if set(payload.get("code_sha256", {})) != expected_files:
        raise ConfirmatoryError("Inventario confirmatorio incompleto")
    for relative, expected in payload["code_sha256"].items():
        if sha256_file(ROOT / relative) != expected:
            raise ConfirmatoryError(f"Código confirmatorio cambió: {relative}")
    return payload


async def run_confirmatory(
    settings: Settings,
    prereg_path: str | Path = PREREG_PATH,
    database: str | Path = DATABASE_PATH,
) -> dict[str, Any]:
    resolved_prereg = Path(prereg_path).resolve()
    payload = load_and_verify_prereg(resolved_prereg)
    lock = ProcessLock(f"{Path(database).resolve()}.lock")
    lock.acquire()
    try:
        return await _run_shadow(
            settings,
            payload,
            resolved_prereg,
            Path(database).resolve(),
        )
    finally:
        lock.release()


def status(database: str | Path = DATABASE_PATH) -> dict[str, Any]:
    result = shadow_status(database)
    result["cohort"] = "BTC5M90_V002_CONFIRMATORY_24H"
    return result


__all__ = [
    "DATABASE_PATH",
    "PREREG_PATH",
    "build_prereg",
    "freeze_prereg",
    "load_and_verify_prereg",
    "run_confirmatory",
    "run_smoke",
    "status",
]
