"""Offline gate for an independently produced accounting report.

This module does not implement the second accounting engine.  It receives its
sealed report, verifies that the report really comes from a different method,
and compares its exact contract with the primary inventory-basis result.  A
missing or mismatching report remains a sealed blocker and never changes the
bundle, PnL, marks, or execution state.
"""
from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import SAFETY
from .common import EvidenceError, digest, now_utc, validate_exact
from .inventory_basis import ENGINE_VERSION as PRIMARY_ENGINE


SCHEMA = 1
ENGINE = "independent-report-contract-gate/1"
REPORT_KEYS = {
    "accounting_input_hash", "wallet", "quote_asset",
    "opening_equity_quote_atomic", "closing_equity_quote_atomic",
    "external_net_flow_quote_atomic", "period_pnl_quote_atomic",
    "closing_balances_hash", "method", "code_commit", "evidence",
}
CONTRACT_KEYS = {
    "accounting_input_hash", "wallet", "quote_asset",
    "opening_equity_quote_atomic", "closing_equity_quote_atomic",
    "external_net_flow_quote_atomic", "period_pnl_quote_atomic",
    "closing_balances_hash",
}
DECIMAL_KEYS = {
    "opening_equity_quote_atomic", "closing_equity_quote_atomic",
    "external_net_flow_quote_atomic", "period_pnl_quote_atomic",
}


def _sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _load_json(path: Path, label: str):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{label} is not valid JSON") from exc
    validate_exact(value)
    return value


def _files_sha256(root: Path) -> dict[str, str]:
    return {
        path.name: _sha256(path)
        for path in sorted(root.glob("*.json"))
        if path.name != "run_manifest.json"
    }


def _write_new(path: Path, value) -> None:
    validate_exact(value)
    with path.open("x", encoding="utf-8") as target:
        target.write(json.dumps(value, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False, allow_nan=False))
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())


def _load_sealed_basis_result(root: Path) -> tuple[dict, dict, dict]:
    required = ("configuration.json", "summary.json", "run_manifest.json")
    if not root.is_dir() or not all((root / name).is_file() for name in required):
        raise EvidenceError("Primary inventory-basis result is incomplete")
    configuration = _load_json(root / "configuration.json", "Primary configuration")
    summary = _load_json(root / "summary.json", "Primary summary")
    manifest = _load_json(root / "run_manifest.json", "Primary manifest")
    files = manifest.get("files")
    if (not isinstance(files, dict)
            or files != _files_sha256(root)
            or manifest.get("summary_hash") != digest(summary)):
        raise EvidenceError("Primary inventory-basis result seal is invalid")
    expected = summary.get("independent_expected_contract")
    if not isinstance(expected, dict) or set(expected) != CONTRACT_KEYS:
        raise EvidenceError("Primary result has no exact independent contract")
    if (manifest.get("engine") != "inventory-basis-period-pnl/1"
            or configuration.get("engine") != "inventory-basis-period-pnl/1"):
        raise EvidenceError("Primary result engine is not the approved inventory-basis engine")
    return configuration, summary, manifest


def _decimal_text(value) -> str:
    if not isinstance(value, str):
        raise EvidenceError("Independent accounting decimals must be strings")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise EvidenceError("Independent accounting decimal is invalid") from exc
    if not number.is_finite():
        raise EvidenceError("Independent accounting decimal is non-finite")
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _validate_report(report: dict, expected: dict, primary_manifest: dict) -> tuple[str, list[str], str | None]:
    reasons: list[str] = []
    if set(report) != REPORT_KEYS:
        return "BLOCKED", ["INDEPENDENT_REPORT_SCHEMA_INVALID"], None
    method = report.get("method")
    code_commit = report.get("code_commit")
    evidence = report.get("evidence")
    if (not isinstance(method, str) or not method or method == PRIMARY_ENGINE):
        reasons.append("INDEPENDENT_PROVENANCE_METHOD_INVALID")
    if (not isinstance(code_commit, str) or not code_commit
            or code_commit == primary_manifest.get("code_commit")):
        reasons.append("INDEPENDENT_PROVENANCE_COMMIT_INVALID")
    if (not isinstance(evidence, list) or not evidence
            or any(not isinstance(item, str) or not item for item in evidence)):
        reasons.append("INDEPENDENT_PROVENANCE_EVIDENCE_INVALID")

    normalized: dict = {}
    for key in CONTRACT_KEYS:
        observed = report.get(key)
        if key in DECIMAL_KEYS:
            if observed is not None:
                try:
                    observed = _decimal_text(observed)
                except EvidenceError:
                    reasons.append("INDEPENDENT_ACCOUNTING_VALUE_INVALID")
                    continue
        elif not isinstance(observed, str) or not observed:
            reasons.append("INDEPENDENT_ACCOUNTING_IDENTITY_INVALID")
            continue
        normalized[key] = observed
        if observed != expected.get(key):
            reasons.append("INDEPENDENT_ACCOUNTING_MISMATCH")
    return ("MATCH" if not reasons else "BLOCKED", sorted(set(reasons)),
            digest(normalized) if not reasons else None)


