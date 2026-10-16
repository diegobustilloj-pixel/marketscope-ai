"""Resumable, fail-closed orchestration of sealed historical-price batches.

The paginator coordinates the existing probe, identity audit, and closing-mark
policy engines.  It deliberately does not integrate a mark, alter a basis
bundle, calculate PnL, sign, or trade.  Every attempt receives a new directory;
failed and partial attempts remain available as evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from . import SAFETY
from .common import EvidenceError, canonical, now_utc, uint, validate_exact
from .price_candidate_audit import (
    AUDIT_VERSION,
    OfficialCandidateMetadataClient,
    _git_state,
    audit_price_candidates_file,
)
from .price_history_probe import (
    CTF_CONTRACT,
    MAX_SAMPLE_SIZE,
    OfficialPriceHistoryClient,
    PROBE_VERSION,
    build_price_history_probe_file,
)
from .price_mark_policy import (
    POLICY_STATUS,
    POLICY_VERSION,
    _load_policy,
    _validate_rpc_endpoint,
    _verify_sealed_directory,
    evaluate_price_policy_file,
)


PAGINATOR_SCHEMA = 1
PAGINATOR_VERSION = "polyledger-price-batch-paginator/1"
DEFAULT_POLICY_TEMPLATE = Path("configs/polyledger/closing_mark_paginator_template_v1.json")
ALLOWED_PROBE_STATUSES = {"FRESH_CANDIDATE", "STALE_CANDIDATE", "NO_OBSERVATION"}

EXPECTED_THRESHOLDS = {
    "maximum_age_seconds": 900,
    "minimum_resolution_seconds": 1,
    "maximum_resolution_seconds": 300,
    "require_closed_bucket": True,
    "price_interval": "STRICT_INTERIOR_0_1",
}
EXPECTED_IDENTITY = {
    "require_current_official_identity": True,
    "require_clob_compact_condition": True,
    "require_gamma_binary_pair": True,
    "require_local_mapping_before_cutoff": True,
    "allow_negrisk": False,
    "require_cutoff_within_declared_window": True,
    "current_closed_allowed_only_if_closed_after_cutoff": True,
}
EXPECTED_SETTLEMENT = {
    "require_two_independent_rpcs": True,
    "require_exact_block_hash": True,
    "unresolved_denominator": 0,
    "settled_precedence": "CTF_PAYOUT_STATE_OVERRIDES_HISTORY",
    "state_anchor": "CLOSING_BLOCK_POST_STATE_MATCHING_INVENTORY",
}
EXPECTED_INTEGRATION = {
    "write_closing_marks": False,
    "modify_basis_bundle": False,
    "automatic_approval": False,
}


def _sha256(path: Path) -> str:
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _require_empty_sqlite_wal(path: Path) -> None:
    wal = Path(f"{Path(path)}-wal")
    if wal.exists() and (not wal.is_file() or wal.stat().st_size != 0):
        raise EvidenceError("Metadata database has an active SQLite WAL")


def _create_metadata_snapshot(source: Path, destination: Path) -> None:
    """Copy a quiescent SQLite main file and prove the copy is byte-identical."""
    _require_empty_sqlite_wal(source)
    before = _sha256(source)
    destination.parent.mkdir(parents=True, exist_ok=False)
    with source.open("rb") as source_handle, destination.open("xb") as target_handle:
        shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
        target_handle.flush()
        os.fsync(target_handle.fileno())
    _require_empty_sqlite_wal(source)
    after = _sha256(source)
    snapshot = _sha256(destination)
    if before != after or snapshot != before:
        raise EvidenceError("Metadata database changed while creating the frozen snapshot")


def _read_json(path: Path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"Paginator JSON is invalid: {Path(path).name}") from exc
    validate_exact(value)
    return value


def _write_new_json(path: Path, value) -> None:
    validate_exact(value)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as target:
        target.write(canonical(value) + "\n")
        target.flush()
        os.fsync(target.fileno())


def _atomic_json(path: Path, value) -> None:
    validate_exact(value)
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as target:
            target.write(canonical(value) + "\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _exclusive_run_lock(root: Path):
    """Hold a process lock without treating a stale lock file as a live owner."""
    lock_path = Path(root) / ".paginator.lock"
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - Windows is the canonical runtime
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise EvidenceError("Another price paginator process already owns this output") from exc
        try:
            yield
        finally:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - Windows is the canonical runtime
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def _load_policy_template(path: Path) -> dict:
    template = _read_json(path)
    required = {
        "schema", "template_id", "status", "chain", "conditional_tokens_contract",
        "thresholds", "identity", "settlement", "integration", "safety",
    }
    if not isinstance(template, dict) or set(template) != required:
        raise EvidenceError("Closing-mark paginator template fields are invalid")
    if (uint(template["schema"]) != PAGINATOR_SCHEMA
            or not isinstance(template["template_id"], str) or not template["template_id"]
            or template["status"] != POLICY_STATUS
            or uint(template["chain"]) != 137
            or not isinstance(template["conditional_tokens_contract"], str)
            or template["conditional_tokens_contract"].lower() != CTF_CONTRACT
            or template["thresholds"] != EXPECTED_THRESHOLDS
            or template["identity"] != EXPECTED_IDENTITY
            or template["settlement"] != EXPECTED_SETTLEMENT
            or template["integration"] != EXPECTED_INTEGRATION
            or template["safety"] != SAFETY):
        raise EvidenceError("Closing-mark paginator template would weaken the P0 policy")
    return template


def _safe_error(exc: BaseException) -> dict:
    message = " ".join(str(exc).split())[:500]
    return {"type": type(exc).__name__, "message": message, "at": now_utc()}


def _state_receipt(root: Path, state: dict) -> dict:
    return {
        "status": state["status"],
        "output": str(Path(root).resolve()),
        "start_index": state["start_index"],
        "next_start_index": state["next_start_index"],
        "stop_index_exclusive": state.get("effective_stop_index_exclusive"),
        "eligible_ctf_outcomes": state.get("eligible_ctf_outcomes"),
        "batches_completed": state["batches_completed"],
        "aggregate": state["aggregate"],
        "active_batch": state.get("active_batch"),
        "last_error": state.get("last_error"),
        "integration": {
            "closing_marks_written": 0,
            "basis_bundle_modified": False,
            "status": "NOT_INTEGRATED",
        },
        "review_gate": {
            "status": "BLOCKED",
            "reasons": [
                "ACCEPTED_CANDIDATES_REQUIRE_NEW_SEALED_BUNDLE_COMPILATION",
                "CLOSING_MARKS_NOT_INTEGRATED",
                "EXTERNAL_FLOW_VALUE_EVIDENCE_REMAINS_OPEN",
                "INDEPENDENT_ACCOUNTING_REPORT_MISSING",
            ],
        },
        "safety": SAFETY,
    }


def price_pagination_status(output: Path) -> dict:
    root = Path(output).resolve()
    state_path = root / "state.json"
    if not state_path.is_file():
        raise EvidenceError("Price paginator state does not exist")
    state = _read_json(state_path)
    if state.get("engine") != PAGINATOR_VERSION:
        raise EvidenceError("Price paginator state engine is unsupported")
    return _state_receipt(root, state)


def _new_state(start_index: int) -> dict:
    return {
        "schema": PAGINATOR_SCHEMA,
        "engine": PAGINATOR_VERSION,
        "status": "RUNNING",
        "started_at": now_utc(),
        "updated_at": now_utc(),
        "start_index": start_index,
        "next_start_index": start_index,
        "configured_stop_index_exclusive": None,
        "effective_stop_index_exclusive": None,
        "eligible_ctf_outcomes": None,
        "batches_completed": 0,
        "attempts_started": 0,
        "failed_attempts": 0,
        "active_batch": None,
        "last_error": None,
        "aggregate": {
            "outcomes_examined": 0,
            "fresh_candidates": 0,
            "stale_candidates": 0,
            "no_observation": 0,
            "candidates_audited": 0,
            "accepted_for_new_bundle_compilation": 0,
            "deferred_missing_historical_identity": 0,
            "deferred_missing_current_official_identity": 0,
            "rejected_by_policy": 0,
            "batches_without_fresh_candidates": 0,
        },
        "integration": {
            "closing_marks_written": 0,
            "basis_bundle_modified": False,
            "status": "NOT_INTEGRATED",
        },
        "safety": SAFETY,
    }


def _save_state(root: Path, state: dict) -> None:
    state["updated_at"] = now_utc()
    _atomic_json(Path(root) / "state.json", state)


def _manifest_sha(root: Path) -> str:
    path = Path(root) / "run_manifest.json"
    if not path.is_file():
        raise EvidenceError("Sealed stage manifest is missing")
    return _sha256(path)


def _strict_probe(summary: dict, *, start_index: int, requested: int) -> None:
    sample = summary.get("sample", {})
    responses = summary.get("responses", {})
    selected = uint(sample.get("selected_ctf_outcomes"))
    statuses = responses.get("status_counts")
    if (summary.get("status") != "PROBE_COMPLETE_EVIDENCE_PENDING_REVIEW"
            or uint(sample.get("selection_start_index")) != start_index
            or not 1 <= selected <= requested
            or uint(sample.get("selection_end_index_exclusive")) != start_index + selected
            or uint(responses.get("attempted")) != selected
            or uint(responses.get("raw_saved")) != selected
            or not isinstance(statuses, dict)
            or set(statuses) - ALLOWED_PROBE_STATUSES
            or sum(uint(value) for value in statuses.values()) != selected
            or summary.get("integration") != {
                "closing_marks_written": 0,
                "basis_bundle_modified": False,
                "status": "NOT_INTEGRATED",
            }):
        raise EvidenceError("Price batch probe did not satisfy the fail-closed transport gate")


def _strict_audit(summary: dict, *, fresh: int) -> None:
    requests = summary.get("requests", {})
    reviews = summary.get("reviews", {})
    deferred_current = uint(
        reviews.get("deferred_current_official_identity_unavailable")
    )
    if (summary.get("status") != "AUDIT_COMPLETE_EVIDENCE_PENDING_REVIEW"
            or uint(summary.get("probe", {}).get("fresh_candidates")) != fresh
            or uint(requests.get("attempted")) != 4 * fresh
            or uint(requests.get("raw_saved")) != 4 * fresh
            or uint(requests.get("transport_errors")) != 0
            or uint(requests.get("parse_errors")) != 0
            or uint(reviews.get("current_official_identity_consistent")) + deferred_current != fresh
            or uint(reviews.get("eligible_for_mark_policy_review")) + deferred_current != fresh
            or uint(reviews.get("not_eligible")) != deferred_current
            or summary.get("integration") != {
                "closing_marks_written": 0,
                "basis_bundle_modified": False,
                "status": "NOT_INTEGRATED",
            }):
        raise EvidenceError("Price candidate audit did not satisfy the fail-closed identity gate")


def _strict_policy(summary: dict, *, fresh: int) -> None:
    decisions = summary.get("decisions", {})
    total = sum(uint(decisions.get(key)) for key in (
        "accepted_for_new_bundle_compilation",
        "deferred_missing_historical_identity",
        "deferred_missing_current_official_identity",
        "rejected_by_policy",
    ))
    if (summary.get("status") != "POLICY_EVALUATION_COMPLETE_NOT_INTEGRATED"
            or summary.get("settlement", {}).get("status") != "EXACT_MATCH"
            or total != fresh
            or summary.get("integration") != {
                "closing_marks_written": 0,
                "basis_bundle_modified": False,
                "status": "NOT_INTEGRATED",
            }):
        raise EvidenceError("Closing-mark policy did not satisfy the fail-closed decision gate")


def _build_policy_document(*, audit: Path, template: dict, primary_host: str,
                           secondary_host: str, policy_id: str) -> dict:
    audit_manifest = _read_json(Path(audit) / "run_manifest.json")
    audit_config = _read_json(Path(audit) / "configuration.json")
    probe = Path(audit_config.get("probe_path", "")).resolve()
    probe_config = _read_json(probe / "configuration.json")
    probe_summary = _read_json(probe / "summary.json")
    bundle = Path(probe_config.get("input_path", "")).resolve()
    bundle_manifest = bundle.parent / "run_manifest.json"
    if not bundle.is_file() or not bundle_manifest.is_file():
        raise EvidenceError("Paginator policy cannot locate the sealed basis bundle")
    root = {
        "audit_engine": AUDIT_VERSION,
        "audit_code_commit": audit_manifest.get("code_commit"),
        "audit_manifest_sha256": _sha256(Path(audit) / "run_manifest.json"),
        "audit_summary_sha256": _sha256(Path(audit) / "summary.json"),
        "probe_manifest_sha256": _sha256(probe / "run_manifest.json"),
        "evidence_gap_manifest_sha256": probe_config.get("evidence_gap_queue", {}).get(
            "manifest_sha256"
        ),
        "bundle_manifest_sha256": _sha256(bundle_manifest),
        "bundle_sha256": probe_config.get("input_sha256"),
        "bundle_digest": probe_config.get("input_digest"),
        "closing_block_number": probe_config.get("closing_block", {}).get("number"),
        "closing_block_hash": probe_config.get("closing_block", {}).get("hash"),
        "cutoff_timestamp": probe_summary.get("closing_block", {}).get(
            "as_of_cutoff_timestamp"
        ),
    }
    return {
        "schema": template["schema"],
        "policy_id": policy_id,
        "status": template["status"],
        "chain": template["chain"],
        "conditional_tokens_contract": template["conditional_tokens_contract"],
        "root_of_trust": root,
        "thresholds": deepcopy(template["thresholds"]),
        "identity": deepcopy(template["identity"]),
        "settlement": {
            **deepcopy(template["settlement"]),
            "approved_rpc_hosts": [primary_host, secondary_host],
        },
        "integration": deepcopy(template["integration"]),
        "safety": deepcopy(template["safety"]),
    }


def _attempt_number(batch_root: Path) -> int:
    numbers = []
    if batch_root.is_dir():
        for path in batch_root.iterdir():
            if path.is_dir() and path.name.startswith("attempt_"):
                suffix = path.name.removeprefix("attempt_")
                if suffix.isdigit():
                    numbers.append(int(suffix))
    return max(numbers, default=0) + 1


def _batch_id(start: int, end: int) -> str:
    return f"batch_{start + 1:05d}_{end:05d}"


def _run_attempt(*, root: Path, start_index: int, sample_size: int, target_end: int,
                 attempt_number: int, bundle: Path, evidence_gaps: Path,
                 metadata_db: Path, template_path: Path, template: dict,
                 max_age_seconds: int, primary_rpc, secondary_rpc,
                 price_client: OfficialPriceHistoryClient,
                 metadata_client: OfficialCandidateMetadataClient,
                 primary_host: str, secondary_host: str) -> dict:
    batch_id = _batch_id(start_index, target_end)
    attempt_root = root / "batches" / batch_id / f"attempt_{attempt_number:03d}"
    attempt_root.mkdir(parents=True)
    probe = attempt_root / "probe"
    probe_summary = build_price_history_probe_file(
        bundle, evidence_gaps, probe,
        rpc=primary_rpc, secondary_rpc=secondary_rpc, client=price_client,
        sample_size=sample_size, start_index=start_index,
        max_age_seconds=max_age_seconds,
    )
    _strict_probe(probe_summary, start_index=start_index, requested=sample_size)
    coverage = probe_summary["sample"]
    actual_end = uint(coverage["selection_end_index_exclusive"])
    fresh = uint(probe_summary["responses"]["fresh_candidates_at_or_before_cut"])

    audit = None
    policy_path = None
    policy_output = None
    decisions = {
        "accepted_for_new_bundle_compilation": 0,
        "deferred_missing_historical_identity": 0,
        "deferred_missing_current_official_identity": 0,
        "rejected_by_policy": 0,
    }
    settlement = {"status": "NOT_REQUIRED_NO_FRESH_CANDIDATES", "condition_count": 0}
    if fresh:
        audit = attempt_root / "audit"
        audit_summary = audit_price_candidates_file(
            probe, audit, client=metadata_client, metadata_db=metadata_db,
        )
        _strict_audit(audit_summary, fresh=fresh)
        policy_path = attempt_root / "policy.json"
        policy = _build_policy_document(
            audit=audit, template=template, primary_host=primary_host,
            secondary_host=secondary_host,
            policy_id=(
                f"car-lifetime-auto-closing-mark-policy-{start_index + 1:05d}-"
                f"{actual_end:05d}-attempt-{attempt_number:03d}-v1"
            ),
        )
        _write_new_json(policy_path, policy)
        _load_policy(policy_path)
        policy_output = attempt_root / "policy"
        policy_summary = evaluate_price_policy_file(
            audit, policy_path, policy_output,
            primary_rpc=primary_rpc, secondary_rpc=secondary_rpc,
        )
        _strict_policy(policy_summary, fresh=fresh)
        decisions = policy_summary["decisions"]
        settlement = policy_summary["settlement"]

    if any(attempt_root.rglob("closing_marks.json")):
        raise EvidenceError("Paginator stage unexpectedly created closing_marks.json")

    statuses = probe_summary["responses"]["status_counts"]
    result = {
        "schema": PAGINATOR_SCHEMA,
        "engine": PAGINATOR_VERSION,
        "batch_id": batch_id,
        "attempt": attempt_number,
        "completed_at": now_utc(),
        "selection_start_index": start_index,
        "selection_end_index_exclusive": actual_end,
        "eligible_ctf_outcomes": uint(coverage["eligible_ctf_outcomes"]),
        "probe": {
            "path": str(probe),
            "manifest_sha256": _manifest_sha(probe),
            "summary_sha256": _sha256(probe / "summary.json"),
            "selected_ctf_outcomes": uint(coverage["selected_ctf_outcomes"]),
            "status_counts": statuses,
            "fresh_candidates": fresh,
        },
        "audit": {
            "required": bool(fresh),
            "path": str(audit) if audit else None,
            "manifest_sha256": _manifest_sha(audit) if audit else None,
            "summary_sha256": _sha256(audit / "summary.json") if audit else None,
            "candidates_audited": fresh,
        },
        "policy": {
            "required": bool(fresh),
            "template_path": str(template_path),
            "template_sha256": _sha256(template_path),
            "configuration_path": str(policy_path) if policy_path else None,
            "configuration_sha256": _sha256(policy_path) if policy_path else None,
            "output_path": str(policy_output) if policy_output else None,
            "manifest_sha256": _manifest_sha(policy_output) if policy_output else None,
            "summary_sha256": (
                _sha256(policy_output / "summary.json") if policy_output else None
            ),
            "settlement": settlement,
            "decisions": decisions,
        },
        "integration": {
            "closing_marks_written": 0,
            "basis_bundle_modified": False,
            "status": "NOT_INTEGRATED",
        },
        "safety": SAFETY,
    }
    return result


def _verified_results(root: Path, *, start_index: int) -> list[dict]:
    by_start = {}
    run_configuration = _read_json(root / "configuration.json")
    batches_root = root / "batches"
    if batches_root.is_dir():
        for path in batches_root.rglob("batch_result.json"):
            attempt_root = path.parent.resolve()
            batch_root = attempt_root.parent
            if ((attempt_root / "orchestrator_failure.json").exists()
                    or batch_root.parent.resolve() != batches_root.resolve()):
                continue
            result = _read_json(path)
            if (result.get("schema") != PAGINATOR_SCHEMA
                    or result.get("engine") != PAGINATOR_VERSION
                    or result.get("integration") != {
                        "closing_marks_written": 0,
                        "basis_bundle_modified": False,
                        "status": "NOT_INTEGRATED",
                    }):
                raise EvidenceError("Paginator batch result is invalid")
            start = uint(result.get("selection_start_index"))
            end = uint(result.get("selection_end_index_exclusive"))
            if start in by_start or end <= start:
                raise EvidenceError("Paginator has duplicate or invalid completed batches")
            attempt = uint(result.get("attempt"))
            batch_parts = batch_root.name.split("_")
            if (result.get("batch_id") != batch_root.name
                    or attempt_root.name != f"attempt_{attempt:03d}"
                    or len(batch_parts) != 3 or batch_parts[0] != "batch"
                    or not batch_parts[1].isdigit() or not batch_parts[2].isdigit()
                    or int(batch_parts[1]) != start + 1
                    or not end <= int(batch_parts[2]) <= start + uint(
                        run_configuration["batch_size"]
                    )):
                raise EvidenceError("Paginator result is not linked to its batch and attempt")
            expected_probe = attempt_root / "probe"
            probe_value = result.get("probe", {}).get("path")
            if not isinstance(probe_value, str) or Path(probe_value).resolve() != expected_probe:
                raise EvidenceError("Paginator probe path escapes its completed attempt")
            probe_root = expected_probe
            if _sha256(probe_root / "summary.json") != result["probe"]["summary_sha256"]:
                raise EvidenceError("Paginator completed probe summary hash changed")
            _verify_sealed_directory(
                probe_root,
                expected_manifest_sha256=result["probe"]["manifest_sha256"],
                expected_engine=PROBE_VERSION,
            )
            probe_summary = _read_json(Path(result["probe"]["path"]) / "summary.json")
            if (probe_summary.get("sample", {}).get("selection_start_index") != start
                    or probe_summary.get("sample", {}).get("selection_end_index_exclusive") != end
                    or probe_summary.get("sample", {}).get("eligible_ctf_outcomes")
                    != result.get("eligible_ctf_outcomes")
                    or probe_summary.get("sample", {}).get("selected_ctf_outcomes")
                    != result["probe"].get("selected_ctf_outcomes")
                    or probe_summary.get("responses", {}).get("status_counts")
                    != result["probe"].get("status_counts")
                    or probe_summary.get("responses", {}).get(
                        "fresh_candidates_at_or_before_cut"
                    ) != result["probe"].get("fresh_candidates")):
                raise EvidenceError("Paginator batch result conflicts with its sealed probe")
            audit = result["audit"]
            if audit.get("required") is not bool(result["probe"].get("fresh_candidates")):
                raise EvidenceError("Paginator audit requirement conflicts with its probe")
            if audit["required"]:
                audit_value = audit.get("path")
                audit_root = attempt_root / "audit"
                if not isinstance(audit_value, str) or Path(audit_value).resolve() != audit_root:
                    raise EvidenceError("Paginator audit path escapes its completed attempt")
                if _sha256(audit_root / "summary.json") != audit["summary_sha256"]:
                    raise EvidenceError("Paginator completed audit summary hash changed")
                _verify_sealed_directory(
                    audit_root,
                    expected_manifest_sha256=audit["manifest_sha256"],
                    expected_engine=AUDIT_VERSION,
                )
                audit_summary = _read_json(audit_root / "summary.json")
                if (audit_summary.get("probe", {}).get("fresh_candidates")
                        != audit.get("candidates_audited")):
                    raise EvidenceError("Paginator batch result conflicts with its sealed audit")
            elif (result["probe"].get("fresh_candidates") != 0
                  or audit.get("path") is not None
                  or audit.get("candidates_audited") != 0):
                raise EvidenceError("Paginator skipped a required candidate audit")
            policy = result["policy"]
            template_path = Path(policy["template_path"])
            if (str(template_path) != run_configuration["policy_template_path"]
                    or policy["template_sha256"] != run_configuration["policy_template_sha256"]
                    or _sha256(template_path) != policy["template_sha256"]):
                raise EvidenceError("Paginator batch policy template changed")
            if policy.get("required") is not bool(result["probe"].get("fresh_candidates")):
                raise EvidenceError("Paginator policy requirement conflicts with its probe")
            if policy["required"]:
                policy_output_value = policy.get("output_path")
                policy_config_value = policy.get("configuration_path")
                policy_root = attempt_root / "policy"
                policy_config = attempt_root / "policy.json"
                if (not isinstance(policy_output_value, str)
                        or not isinstance(policy_config_value, str)
                        or Path(policy_output_value).resolve() != policy_root
                        or Path(policy_config_value).resolve() != policy_config):
                    raise EvidenceError("Paginator policy path escapes its completed attempt")
                policy_config_sha = _sha256(policy_config)
                if (_sha256(policy_root / "summary.json") != policy["summary_sha256"]
                        or policy_config_sha != policy["configuration_sha256"]):
                    raise EvidenceError("Paginator completed policy hash changed")
                _, _, policy_manifest = _verify_sealed_directory(
                    policy_root,
                    expected_manifest_sha256=policy["manifest_sha256"],
                    expected_engine=POLICY_VERSION,
                )
                if policy_manifest.get("policy_sha256") != policy_config_sha:
                    raise EvidenceError("Paginator policy output is not linked to its configuration")
                policy_summary = _read_json(policy_root / "summary.json")
                if (policy_summary.get("decisions") != policy.get("decisions")
                        or policy_summary.get("settlement") != policy.get("settlement")):
                    raise EvidenceError("Paginator batch result conflicts with its sealed policy")
            elif (result["probe"].get("fresh_candidates") != 0
                  or policy.get("configuration_path") is not None
                  or policy.get("output_path") is not None
                  or policy.get("decisions") not in ({
                          "accepted_for_new_bundle_compilation": 0,
                          "deferred_missing_historical_identity": 0,
                          "rejected_by_policy": 0,
                      }, {
                          "accepted_for_new_bundle_compilation": 0,
                          "deferred_missing_historical_identity": 0,
                          "deferred_missing_current_official_identity": 0,
                          "rejected_by_policy": 0,
                      })
                  or policy.get("settlement") != {
                      "status": "NOT_REQUIRED_NO_FRESH_CANDIDATES",
                      "condition_count": 0,
                  }):
                raise EvidenceError("Paginator skipped a required closing-mark policy")
            by_start[start] = result
    ordered = [by_start[key] for key in sorted(by_start)]
    cursor = start_index
    for result in ordered:
        if uint(result["selection_start_index"]) != cursor:
            raise EvidenceError("Paginator completed batches are not contiguous")
        cursor = uint(result["selection_end_index_exclusive"])
    return ordered


def _reconcile_state(root: Path, state: dict) -> list[dict]:
    results = _verified_results(root, start_index=uint(state["start_index"]))
    aggregate = _new_state(uint(state["start_index"]))["aggregate"]
    eligible = None
    cursor = uint(state["start_index"])
    for result in results:
        probe = result["probe"]
        policy = result["policy"]
        statuses = probe["status_counts"]
        selected = uint(probe["selected_ctf_outcomes"])
        aggregate["outcomes_examined"] += selected
        aggregate["fresh_candidates"] += uint(probe["fresh_candidates"])
        aggregate["stale_candidates"] += uint(statuses.get("STALE_CANDIDATE", 0))
        aggregate["no_observation"] += uint(statuses.get("NO_OBSERVATION", 0))
        aggregate["candidates_audited"] += uint(result["audit"]["candidates_audited"])
        aggregate["accepted_for_new_bundle_compilation"] += uint(
            policy["decisions"]["accepted_for_new_bundle_compilation"]
        )
        aggregate["deferred_missing_historical_identity"] += uint(
            policy["decisions"]["deferred_missing_historical_identity"]
        )
        aggregate["deferred_missing_current_official_identity"] += uint(
            policy["decisions"].get("deferred_missing_current_official_identity", 0)
        )
        aggregate["rejected_by_policy"] += uint(
            policy["decisions"]["rejected_by_policy"]
        )
        if not uint(probe["fresh_candidates"]):
            aggregate["batches_without_fresh_candidates"] += 1
        eligible_value = uint(result["eligible_ctf_outcomes"])
        if eligible is not None and eligible != eligible_value:
            raise EvidenceError("Eligible CTF queue changed between completed batches")
        eligible = eligible_value
        cursor = uint(result["selection_end_index_exclusive"])
    state["aggregate"] = aggregate
    state["batches_completed"] = len(results)
    state["next_start_index"] = cursor
    state["eligible_ctf_outcomes"] = eligible
    configured_stop = state.get("configured_stop_index_exclusive")
    if eligible is not None:
        state["effective_stop_index_exclusive"] = min(
            eligible, uint(configured_stop) if configured_stop is not None else eligible
        )
    state["active_batch"] = None
    return results


def _seal_complete(root: Path, state: dict, results: list[dict], *, code_commit: str,
                   working_tree_clean: bool) -> None:
    summary_path = root / "summary.json"
    manifest_path = root / "run_manifest.json"
    summary = {
        "schema": PAGINATOR_SCHEMA,
        "engine": PAGINATOR_VERSION,
        "status": "PAGINATION_COMPLETE_EVIDENCE_NOT_INTEGRATED",
        "coverage": {
            "start_index": state["start_index"],
            "stop_index_exclusive": state["effective_stop_index_exclusive"],
            "eligible_ctf_outcomes": state["eligible_ctf_outcomes"],
            "outcomes_examined": state["aggregate"]["outcomes_examined"],
            "batches_completed": state["batches_completed"],
        },
        "aggregate": state["aggregate"],
        "integration": state["integration"],
        "review_gate": _state_receipt(root, state)["review_gate"],
        "safety": SAFETY,
    }
    if summary_path.exists():
        if _read_json(summary_path) != summary:
            raise EvidenceError("Existing paginator summary conflicts with completed state")
    else:
        _write_new_json(summary_path, summary)
    batch_hashes = {
        result["batch_id"]: _sha256(
            Path(result["probe"]["path"]).parent / "batch_result.json"
        )
        for result in results
    }
    manifest = {
        "schema": PAGINATOR_SCHEMA,
        "engine": PAGINATOR_VERSION,
        "completed_at": now_utc(),
        "code_commit": code_commit,
        "working_tree_clean": working_tree_clean,
        "configuration_sha256": _sha256(root / "configuration.json"),
        "state_sha256": _sha256(root / "state.json"),
        "summary_sha256": _sha256(summary_path),
        "batch_result_sha256": batch_hashes,
        "safety": SAFETY,
    }
    if manifest_path.exists():
        existing = _read_json(manifest_path)
        stable_keys = {key: value for key, value in manifest.items() if key != "completed_at"}
        prior_stable = {key: value for key, value in existing.items() if key != "completed_at"}
        if prior_stable != stable_keys:
            raise EvidenceError("Existing paginator manifest conflicts with completed state")
    else:
        _write_new_json(manifest_path, manifest)


def _configuration(*, bundle: Path, evidence_gaps: Path, metadata_db_source: Path,
                   metadata_db_snapshot: Path,
                   template_path: Path, start_index: int, stop_index: int | None,
                   batch_size: int, max_age_seconds: int, max_attempts: int,
                   retry_delay_seconds: int, batch_delay_seconds: int,
                   primary_host: str, secondary_host: str, code_commit: str,
                   working_tree_clean: bool) -> dict:
    return {
        "schema": PAGINATOR_SCHEMA,
        "engine": PAGINATOR_VERSION,
        "created_at": now_utc(),
        "code_commit": code_commit,
        "working_tree_clean": working_tree_clean,
        "bundle_path": str(bundle),
        "bundle_sha256": _sha256(bundle),
        "bundle_manifest_sha256": _sha256(bundle.parent / "run_manifest.json"),
        "evidence_gaps_path": str(evidence_gaps),
        "evidence_gaps_manifest_sha256": _sha256(evidence_gaps / "run_manifest.json"),
        "metadata_db_source_path": str(metadata_db_source),
        "metadata_db_source_sha256": _sha256(metadata_db_source),
        "metadata_db_path": str(metadata_db_snapshot),
        "metadata_db_sha256": _sha256(metadata_db_snapshot),
        "policy_template_path": str(template_path),
        "policy_template_sha256": _sha256(template_path),
        "start_index": start_index,
        "stop_index_exclusive": stop_index,
        "batch_size": batch_size,
        "maximum_observation_age_seconds": max_age_seconds,
        "max_attempts_per_invocation": max_attempts,
        "retry_delay_seconds": retry_delay_seconds,
        "batch_delay_seconds": batch_delay_seconds,
        "rpc_hosts": [primary_host, secondary_host],
        "integration": "NONE; paginator cannot write marks or modify the bundle",
        "safety": SAFETY,
    }


def _validate_resume_configuration(existing: dict, expected: dict) -> None:
    # The source DB may continue evolving; all batches read the frozen snapshot.
    ignored = {"created_at", "metadata_db_source_sha256"}
    left = {key: value for key, value in existing.items() if key not in ignored}
    right = {key: value for key, value in expected.items() if key not in ignored}
    if left != right:
        raise EvidenceError("Paginator resume arguments or sealed inputs changed")


def _verify_inputs_unchanged(configuration: dict) -> None:
    bundle = Path(configuration["bundle_path"])
    evidence_gaps = Path(configuration["evidence_gaps_path"])
    metadata_db = Path(configuration["metadata_db_path"])
    policy_template = Path(configuration["policy_template_path"])
    if (not metadata_db.is_file()
            or _sha256(bundle) != configuration["bundle_sha256"]
            or _sha256(bundle.parent / "run_manifest.json")
            != configuration["bundle_manifest_sha256"]
            or _sha256(evidence_gaps / "run_manifest.json")
            != configuration["evidence_gaps_manifest_sha256"]
            or _sha256(metadata_db) != configuration["metadata_db_sha256"]
            or _sha256(policy_template) != configuration["policy_template_sha256"]):
        raise EvidenceError("A sealed paginator input changed during execution")


def run_price_batch_paginator(
    *, bundle: Path, evidence_gaps: Path, metadata_db: Path,
    policy_template: Path, output: Path, primary_rpc, secondary_rpc,
    price_client: OfficialPriceHistoryClient,
    metadata_client: OfficialCandidateMetadataClient,
    start_index: int, stop_index: int | None = None,
    batch_size: int = MAX_SAMPLE_SIZE, max_age_seconds: int = 900,
    max_attempts: int = 3, retry_delay_seconds: int = 2,
    batch_delay_seconds: int = 1, resume: bool = False,
    retry_blocked: bool = False, max_batches: int | None = None,
    require_clean_git: bool = True,
) -> dict:
    """Run or resume contiguous sealed batches; never skip a failed batch."""
    bundle = Path(bundle).resolve()
    evidence_gaps = Path(evidence_gaps).resolve()
    metadata_db = Path(metadata_db).resolve()
    policy_template = Path(policy_template).resolve()
    root = Path(output).resolve()
    if (type(start_index) is not int or start_index < 0
            or (stop_index is not None and (type(stop_index) is not int or stop_index <= start_index))
            or type(batch_size) is not int or not 1 <= batch_size <= MAX_SAMPLE_SIZE
            or type(max_age_seconds) is not int or max_age_seconds < 0
            or type(max_attempts) is not int or not 1 <= max_attempts <= 5
            or type(retry_delay_seconds) is not int or not 0 <= retry_delay_seconds <= 60
            or type(batch_delay_seconds) is not int or not 0 <= batch_delay_seconds <= 60
            or (max_batches is not None and (type(max_batches) is not int or max_batches < 1))):
        raise EvidenceError("Price paginator bounds are invalid")
    if not bundle.is_file() or not evidence_gaps.is_dir() or not metadata_db.is_file():
        raise EvidenceError("Price paginator sealed inputs are missing")
    if not (evidence_gaps / "run_manifest.json").is_file() or not policy_template.is_file():
        raise EvidenceError("Price paginator manifest or policy template is missing")
    if not (bundle.parent / "run_manifest.json").is_file():
        raise EvidenceError("Price paginator basis-bundle manifest is missing")
    for sealed_root in (bundle.parent, evidence_gaps):
        if root == sealed_root or root.is_relative_to(sealed_root):
            raise EvidenceError("Price paginator output cannot be inside sealed evidence")
    if root.exists() and not root.is_dir():
        raise EvidenceError("Price paginator output must be a directory")
    output_existed = root.exists()
    if output_existed and not resume:
        raise EvidenceError("Price paginator output already exists; use --resume explicitly")
    if not output_existed and resume:
        raise EvidenceError("Price paginator output does not exist for resume")
    template = _load_policy_template(policy_template)
    if max_age_seconds != uint(template["thresholds"]["maximum_age_seconds"]):
        raise EvidenceError("Paginator freshness bound must equal the approved policy template")
    primary_host = _validate_rpc_endpoint(primary_rpc)
    secondary_host = _validate_rpc_endpoint(secondary_rpc)
    if primary_host == secondary_host:
        raise EvidenceError("Price paginator requires two independent RPC hosts")
    project = Path(__file__).resolve().parents[3]
    code_commit, working_tree_clean = _git_state(project)
    if require_clean_git and (not working_tree_clean or len(code_commit) != 40):
        raise EvidenceError("Price paginator requires a clean, committed Git working tree")
    metadata_snapshot = root / "inputs" / "metadata_snapshot.db"
    if not output_existed:
        _require_empty_sqlite_wal(metadata_db)
        root.mkdir(parents=True)
        _create_metadata_snapshot(metadata_db, metadata_snapshot)
    expected_config = _configuration(
        bundle=bundle, evidence_gaps=evidence_gaps,
        metadata_db_source=metadata_db, metadata_db_snapshot=metadata_snapshot,
        template_path=policy_template, start_index=start_index, stop_index=stop_index,
        batch_size=batch_size, max_age_seconds=max_age_seconds,
        max_attempts=max_attempts, retry_delay_seconds=retry_delay_seconds,
        batch_delay_seconds=batch_delay_seconds, primary_host=primary_host,
        secondary_host=secondary_host, code_commit=code_commit,
        working_tree_clean=working_tree_clean,
    )
    if not (root / "configuration.json").exists():
        _write_new_json(root / "configuration.json", expected_config)
        state = _new_state(start_index)
        state["configured_stop_index_exclusive"] = stop_index
        _write_new_json(root / "state.json", state)

    with _exclusive_run_lock(root):
        existing_config = _read_json(root / "configuration.json")
        _validate_resume_configuration(existing_config, expected_config)
        _verify_inputs_unchanged(existing_config)
        state = _read_json(root / "state.json")
        if state.get("engine") != PAGINATOR_VERSION:
            raise EvidenceError("Price paginator state engine is unsupported")
        results = _reconcile_state(root, state)
        if state.get("status") == "COMPLETE":
            _seal_complete(
                root, state, results, code_commit=code_commit,
                working_tree_clean=working_tree_clean,
            )
            return _state_receipt(root, state)
        if state.get("status") == "BLOCKED" and not retry_blocked:
            raise EvidenceError("Paginator is blocked; inspect evidence and use --retry-blocked")
        state["status"] = "RUNNING"
        state["last_error"] = None
        _save_state(root, state)
        completed_this_invocation = 0

        while True:
            cursor = uint(state["next_start_index"])
            effective_stop = state.get("effective_stop_index_exclusive")
            if effective_stop is not None and cursor >= uint(effective_stop):
                state["status"] = "COMPLETE"
                _save_state(root, state)
                results = _reconcile_state(root, state)
                _save_state(root, state)
                _seal_complete(
                    root, state, results, code_commit=code_commit,
                    working_tree_clean=working_tree_clean,
                )
                return _state_receipt(root, state)
            if max_batches is not None and completed_this_invocation >= max_batches:
                state["status"] = "PAUSED_BATCH_LIMIT"
                _save_state(root, state)
                return _state_receipt(root, state)

            requested = batch_size
            if effective_stop is not None:
                requested = min(requested, uint(effective_stop) - cursor)
            target_end = cursor + requested
            batch_id = _batch_id(cursor, target_end)
            batch_root = root / "batches" / batch_id
            state["active_batch"] = {
                "batch_id": batch_id,
                "selection_start_index": cursor,
                "target_end_index_exclusive": target_end,
                "stage": "STARTING",
            }
            _save_state(root, state)
            succeeded = False
            for invocation_attempt in range(1, max_attempts + 1):
                attempt_number = _attempt_number(batch_root)
                state["attempts_started"] = uint(state["attempts_started"]) + 1
                state["active_batch"]["attempt"] = attempt_number
                state["active_batch"]["invocation_attempt"] = invocation_attempt
                state["active_batch"]["stage"] = "PROBE_AUDIT_POLICY"
                _save_state(root, state)
                try:
                    _verify_inputs_unchanged(existing_config)
                    result = _run_attempt(
                        root=root, start_index=cursor, sample_size=requested,
                        target_end=target_end, attempt_number=attempt_number,
                        bundle=bundle, evidence_gaps=evidence_gaps,
                        metadata_db=Path(existing_config["metadata_db_path"]),
                        template_path=policy_template,
                        template=template, max_age_seconds=max_age_seconds,
                        primary_rpc=primary_rpc, secondary_rpc=secondary_rpc,
                        price_client=price_client, metadata_client=metadata_client,
                        primary_host=primary_host, secondary_host=secondary_host,
                    )
                    _verify_inputs_unchanged(existing_config)
                    _write_new_json(
                        Path(result["probe"]["path"]).parent / "batch_result.json",
                        result,
                    )
                    eligible = uint(result["eligible_ctf_outcomes"])
                    configured_stop = state.get("configured_stop_index_exclusive")
                    effective = min(
                        eligible,
                        uint(configured_stop) if configured_stop is not None else eligible,
                    )
                    if effective <= uint(state["start_index"]):
                        raise EvidenceError("Paginator effective stop does not advance the cursor")
                    state["eligible_ctf_outcomes"] = eligible
                    state["effective_stop_index_exclusive"] = effective
                    results = _reconcile_state(root, state)
                    state["status"] = "RUNNING"
                    state["last_error"] = None
                    _save_state(root, state)
                except Exception as exc:
                    failure = _safe_error(exc)
                    state["failed_attempts"] = uint(state["failed_attempts"]) + 1
                    state["last_error"] = failure
                    state["active_batch"]["stage"] = "ATTEMPT_FAILED"
                    state["active_batch"]["last_error"] = failure
                    attempt_root = batch_root / f"attempt_{attempt_number:03d}"
                    failure_path = attempt_root / "orchestrator_failure.json"
                    if attempt_root.is_dir() and not failure_path.exists():
                        _write_new_json(failure_path, {
                            "schema": PAGINATOR_SCHEMA,
                            "engine": PAGINATOR_VERSION,
                            "status": "ATTEMPT_FAILED_NOT_SKIPPED",
                            "batch_id": batch_id,
                            "attempt": attempt_number,
                            "error": failure,
                            "integration": state["integration"],
                            "safety": SAFETY,
                        })
                    _save_state(root, state)
                    if invocation_attempt < max_attempts:
                        if retry_delay_seconds:
                            time.sleep(retry_delay_seconds * invocation_attempt)
                        continue
                    state["status"] = "BLOCKED"
                    state["active_batch"]["stage"] = "BLOCKED_AFTER_RETRIES"
                    _save_state(root, state)
                    return _state_receipt(root, state)
                else:
                    completed_this_invocation += 1
                    succeeded = True
                    break
            if not succeeded:  # pragma: no cover - loop returns when retries exhaust
                raise AssertionError("Paginator retry loop ended without a terminal state")
            if batch_delay_seconds:
                time.sleep(batch_delay_seconds)
