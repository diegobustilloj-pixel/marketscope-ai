"""Compile accepted closing-mark decisions into a sealed, non-integrated artifact.

The compiler is intentionally offline.  It verifies every supplied policy and
paginator seal, proves contiguous coverage of the eligible CTF queue, and
copies only decisions already accepted by the fail-closed policy.  It never
changes the basis bundle or writes an accounting result.
"""
from __future__ import annotations

import os
from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import SAFETY
from .common import EvidenceError, digest, now_utc, uint
from .price_batch_paginator import (
    PAGINATOR_VERSION,
    _read_json as _read_paginator_json,
    _reconcile_state,
    _verified_results,
)
from .price_candidate_audit import (
    AUDIT_VERSION,
    _files_sha256,
    _git_state,
    _load_json,
    _sha256,
)
from .price_history_probe import CTF_CONTRACT, PROBE_VERSION, _write_new
from .price_mark_policy import POLICY_VERSION, _verify_bundle_directory, _verify_sealed_directory


COMPILER_SCHEMA = 1
COMPILER_VERSION = "polyledger-closing-mark-candidate-compiler/1"
ACCEPTED = "ACCEPTED_FOR_NEW_BUNDLE_COMPILATION"
DEFERRED_HISTORICAL = "DEFERRED_MISSING_HISTORICAL_IDENTITY"
DEFERRED_CURRENT = "DEFERRED_MISSING_CURRENT_OFFICIAL_IDENTITY"
REJECTED = "REJECTED_BY_POLICY"
ALLOWED_DECISIONS = {ACCEPTED, DEFERRED_HISTORICAL, DEFERRED_CURRENT, REJECTED}


def _normalized_counts(value: dict) -> dict:
    if not isinstance(value, dict):
        raise EvidenceError("Closing-mark decision counts are invalid")
    return {
        "accepted_for_new_bundle_compilation": uint(
            value.get("accepted_for_new_bundle_compilation", 0)
        ),
        "deferred_missing_historical_identity": uint(
            value.get("deferred_missing_historical_identity", 0)
        ),
        "deferred_missing_current_official_identity": uint(
            value.get("deferred_missing_current_official_identity", 0)
        ),
        "rejected_by_policy": uint(value.get("rejected_by_policy", 0)),
    }


def _decision_counts(decisions: list[dict]) -> dict:
    statuses = Counter(row.get("status") for row in decisions if isinstance(row, dict))
    if sum(statuses.values()) != len(decisions) or set(statuses) - ALLOWED_DECISIONS:
        raise EvidenceError("Closing-mark policy contains an unsupported decision")
    return {
        "accepted_for_new_bundle_compilation": statuses[ACCEPTED],
        "deferred_missing_historical_identity": statuses[DEFERRED_HISTORICAL],
        "deferred_missing_current_official_identity": statuses[DEFERRED_CURRENT],
        "rejected_by_policy": statuses[REJECTED],
    }


def _probe_chunk(probe: Path, *, expected_manifest_sha256: str | None = None) -> tuple[dict, dict]:
    probe = Path(probe).resolve()
    manifest_sha = expected_manifest_sha256 or _sha256(probe / "run_manifest.json")
    configuration, summary, _ = _verify_sealed_directory(
        probe, expected_manifest_sha256=manifest_sha, expected_engine=PROBE_VERSION,
    )
    sample = summary.get("sample", {})
    responses = summary.get("responses", {})
    selected = uint(sample.get("selected_ctf_outcomes"))
    start = uint(sample.get("selection_start_index", 0))
    end = uint(sample.get("selection_end_index_exclusive", start + selected))
    statuses = responses.get("status_counts")
    if (summary.get("status") != "PROBE_COMPLETE_EVIDENCE_PENDING_REVIEW"
            or end != start + selected or not isinstance(statuses, dict)
            or sum(uint(value) for value in statuses.values()) != selected
            or uint(responses.get("fresh_candidates_at_or_before_cut"))
            != uint(statuses.get("FRESH_CANDIDATE", 0))):
        raise EvidenceError("Price-probe coverage cannot be compiled")
    gap = configuration.get("evidence_gap_queue", {})
    root = {
        "bundle_path": str(Path(configuration["input_path"]).resolve()),
        "bundle_sha256": configuration["input_sha256"],
        "bundle_digest": configuration["input_digest"],
        "evidence_gaps_path": str(Path(gap["path"]).resolve()),
        "evidence_gaps_manifest_sha256": gap["manifest_sha256"],
        "eligible_ctf_outcomes": uint(sample.get("eligible_ctf_outcomes")),
        "positive_non_quote_assets": uint(sample.get("positive_non_quote_assets")),
    }
    return {
        "start_index": start,
        "end_index_exclusive": end,
        "selected_ctf_outcomes": selected,
        "fresh_candidates": uint(responses.get("fresh_candidates_at_or_before_cut")),
        "status_counts": {key: uint(value) for key, value in sorted(statuses.items())},
        "probe_path": str(probe),
        "probe_manifest_sha256": manifest_sha,
    }, root