def verify_independent_report_file(
    basis_result: Path,
    report: Path,
    output: Path,
) -> dict:
    """Seal an offline comparison between a primary result and a separate report."""
    basis_result, report, output = (Path(basis_result).resolve(), Path(report).resolve(),
                                    Path(output).resolve())
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists():
        raise EvidenceError("Independent-report gate output or partial already exists")
    if output.is_relative_to(basis_result) or partial.is_relative_to(basis_result):
        raise EvidenceError("Independent-report gate output cannot be inside primary result")
    configuration, primary, primary_manifest = _load_sealed_basis_result(basis_result)
    if output.is_relative_to(report) or partial.is_relative_to(report):
        raise EvidenceError("Independent-report gate output cannot overlap report input")

    report_exists = report.is_file()
    report_before = _sha256(report) if report_exists else None
    report_value = _load_json(report, "Independent report") if report_exists else None
    report_after = _sha256(report) if report_exists else None
    if report_exists and report_before != report_after:
        raise EvidenceError("Independent report changed during read")

    expected = primary["independent_expected_contract"]
    if report_value is None:
        contract_status = "BLOCKED"
        contract_reasons = ["INDEPENDENT_REPORT_MISSING"]
        observed_contract_hash = None
    else:
        contract_status, contract_reasons, observed_contract_hash = _validate_report(
            report_value, expected, primary_manifest
        )
    primary_ready = primary.get("status") == "COMPLETE" and primary.get("basis_gate", {}).get("status") == "PASS"
    reasons = list(contract_reasons)
    if not primary_ready:
        reasons.append("PRIMARY_BASIS_GATE_NOT_PASS")
    reasons = sorted(set(reasons))
    if not reasons:
        status = "INDEPENDENT_REPORT_ACCEPTED"
    elif contract_status == "MATCH":
        status = "INDEPENDENT_REPORT_MATCH_PRIMARY_BASIS_BLOCKED"
    else:
        status = "INDEPENDENT_REPORT_GATE_BLOCKED"

    partial.mkdir(parents=True)
    _write_new(partial / "configuration.json", {
        "schema": SCHEMA,
        "engine": ENGINE,
        "basis_result_path": str(basis_result),
        "basis_result_manifest_sha256": _sha256(basis_result / "run_manifest.json"),
        "basis_summary_sha256": _sha256(basis_result / "summary.json"),
        "report_path": str(report),
        "report_sha256": report_before,
        "report_present": report_exists,
        "serialization": "sealed-input-reference-v1",
        "integration": "NONE; contract gate cannot modify basis or P0 state",
        "safety": SAFETY,
    })
    summary = {
        "schema": SCHEMA,
        "engine": ENGINE,
        "status": status,
        "primary": {
            "status": primary.get("status"),
            "basis_gate": primary.get("basis_gate"),
            "accounting_input_hash": primary.get("accounting_input_hash"),
        },
        "contract": {
            "status": contract_status,
            "reasons": contract_reasons,
            "expected_hash": digest(expected),
            "observed_hash": observed_contract_hash,
        },
        "gate": {"status": "PASS" if not reasons else "BLOCKED", "reasons": reasons},
        "integration": {"report_accepted": not reasons, "p0_state_changed": False},
        "safety": SAFETY,
    }
    _write_new(partial / "summary.json", summary)
    _write_new(partial / "run_manifest.json", {
        "schema": SCHEMA,
        "engine": ENGINE,
        "completed_at": now_utc(),
        "basis_result_manifest_sha256": _sha256(basis_result / "run_manifest.json"),
        "report_sha256": report_before,
        "files": _files_sha256(partial),
        "summary_hash": digest(summary),
        "safety": SAFETY,
    })
    os.replace(partial, output)
    return summary
