"""Build a sealed, fail-closed work queue for unresolved closing marks.

The queue is derived entirely from sealed compiler, probe, and evidence-gap
artifacts.  It classifies every unresolved asset exactly once and records the
permitted recovery route.  It never promotes stale observations, changes a
freshness threshold, or writes a mark into the basis bundle.
"""
from __future__ import annotations

import os
from collections import Counter
from copy import deepcopy
from pathlib import Path

from . import SAFETY
from .closing_mark_compiler import (
    ACCEPTED,
    COMPILER_VERSION,
    DEFERRED_CURRENT,
    DEFERRED_HISTORICAL,
    REJECTED,
)
from .common import EvidenceError, digest, now_utc, uint
from .price_candidate_audit import _files_sha256, _git_state, _load_json, _sha256
from .price_history_probe import (
    COMBO_CONTRACT,
    CTF_CONTRACT,
    PROBE_VERSION,
    PUSD_CONTRACT,
    _asset_parts,
    _write_new,
)
from .price_mark_policy import _verify_sealed_directory


GAP_QUEUE_SCHEMA = 1
GAP_QUEUE_VERSION = "polyledger-closing-mark-gap-queue/1"
EVIDENCE_GAP_VERSION = "basis-evidence-gap-audit/1"

STALE = "STALE_CANDIDATE"
NO_OBSERVATION = "NO_OBSERVATION"
REJECTED_POLICY = "REJECTED_BY_POLICY"
DEFERRED_HISTORICAL_IDENTITY = "DEFERRED_HISTORICAL_IDENTITY"
DEFERRED_CURRENT_IDENTITY = "DEFERRED_CURRENT_IDENTITY"
COMBO_POSITION = "COMBO_POSITION_NOT_CLOB_OUTCOME"
PUSD_COLLATERAL = "PUSD_COLLATERAL_NOT_CLOB_OUTCOME"

CATEGORY_FILES = {
    STALE: "stale_candidates.json",
    NO_OBSERVATION: "no_observation.json",
    REJECTED_POLICY: "rejected_by_policy.json",
    DEFERRED_HISTORICAL_IDENTITY: "deferred_identity.json",
    DEFERRED_CURRENT_IDENTITY: "deferred_identity.json",
    COMBO_POSITION: "excluded_non_ctf.json",
    PUSD_COLLATERAL: "excluded_non_ctf.json",
}

ACTION_POLICIES = {
    "RECOVER_FRESH_HISTORICAL_PRICE": {
        "objective": "Find a new independently verifiable observation at or before the cutoff.",
        "allowed_sources": [
            "sealed exact CLOB trade or order-book archive at the cutoff",
            "official Polymarket historical response with a qualifying near-cutoff observation",
            "exact-block CTF settlement state when settlement had already occurred",
        ],
        "prohibitions": [
            "do not reuse the stale price as a closing mark",
            "do not increase the maximum observation age",
            "do not interpolate or invent a price",
        ],
    },
    "RECOVER_MISSING_HISTORICAL_PRICE": {
        "objective": "Find an alternate sealed historical source or retain an explicit unavailable result.",
        "prohibitions": [
            "do not replace a missing observation with a current price",
            "do not infer a price from the position size or later outcome",
        ],
    },
    "RECOVER_HISTORICAL_MARKET_IDENTITY": {
        "objective": "Prove the token, condition, outcome, and market identity as they existed by the cutoff.",
        "prohibitions": ["do not accept current-only identity as historical proof"],
    },
    "RECOVER_CURRENT_OR_ARCHIVED_OFFICIAL_IDENTITY": {
        "objective": "Recover an official current or archived identity record and bind it to the historical token.",
        "prohibitions": ["do not guess the outcome index or market identity"],
    },
    "RECONSTRUCT_NEGRISK_COMPONENT_VALUE": {
        "objective": "Reconstruct the supported NegRisk component and its value without treating it as a binary CTF mark.",
        "prohibitions": ["do not pass a NegRisk position through the binary-market policy"],
    },
    "RECONSTRUCT_CTF_SETTLEMENT_VALUE": {
        "objective": "Use the exact closing-block CTF payout state because settlement supersedes price history.",
        "prohibitions": ["do not use the rejected historical candidate as the mark"],
    },
    "RECONSTRUCT_MARKET_WINDOW": {
        "objective": "Prove the declared market window and cutoff relationship before reconsidering valuation.",
        "prohibitions": ["do not relax the cutoff-window policy"],
    },
    "MANUAL_POLICY_SPECIFIC_REVIEW": {
        "objective": "Resolve every recorded policy reason with new evidence before reevaluation.",
        "prohibitions": ["do not override a failed policy check manually"],
    },
    "RECONSTRUCT_COMBO_POSITION_COMPONENTS": {
        "objective": "Decode the Combo position into its underlying components and value the components under their own rules.",
        "prohibitions": ["do not query or mark a Combo token as a CLOB outcome token"],
    },
    "VERIFY_PUSD_COLLATERAL_VALUE": {
        "objective": "Verify pUSD redemption or collateral value under its token-specific accounting rule.",
        "prohibitions": ["do not query or mark pUSD as a CLOB outcome token"],
    },
}