def _policy_chunk(policy_output: Path) -> tuple[dict, dict, list[dict], dict]:
    policy_output = Path(policy_output).resolve()
    manifest_sha = _sha256(policy_output / "run_manifest.json")
    configuration, summary, _ = _verify_sealed_directory(
        policy_output, expected_manifest_sha256=manifest_sha, expected_engine=POLICY_VERSION,
    )
    decisions = _load_json(policy_output / "decisions.json")
    if not isinstance(decisions, list):
        raise EvidenceError("Closing-mark decisions are not a list")
    counts = _decision_counts(decisions)
    if (summary.get("status") != "POLICY_EVALUATION_COMPLETE_NOT_INTEGRATED"
            or summary.get("integration") != {
                "closing_marks_written": 0,
                "basis_bundle_modified": False,
                "status": "NOT_INTEGRATED",
            }
            or counts != _normalized_counts(summary.get("decisions"))
            or uint(summary.get("audit", {}).get("candidate_count")) != len(decisions)):
        raise EvidenceError("Closing-mark policy summary conflicts with its decisions")

    audit = Path(configuration["audit_path"]).resolve()
    audit_config, _, _ = _verify_sealed_directory(
        audit,
        expected_manifest_sha256=configuration["audit_manifest_sha256"],
        expected_engine=AUDIT_VERSION,
    )
    probe = Path(configuration["probe_path"]).resolve()
    if (Path(audit_config["probe_path"]).resolve() != probe
            or audit_config["probe_manifest_sha256"] != configuration["probe_manifest_sha256"]):
        raise EvidenceError("Policy audit and probe roots do not agree")
    chunk, root = _probe_chunk(
        probe, expected_manifest_sha256=configuration["probe_manifest_sha256"]
    )
    if chunk["fresh_candidates"] != len(decisions):
        raise EvidenceError("Policy decision count conflicts with probe candidates")
    for decision in decisions:
        sequence = uint(decision.get("sequence"))
        if not chunk["start_index"] < sequence <= chunk["end_index_exclusive"]:
            raise EvidenceError("Policy decision sequence lies outside its probe range")
    chunk.update({
        "source_kind": "STANDALONE_POLICY",
        "policy_output_path": str(policy_output),
        "policy_manifest_sha256": manifest_sha,
        "decision_counts": counts,
    })
    root.update({
        "bundle_manifest_sha256": configuration["bundle_manifest_sha256"],
        "evidence_gaps_manifest_sha256": configuration["evidence_gap_manifest_sha256"],
    })
    source = {
        "kind": "STANDALONE_POLICY",
        "path": str(policy_output),
        "manifest_sha256": manifest_sha,
        "policy_manifest_sha256": manifest_sha,
        "start_index": chunk["start_index"],
        "end_index_exclusive": chunk["end_index_exclusive"],
    }
    return chunk, root, decisions, source


