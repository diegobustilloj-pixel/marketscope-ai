from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v043_contract import VARIANT, load_and_verify_prereg


IMPLEMENTATION_SCHEMA = "implementation_v043_chainlink_outage_fail_closed_audit_1"
IMPLEMENTATION_STATUS = "BUILT_TESTED_FOR_SINGLE_CLOSED_INCIDENT_AUDIT"
IMPLEMENTATION_FILES = {
    "design": "src/polymarket_bot/v043_chainlink_outage_design.py",
    "contract": "src/polymarket_bot/v043_contract.py",
    "auditor": "src/polymarket_bot/v043_audit.py",
    "runner": "src/polymarket_bot/v043_runner.py",
    "entrypoint": "v043_monitor.py",
    "tests": "tests/test_v043_chainlink_outage_fail_closed.py",
}


class V043RunnerError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_implementation_manifest(
    *,
    prereg_path: str | Path,
    output_path: str | Path,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    output = Path(output_path).resolve()
    prereg = load_and_verify_prereg(prereg_file, project_root=root)
    hashes: dict[str, str] = {}
    for key, relative in IMPLEMENTATION_FILES.items():
        source = root / relative
        if not source.is_file():
            raise V043RunnerError(f"Implementacion V0.43 incompleta: {relative}")
        hashes[key] = sha256_file(source)
    payload = {
        "schema": IMPLEMENTATION_SCHEMA,
        "status": IMPLEMENTATION_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "preregistration": str(prereg_file),
        "preregistration_sha256": sha256_file(prereg_file),
        "code_hashes": hashes,
        "closed_incident_auditor_built": True,
        "fresh_capture_built": False,
        "provider_liveness_claim_built": False,
        "economic_strategy_built": False,
        "orders_built": False,
        "single_terminal_audit": True,
        "scheduled_supervision_built": False,
        "automatic_followup_launch_built": False,
        "technical_gates": prereg["technical_gates"],
        "classification_policy": prereg["classification_policy"],
        "data_policy": prereg["data_policy"],
        "safety": prereg["safety"],
    }
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == IMPLEMENTATION_SCHEMA
            and existing.get("code_hashes") == hashes
        ):
            return existing
        raise V043RunnerError("Existe otro manifiesto V0.43")
    _write_atomic(output, payload)
    return payload


def load_and_verify_implementation(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V043RunnerError("Implementacion V0.43 no sellada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema") != IMPLEMENTATION_SCHEMA:
        raise V043RunnerError("Manifiesto V0.43 incompatible")
    prereg_file = Path(str(payload.get("preregistration") or "")).resolve()
    prereg = load_and_verify_prereg(prereg_file, project_root=root)
    expected = {
        "status": IMPLEMENTATION_STATUS,
        "variant": VARIANT,
        "preregistration_sha256": sha256_file(prereg_file),
        "closed_incident_auditor_built": True,
        "fresh_capture_built": False,
        "provider_liveness_claim_built": False,
        "economic_strategy_built": False,
        "orders_built": False,
        "single_terminal_audit": True,
        "scheduled_supervision_built": False,
        "automatic_followup_launch_built": False,
        "technical_gates": prereg["technical_gates"],
        "classification_policy": prereg["classification_policy"],
        "data_policy": prereg["data_policy"],
        "safety": prereg["safety"],
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V043RunnerError(f"Manifiesto V0.43 invalido: {key}")
    hashes = payload.get("code_hashes")
    if not isinstance(hashes, Mapping) or set(hashes) != set(IMPLEMENTATION_FILES):
        raise V043RunnerError("Inventario V0.43 incompatible")
    for key, relative in IMPLEMENTATION_FILES.items():
        if hashes.get(key) != sha256_file(root / relative):
            raise V043RunnerError(f"Hash V0.43 no coincide: {key}")
    return dict(payload)


__all__ = [
    "IMPLEMENTATION_SCHEMA",
    "IMPLEMENTATION_STATUS",
    "V043RunnerError",
    "build_implementation_manifest",
    "load_and_verify_implementation",
]