def _requests(evidence_gaps: Path) -> tuple[dict[str, int], dict, str]:
    manifest_sha = _sha256(evidence_gaps / "run_manifest.json")
    configuration, summary, _ = _verify_sealed_directory(
        evidence_gaps,
        expected_manifest_sha256=manifest_sha,
        expected_engine=EVIDENCE_GAP_VERSION,
    )
    raw = _load_json(evidence_gaps / "closing_mark_requests.json")
    if not isinstance(raw, list):
        raise EvidenceError("Closing-mark request queue is invalid")
    rows: dict[str, int] = {}
    for row in raw:
        required = row.get("required") if isinstance(row, dict) else None
        if (not isinstance(row, dict) or not isinstance(row.get("asset"), str)
                or row["asset"] in rows or not isinstance(required, list)
                or "CLOSING_MARK_MISSING" not in required):
            raise EvidenceError("Closing-mark request queue contains an invalid row")
        _asset_parts(row["asset"])
        quantity = uint(row.get("quantity_atomic"))
        if quantity == 0:
            raise EvidenceError("Closing-mark request queue contains a zero quantity")
        rows[row["asset"]] = quantity
    if (uint(summary.get("closing_marks", {}).get("requests")) != len(rows)
            or summary.get("request_hashes", {}).get("closing_mark_requests") != digest(raw)):
        raise EvidenceError("Closing-mark request queue does not match its seal")
    return rows, configuration, manifest_sha


