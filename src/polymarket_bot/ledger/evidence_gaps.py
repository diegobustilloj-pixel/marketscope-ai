"""Create deterministic evidence requests for a sealed basis bundle.

This stage is deliberately offline.  It identifies which public transaction
boundary values, price marks and independent-accounting artefacts are still
needed; it never guesses a value, contacts a wallet or enables execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections import Counter
from pathlib import Path

from . import SAFETY
from .common import EvidenceError, digest, now_utc, uint, validate_exact

ENGINE_SCHEMA = 1
ENGINE_VERSION = "basis-evidence-gap-audit/1"


def _sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _write_new(path: Path, value) -> None:
    validate_exact(value)
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False)
    with path.open("x", encoding="utf-8") as target:
        for chunk in encoder.iterencode(value):
            target.write(chunk)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())


def _nonempty_string_list(value) -> bool:
    return (isinstance(value, list) and bool(value)
            and all(isinstance(item, str) and item for item in value))


def _action_reference(action: dict) -> dict:
    identifier = action.get("id")
    order = action.get("order")
    raw_ids = action.get("raw_ids")
    if (not isinstance(identifier, str) or not identifier
            or not isinstance(order, list) or len(order) != 3
            or not _nonempty_string_list(raw_ids)):
        raise EvidenceError("Basis-gap action identity/order/provenance is invalid")
    legs = {}
    for field in ("inputs", "outputs"):
        source = action.get(field, [])
        if not isinstance(source, list):
            raise EvidenceError("Basis-gap action legs are invalid")
        normalized = []
        for leg in source:
            if (not isinstance(leg, dict) or not isinstance(leg.get("asset"), str)
                    or not leg["asset"]):
                raise EvidenceError("Basis-gap action asset is invalid")
            normalized.append({"asset": leg["asset"], "quantity": uint(leg.get("quantity"))})
        legs[field] = normalized
    cash = action.get("cash_delta")
    if type(cash) is not int:
        raise EvidenceError("Basis-gap action cash delta is invalid")
    return {
        "id": identifier,
        "order": [uint(item) for item in order],
        "kind": action.get("kind"),
        "cash_delta": cash,
        **legs,
        # The immutable input bundle holds the raw IDs.  A hash/count link
        # keeps the request small and prevents the audit from duplicating raw
        # evidence just to ask for its valuation.
        "raw_id_count": len(raw_ids),
        "raw_ids_hash": digest(raw_ids),
        "source_action_hash": digest(action),
    }


def _external_flow_requests(actions: list[dict]) -> tuple[list[dict], Counter, Counter]:
    requests, candidates, requested = [], Counter(), Counter()
    for action in actions:
        kind = action.get("kind")
        if kind not in {"receive", "transfer"}:
            continue
        candidates[kind] += 1
        required = []
        value = action.get("external_flow_value")
        evidence = action.get("external_flow_evidence")
        if type(value) is not int:
            required.append("EXTERNAL_FLOW_VALUE_MISSING")
        if not _nonempty_string_list(evidence):
            required.append("EXTERNAL_FLOW_EVIDENCE_MISSING")
        if kind == "receive":
            basis = action.get("received_basis")
            basis_evidence = action.get("basis_evidence")
            if type(basis) is not int or basis < 0:
                required.append("RECEIVED_BASIS_MISSING")
            if not _nonempty_string_list(basis_evidence):
                required.append("RECEIVED_BASIS_EVIDENCE_MISSING")
            if type(value) is int and type(basis) is int and value != basis:
                required.append("RECEIVED_BASIS_VALUE_MISMATCH")
        if not required:
            continue
        requested[kind] += 1
        requests.append({**_action_reference(action), "required": sorted(required)})
    return requests, candidates, requested


def _closing_mark_requests(bundle: dict) -> tuple[list[dict], int, bool]:
    closing = bundle.get("closing")
    marks = bundle.get("closing_marks", {})
    marks_evidence = bundle.get("marks_evidence", {})
    quote_asset = bundle.get("quote_asset")
    if (not isinstance(closing, dict) or not isinstance(closing.get("balances"), dict)
            or not isinstance(marks, dict) or not isinstance(marks_evidence, dict)
            or not isinstance(quote_asset, str) or not quote_asset):
        raise EvidenceError("Basis-gap closing snapshot or marks declaration is invalid")
    evidence_present = _nonempty_string_list(marks_evidence.get("closing"))
    requests, positive_assets = [], 0
    for asset, quantity in sorted(closing["balances"].items()):
        quantity = uint(quantity)
        if asset == quote_asset or not quantity:
            continue
        if not isinstance(asset, str) or not asset:
            raise EvidenceError("Basis-gap closing asset is invalid")
        positive_assets += 1
        required = []
        if asset not in marks:
            required.append("CLOSING_MARK_MISSING")
        if not evidence_present:
            required.append("CLOSING_MARK_EVIDENCE_MISSING")
        if required:
            requests.append({"asset": asset, "quantity_atomic": quantity,
                             "required": required})
    return requests, positive_assets, evidence_present


def audit_basis_evidence(bundle: dict) -> dict:
    """Return explicit, non-speculative requests needed to complete basis."""
    validate_exact(bundle)
    if uint(bundle.get("schema")) != ENGINE_SCHEMA:
        raise EvidenceError("Unsupported basis-gap bundle schema")
    actions = bundle.get("actions")
    if not isinstance(actions, list):
        raise EvidenceError("Basis-gap bundle actions must be a list")
    flow_requests, flow_candidates, flow_requested = _external_flow_requests(actions)
    mark_requests, positive_assets, marks_evidence_present = _closing_mark_requests(bundle)
    independent_present = isinstance(bundle.get("independent_report"), dict)
    required = bool(flow_requests or mark_requests or not independent_present)
    summary = {
        "schema": ENGINE_SCHEMA,
        "engine": ENGINE_VERSION,
        "status": "EVIDENCE_REQUIRED" if required else "NO_DECLARED_GAPS",
        "actions": {"total": len(actions),
                    "external_flow_candidates_by_kind": dict(sorted(flow_candidates.items())),
                    "external_flow_requests_by_kind": dict(sorted(flow_requested.items()))},
        "closing_marks": {"positive_non_quote_assets": positive_assets,
                          "requests": len(mark_requests),
                          "evidence_present": marks_evidence_present},
        "independent_accounting": {
            "report_present": independent_present,
            "request_required": not independent_present,
        },
        "request_hashes": {
            "external_flow_requests": digest(flow_requests),
            "closing_mark_requests": digest(mark_requests),
        },
        "safety": SAFETY,
    }
    return {
        "summary": summary,
        "external_flow_requests": flow_requests,
        "closing_mark_requests": mark_requests,
        "independent_report_request": {
            "required": not independent_present,
            "reason": None if independent_present else "INDEPENDENT_ACCOUNTING_REPORT_MISSING",
            "required_contract": "inventory-basis/independent_expected_contract",
        },
    }


def build_basis_evidence_gap_file(bundle_path: Path, output: Path) -> dict:
    """Seal an offline evidence-request queue in a brand-new directory."""
    bundle_path, output = Path(bundle_path).resolve(), Path(output).resolve()
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists():
        raise EvidenceError("Basis-gap output or partial directory already exists")
    input_sha256 = _sha256(bundle_path)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    audit = audit_basis_evidence(bundle)
    input_digest = digest(bundle)
    partial.mkdir(parents=True)
    _write_new(partial / "configuration.json", {
        "schema": ENGINE_SCHEMA,
        "engine": ENGINE_VERSION,
        "input_path": str(bundle_path),
        "input_sha256": input_sha256,
        "input_digest": input_digest,
        "input_action_count": len(bundle["actions"]),
        "serialization": "sealed-input-reference-v1",
        "safety": SAFETY,
    })
    _write_new(partial / "external_flow_requests.json", audit["external_flow_requests"])
    _write_new(partial / "closing_mark_requests.json", audit["closing_mark_requests"])
    _write_new(partial / "independent_report_request.json", audit["independent_report_request"])
    _write_new(partial / "summary.json", audit["summary"])
    project = Path(__file__).resolve().parents[3]
    try:
        git = ["git", "-c", "safe.directory=" + str(project).replace("\\", "/"), "-C", str(project)]
        commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
        clean = not subprocess.check_output(git + ["status", "--porcelain"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit, clean = "unavailable", False
    files = {path.name: _sha256(path) for path in sorted(partial.glob("*.json"))}
    manifest = {
        "schema": ENGINE_SCHEMA,
        "engine": ENGINE_VERSION,
        "completed_at": now_utc(),
        "code_commit": commit,
        "working_tree_clean": clean,
        "input_path": str(bundle_path),
        "input_sha256": input_sha256,
        "files": files,
        "summary_hash": digest(audit["summary"]),
        "safety": SAFETY,
    }
    _write_new(partial / "run_manifest.json", manifest)
    os.replace(partial, output)
    return audit["summary"]
