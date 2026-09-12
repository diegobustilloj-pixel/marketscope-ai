from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v054_contract import frozen_contract as v054_frozen_contract


PREREG_SCHEMA = "prereg_v054b_public_combo_binary_labels_census_1"
PREREG_STATUS = "FROZEN_BEFORE_CORRECTED_PUBLIC_COMBO_CATALOG_ACCESS"
VARIANT = "V0.54B_PUBLIC_COMBO_BINARY_LABELS_OBSERVABILITY_CENSUS"

SOURCE_FILES = {
    "v054_preregistration": "data/prereg_v054_public_combo_observability_census.json",
    "v054_result": "data/resultado_v054_public_combo_observability_census.json",
    "v054b_contract_code": "src/polymarket_bot/v054b_contract.py",
    "v054b_census_code": "src/polymarket_bot/v054b_combo_public_census.py",
    "v054b_monitor_code": "v054b_monitor.py",
    "v054b_tests": "tests/test_v054b_combo_public_census.py",
}


class V054BContractError(RuntimeError):
    pass


def frozen_contract() -> dict[str, Any]:
    contract = copy.deepcopy(v054_frozen_contract())
    contract["market_schema"].pop("binary_outcomes", None)
    contract["market_schema"]["outcome_labels"] = (
        "EXACTLY_TWO_DISTINCT_NONEMPTY_STRINGS_AS_RETURNED_BY_OFFICIAL_CATALOG"
    )
    contract["market_schema"]["position_outcome_price_mapping"] = (
        "SAME_ARRAY_INDEX_WITHOUT_RENAMING_LABELS"
    )
    contract["decision"]["insufficient_catalog"] = (
        "FAIL_CORRECTED_PUBLIC_COMBO_CATALOG_INSUFFICIENT_VALID_MARKETS"
    )
    return contract


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _source_evidence(root: Path) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for key, relative in SOURCE_FILES.items():
        source = root / relative
        if not source.is_file():
            raise V054BContractError(f"Falta evidencia V0.54b: {relative}")
        evidence[key] = {"relative_path": relative, "sha256": sha256_file(source)}
    return evidence


def build_preregistration(
    *, output_path: str | Path, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    v054 = json.loads((root / SOURCE_FILES["v054_result"]).read_text(encoding="utf-8"))
    if v054.get("verdict") != "FAIL_PUBLIC_COMBO_CATALOG_INSUFFICIENT_VALID_MARKETS":
        raise V054BContractError("V0.54 no conserva el fallo de esquema esperado")
    if v054.get("catalog", {}).get("validation_rejections") != {
        "OUTCOMES_NOT_EXACT_YES_NO": 101
    }:
        raise V054BContractError("V0.54 no conserva la evidencia exacta de etiquetas")
    payload = {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "correct_only_the_binary_outcome_label_assumption_then_retest_public_combo_observability",
        "source_evidence": _source_evidence(root),
        "official_references": [
            "https://docs.polymarket.com/api-reference/combo-markets/get-combo-markets",
            "https://docs.polymarket.com/api-reference/wss/rfq",
            "https://docs.polymarket.com/api-reference/maker/submit-a-quote",
        ],
        "contract": frozen_contract(),
        "only_change_from_v054": {
            "old": "OUTCOMES_MUST_EQUAL_YES_NO",
            "new": "OUTCOMES_MUST_BE_TWO_DISTINCT_NONEMPTY_LABELS",
            "reason": "OFFICIAL_LIVE_CATALOG_RETURNED_OVER_UNDER_AND_TWO_COMPETITOR_LABELS",
            "selection_price_execution_and_safety_unchanged": True,
            "v054_artifacts_modified": False,
        },
        "safety": copy.deepcopy(frozen_contract()["safety"]),
    }
    output = Path(output_path).resolve()
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        comparable = dict(existing)
        comparable["created_at"] = payload["created_at"]
        if comparable != payload:
            raise V054BContractError("Ya existe otra preinscripcion V0.54b")
        return existing
    _write_atomic(output, payload)
    return payload


def load_and_verify_preregistration(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V054BContractError("Preinscripcion V0.54b no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    for key, expected in {
        "schema": PREREG_SCHEMA,
        "status": PREREG_STATUS,
        "variant": VARIANT,
        "contract": frozen_contract(),
    }.items():
        if payload.get(key) != expected:
            raise V054BContractError(f"Preinscripcion V0.54b incompatible: {key}")
    evidence = payload.get("source_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != set(SOURCE_FILES):
        raise V054BContractError("Inventario V0.54b incompatible")
    for key, relative in SOURCE_FILES.items():
        record = evidence[key]
        if record.get("relative_path") != relative:
            raise V054BContractError(f"Ruta V0.54b incompatible: {key}")
        if record.get("sha256") != sha256_file(root / relative):
            raise V054BContractError(f"Hash V0.54b no coincide: {key}")
    if payload.get("safety") != frozen_contract()["safety"]:
        raise V054BContractError("Seguridad V0.54b incompatible")
    return dict(payload)


__all__ = [
    "PREREG_SCHEMA",
    "PREREG_STATUS",
    "SOURCE_FILES",
    "VARIANT",
    "V054BContractError",
    "build_preregistration",
    "frozen_contract",
    "load_and_verify_preregistration",
]