def _compiled(compiled: Path, requests: dict[str, int], evidence_gaps: Path,
              evidence_manifest_sha: str) -> tuple[dict, dict, list[dict], list[dict], dict]:
    manifest_sha = _sha256(compiled / "run_manifest.json")
    configuration, summary, manifest = _verify_sealed_directory(
        compiled,
        expected_manifest_sha256=manifest_sha,
        expected_engine=COMPILER_VERSION,
    )
    coverage = _load_json(compiled / "coverage.json")
    decisions = _load_json(compiled / "decisions.json")
    marks = _load_json(compiled / "closing_marks.json")
    marks_evidence = _load_json(compiled / "marks_evidence.json")
    if (not isinstance(coverage, list) or not isinstance(decisions, list)
            or not isinstance(marks, dict) or not isinstance(marks_evidence, dict)):
        raise EvidenceError("Compiled closing-mark artifact has invalid payload types")
    decision_counts = Counter(row.get("status") for row in decisions if isinstance(row, dict))
    expected_counts = {
        ACCEPTED: uint(summary.get("decisions", {}).get("accepted_for_new_bundle_compilation")),
        DEFERRED_HISTORICAL: uint(summary.get("decisions", {}).get(
            "deferred_missing_historical_identity"
        )),
        DEFERRED_CURRENT: uint(summary.get("decisions", {}).get(
            "deferred_missing_current_official_identity"
        )),
        REJECTED: uint(summary.get("decisions", {}).get("rejected_by_policy")),
    }
    accepted_assets = {
        row.get("asset") for row in decisions
        if isinstance(row, dict) and row.get("status") == ACCEPTED
    }
    bundle_path = Path(configuration.get("bundle_path", "")).resolve()
    if (summary.get("status") != "COMPILATION_COMPLETE_NOT_INTEGRATED"
            or Path(configuration.get("evidence_gaps_path", "")).resolve() != evidence_gaps
            or configuration.get("evidence_gaps_manifest_sha256") != evidence_manifest_sha
            or not bundle_path.is_file()
            or _sha256(bundle_path) != configuration.get("bundle_sha256")
            or set(marks) != set(marks_evidence) or set(marks) != accepted_assets
            or manifest.get("closing_marks_digest") != digest(marks)
            or decision_counts != Counter(expected_counts)
            or len(decisions) != uint(summary.get("coverage", {}).get("candidate_decisions"))
            or len(requests) != uint(summary.get("marks", {}).get("requested"))
            or len(marks) != uint(summary.get("marks", {}).get("proposed"))
            or len(requests) - len(marks)
            != uint(summary.get("marks", {}).get("remaining_without_accepted_mark"))):
        raise EvidenceError("Compiled closing-mark artifact does not reconcile")
    return configuration, summary, coverage, decisions, {
        "manifest_sha256": manifest_sha,
        "marks": marks,
        "marks_evidence": marks_evidence,
        "accepted_assets": accepted_assets,
    }


def _probe_reference(probe: Path, manifest_sha: str, request: dict) -> dict:
    value = {
        "probe_path": str(probe),
        "probe_manifest_sha256": manifest_sha,
        "request_sha256": request.get("request_sha256"),
        "raw_response_file": request.get("raw_response_file"),
        "raw_response_sha256": request.get("raw_response_sha256"),
    }
    raw_name = value["raw_response_file"]
    raw_sha = value["raw_response_sha256"]
    if raw_name is not None:
        if not isinstance(raw_name, str) or not isinstance(raw_sha, str):
            raise EvidenceError("Price-probe raw evidence reference is invalid")
        raw_path = (probe / raw_name).resolve()
        try:
            raw_path.relative_to(probe)
        except ValueError as exc:
            raise EvidenceError("Price-probe raw evidence leaves its sealed directory") from exc
        if not raw_path.is_file() or _sha256(raw_path) != raw_sha:
            raise EvidenceError("Price-probe raw evidence hash mismatch")
    elif raw_sha is not None:
        raise EvidenceError("Price-probe raw evidence hash has no file")
    return value


def _policy_route(reasons: list[str]) -> str:
    if "SETTLED_AT_CLOSING_BLOCK_HISTORY_SUPERSEDED_BY_CTF" in reasons:
        return "RECONSTRUCT_CTF_SETTLEMENT_VALUE"
    if "NEGRISK_OR_UNDECLARED_MARKET_NOT_SUPPORTED" in reasons:
        return "RECONSTRUCT_NEGRISK_COMPONENT_VALUE"
    if "HISTORICAL_IDENTITY_BEFORE_CUTOFF_MISSING" in reasons:
        return "RECOVER_HISTORICAL_MARKET_IDENTITY"
    if "CUTOFF_OUTSIDE_DECLARED_MARKET_WINDOW" in reasons:
        return "RECONSTRUCT_MARKET_WINDOW"
    return "MANUAL_POLICY_SPECIFIC_REVIEW"


