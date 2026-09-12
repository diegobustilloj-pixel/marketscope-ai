from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polymarket_bot.v022_audit as base_audit
from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v022b_runner import load_and_verify_prereg


RESULT_SCHEMA = "result_v022b_synced_persistent_observer_1"


def audit_v022b(
    *, database: str | Path, prereg_path: str | Path, result_path: str | Path | None = None
) -> dict[str, Any]:
    db_path = Path(database).resolve()
    prereg_file = Path(prereg_path).resolve()
    output = Path(result_path).resolve() if result_path is not None else None
    load_and_verify_prereg(prereg_file)
    if output is not None and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == RESULT_SCHEMA
            and existing.get("database_sha256") == sha256_file(db_path)
            and existing.get("preregistration_sha256") == sha256_file(prereg_file)
        ):
            return existing
        raise RuntimeError("Existe un resultado V0.22b para otra evidencia")

    original_loader = base_audit.load_and_verify_prereg
    base_audit.load_and_verify_prereg = load_and_verify_prereg
    try:
        result = base_audit.audit_v022(
            database=db_path, prereg_path=prereg_file, result_path=None
        )
    finally:
        base_audit.load_and_verify_prereg = original_loader
    result["schema"] = RESULT_SCHEMA
    result["variant"] = "V0.22b_8h"
    result["meaning"] = (
        "PASS_SYNCHRONIZED_OBSERVER_ONLY en V0.22b demuestra asks sincronizados y "
        "persistentes dentro de ocho horas; no demuestra atomicidad, fills, PnL ni "
        "habilita dinero real."
    )
    if output is not None:
        base_audit._write_atomic(output, result)
    return result


__all__ = ["RESULT_SCHEMA", "audit_v022b"]
