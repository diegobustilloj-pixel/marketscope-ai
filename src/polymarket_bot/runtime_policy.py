from __future__ import annotations

import json
import math
import _thread
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
POLICY_FILE = ROOT / "data" / "policy_experiment_duration_24h.json"
POLICY_SCHEMA = "policy_experiment_duration_24h_1"


class BacktestRuntimeExceeded(TimeoutError):
    pass


def load_duration_policy(path: str | Path = POLICY_FILE) -> dict[str, Any]:
    policy_path = Path(path).expanduser().resolve()
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != POLICY_SCHEMA:
        raise RuntimeError("Politica de duracion incompatible")
    maximum = float(payload.get("maximum_new_experiment_hours", 0))
    runtime = float(payload.get("maximum_backtest_runtime_hours", 0))
    if maximum != 24.0 or runtime != 24.0:
        raise RuntimeError("La politica debe limitar experimentos y backtests a 24 horas")
    if payload.get("real_money") != "BLOQUEADO":
        raise RuntimeError("La politica debe mantener dinero real bloqueado")
    if not isinstance(payload.get("grandfathered_experiments"), list):
        raise RuntimeError("Politica sin lista de excepciones congeladas")
    return payload


def _decoded_meta(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(
        f"{database.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=3,
    )
    try:
        rows = connection.execute("SELECT key,value FROM shadow_meta").fetchall()
    finally:
        connection.close()
    decoded: dict[str, Any] = {}
    for key, value in rows:
        try:
            decoded[str(key)] = json.loads(str(value))
        except json.JSONDecodeError:
            decoded[str(key)] = value
    return decoded


def enforce_backtest_runtime_hours(
    requested_hours: float,
    *,
    policy_path: str | Path = POLICY_FILE,
) -> float:
    policy = load_duration_policy(policy_path)
    value = float(requested_hours)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("La duracion debe ser positiva y finita")
    maximum = float(policy["maximum_backtest_runtime_hours"])
    if value > maximum:
        raise ValueError(f"Backtest rechazado: maximo permitido {maximum:g} horas")
    return value


@contextmanager
def backtest_runtime_guard(
    requested_hours: float,
    *,
    policy_path: str | Path = POLICY_FILE,
):
    hours = enforce_backtest_runtime_hours(
        requested_hours,
        policy_path=policy_path,
    )
    expired = threading.Event()

    def interrupt() -> None:
        expired.set()
        _thread.interrupt_main()

    timer = threading.Timer(hours * 3600.0, interrupt)
    timer.daemon = True
    timer.start()
    try:
        yield hours
    except KeyboardInterrupt as exc:
        if expired.is_set():
            raise BacktestRuntimeExceeded(
                f"Backtest detenido al alcanzar el limite de {hours:g} horas"
            ) from exc
        raise
    finally:
        timer.cancel()


def enforce_forward_duration(
    requested_hours: float,
    output_db: str | Path,
    *,
    policy_path: str | Path = POLICY_FILE,
    root: str | Path = ROOT,
) -> dict[str, Any]:
    policy = load_duration_policy(policy_path)
    value = float(requested_hours)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("--hours debe ser positivo y finito")
    maximum = float(policy["maximum_new_experiment_hours"])
    if value <= maximum:
        return {
            "status": "WITHIN_24H_POLICY",
            "requested_hours": value,
            "maximum_hours": maximum,
        }

    project_root = Path(root).expanduser().resolve()
    database = Path(output_db).expanduser()
    if not database.is_absolute():
        database = project_root / database
    database = database.resolve()
    for exception in policy["grandfathered_experiments"]:
        if exception.get("kind") != "forward_shadow":
            continue
        expected_path = (
            project_root / str(exception["relative_output_db"])
        ).resolve()
        if database != expected_path or not database.is_file():
            continue
        meta = _decoded_meta(database)
        matches = (
            float(meta.get("target_hours", -1)) == float(exception["target_hours"])
            and meta.get("experiment_started_at") == exception["experiment_started_at"]
            and meta.get("target_end_at") == exception["target_end_at"]
            and meta.get("orders_enabled") is False
            and meta.get("money_real_enabled") is False
            and meta.get("wallet_required") is False
        )
        if matches and value == float(exception["target_hours"]):
            return {
                "status": "GRANDFATHERED_EXISTING_FORWARD",
                "requested_hours": value,
                "maximum_new_hours": maximum,
                "output_db": str(database),
                "target_end_at": meta["target_end_at"],
            }
        raise ValueError("La base excepcional no coincide con sus metadatos congelados")
    raise ValueError(
        f"Experimento nuevo rechazado: maximo {maximum:g} horas; "
        "la unica excepcion es reanudar el forward de siete dias ya iniciado"
    )


__all__ = [
    "BacktestRuntimeExceeded",
    "POLICY_FILE",
    "backtest_runtime_guard",
    "enforce_backtest_runtime_hours",
    "enforce_forward_duration",
    "load_duration_policy",
]