def _probe_gap_row(request: dict, *, maximum_age_seconds: int, probe: Path,
                   manifest_sha: str) -> dict:
    """Classify a non-fresh probe row; exposed for focused policy tests."""
    status = request.get("status")
    reference = _probe_reference(probe, manifest_sha, request)
    common = {
        "asset": request.get("asset"),
        "quantity_atomic": uint(request.get("quantity_atomic")),
        "sequence": uint(request.get("sequence")),
        "source_status": status,
        "cutoff_timestamp": uint(request.get("cutoff_timestamp")),
        "source_evidence": reference,
    }
    if status == STALE:
        observation = request.get("observation")
        if not isinstance(observation, dict):
            raise EvidenceError("Stale candidate has no observation")
        age = uint(observation.get("age_seconds"))
        if age <= maximum_age_seconds:
            raise EvidenceError("Stale candidate does not exceed its sealed age limit")
        return {
            **common,
            "category": STALE,
            "priority": 1,
            "route": "RECOVER_FRESH_HISTORICAL_PRICE",
            "reason_codes": ["OBSERVATION_EXCEEDS_MAXIMUM_AGE"],
            "diagnostic_observation_only": deepcopy(observation),
            "maximum_age_seconds": maximum_age_seconds,
            "excess_age_seconds": age - maximum_age_seconds,
            "eligible_as_closing_mark": False,
        }
    if status == NO_OBSERVATION:
        if request.get("observation") is not None:
            raise EvidenceError("No-observation result unexpectedly contains an observation")
        return {
            **common,
            "category": NO_OBSERVATION,
            "priority": 2,
            "route": "RECOVER_MISSING_HISTORICAL_PRICE",
            "reason_codes": ["NO_OBSERVATION_AT_OR_BEFORE_CUTOFF"],
            "eligible_as_closing_mark": False,
        }
    raise EvidenceError("Unsupported non-fresh price-probe status: " + str(status))


def _decision_gap_row(decision: dict, probe_request: dict, *, probe: Path,
                      probe_manifest_sha: str) -> dict:
    status = decision.get("status")
    reasons = decision.get("reasons")
    if not isinstance(reasons, list) or not all(isinstance(value, str) for value in reasons):
        raise EvidenceError("Closing-mark decision reasons are invalid")
    if status == DEFERRED_HISTORICAL:
        category = DEFERRED_HISTORICAL_IDENTITY
        route = "RECOVER_HISTORICAL_MARKET_IDENTITY"
        priority = 3
    elif status == DEFERRED_CURRENT:
        category = DEFERRED_CURRENT_IDENTITY
        route = "RECOVER_CURRENT_OR_ARCHIVED_OFFICIAL_IDENTITY"
        priority = 3
    elif status == REJECTED:
        category = REJECTED_POLICY
        route = _policy_route(reasons)
        priority = 4
    else:
        raise EvidenceError("Unsupported unresolved closing-mark decision")
    return {
        "asset": decision.get("asset"),
        "quantity_atomic": uint(probe_request.get("quantity_atomic")),
        "sequence": uint(decision.get("sequence")),
        "category": category,
        "priority": priority,
        "route": route,
        "source_status": "FRESH_CANDIDATE",
        "reason_codes": reasons,
        "identity": {
            "condition_id": decision.get("condition_id"),
            "outcome": decision.get("outcome"),
            "outcome_index": decision.get("outcome_index"),
            "question": decision.get("question"),
            "token_id": decision.get("token_id"),
        },
        "price_candidate": deepcopy(decision.get("price_candidate")),
        "checks": deepcopy(decision.get("checks")),
        "settlement_at_closing_block": deepcopy(decision.get("settlement_at_closing_block")),
        "decision_source": deepcopy(decision.get("compilation_source")),
        "source_evidence": _probe_reference(probe, probe_manifest_sha, probe_request),
        "eligible_as_closing_mark": False,
    }