def _pagination_chunks(pagination: Path) -> tuple[list[tuple[dict, dict, list[dict], dict]], dict]:
    pagination = Path(pagination).resolve()
    configuration = _read_paginator_json(pagination / "configuration.json")
    state = _read_paginator_json(pagination / "state.json")
    if (configuration.get("engine") != PAGINATOR_VERSION
            or state.get("engine") != PAGINATOR_VERSION
            or state.get("status") not in {"BLOCKED", "COMPLETE"}):
        raise EvidenceError("Paginator segment is not a closed evidence source")
    results = _verified_results(pagination, start_index=uint(configuration["start_index"]))
    reconciled = deepcopy(state)
    _reconcile_state(pagination, reconciled)
    actual_aggregate = dict(state.get("aggregate", {}))
    actual_aggregate.setdefault("deferred_missing_current_official_identity", 0)
    if (actual_aggregate != reconciled["aggregate"]
            or uint(state.get("batches_completed")) != len(results)
            or uint(state.get("next_start_index")) != reconciled["next_start_index"]):
        raise EvidenceError("Paginator state does not reproduce from completed batches")
    if state["status"] == "COMPLETE":
        manifest = _read_paginator_json(pagination / "run_manifest.json")
        summary = _read_paginator_json(pagination / "summary.json")
        expected_batch_hashes = {
            result["batch_id"]: _sha256(
                Path(result["probe"]["path"]).parent / "batch_result.json"
            )
            for result in results
        }
        if (_sha256(pagination / "configuration.json") != manifest.get("configuration_sha256")
                or _sha256(pagination / "state.json") != manifest.get("state_sha256")
                or _sha256(pagination / "summary.json") != manifest.get("summary_sha256")
                or manifest.get("engine") != PAGINATOR_VERSION
                or manifest.get("safety") != SAFETY
                or manifest.get("batch_result_sha256") != expected_batch_hashes
                or summary.get("status") != "PAGINATION_COMPLETE_EVIDENCE_NOT_INTEGRATED"
                or summary.get("aggregate") != state.get("aggregate")
                or summary.get("coverage", {}).get("start_index") != state.get("start_index")
                or summary.get("coverage", {}).get("stop_index_exclusive")
                != state.get("effective_stop_index_exclusive")):
            raise EvidenceError("Completed paginator top-level seal is invalid")
    elif (state.get("active_batch", {}).get("stage") != "BLOCKED_AFTER_RETRIES"
          or uint(state["active_batch"].get("selection_start_index"))
          != uint(state["next_start_index"])):
        raise EvidenceError("Partial paginator segment did not stop fail-closed at its cursor")

    rows = []
    common_root = None
    for result in results:
        chunk, root = _probe_chunk(
            Path(result["probe"]["path"]),
            expected_manifest_sha256=result["probe"]["manifest_sha256"],
        )
        root["bundle_manifest_sha256"] = configuration["bundle_manifest_sha256"]
        if (chunk["start_index"] != uint(result["selection_start_index"])
                or chunk["end_index_exclusive"]
                != uint(result["selection_end_index_exclusive"])):
            raise EvidenceError("Paginator probe range conflicts with batch result")
        decisions = []
        policy_manifest = None
        policy_path = None
        counts = _normalized_counts(result["policy"]["decisions"])
        if result["policy"]["required"]:
            policy_path = Path(result["policy"]["output_path"]).resolve()
            _, policy_root, decisions, _ = _policy_chunk(policy_path)
            policy_manifest = result["policy"]["manifest_sha256"]
            if policy_root != root or _decision_counts(decisions) != counts:
                raise EvidenceError("Paginator policy root or decisions conflict with its batch")
        elif chunk["fresh_candidates"]:
            raise EvidenceError("Paginator omitted policy decisions for fresh candidates")
        chunk.update({
            "source_kind": "PAGINATOR_BATCH",
            "paginator_path": str(pagination),
            "batch_id": result["batch_id"],
            "policy_output_path": str(policy_path) if policy_path else None,
            "policy_manifest_sha256": policy_manifest,
            "decision_counts": counts,
        })
        source = {
            "kind": "PAGINATOR_BATCH",
            "path": str(pagination),
            "batch_id": result["batch_id"],
            "policy_manifest_sha256": policy_manifest,
            "start_index": chunk["start_index"],
            "end_index_exclusive": chunk["end_index_exclusive"],
        }
        rows.append((chunk, root, decisions, source))
        if common_root is None:
            common_root = root
        elif common_root != root:
            raise EvidenceError("Paginator batches do not share one root of trust")
    if not rows:
        raise EvidenceError("Paginator segment has no completed batches")
    return rows, {
        "kind": "PAGINATOR",
        "path": str(pagination),
        "configuration_sha256": _sha256(pagination / "configuration.json"),
        "state_sha256": _sha256(pagination / "state.json"),
        "manifest_sha256": (
            _sha256(pagination / "run_manifest.json")
            if (pagination / "run_manifest.json").is_file() else None
        ),
        "start_index": uint(configuration["start_index"]),
        "end_index_exclusive": uint(state["next_start_index"]),
        "status": state["status"],
    }


