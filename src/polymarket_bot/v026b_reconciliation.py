from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from polymarket_bot.v018_runner import sha256_file


RECONCILIATION_SCHEMA = "reconciliation_v026b_terminal_audit_1"
ORIGINAL_RESULT_SCHEMA = "result_v026b_adaptive_checkpoints_1"
REQUIRED_FEEDS = ("shadow-rtds", "shadow-binance")


def terminal_feed_shutdown_is_expected(
    *,
    completion_reason: str | None,
    run_rows: Sequence[Mapping[str, Any]],
    final_connections: Mapping[str, Any],
    final_non_feed_gates: Mapping[str, Any],
    checkpoints: Sequence[Mapping[str, Any]],
) -> bool:
    if completion_reason != "FULL_24H_REACHED":
        return False
    if not run_rows or str(run_rows[-1].get("status")) != "COMPLETED":
        return False
    if not all(bool(value) for value in final_non_feed_gates.values()):
        return False
    if not all(final_connections.get(feed) == "CANCELLED" for feed in REQUIRED_FEEDS):
        return False
    if not checkpoints:
        return False
    latest = max(checkpoints, key=lambda item: float(item["checkpoint_hour"]))
    technical = latest.get("technical")
    if not isinstance(technical, Mapping) or technical.get("passed") is not True:
        return False
    checkpoint_connections = technical.get("latest_connections")
    if not isinstance(checkpoint_connections, Mapping):
        return False
    return all(
        checkpoint_connections.get(feed) == "CONNECTED" for feed in REQUIRED_FEEDS
    )


def reconciled_verdict(result: Mapping[str, Any], *, technical_passed: bool) -> str:
    if result.get("safety_passed") is not True:
        return "FAIL_SAFETY"
    if not technical_passed:
        return "FAIL_TECHNICAL_QUALITY"
    if result.get("frequency_passed") is not True:
        return "FAIL_INSUFFICIENT_FREQUENCY"
    if result.get("economic_replication_passed") is not True:
        return "FAIL_REPLICATION"
    if result.get("full_statistical_passed") is True:
        return "PASS_STATISTICAL_PAPER_CANDIDATE"
    return "CONTINUE_PAPER_ACCUMULATION"


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def reconcile_v026b_terminal_audit(
    *,
    result_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    original_path = Path(result_path).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    result = json.loads(original_path.read_text(encoding="utf-8"))
    if result.get("schema") != ORIGINAL_RESULT_SCHEMA:
        raise RuntimeError("Resultado V0.26b incompatible")
    database = Path(str(result["database"])).resolve()
    if not database.is_file():
        raise RuntimeError("Base V0.26b ausente")
    if sha256_file(database) != result.get("database_sha256"):
        raise RuntimeError("La base V0.26b no coincide con el resultado original")
    original_hash = sha256_file(original_path)
    database_hash = sha256_file(database)
    if output is not None and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == RECONCILIATION_SCHEMA
            and existing.get("original_result_sha256") == original_hash
            and existing.get("database_sha256") == database_hash
        ):
            return existing
        raise RuntimeError("Existe una reconciliacion para otra evidencia")

    technical = result["technical_snapshot"]
    gates = dict(technical["gates"])
    feed_gate_names = {
        "rtds_connected_passed",
        "binance_connected_passed",
    }
    non_feed_gates = {
        key: value for key, value in gates.items() if key not in feed_gate_names
    }
    run_rows = result["technical_audit"]["runs"]
    expected_shutdown = terminal_feed_shutdown_is_expected(
        completion_reason=result["window"].get("completion_reason"),
        run_rows=run_rows,
        final_connections=technical["latest_connections"],
        final_non_feed_gates=non_feed_gates,
        checkpoints=result["checkpoints"],
    )
    corrected_technical_passed = bool(
        expected_shutdown
        and result["technical_audit"]["database_opened_query_only"] is True
        and result["technical_audit"]["sqlite_quick_check"] == "ok"
        and result["coverage"]["market_coverage"] >= 0.90
        and result["coverage"]["feature_coverage"] >= 0.90
        and result["coverage"]["resolution_coverage"] == 1.0
    )
    corrected_verdict = reconciled_verdict(
        result, technical_passed=corrected_technical_passed
    )
    payload = {
        "schema": RECONCILIATION_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "versioned_reconciliation_original_result_immutable",
        "original_result": str(original_path),
        "original_result_sha256": original_hash,
        "database": str(database),
        "database_sha256": database_hash,
        "original_verdict": result["verdict"],
        "corrected_verdict": corrected_verdict,
        "original_technical_passed": result["technical_passed"],
        "corrected_technical_passed": corrected_technical_passed,
        "terminal_feed_shutdown_expected": expected_shutdown,
        "terminal_connections": technical["latest_connections"],
        "non_feed_terminal_gates": non_feed_gates,
        "last_checkpoint_technical": result["checkpoints"][-1]["technical"],
        "economic_replication_passed": result["economic_replication_passed"],
        "frequency_passed": result["frequency_passed"],
        "full_statistical_passed": result["full_statistical_passed"],
        "selected_strategy": None,
        "meaning": (
            "El cierre CANCELLED de los feeds fue terminal y esperado. La evidencia "
            "tecnica es valida, pero la estrategia falla la replica economica."
            if corrected_verdict == "FAIL_REPLICATION"
            else "La reconciliacion conserva un fallo cerrado."
        ),
        "original_result_modified": False,
        "database_opened_query_only": True,
        "orders_created": False,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }
    # Confirma de nuevo que ni la base ni el resultado cambiaron durante la lectura.
    if sha256_file(original_path) != original_hash or sha256_file(database) != database_hash:
        raise RuntimeError("La evidencia V0.26b cambio durante la reconciliacion")
    if output is not None:
        _write_atomic(output, payload)
    return payload


__all__ = [
    "ORIGINAL_RESULT_SCHEMA",
    "RECONCILIATION_SCHEMA",
    "reconcile_v026b_terminal_audit",
    "reconciled_verdict",
    "terminal_feed_shutdown_is_expected",
]