def build_closing_mark_gap_queue_file(compiled_candidates: Path, evidence_gaps: Path,
                                      output: Path, *,
                                      require_clean_git: bool = True) -> dict:
    """Classify and seal every unresolved mark request without changing valuation."""
    compiled_candidates = Path(compiled_candidates).resolve()
    evidence_gaps = Path(evidence_gaps).resolve()
    output = Path(output).resolve()
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists():
        raise EvidenceError("Closing-mark gap queue output or partial directory already exists")
    project = Path(__file__).resolve().parents[3]
    commit, clean = _git_state(project)
    if require_clean_git and (commit == "unavailable" or not clean):
        raise EvidenceError("Closing-mark gap queue requires a clean Git commit")

    requests, gap_configuration, gap_manifest_sha = _requests(evidence_gaps)
    compiled_configuration, compiled_summary, coverage, decisions, compiled_seal = _compiled(
        compiled_candidates, requests, evidence_gaps, gap_manifest_sha,
    )
    if gap_configuration.get("input_sha256") != compiled_configuration.get("bundle_sha256"):
        raise EvidenceError("Gap queue and compiler do not share the same basis bundle")

    decision_by_sequence: dict[int, dict] = {}
    decision_by_asset: dict[str, dict] = {}
    for decision in decisions:
        if not isinstance(decision, dict) or not isinstance(decision.get("asset"), str):
            raise EvidenceError("Compiled closing-mark decision is invalid")
        sequence = uint(decision.get("sequence"))
        asset = decision["asset"]
        if sequence in decision_by_sequence or asset in decision_by_asset:
            raise EvidenceError("Compiled closing-mark decisions contain duplicates")
        decision_by_sequence[sequence] = decision
        decision_by_asset[asset] = decision

    queue: list[dict] = []
    probed_assets: set[str] = set()
    probed_sequences: set[int] = set()
    fresh_sequences: set[int] = set()
    probe_statuses: Counter[str] = Counter()
    coverage_cursor = 0
    maximum_ages: Counter[int] = Counter()
    for chunk in coverage:
        if not isinstance(chunk, dict):
            raise EvidenceError("Compiled coverage contains an invalid chunk")
        start = uint(chunk.get("start_index"))
        end = uint(chunk.get("end_index_exclusive"))
        if start != coverage_cursor or end <= start:
            raise EvidenceError("Compiled probe coverage has a gap or overlap")
        coverage_cursor = end
        probe = Path(chunk.get("probe_path", "")).resolve()
        probe_manifest_sha = chunk.get("probe_manifest_sha256")
        if not isinstance(probe_manifest_sha, str):
            raise EvidenceError("Compiled coverage has no probe seal")
        probe_configuration, probe_summary, _ = _verify_sealed_directory(
            probe,
            expected_manifest_sha256=probe_manifest_sha,
            expected_engine=PROBE_VERSION,
        )
        probe_rows = _load_json(probe / "request_results.json")
        if not isinstance(probe_rows, list):
            raise EvidenceError("Price-probe request results are invalid")
        maximum_age = uint(probe_summary.get("maximum_observation_age_seconds"))
        maximum_ages[maximum_age] += len(probe_rows)
        expected_statuses = {
            key: uint(value)
            for key, value in probe_summary.get("responses", {}).get("status_counts", {}).items()
        }
        actual_statuses = Counter(row.get("status") for row in probe_rows if isinstance(row, dict))
        if (len(probe_rows) != end - start
                or uint(chunk.get("selected_ctf_outcomes")) != len(probe_rows)
                or chunk.get("status_counts") != dict(sorted(expected_statuses.items()))
                or actual_statuses != Counter(expected_statuses)
                or Path(probe_configuration.get("evidence_gap_queue", {}).get("path", "")).resolve()
                != evidence_gaps
                or probe_configuration.get("evidence_gap_queue", {}).get("manifest_sha256")
                != gap_manifest_sha):
            raise EvidenceError("Price-probe chunk does not match compiled coverage")
        for offset, probe_request in enumerate(probe_rows, start=1):
            if not isinstance(probe_request, dict):
                raise EvidenceError("Price-probe request result is invalid")
            sequence = uint(probe_request.get("sequence"))
            expected_sequence = start + offset
            asset = probe_request.get("asset")
            if (sequence != expected_sequence or not isinstance(asset, str)
                    or asset in probed_assets or sequence in probed_sequences
                    or asset not in requests
                    or uint(probe_request.get("quantity_atomic")) != requests[asset]):
                raise EvidenceError("Price-probe request identity or sequence is invalid")
            _, contract, token = _asset_parts(asset)
            if contract != CTF_CONTRACT or token != probe_request.get("token_id"):
                raise EvidenceError("Price-probe row is not the expected CTF outcome")
            probed_assets.add(asset)
            probed_sequences.add(sequence)
            status = probe_request.get("status")
            probe_statuses[status] += 1
            if status == "FRESH_CANDIDATE":
                decision = decision_by_sequence.get(sequence)
                if decision is None or decision.get("asset") != asset:
                    raise EvidenceError("Fresh probe candidate has no matching policy decision")
                fresh_sequences.add(sequence)
                if decision.get("status") != ACCEPTED:
                    queue.append(_decision_gap_row(
                        decision, probe_request, probe=probe,
                        probe_manifest_sha=probe_manifest_sha,
                    ))
            else:
                if sequence in decision_by_sequence or asset in decision_by_asset:
                    raise EvidenceError("Non-fresh probe row unexpectedly has a policy decision")
                queue.append(_probe_gap_row(
                    probe_request, maximum_age_seconds=maximum_age,
                    probe=probe, manifest_sha=probe_manifest_sha,
                ))

    eligible = uint(compiled_summary.get("coverage", {}).get("eligible_ctf_outcomes"))
    if (coverage_cursor != eligible or len(probed_assets) != eligible
            or fresh_sequences != set(decision_by_sequence)):
        raise EvidenceError("Compiled CTF queue does not have complete probe coverage")
    expected_ctf_assets = {
        asset for asset in requests if _asset_parts(asset)[1] == CTF_CONTRACT
    }
    if probed_assets != expected_ctf_assets:
        raise EvidenceError("Probed CTF assets do not equal the evidence-gap CTF universe")

    for asset, quantity in requests.items():
        if asset in probed_assets:
            continue
        _, contract, _ = _asset_parts(asset)
        if contract == COMBO_CONTRACT:
            category = COMBO_POSITION
            route = "RECONSTRUCT_COMBO_POSITION_COMPONENTS"
            priority = 5
        elif contract == PUSD_CONTRACT:
            category = PUSD_COLLATERAL
            route = "VERIFY_PUSD_COLLATERAL_VALUE"
            priority = 6
        else:
            raise EvidenceError("Unprobed asset has no supported exclusion policy")
        queue.append({
            "asset": asset,
            "quantity_atomic": quantity,
            "sequence": None,
            "category": category,
            "priority": priority,
            "route": route,
            "source_status": "EXCLUDED_FROM_CTF_PRICE_PROBE",
            "reason_codes": [category],
            "eligible_as_closing_mark": False,
        })

    queue.sort(key=lambda row: (
        uint(row["priority"]),
        row["sequence"] if row["sequence"] is not None else 2**63,
        row["asset"],
    ))
    for index, row in enumerate(queue, start=1):
        row["queue_sequence"] = index
    category_counts = Counter(row["category"] for row in queue)
    route_counts = Counter(row["route"] for row in queue)
    accepted_count = len(compiled_seal["accepted_assets"])
    unresolved = len(requests) - accepted_count
    if (len(queue) != unresolved
            or len({row["asset"] for row in queue}) != len(queue)
            or set(requests) != compiled_seal["accepted_assets"] | {
                row["asset"] for row in queue
            }
            or compiled_seal["accepted_assets"] & {row["asset"] for row in queue}):
        raise EvidenceError("Closing-mark gap queue does not partition the request universe")

    summary = {
        "schema": GAP_QUEUE_SCHEMA,
        "engine": GAP_QUEUE_VERSION,
        "status": "GAP_QUEUE_COMPLETE_NOT_RESOLVED",
        "coverage": {
            "closing_mark_requests": len(requests),
            "accepted_candidates_excluded_from_queue": accepted_count,
            "unresolved_assets_queued": len(queue),
            "eligible_ctf_outcomes_probed": len(probed_assets),
            "excluded_non_ctf_assets_queued": len(requests) - len(probed_assets),
            "probe_chunks": len(coverage),
            "fresh_policy_decisions": len(decisions),
        },
        "probe_status_counts": dict(sorted(probe_statuses.items())),
        "category_counts": dict(sorted(category_counts.items())),
        "route_counts": dict(sorted(route_counts.items())),
        "maximum_observation_age_seconds_by_rows": {
            str(key): value for key, value in sorted(maximum_ages.items())
        },
        "reconciliation": {
            "status": "PASS",
            "requested_equals_accepted_plus_queued": len(requests) == accepted_count + len(queue),
            "ctf_probe_coverage_complete": len(probed_assets) == eligible,
            "every_queued_asset_has_one_category": len(queue) == len({row["asset"] for row in queue}),
        },
        "integration": {
            "closing_marks_written_to_bundle": 0,
            "basis_bundle_modified": False,
            "compiled_candidates_modified": False,
            "stale_candidates_promoted": 0,
            "freshness_threshold_modified": False,
            "status": "NOT_INTEGRATED",
        },
        "review_gate": {
            "status": "BLOCKED",
            "reasons": [
                "UNRESOLVED_CLOSING_MARK_QUEUE_REMAINS",
                "NO_QUEUE_ROW_IS_AN_APPROVED_MARK",
                "EXTERNAL_FLOW_VALUE_EVIDENCE_REMAINS_OPEN",
                "INDEPENDENT_ACCOUNTING_REPORT_MISSING",
            ],
        },
        "safety": SAFETY,
    }
    configuration = {
        "schema": GAP_QUEUE_SCHEMA,
        "engine": GAP_QUEUE_VERSION,
        "created_at": now_utc(),
        "code_commit": commit,
        "working_tree_clean": clean,
        "compiled_candidates_path": str(compiled_candidates),
        "compiled_candidates_manifest_sha256": compiled_seal["manifest_sha256"],
        "evidence_gaps_path": str(evidence_gaps),
        "evidence_gaps_manifest_sha256": gap_manifest_sha,
        "bundle_path": compiled_configuration["bundle_path"],
        "bundle_sha256": compiled_configuration["bundle_sha256"],
        "classification_policy": "exclusive-category-v1",
        "action_policies": ACTION_POLICIES,
        "integration": "NONE; the queue classifies gaps and cannot write closing marks",
        "safety": SAFETY,
    }

    compiled_before = _sha256(compiled_candidates / "run_manifest.json")
    gap_before = _sha256(evidence_gaps / "run_manifest.json")
    bundle_path = Path(compiled_configuration["bundle_path"])
    bundle_before = _sha256(bundle_path)
    partial.mkdir(parents=True)
    _write_new(partial / "configuration.json", configuration)
    _write_new(partial / "gap_queue.json", queue)
    for filename in sorted(set(CATEGORY_FILES.values())):
        categories = {key for key, value in CATEGORY_FILES.items() if value == filename}
        _write_new(partial / filename, [
            row for row in queue if row["category"] in categories
        ])
    _write_new(partial / "summary.json", summary)
    if (_sha256(compiled_candidates / "run_manifest.json") != compiled_before
            or _sha256(evidence_gaps / "run_manifest.json") != gap_before
            or _sha256(bundle_path) != bundle_before):
        raise EvidenceError("A sealed closing-mark source changed while the queue was built")
    _write_new(partial / "run_manifest.json", {
        "schema": GAP_QUEUE_SCHEMA,
        "engine": GAP_QUEUE_VERSION,
        "completed_at": now_utc(),
        "code_commit": commit,
        "working_tree_clean": clean,
        "compiled_candidates_manifest_sha256": compiled_before,
        "evidence_gaps_manifest_sha256": gap_before,
        "bundle_sha256": bundle_before,
        "files": _files_sha256(partial),
        "summary_sha256": _sha256(partial / "summary.json"),
        "gap_queue_digest": digest(queue),
        "safety": SAFETY,
    })
    os.replace(partial, output)
    return summary