def _validated_accepted_mark(decision: dict, requests: dict[str, int]) -> tuple[str, str]:
    asset = decision.get("asset")
    observation = decision.get("price_candidate")
    checks = decision.get("checks")
    settlement = decision.get("settlement_at_closing_block", {})
    integration = decision.get("integration")
    token_id = decision.get("token_id")
    try:
        price = Decimal(observation.get("price"))
    except (AttributeError, InvalidOperation, TypeError, ValueError) as exc:
        raise EvidenceError("Accepted closing mark has an invalid price") from exc
    if (not isinstance(token_id, str) or not token_id.isdigit()
            or asset != f"137:{CTF_CONTRACT}:{token_id}" or asset not in requests
            or decision.get("status") != ACCEPTED or decision.get("reasons") != []
            or decision.get("outcome_index") not in {0, 1}
            or not isinstance(decision.get("outcome"), str) or not decision["outcome"]
            or not isinstance(checks, dict) or not checks or not all(value is True for value in checks.values())
            or settlement.get("status") != "UNRESOLVED_AT_CLOSING_BLOCK"
            or integration != {"closing_mark_written": False,
                               "basis_bundle_modified": False,
                               "automatic_approval": False}
            or not price.is_finite() or not Decimal(0) < price < Decimal(1)):
        raise EvidenceError("Accepted closing mark does not satisfy the compilation gate")
    return asset, str(observation["price"])


