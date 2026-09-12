from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v058_contract import frozen_contract as v058_frozen_contract


PREREG_SCHEMA = "prereg_v059_lifecycle_aware_mapped_rfq_clob_replay_1"
PREREG_STATUS = "FROZEN_BEFORE_CREDENTIAL_ACTIVE_MAPPING_OR_RFQ_PREFLIGHT"
VARIANT = "V0.59_LIFECYCLE_AWARE_MAPPED_RFQ_CLOB_PAPER_REPLAY_NO_QUOTES"

SOURCE_FILES = {
    "v058_preregistration": "data/prereg_v058_mapped_rfq_clob_replay.json",
    "v058_blocked_result": "data/resultado_v058_blocked_position_seed_drift.json",
    "v058_position_seed": "data/v058_position_seed_from_v057.json",
    "v059_mapping_feasibility": "data/diagnostico_v059_closed_mapping_feasibility.json",
    "v059_contract_code": "src/polymarket_bot/v059_contract.py",
    "v059_replay_code": "src/polymarket_bot/v059_active_mapping_replay.py",
    "v059_audit_code": "src/polymarket_bot/v059_audit.py",
    "v059_monitor_code": "v059_monitor.py",
    "v059_bootstrap": "v059_phantom_bootstrap.py",
    "v059_tests": "tests/test_v059_active_mapping_replay.py",
    "v059_bootstrap_tests": "tests/test_v059_phantom_bootstrap.py",
    "v059_design": "docs/V059_LIFECYCLE_AWARE_MAPPED_RFQ_CLOB_REPLAY.md",
}


class V059ContractError(RuntimeError):
    pass


def frozen_contract() -> dict[str, Any]:
    contract = json.loads(json.dumps(v058_frozen_contract()))
    contract["mapping"]["minimum_seed_resolution_rate"] = 0.70
    contract["mapping"]["seed_denominator_semantics"] = "HISTORICAL_V057_POSITIONS_STILL_OPEN_AT_PREFLIGHT"
    contract["mapping"]["closed_markets_intentionally_excluded"] = True
    contract["outbound"]["public_requests"][0] = (
        "GET GAMMA /markets/keyset FOR_CURRENTLY_OPEN_V057_POSITION_SEED BEFORE_RFQ_CONNECTION WITH_BOUNDED_RETRY"
    )
    contract["storage"]["format"] = (
        "SQLITE_WAL_COMPRESSED_SANITIZED_ASKS_WITH_ACTIVE_POSITION_MAP_V058_TABLE_COMPATIBLE"
    )
    return contract


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _source_evidence(root: Path) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for key, relative in SOURCE_FILES.items():
        source = root / relative
        if not source.is_file():
            raise V059ContractError(f"Falta evidencia V0.59: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    return evidence


def build_preregistration(*, output_path: str | Path, project_root: str | Path = ROOT) -> dict[str, Any]:
    root = Path(project_root).resolve()
    failure = json.loads((root / SOURCE_FILES["v058_blocked_result"]).read_text(encoding="utf-8"))
    if failure.get("verdict") != "FAIL_OPEN_ONLY_POSITION_SEED_DRIFT":
        raise V059ContractError("V0.58 no conserva el diagnostico esperado")
    feasibility = json.loads((root / SOURCE_FILES["v059_mapping_feasibility"]).read_text(encoding="utf-8"))
    if feasibility.get("conclusion", {}).get("selected_design") != (
        "V059_LIFECYCLE_AWARE_OPEN_MAPPING_WITH_70_PERCENT_HISTORICAL_SEED_AND_90_PERCENT_LIVE_COVERAGE"
    ):
        raise V059ContractError("V0.59 no conserva el diagnostico de ciclo de vida esperado")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "resolve_the_currently_open_subset_of_the_frozen_v057_position_seed_then_measure_mapping_coverage_against_live_rfq_requests",
        "source_evidence": _source_evidence(root),
        "official_references": [
            "https://docs.polymarket.com/trading/combos/market-makers#map-legs-to-markets",
            "https://docs.polymarket.com/api-reference/markets/list-markets-keyset-pagination",
            "https://docs.polymarket.com/api-reference/wss/rfq",
        ],
        "contract": frozen_contract(),
        "change_from_v058": {
            "v058_artifacts_modified": False,
            "only_semantic_fix": "TREAT_CLOSED_HISTORICAL_SEED_POSITIONS_AS_LIFECYCLE_DRIFT_NOT_ACTIVE_MAPPING_FAILURE",
            "closed_markets_intentionally_excluded": True,
            "live_mapping_coverage_gate_preserved_at_90_percent": True,
            "mapping_built_before_rfq_connection": True,
            "economic_mechanism_changed": False,
            "sampling_changed": False,
            "quotes_orders_signatures_transactions_enabled": False,
        },
        "safety": frozen_contract()["safety"],
    }
    output = Path(output_path).resolve()
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        comparable = dict(existing)
        comparable["created_at"] = payload["created_at"]
        if comparable != payload:
            raise V059ContractError("Ya existe otra preinscripcion V0.59")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_preregistration(path: str | Path, *, project_root: str | Path = ROOT) -> dict[str, Any]:
    root = Path(project_root).resolve()
    payload = json.loads(Path(path).resolve().read_text(encoding="utf-8"))
    expected = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "variant": VARIANT,
        "contract": frozen_contract(),
        "safety": frozen_contract()["safety"],
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V059ContractError(f"Preinscripcion V0.59 incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V059ContractError("Inventario V0.59 incompatible")
    for key, relative in SOURCE_FILES.items():
        if evidence[key].get("relative_path") != relative or evidence[key].get("sha256") != sha256_file(root / relative):
            raise V059ContractError(f"Hash V0.59 no coincide: {key}")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA", "PREREG_STATUS", "SOURCE_FILES", "VARIANT", "V059ContractError",
    "build_preregistration", "frozen_contract", "load_and_verify_preregistration",
]
