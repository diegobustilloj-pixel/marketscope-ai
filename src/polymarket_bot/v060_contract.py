from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v059_contract import frozen_contract as v059_frozen_contract


PREREG_SCHEMA = "prereg_v060_absolute_active_map_rfq_clob_replay_1"
PREREG_STATUS = "FROZEN_BEFORE_CREDENTIAL_ACTIVE_MAPPING_OR_RFQ_PREFLIGHT"
VARIANT = "V0.60_ABSOLUTE_ACTIVE_MAP_RFQ_CLOB_PAPER_REPLAY_NO_QUOTES"

SOURCE_FILES = {
    "v059_preregistration": "data/prereg_v059_lifecycle_aware_mapped_rfq_clob_replay.json",
    "v059_blocked_result": "data/resultado_v059_blocked_historical_seed_decay.json",
    "v058_position_seed": "data/v058_position_seed_from_v057.json",
    "v060_contract_code": "src/polymarket_bot/v060_contract.py",
    "v060_replay_code": "src/polymarket_bot/v060_active_mapping_replay.py",
    "v060_audit_code": "src/polymarket_bot/v060_audit.py",
    "v060_monitor_code": "v060_monitor.py",
    "v060_bootstrap": "v060_phantom_bootstrap.py",
    "v060_tests": "tests/test_v060_active_mapping_replay.py",
    "v060_bootstrap_tests": "tests/test_v060_phantom_bootstrap.py",
    "v060_design": "docs/V060_ABSOLUTE_ACTIVE_MAP_RFQ_CLOB_REPLAY.md",
}


class V060ContractError(RuntimeError):
    pass


def frozen_contract() -> dict[str, Any]:
    contract = json.loads(json.dumps(v059_frozen_contract()))
    mapping = contract["mapping"]
    mapping.pop("minimum_seed_resolution_rate", None)
    mapping["minimum_markets"] = 1000
    mapping["minimum_mapped_positions"] = 2000
    mapping["minimum_resolved_seed_positions"] = 1000
    mapping["historical_seed_resolution_rate_gate_enabled"] = False
    mapping["seed_denominator_semantics"] = "HISTORICAL_DIAGNOSTIC_ONLY_NOT_A_READINESS_GATE"
    mapping["absolute_threshold_basis"] = "V059_OBSERVED_3623_MARKETS_7228_MAP_RECORDS_4459_RESOLVED_SEED"
    contract["storage"]["format"] = (
        "SQLITE_WAL_COMPRESSED_SANITIZED_ASKS_WITH_ABSOLUTE_ACTIVE_MAP_V058_TABLE_COMPATIBLE"
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
            raise V060ContractError(f"Falta evidencia V0.60: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    return evidence


def build_preregistration(*, output_path: str | Path, project_root: str | Path = ROOT) -> dict[str, Any]:
    root = Path(project_root).resolve()
    failure = json.loads((root / SOURCE_FILES["v059_blocked_result"]).read_text(encoding="utf-8"))
    if failure.get("verdict") != "FAIL_HISTORICAL_OPEN_SHARE_DECAYED_BELOW_FROZEN_GATE":
        raise V060ContractError("V0.59 no conserva el diagnostico esperado")
    diagnostic = failure.get("diagnostic", {})
    expected_observation = {
        "position_seed_count": 6518,
        "resolved_open_position_count": 4459,
        "open_markets_returned": 3623,
        "mapped_position_records_returned": 7228,
        "gamma_batches": 131,
        "gamma_retries": 0,
    }
    for key, value in expected_observation.items():
        if diagnostic.get(key) != value:
            raise V060ContractError(f"Diagnostico V0.59 incompatible: {key}")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "validate_a_broad_absolute_active_position_map_then_measure_coverage_against_live_rfq_requests",
        "source_evidence": _source_evidence(root),
        "official_references": [
            "https://docs.polymarket.com/trading/combos/market-makers#map-legs-to-markets",
            "https://docs.polymarket.com/api-reference/markets/list-markets-keyset-pagination",
            "https://docs.polymarket.com/api-reference/wss/rfq",
        ],
        "threshold_basis": {
            "observed_active_markets": 3623,
            "observed_mapped_positions": 7228,
            "observed_resolved_seed_positions": 4459,
            "minimum_active_markets": 1000,
            "minimum_mapped_positions": 2000,
            "minimum_resolved_seed_positions": 1000,
            "historical_resolution_rate_used_as_gate": False,
            "terminal_live_rfq_mapping_coverage": 0.90,
        },
        "contract": frozen_contract(),
        "change_from_v059": {
            "v059_artifacts_modified": False,
            "only_mapping_readiness_fix": "REPLACE_MONOTONIC_HISTORICAL_RATE_WITH_ABSOLUTE_ACTIVE_MAP_MINIMUMS",
            "live_mapping_coverage_gate_preserved_at_90_percent": True,
            "mapping_built_before_rfq_connection": True,
            "economic_mechanism_changed": False,
            "sampling_changed": False,
            "quotes_orders_signatures_transactions_enabled": False,
            "bootstrap_distinguishes_mapping_from_capture_started": True,
        },
        "safety": frozen_contract()["safety"],
    }
    output = Path(output_path).resolve()
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        comparable = dict(existing)
        comparable["created_at"] = payload["created_at"]
        if comparable != payload:
            raise V060ContractError("Ya existe otra preinscripcion V0.60")
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
            raise V060ContractError(f"Preinscripcion V0.60 incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V060ContractError("Inventario V0.60 incompatible")
    for key, relative in SOURCE_FILES.items():
        if evidence[key].get("relative_path") != relative or evidence[key].get("sha256") != sha256_file(root / relative):
            raise V060ContractError(f"Hash V0.60 no coincide: {key}")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA", "PREREG_STATUS", "SOURCE_FILES", "VARIANT", "V060ContractError",
    "build_preregistration", "frozen_contract", "load_and_verify_preregistration",
]