def compile_closing_mark_candidates_file(bundle: Path, evidence_gaps: Path,
                                         policy_outputs: list[Path],
                                         paginations: list[Path], output: Path, *,
                                         require_clean_git: bool = True) -> dict:
    """Seal accepted marks from contiguous policy evidence without integrating them."""
    bundle = Path(bundle).resolve()
    evidence_gaps = Path(evidence_gaps).resolve()
    output = Path(output).resolve()
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists():
        raise EvidenceError("Closing-mark compilation output or partial directory already exists")
    if not policy_outputs and not paginations:
        raise EvidenceError("Closing-mark compilation requires at least one evidence source")
    project = Path(__file__).resolve().parents[3]
    commit, clean = _git_state(project)
    if require_clean_git and (commit == "unavailable" or not clean):
        raise EvidenceError("Closing-mark compilation requires a clean Git commit")

    gap_manifest_sha = _sha256(evidence_gaps / "run_manifest.json")
    gap_config, gap_summary, _ = _verify_sealed_directory(
        evidence_gaps, expected_manifest_sha256=gap_manifest_sha,
        expected_engine="basis-evidence-gap-audit/1",
    )
    request_rows = _load_json(evidence_gaps / "closing_mark_requests.json")
    if not isinstance(request_rows, list):
        raise EvidenceError("Closing-mark request queue is invalid")
    requests = {}
    for row in request_rows:
        if (not isinstance(row, dict) or not isinstance(row.get("asset"), str)
                or row["asset"] in requests):
            raise EvidenceError("Closing-mark request queue has duplicate or invalid assets")
        quantity = uint(row["quantity_atomic"])
        if quantity == 0:
            raise EvidenceError("Closing-mark request queue contains a zero quantity")
        requests[row["asset"]] = quantity
    if (uint(gap_summary.get("closing_marks", {}).get("requests")) != len(requests)
            or gap_config.get("input_sha256") != _sha256(bundle)):
        raise EvidenceError("Closing-mark request queue conflicts with the basis bundle")

    compiled = []
    sources = []
    for path in policy_outputs:
        chunk, root, decisions, source = _policy_chunk(path)
        compiled.append((chunk, root, decisions, source))
        sources.append(source)
    for path in paginations:
        rows, source = _pagination_chunks(path)
        compiled.extend(rows)
        sources.append(source)
    compiled.sort(key=lambda item: item[0]["start_index"])
    cursor = 0
    canonical_root = None
    for chunk, root, _, _ in compiled:
        if chunk["start_index"] != cursor:
            raise EvidenceError("Closing-mark evidence coverage has a gap or overlap")
        cursor = chunk["end_index_exclusive"]
        if canonical_root is None:
            canonical_root = root
        elif canonical_root != root:
            raise EvidenceError("Closing-mark evidence sources do not share one root of trust")
    if canonical_root is None or cursor != canonical_root["eligible_ctf_outcomes"]:
        raise EvidenceError("Closing-mark evidence does not cover the complete eligible CTF queue")
    if (Path(canonical_root["bundle_path"]).resolve() != bundle
            or Path(canonical_root["evidence_gaps_path"]).resolve() != evidence_gaps
            or canonical_root["bundle_sha256"] != gap_config["input_sha256"]
            or canonical_root["bundle_digest"] != gap_config["input_digest"]
            or canonical_root["evidence_gaps_manifest_sha256"] != gap_manifest_sha):
        raise EvidenceError("Closing-mark evidence root does not match requested inputs")
    _verify_bundle_directory(
        bundle, manifest_sha256=canonical_root["bundle_manifest_sha256"],
        bundle_sha256=canonical_root["bundle_sha256"],
        bundle_digest=canonical_root["bundle_digest"],
    )

    marks = {}
    mark_evidence = {}
    decisions_output = []
    aggregate = Counter()
    coverage = []
    seen_assets = set()
    seen_sequences = set()
    for chunk, _, decisions, source in compiled:
        coverage.append(chunk)
        for decision in decisions:
            asset = decision.get("asset")
            sequence = uint(decision.get("sequence"))
            if (not isinstance(asset, str) or asset in seen_assets
                    or sequence in seen_sequences):
                raise EvidenceError("Closing-mark decisions contain duplicate identities")
            seen_assets.add(asset)
            seen_sequences.add(sequence)
            aggregate[decision["status"]] += 1
            decision_copy = deepcopy(decision)
            decision_copy["compilation_source"] = source
            decisions_output.append(decision_copy)
            if decision["status"] != ACCEPTED:
                continue
            asset, price = _validated_accepted_mark(decision, requests)
            if asset in marks:
                raise EvidenceError("One closing-mark asset was accepted more than once")
            marks[asset] = price
            mark_evidence[asset] = {
                "policy_manifest_sha256": chunk["policy_manifest_sha256"],
                "policy_output_path": chunk["policy_output_path"],
                "probe_manifest_sha256": chunk["probe_manifest_sha256"],
                "sequence": decision["sequence"],
                "condition_id": decision["condition_id"],
                "outcome": decision["outcome"],
                "outcome_index": decision["outcome_index"],
                "observation": decision["price_candidate"],
                "settlement_status": decision["settlement_at_closing_block"]["status"],
            }
    counts = {
        "accepted_for_new_bundle_compilation": aggregate[ACCEPTED],
        "deferred_missing_historical_identity": aggregate[DEFERRED_HISTORICAL],
        "deferred_missing_current_official_identity": aggregate[DEFERRED_CURRENT],
        "rejected_by_policy": aggregate[REJECTED],
    }
    if sum(counts.values()) != len(decisions_output) or len(marks) != counts[
            "accepted_for_new_bundle_compilation"]:
        raise EvidenceError("Compiled closing-mark totals do not reconcile")

    requested = len(requests)
    summary = {
        "schema": COMPILER_SCHEMA,
        "engine": COMPILER_VERSION,
        "status": "COMPILATION_COMPLETE_NOT_INTEGRATED",
        "coverage": {
            "start_index": 0,
            "stop_index_exclusive": cursor,
            "eligible_ctf_outcomes": canonical_root["eligible_ctf_outcomes"],
            "positive_non_quote_assets": requested,
            "chunks": len(coverage),
            "candidate_decisions": len(decisions_output),
        },
        "decisions": counts,
        "marks": {
            "requested": requested,
            "proposed": len(marks),
            "remaining_without_accepted_mark": requested - len(marks),
        },
        "integration": {
            "closing_marks_written_to_bundle": 0,
            "basis_bundle_modified": False,
            "status": "NOT_INTEGRATED",
        },
        "review_gate": {"status": "BLOCKED", "reasons": [
            "CLOSING_MARK_COVERAGE_INCOMPLETE",
            "PROPOSED_MARKS_NOT_INTEGRATED",
            "EXTERNAL_FLOW_VALUE_EVIDENCE_REMAINS_OPEN",
            "INDEPENDENT_ACCOUNTING_REPORT_MISSING",
        ]},
        "safety": SAFETY,
    }
    configuration = {
        "schema": COMPILER_SCHEMA,
        "engine": COMPILER_VERSION,
        "created_at": now_utc(),
        "code_commit": commit,
        "working_tree_clean": clean,
        "bundle_path": str(bundle),
        "bundle_sha256": canonical_root["bundle_sha256"],
        "bundle_digest": canonical_root["bundle_digest"],
        "bundle_manifest_sha256": canonical_root["bundle_manifest_sha256"],
        "evidence_gaps_path": str(evidence_gaps),
        "evidence_gaps_manifest_sha256": gap_manifest_sha,
        "sources": sources,
        "integration": "NONE; proposed marks are not written to the basis bundle",
        "safety": SAFETY,
    }
    bundle_before = _sha256(bundle)
    source_seals_before = {
        source["path"]: {
            key: value for key, value in source.items()
            if key in {"manifest_sha256", "configuration_sha256", "state_sha256"}
            and value is not None
        }
        for source in sources
    }
    partial.mkdir(parents=True)
    _write_new(partial / "configuration.json", configuration)
    _write_new(partial / "coverage.json", coverage)
    _write_new(partial / "decisions.json", decisions_output)
    _write_new(partial / "closing_marks.json", dict(sorted(marks.items())))
    _write_new(partial / "marks_evidence.json", dict(sorted(mark_evidence.items())))
    _write_new(partial / "summary.json", summary)
    if (_sha256(bundle) != bundle_before
            or bundle_before != canonical_root["bundle_sha256"]
            or _sha256(evidence_gaps / "run_manifest.json") != gap_manifest_sha
            or any(
                source_seals_before[source["path"]] != {
                    key: (_sha256(Path(source["path"]) / "run_manifest.json")
                          if key == "manifest_sha256"
                          else _sha256(Path(source["path"]) / (
                              "configuration.json" if key == "configuration_sha256"
                              else "state.json")))
                    for key in source_seals_before[source["path"]]
                }
                for source in sources
            )):
        raise EvidenceError("A sealed closing-mark source changed during compilation")
    _write_new(partial / "run_manifest.json", {
        "schema": COMPILER_SCHEMA,
        "engine": COMPILER_VERSION,
        "completed_at": now_utc(),
        "code_commit": commit,
        "working_tree_clean": clean,
        "bundle_sha256": bundle_before,
        "evidence_gaps_manifest_sha256": gap_manifest_sha,
        "files": _files_sha256(partial),
        "summary_sha256": _sha256(partial / "summary.json"),
        "closing_marks_digest": digest(dict(sorted(marks.items()))),
        "safety": SAFETY,
    })
    os.replace(partial, output)
    return summary
