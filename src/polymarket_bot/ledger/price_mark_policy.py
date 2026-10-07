"""Conservative, sealed acceptance policy for historical closing-price candidates.

This module is deliberately a gate, not a valuation writer.  It revalidates
the complete sealed evidence chain, reparses the original official response
bytes, checks an exact-block CTF settlement state through two independent
read-only Polygon RPCs, and records deterministic decisions.  It never writes
``closing_marks`` and never modifies a basis bundle.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from eth_abi import encode

from . import SAFETY
from .common import EvidenceError, address, digest, hex_bytes, now_utc, uint
from .price_candidate_audit import (
    AUDIT_VERSION,
    _candidate_review,
    _files_sha256,
    _json_payload,
    _load_json,
    _load_verified_probe,
    _lookup_local_metadata,
    _parse_clob_market,
    _parse_gamma_market,
    _parse_parent_market,
    _request_url,
    _sha256,
    _string_json_list,
)
from .price_history_probe import CTF_CONTRACT, _write_new


POLICY_SCHEMA = 1
POLICY_VERSION = "polyledger-closing-mark-policy/1"
POLICY_STATUS = "APPROVED_FOR_EVIDENCE_EVALUATION_ONLY"
DENOMINATOR_SELECTOR = "0xdd34de67"
NUMERATOR_SELECTOR = "0x0504c814"
MAX_POLICY_AGE_SECONDS = 900
MAX_POLICY_RESOLUTION_SECONDS = 300


def _require_exact_keys(value: dict, keys: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise EvidenceError(f"{label} fields are invalid")


def _load_policy(path: Path) -> dict:
    policy = _load_json(path)
    _require_exact_keys(policy, {
        "schema", "policy_id", "status", "chain", "conditional_tokens_contract",
        "root_of_trust", "thresholds", "identity", "settlement", "integration", "safety",
    }, "Closing-mark policy")
    _require_exact_keys(policy["root_of_trust"], {
        "audit_engine", "audit_code_commit", "audit_manifest_sha256",
        "audit_summary_sha256", "probe_manifest_sha256", "evidence_gap_manifest_sha256",
        "bundle_manifest_sha256", "bundle_sha256", "bundle_digest",
        "closing_block_number", "closing_block_hash", "cutoff_timestamp",
    }, "Policy root of trust")
    _require_exact_keys(policy["thresholds"], {
        "maximum_age_seconds", "minimum_resolution_seconds", "maximum_resolution_seconds",
        "require_closed_bucket", "price_interval",
    }, "Policy thresholds")
    _require_exact_keys(policy["identity"], {
        "require_current_official_identity", "require_clob_compact_condition",
        "require_gamma_binary_pair", "require_local_mapping_before_cutoff",
        "allow_negrisk", "require_cutoff_within_declared_window",
        "current_closed_allowed_only_if_closed_after_cutoff",
    }, "Policy identity rules")
    _require_exact_keys(policy["settlement"], {
        "require_two_independent_rpcs", "require_exact_block_hash",
        "unresolved_denominator", "settled_precedence", "state_anchor",
        "approved_rpc_hosts",
    }, "Policy settlement rules")
    _require_exact_keys(policy["integration"], {
        "write_closing_marks", "modify_basis_bundle", "automatic_approval",
    }, "Policy integration rules")

    if (uint(policy["schema"]) != POLICY_SCHEMA or policy["status"] != POLICY_STATUS
            or not isinstance(policy["policy_id"], str) or not policy["policy_id"]
            or uint(policy["chain"]) != 137
            or address(policy["conditional_tokens_contract"]) != CTF_CONTRACT
            or policy["root_of_trust"]["audit_engine"] != AUDIT_VERSION
            or not isinstance(policy["root_of_trust"]["audit_code_commit"], str)
            or len(policy["root_of_trust"]["audit_code_commit"]) != 40
            or not all(isinstance(policy["root_of_trust"][key], str)
                       and len(policy["root_of_trust"][key]) == 64
                       for key in ("audit_manifest_sha256", "audit_summary_sha256",
                                   "probe_manifest_sha256", "evidence_gap_manifest_sha256",
                                   "bundle_manifest_sha256", "bundle_sha256", "bundle_digest"))
            or uint(policy["root_of_trust"]["closing_block_number"]) == 0
            or hex_bytes(policy["root_of_trust"]["closing_block_hash"], 32)
            != policy["root_of_trust"]["closing_block_hash"].lower()
            or uint(policy["root_of_trust"]["cutoff_timestamp"]) == 0):
        raise EvidenceError("Closing-mark policy identity or root of trust is invalid")

    thresholds = policy["thresholds"]
    if (uint(thresholds["maximum_age_seconds"]) > MAX_POLICY_AGE_SECONDS
            or uint(thresholds["minimum_resolution_seconds"]) != 1
            or not 1 <= uint(thresholds["maximum_resolution_seconds"]) <= MAX_POLICY_RESOLUTION_SECONDS
            or thresholds["require_closed_bucket"] is not True
            or thresholds["price_interval"] != "STRICT_INTERIOR_0_1"):
        raise EvidenceError("Closing-mark policy thresholds would weaken the P0 gate")
    identity = policy["identity"]
    if (identity["require_current_official_identity"] is not True
            or identity["require_clob_compact_condition"] is not True
            or identity["require_gamma_binary_pair"] is not True
            or identity["require_local_mapping_before_cutoff"] is not True
            or identity["allow_negrisk"] is not False
            or identity["require_cutoff_within_declared_window"] is not True
            or identity["current_closed_allowed_only_if_closed_after_cutoff"] is not True):
        raise EvidenceError("Closing-mark policy identity rules would weaken the P0 gate")
    settlement = policy["settlement"]
    approved_hosts = settlement.get("approved_rpc_hosts")
    if (settlement["require_two_independent_rpcs"] is not True
            or settlement["require_exact_block_hash"] is not True
            or uint(settlement["unresolved_denominator"]) != 0
            or settlement["settled_precedence"] != "CTF_PAYOUT_STATE_OVERRIDES_HISTORY"
            or settlement["state_anchor"]
            != "CLOSING_BLOCK_POST_STATE_MATCHING_INVENTORY"
            or not isinstance(approved_hosts, list) or len(approved_hosts) != 2
            or not all(isinstance(host, str) and host == host.lower().rstrip(".") and host
                       for host in approved_hosts)
            or len(set(approved_hosts)) != 2):
        raise EvidenceError("Closing-mark settlement precedence is invalid")
    integration = policy["integration"]
    if (integration != {"write_closing_marks": False, "modify_basis_bundle": False,
                        "automatic_approval": False} or policy["safety"] != SAFETY):
        raise EvidenceError("Closing-mark policy cannot authorize integration or execution")
    return policy


def _verify_sealed_directory(root: Path, *, expected_manifest_sha256: str,
                             expected_engine: str | None = None) -> tuple[dict, dict, dict]:
    if not root.is_dir():
        raise EvidenceError("Sealed evidence directory is missing")
    manifest_path = root / "run_manifest.json"
    configuration_path = root / "configuration.json"
    summary_path = root / "summary.json"
    if not manifest_path.is_file() or not configuration_path.is_file() or not summary_path.is_file():
        raise EvidenceError("Sealed evidence directory is incomplete")
    if _sha256(manifest_path) != expected_manifest_sha256:
        raise EvidenceError("Sealed evidence manifest is outside the approved root of trust")
    manifest = _load_json(manifest_path)
    configuration = _load_json(configuration_path)
    summary = _load_json(summary_path)
    summary_seal_ok = (
        manifest.get("summary_sha256") == _sha256(summary_path)
        or (manifest.get("summary_hash") == digest(summary)
            and manifest.get("files", {}).get("summary.json") == _sha256(summary_path))
    )
    if (not isinstance(manifest.get("files"), dict)
            or manifest["files"] != _files_sha256(root)
            or not summary_seal_ok):
        raise EvidenceError("Sealed evidence manifest hash verification failed")
    if (expected_engine is not None
            and (manifest.get("engine") != expected_engine
                 or configuration.get("engine") != expected_engine
                 or summary.get("engine") != expected_engine)):
        raise EvidenceError("Sealed evidence engine is not supported")
    return configuration, summary, manifest


def _safe_audit_raw(audit: Path, request: dict) -> bytes:
    value = request.get("raw_response_file")
    if not isinstance(value, str) or not value:
        raise EvidenceError("Candidate-audit raw response path is invalid")
    path = (audit / value).resolve()
    try:
        path.relative_to(audit)
    except ValueError as exc:
        raise EvidenceError("Candidate-audit raw response leaves the sealed audit") from exc
    if not path.is_file() or _sha256(path) != request.get("raw_response_sha256"):
        raise EvidenceError("Candidate-audit raw response hash mismatch")
    return path.read_bytes()


def _verify_complete_price_pages(probe: Path, candidates: list[dict]) -> None:
    for candidate in candidates:
        value = candidate.get("price_raw_file")
        if not isinstance(value, str) or not value:
            raise EvidenceError("Price-history raw response path is invalid")
        path = (probe / value).resolve()
        try:
            path.relative_to(probe)
        except ValueError as exc:
            raise EvidenceError("Price-history raw response leaves the sealed probe") from exc
        if (not path.is_file() or _sha256(path) != candidate.get("price_raw_sha256")):
            raise EvidenceError("Price-history raw response hash mismatch")
        payload = _json_payload(path.read_bytes(), label="price-history")
        pagination = payload.get("pagination") if isinstance(payload, dict) else None
        data = payload.get("data") if isinstance(payload, dict) else None
        if (not isinstance(data, list) or not isinstance(pagination, dict)
                or pagination.get("has_more") is not False
                or pagination.get("next_cursor") is not None
                or uint(pagination.get("offset")) != 0
                or uint(pagination.get("limit")) < len(data)):
            raise EvidenceError("Price-history page is incomplete or still paginated")


def _verify_audit_request_identity(request: dict, *, kind: str, value: str) -> None:
    url = _request_url(kind, value)
    request_sha256 = hashlib.sha256(
        ("GET\n" + url + "\naccept:application/json\n").encode("utf-8")
    ).hexdigest()
    if request.get("query_url") != url or request.get("request_sha256") != request_sha256:
        raise EvidenceError("Candidate-audit request provenance does not match official endpoint")


def _verify_bundle_directory(bundle: Path, *, manifest_sha256: str,
                             bundle_sha256: str, bundle_digest: str) -> tuple[Path, dict]:
    root = bundle.parent
    manifest_path = root / "run_manifest.json"
    configuration_path = root / "configuration.json"
    summary_path = root / "summary.json"
    if (not manifest_path.is_file() or not configuration_path.is_file()
            or not summary_path.is_file() or bundle.name != "bundle.json"):
        raise EvidenceError("Sealed basis-bundle directory is incomplete")
    if _sha256(manifest_path) != manifest_sha256:
        raise EvidenceError("Basis-bundle manifest is outside the approved root of trust")
    manifest = _load_json(manifest_path)
    _load_json(configuration_path)
    _load_json(summary_path)
    if (manifest.get("status") != "BUNDLE_COMPLETE"
            or manifest.get("version") != "lifetime-basis-bundle/3"
            or manifest.get("safety") != SAFETY
            or manifest.get("files") != _files_sha256(root)
            or manifest.get("files", {}).get("bundle.json") != bundle_sha256
            or manifest.get("bundle_hash") != bundle_digest):
        raise EvidenceError("Basis-bundle directory seal is invalid")
    return root, manifest


def _metadata_sidecars(path: Path) -> dict:
    result = {"database_path": str(path), "database_sha256": _sha256(path)}
    for suffix, label in (("-wal", "wal"), ("-shm", "shm")):
        sidecar = Path(str(path) + suffix)
        result[label] = {
            "exists": sidecar.exists(),
            "length": sidecar.stat().st_size if sidecar.exists() else 0,
            "sha256": _sha256(sidecar) if sidecar.is_file() else None,
        }
    return result


def _verified_local_metadata(candidates_doc: dict, candidates: list[dict],
                             cutoff: int) -> tuple[dict, dict]:
    declared = candidates_doc.get("local_metadata")
    if not isinstance(declared, dict) or declared.get("status") != "AVAILABLE":
        raise EvidenceError("Historical local metadata snapshot is unavailable")
    path_value = declared.get("path")
    if not isinstance(path_value, str):
        raise EvidenceError("Historical local metadata path is invalid")
    path = Path(path_value).resolve()
    if not path.is_file():
        raise EvidenceError("Historical local metadata database is missing")
    before = _metadata_sidecars(path)
    if before["database_sha256"] != declared.get("sha256") or before["wal"]["length"] != 0:
        raise EvidenceError("Historical local metadata database or WAL differs from audited state")
    result = _lookup_local_metadata(path, candidates, cutoff)
    after = _metadata_sidecars(path)
    if (before["database_sha256"] != after["database_sha256"]
            or before["wal"] != after["wal"] or after["wal"]["length"] != 0):
        raise EvidenceError("Historical local metadata changed during policy evaluation")
    declared_header = {key: result.get(key) for key in ("status", "path", "sha256")}
    if declared_header != declared:
        raise EvidenceError("Historical local metadata declaration no longer reproduces")
    return result, {"before": before, "after": after, "wal_required_empty": True}


def _gamma_outcome_index(raws: list[bytes], *, condition: str, token_id: str) -> int:
    rows = []
    for raw in raws:
        payload = _json_payload(raw, label="Gamma markets")
        if not isinstance(payload, list):
            raise EvidenceError("Gamma markets response is not a list")
        rows.extend(payload)
    matches = [row for row in rows if isinstance(row, dict)
               and isinstance(row.get("conditionId"), str)
               and hex_bytes(row["conditionId"], 32) == condition]
    if len(matches) != 1:
        raise EvidenceError("Gamma market does not identify one exact condition")
    tokens = _string_json_list(matches[0].get("clobTokenIds"), label="clobTokenIds")
    outcomes = _string_json_list(matches[0].get("outcomes"), label="outcomes")
    indexes = [index for index, value in enumerate(tokens) if value == token_id]
    if (len(indexes) != 1 or len(tokens) != len(outcomes) or len(tokens) != 2
            or {value.strip().upper() for value in outcomes} != {"YES", "NO"}):
        raise EvidenceError("Gamma binary outcome index is ambiguous")
    return indexes[0]


def _reparse_audit(audit: Path, candidates: list[dict], candidates_doc: dict,
                   cutoff: int) -> tuple[list[dict], dict]:
    requests = _load_json(audit / "request_results.json")
    reviews = _load_json(audit / "candidate_reviews.json")
    if not isinstance(requests, list) or not isinstance(reviews, list):
        raise EvidenceError("Candidate-audit requests or reviews are invalid")
    by_id = {}
    for request in requests:
        if (not isinstance(request, dict) or not isinstance(request.get("request_id"), str)
                or request["request_id"] in by_id or request.get("status") != "HTTP_OK"
                or uint(request.get("http_status")) != 200 or "parse_error" in request):
            raise EvidenceError("Candidate-audit request set is incomplete or ambiguous")
        by_id[request["request_id"]] = request
    by_sequence = {uint(row.get("sequence")): row for row in reviews if isinstance(row, dict)}
    if len(by_sequence) != len(reviews) or len(reviews) != len(candidates):
        raise EvidenceError("Candidate-audit review set is incomplete or ambiguous")
    local, metadata_evidence = _verified_local_metadata(candidates_doc, candidates, cutoff)
    expected_request_ids = set()
    result = []
    for candidate in candidates:
        sequence = uint(candidate["sequence"])
        token_id = candidate["token_id"]
        prefix = f"{sequence:02d}_{token_id}"
        ids = {kind: prefix + "_" + kind for kind in ("parent", "clob", "gamma_open", "gamma_closed")}
        expected_request_ids.update(ids.values())
        try:
            selected = {kind: by_id[request_id] for kind, request_id in ids.items()}
        except KeyError as exc:
            raise EvidenceError("Candidate-audit request is missing") from exc
        if any(selected[kind].get("kind") != kind for kind in ids):
            raise EvidenceError("Candidate-audit request kind conflicts with its identity")
        _verify_audit_request_identity(selected["parent"], kind="parent", value=token_id)
        raws = {kind: _safe_audit_raw(audit, request) for kind, request in selected.items()}
        parent = _parse_parent_market(raws["parent"], token_id)
        for kind in ("clob", "gamma_open", "gamma_closed"):
            _verify_audit_request_identity(
                selected[kind], kind=kind, value=parent["condition_id"]
            )
        clob_payload = _json_payload(raws["clob"], label="clob-markets")
        compact_condition = clob_payload.get("c") if isinstance(clob_payload, dict) else None
        if not isinstance(compact_condition, str) or hex_bytes(compact_condition, 32) != parent["condition_id"]:
            raise EvidenceError("CLOB compact condition field conflicts with parent market")
        clob = _parse_clob_market(raws["clob"], token_id=token_id, parent=parent)
        gamma_raws = [raws["gamma_open"], raws["gamma_closed"]]
        gamma = _parse_gamma_market(gamma_raws, token_id=token_id, parent=parent, clob=clob)
        outcome_index = _gamma_outcome_index(
            gamma_raws, condition=parent["condition_id"], token_id=token_id
        )
        local_row = local["rows"].get(token_id)
        expected_review = _candidate_review(
            candidate, parent=parent, clob=clob, gamma=gamma, local=local_row,
            cutoff_timestamp=cutoff,
        )
        review = by_sequence[sequence]
        if review != expected_review:
            raise EvidenceError("Candidate-audit review does not reproduce from sealed raw bytes")
        result.append({
            "candidate": candidate,
            "review": review,
            "condition_id": parent["condition_id"],
            "outcome": clob["outcome"],
            "outcome_index": outcome_index,
            "market": gamma["market"],
            "compact_condition": hex_bytes(compact_condition, 32),
        })
    if set(by_id) != expected_request_ids:
        raise EvidenceError("Candidate-audit request set contains unexpected entries")
    return result, metadata_evidence


def _validate_rpc_endpoint(rpc) -> str:
    value = getattr(rpc, "url", None)
    if not isinstance(value, str):
        raise EvidenceError("Settlement RPC must expose its public URL")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise EvidenceError("Settlement RPC URL has an invalid port") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or port not in {None, 443}):
        raise EvidenceError("Settlement RPC must be credential-free HTTPS")
    try:
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise EvidenceError("Settlement RPC hostname is invalid") from exc
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise EvidenceError("Settlement RPC must be a public endpoint")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_unspecified or ip.is_multicast):
            raise EvidenceError("Settlement RPC must be a public endpoint")
    return host


def _state_call(rpc, *, contract: str, data: str, block_hash: str) -> tuple[str, int]:
    pinned = {"blockHash": block_hash, "requireCanonical": True}
    raw = rpc.call("eth_call", [{"to": contract, "data": data}, pinned])
    normalized = hex_bytes(raw, 32)
    return normalized, int(normalized, 16)


def _rpc_settlement_snapshot(rpc, *, block_number: int, block_hash: str,
                             block_timestamp: int, ctf: str,
                             conditions: dict[str, list[dict]]) -> dict:
    host = _validate_rpc_endpoint(rpc)
    chain = uint(rpc.call("eth_chainId", []))
    header = rpc.block(block_number)
    compact_header = {
        "number": uint(header.get("number")),
        "hash": hex_bytes(header.get("hash"), 32),
        "timestamp": uint(header.get("timestamp")),
    }
    if (chain != 137 or compact_header != {"number": block_number, "hash": block_hash,
                                            "timestamp": block_timestamp}):
        raise EvidenceError("Settlement RPC chain or closing block anchor mismatch")
    states = {}
    for condition in sorted(conditions):
        denominator_data = DENOMINATOR_SELECTOR + encode(
            ["bytes32"], [bytes.fromhex(condition[2:])]
        ).hex()
        denominator_raw, denominator = _state_call(
            rpc, contract=ctf, data=denominator_data, block_hash=block_hash
        )
        payout_vector = []
        if denominator > 0:
            for outcome_index in (0, 1):
                numerator_data = NUMERATOR_SELECTOR + encode(
                    ["bytes32", "uint256"],
                    [bytes.fromhex(condition[2:]), outcome_index],
                ).hex()
                numerator_raw, numerator = _state_call(
                    rpc, contract=ctf, data=numerator_data, block_hash=block_hash
                )
                if numerator > denominator:
                    raise EvidenceError("CTF payout numerator exceeds denominator")
                payout_vector.append({"outcome_index": outcome_index,
                                      "numerator_calldata": numerator_data,
                                      "numerator_result_hex": numerator_raw,
                                      "payout_numerator": numerator})
            if sum(row["payout_numerator"] for row in payout_vector) != denominator:
                raise EvidenceError("Binary CTF payout vector does not sum to denominator")
        positions = []
        for position in sorted(conditions[condition], key=lambda row: int(row["token_id"])):
            row = {"token_id": position["token_id"], "outcome": position["outcome"],
                   "outcome_index": position["outcome_index"]}
            if denominator > 0:
                numerator = payout_vector[uint(position["outcome_index"])]["payout_numerator"]
                row.update({"payout_numerator": numerator,
                            "settlement_ratio": f"{numerator}/{denominator}"})
            positions.append(row)
        states[condition] = {
            "status": ("UNRESOLVED_AT_CLOSING_BLOCK" if denominator == 0
                       else "SETTLED_AT_CLOSING_BLOCK"),
            "denominator_calldata": denominator_data,
            "denominator_result_hex": denominator_raw,
            "payout_denominator": denominator,
            "payout_vector": payout_vector,
            "positions": positions,
        }
    recheck = rpc.block(block_number)
    if (hex_bytes(recheck.get("hash"), 32) != block_hash
            or uint(recheck.get("number")) != block_number
            or uint(recheck.get("timestamp")) != block_timestamp):
        raise EvidenceError("Settlement RPC reorg detected during exact-block reads")
    return {"source": "rpc:" + host, "chain": chain, "block": compact_header,
            "pinning": "EIP-1898-blockHash-requireCanonical", "contract": ctf,
            "conditions": states}


def _dual_settlement(primary_rpc, secondary_rpc, *, policy: dict,
                     entries: list[dict], block_timestamp: int) -> dict:
    primary_host = _validate_rpc_endpoint(primary_rpc)
    secondary_host = _validate_rpc_endpoint(secondary_rpc)
    if primary_host == secondary_host:
        raise EvidenceError("Settlement evidence requires two independent RPC hosts")
    if [primary_host, secondary_host] != policy["settlement"]["approved_rpc_hosts"]:
        raise EvidenceError("Settlement RPC hosts are outside the approved policy allowlist")
    root = policy["root_of_trust"]
    block_number = uint(root["closing_block_number"])
    block_hash = hex_bytes(root["closing_block_hash"], 32)
    ctf = address(policy["conditional_tokens_contract"])
    conditions: dict[str, list[dict]] = {}
    for entry in entries:
        conditions.setdefault(entry["condition_id"], []).append({
            "token_id": entry["candidate"]["token_id"],
            "outcome": entry["outcome"],
            "outcome_index": entry["outcome_index"],
        })
    primary = _rpc_settlement_snapshot(
        primary_rpc, block_number=block_number, block_hash=block_hash,
        block_timestamp=block_timestamp, ctf=ctf, conditions=conditions,
    )
    secondary = _rpc_settlement_snapshot(
        secondary_rpc, block_number=block_number, block_hash=block_hash,
        block_timestamp=block_timestamp, ctf=ctf, conditions=conditions,
    )
    if ({key: value for key, value in primary.items() if key != "source"}
            != {key: value for key, value in secondary.items() if key != "source"}):
        raise EvidenceError("Independent settlement RPCs disagree")
    return {"schema": POLICY_SCHEMA, "engine": POLICY_VERSION,
            "primary": primary, "secondary": secondary,
            "agreement": {"status": "EXACT_MATCH", "independent_hosts": True,
                          "condition_count": len(conditions)},
            "temporal_semantics": {
                "price_history_cutoff": "closing-block-timestamp-minus-one-second",
                "ctf_state_anchor": "closing-block-post-state-matching-inventory",
                "precedence": "CTF_PAYOUT_STATE_OVERRIDES_HISTORY",
            }}


def _iso_timestamp(value) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    from datetime import datetime, timezone
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return int(parsed.astimezone(timezone.utc).timestamp())
    except ValueError:
        return None


def _decision(entry: dict, *, cutoff: int, policy: dict, settlement: dict) -> dict:
    candidate, review, market = entry["candidate"], entry["review"], entry["market"]
    observation = candidate.get("observation")
    reasons = []
    checks = {}
    try:
        timestamp = uint(observation.get("timestamp"))
        age = uint(observation.get("age_seconds"))
        resolution = uint(observation.get("resolution_seconds"))
        price = Decimal(observation.get("price"))
        temporal_exact = age == cutoff - timestamp if timestamp <= cutoff else False
        price_valid = price.is_finite() and Decimal(0) < price < Decimal(1)
    except (AttributeError, EvidenceError, InvalidOperation, TypeError, ValueError):
        timestamp = age = resolution = 0
        temporal_exact = price_valid = False
    thresholds = policy["thresholds"]
    checks["price_strictly_between_zero_and_one"] = price_valid
    checks["timestamp_not_after_cutoff"] = timestamp <= cutoff
    checks["age_matches_cutoff"] = temporal_exact
    checks["age_within_policy"] = temporal_exact and age <= uint(thresholds["maximum_age_seconds"])
    checks["resolution_within_policy"] = (
        uint(thresholds["minimum_resolution_seconds"]) <= resolution
        <= uint(thresholds["maximum_resolution_seconds"])
    )
    checks["bucket_closed_by_cutoff"] = resolution > 0 and timestamp + resolution <= cutoff
    if not checks["price_strictly_between_zero_and_one"]:
        reasons.append("HISTORICAL_PRICE_OUTSIDE_STRICT_INTERVAL")
    if not checks["timestamp_not_after_cutoff"] or not checks["age_matches_cutoff"]:
        reasons.append("HISTORICAL_TIMESTAMP_OR_AGE_CONFLICT")
    elif not checks["age_within_policy"]:
        reasons.append("HISTORICAL_OBSERVATION_TOO_OLD")
    if not checks["resolution_within_policy"]:
        reasons.append("HISTORICAL_RESOLUTION_OUTSIDE_POLICY")
    if not checks["bucket_closed_by_cutoff"]:
        reasons.append("HISTORICAL_BUCKET_NOT_CLOSED_AT_CUTOFF")

    identity = review.get("current_identity", {})
    checks["current_official_identity_consistent"] = (
        identity.get("status") == "CURRENT_OFFICIAL_IDENTITY_CONSISTENT"
        and entry["compact_condition"] == entry["condition_id"]
    )
    checks["local_identity_before_cutoff"] = (
        review.get("auxiliary_local_metadata", {}).get("status")
        == "AUXILIARY_MAPPING_BEFORE_CUTOFF"
    )
    checks["binary_non_negrisk_market"] = market.get("negRisk") is False
    start, end, closed_time = (_iso_timestamp(market.get("startDate")),
                               _iso_timestamp(market.get("endDate")),
                               _iso_timestamp(market.get("closedTime")))
    checks["cutoff_within_declared_window"] = (
        start is not None and end is not None and start <= cutoff <= end
    )
    checks["current_close_occurred_after_cutoff"] = (
        (market.get("closed") is False and (closed_time is None or closed_time > cutoff))
        or (market.get("closed") is True and closed_time is not None and closed_time > cutoff)
    )
    if not checks["current_official_identity_consistent"]:
        reasons.append("CURRENT_OFFICIAL_IDENTITY_INCONSISTENT")
    if not checks["local_identity_before_cutoff"]:
        reasons.append("HISTORICAL_IDENTITY_BEFORE_CUTOFF_MISSING")
    if not checks["binary_non_negrisk_market"]:
        reasons.append("NEGRISK_OR_UNDECLARED_MARKET_NOT_SUPPORTED")
    if not checks["cutoff_within_declared_window"]:
        reasons.append("CUTOFF_OUTSIDE_DECLARED_MARKET_WINDOW")
    if not checks["current_close_occurred_after_cutoff"]:
        reasons.append("MARKET_CLOSED_NO_LATER_THAN_CUTOFF")
    integration = review.get("integration", {})
    checks["audit_pending_policy_without_mutation"] = (
        integration.get("status") == "PENDING_MARK_POLICY_REVIEW"
        and integration.get("closing_mark_written") is False
        and integration.get("basis_bundle_modified") is False
    )
    if not checks["audit_pending_policy_without_mutation"]:
        reasons.append("AUDIT_NOT_ELIGIBLE_FOR_POLICY_REVIEW")

    state = settlement["primary"]["conditions"][entry["condition_id"]]
    checks["ctf_unresolved_at_closing_block"] = (
        state["status"] == "UNRESOLVED_AT_CLOSING_BLOCK"
    )
    if not checks["ctf_unresolved_at_closing_block"]:
        reasons.append("SETTLED_AT_CLOSING_BLOCK_HISTORY_SUPERSEDED_BY_CTF")
    unique_reasons = sorted(set(reasons))
    if not unique_reasons:
        status = "ACCEPTED_FOR_NEW_BUNDLE_COMPILATION"
    elif unique_reasons == ["HISTORICAL_IDENTITY_BEFORE_CUTOFF_MISSING"]:
        status = "DEFERRED_MISSING_HISTORICAL_IDENTITY"
    else:
        status = "REJECTED_BY_POLICY"
    return {
        "sequence": candidate["sequence"], "asset": candidate["asset"],
        "token_id": candidate["token_id"], "condition_id": entry["condition_id"],
        "outcome": entry["outcome"], "outcome_index": entry["outcome_index"],
        "question": market.get("question"), "price_candidate": observation,
        "status": status, "checks": checks, "reasons": unique_reasons,
        "settlement_at_closing_block": state,
        "integration": {"closing_mark_written": False, "basis_bundle_modified": False,
                        "automatic_approval": False},
    }


def evaluate_price_policy_file(audit: Path, policy_path: Path, output: Path, *,
                               primary_rpc, secondary_rpc) -> dict:
    """Evaluate an approved audit without writing any mark or changing its bundle."""
    audit, policy_path, output = Path(audit).resolve(), Path(policy_path).resolve(), Path(output).resolve()
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists():
        raise EvidenceError("Policy output or partial directory already exists")
    if not policy_path.is_file():
        raise EvidenceError("Closing-mark policy file is missing")
    if output.is_relative_to(audit) or partial.is_relative_to(audit):
        raise EvidenceError("Policy output cannot be inside the sealed audit")
    policy_sha256 = _sha256(policy_path)
    policy = _load_policy(policy_path)
    root = policy["root_of_trust"]
    audit_config, audit_summary, audit_manifest = _verify_sealed_directory(
        audit, expected_manifest_sha256=root["audit_manifest_sha256"],
        expected_engine=AUDIT_VERSION,
    )
    if (audit_manifest.get("code_commit") != root["audit_code_commit"]
            or audit_manifest.get("summary_sha256") != root["audit_summary_sha256"]
            or audit_summary.get("status") != "AUDIT_COMPLETE_EVIDENCE_PENDING_REVIEW"
            or audit_summary.get("integration") != {
                "closing_marks_written": 0, "basis_bundle_modified": False,
                "status": "NOT_INTEGRATED",
            }):
        raise EvidenceError("Candidate audit is outside the approved policy root")

    probe = Path(audit_config.get("probe_path", "")).resolve()
    if output.is_relative_to(probe) or partial.is_relative_to(probe):
        raise EvidenceError("Policy output cannot be inside the sealed price probe")
    probe_config, _, probe_manifest = _verify_sealed_directory(
        probe, expected_manifest_sha256=root["probe_manifest_sha256"],
        expected_engine="polyledger-price-history-probe/1",
    )
    source, candidates, _ = _load_verified_probe(probe)
    _verify_complete_price_pages(probe, candidates)
    if (audit_config.get("probe_manifest_sha256") != root["probe_manifest_sha256"]
            or probe_manifest.get("input_sha256") != root["bundle_sha256"]
            or probe_config.get("input_sha256") != root["bundle_sha256"]
            or probe_config.get("input_digest") != root["bundle_digest"]):
        raise EvidenceError("Probe-to-bundle root of trust is inconsistent")
    gap = Path(probe_config.get("evidence_gap_queue", {}).get("path", "")).resolve()
    gap_config, _, gap_manifest = _verify_sealed_directory(
        gap, expected_manifest_sha256=root["evidence_gap_manifest_sha256"]
    )
    if output.is_relative_to(gap) or partial.is_relative_to(gap):
        raise EvidenceError("Policy output cannot be inside the sealed evidence-gap queue")
    if (probe_config["evidence_gap_queue"].get("manifest_sha256")
            != root["evidence_gap_manifest_sha256"]):
        raise EvidenceError("Probe-to-gap root of trust is inconsistent")
    bundle = Path(probe_config.get("input_path", "")).resolve()
    if not bundle.is_file() or output == bundle or partial == bundle:
        raise EvidenceError("Approved basis bundle is missing or overlaps output")
    bundle_root, bundle_manifest = _verify_bundle_directory(
        bundle, manifest_sha256=root["bundle_manifest_sha256"],
        bundle_sha256=root["bundle_sha256"], bundle_digest=root["bundle_digest"],
    )
    if output.is_relative_to(bundle_root) or partial.is_relative_to(bundle_root):
        raise EvidenceError("Policy output cannot be inside the sealed basis-bundle directory")
    bundle_before = _sha256(bundle)
    bundle_value = _load_json(bundle)
    if (bundle_before != root["bundle_sha256"] or digest(bundle_value) != root["bundle_digest"]
            or gap_config.get("input_sha256") != root["bundle_sha256"]
            or gap_config.get("input_digest") != root["bundle_digest"]):
        raise EvidenceError("Basis bundle no longer matches the approved root of trust")
    source_before = {
        "audit_manifest_sha256": root["audit_manifest_sha256"],
        "probe_manifest_sha256": root["probe_manifest_sha256"],
        "gap_manifest_sha256": root["evidence_gap_manifest_sha256"],
        "bundle_manifest_sha256": root["bundle_manifest_sha256"],
        "bundle_sha256": bundle_before,
    }

    closing_block = probe_config.get("closing_block")
    cutoff = uint(root["cutoff_timestamp"])
    if (not isinstance(closing_block, dict)
            or uint(closing_block.get("number")) != uint(root["closing_block_number"])
            or hex_bytes(closing_block.get("hash"), 32) != root["closing_block_hash"].lower()
            or uint(source["cutoff_timestamp"]) != cutoff):
        raise EvidenceError("Policy closing block or cutoff conflicts with the sealed probe")
    candidates_doc = _load_json(audit / "candidates.json")
    if (candidates_doc.get("candidates") != candidates
            or uint(candidates_doc.get("cutoff_timestamp")) != cutoff):
        raise EvidenceError("Candidate audit does not reproduce the sealed probe candidate set")
    entries, metadata_evidence = _reparse_audit(audit, candidates, candidates_doc, cutoff)
    block_timestamp = uint(source["summary"]["closing_block"]["timestamp"])
    if block_timestamp != cutoff + 1:
        raise EvidenceError("Closing block timestamp no longer matches the sealed cutoff rule")
    settlement = _dual_settlement(
        primary_rpc, secondary_rpc, policy=policy, entries=entries,
        block_timestamp=block_timestamp,
    )
    decisions = [_decision(entry, cutoff=cutoff, policy=policy, settlement=settlement)
                 for entry in entries]

    source_after = {
        "audit_manifest_sha256": _sha256(audit / "run_manifest.json"),
        "probe_manifest_sha256": _sha256(probe / "run_manifest.json"),
        "gap_manifest_sha256": _sha256(gap / "run_manifest.json"),
        "bundle_manifest_sha256": _sha256(bundle_root / "run_manifest.json"),
        "bundle_sha256": _sha256(bundle),
    }
    metadata_final = _metadata_sidecars(Path(metadata_evidence["before"]["database_path"]))
    metadata_evidence["final"] = metadata_final
    metadata_unchanged = (
        metadata_final["database_sha256"] == metadata_evidence["before"]["database_sha256"]
        and metadata_final["wal"] == metadata_evidence["before"]["wal"]
        and metadata_final["wal"]["length"] == 0
    )
    if (source_before != source_after or _sha256(policy_path) != policy_sha256
            or _files_sha256(audit) != audit_manifest["files"]
            or _files_sha256(probe) != probe_manifest["files"]
            or _files_sha256(gap) != gap_manifest["files"]
            or _files_sha256(bundle_root) != bundle_manifest["files"]
            or not metadata_unchanged):
        raise EvidenceError("A sealed source changed during policy evaluation")

    accepted = sum(row["status"] == "ACCEPTED_FOR_NEW_BUNDLE_COMPILATION" for row in decisions)
    deferred = sum(row["status"] == "DEFERRED_MISSING_HISTORICAL_IDENTITY" for row in decisions)
    rejected = len(decisions) - accepted - deferred
    summary = {
        "schema": POLICY_SCHEMA, "engine": POLICY_VERSION,
        "status": "POLICY_EVALUATION_COMPLETE_NOT_INTEGRATED",
        "policy_id": policy["policy_id"],
        "audit": {"path": str(audit), "manifest_sha256": root["audit_manifest_sha256"],
                  "candidate_count": len(decisions)},
        "settlement": {"status": settlement["agreement"]["status"],
                       "condition_count": settlement["agreement"]["condition_count"],
                       "unresolved_at_closing_block": sum(
                           row["status"] == "UNRESOLVED_AT_CLOSING_BLOCK"
                           for row in settlement["primary"]["conditions"].values()
                       ),
                       "settled_at_closing_block": sum(
                           row["status"] == "SETTLED_AT_CLOSING_BLOCK"
                           for row in settlement["primary"]["conditions"].values()
                       )},
        "decisions": {"accepted_for_new_bundle_compilation": accepted,
                      "deferred_missing_historical_identity": deferred,
                      "rejected_by_policy": rejected},
        "integration": {"closing_marks_written": 0, "basis_bundle_modified": False,
                        "status": "NOT_INTEGRATED"},
        "review_gate": {"status": "BLOCKED", "reasons": [
            "ACCEPTED_CANDIDATES_REQUIRE_NEW_SEALED_BUNDLE_COMPILATION",
            "CLOSING_MARKS_NOT_INTEGRATED", "EXTERNAL_FLOW_VALUE_EVIDENCE_REMAINS_OPEN",
            "INDEPENDENT_ACCOUNTING_REPORT_MISSING",
        ]},
        "safety": SAFETY,
    }
    configuration = {
        "schema": POLICY_SCHEMA, "engine": POLICY_VERSION,
        "policy_path": str(policy_path), "policy_sha256": policy_sha256,
        "audit_path": str(audit), "audit_manifest_sha256": root["audit_manifest_sha256"],
        "probe_path": str(probe), "probe_manifest_sha256": root["probe_manifest_sha256"],
        "evidence_gap_path": str(gap),
        "evidence_gap_manifest_sha256": root["evidence_gap_manifest_sha256"],
        "bundle_path": str(bundle), "bundle_sha256": root["bundle_sha256"],
        "bundle_manifest_sha256": root["bundle_manifest_sha256"],
        "bundle_digest": root["bundle_digest"], "source_hashes_before": source_before,
        "source_hashes_after": source_after, "metadata_evidence": metadata_evidence,
        "integration": "NONE; policy evaluation cannot write marks or modify a bundle",
        "safety": SAFETY,
    }
    partial.mkdir(parents=True)
    _write_new(partial / "configuration.json", configuration)
    _write_new(partial / "settlement_evidence.json", settlement)
    _write_new(partial / "decisions.json", decisions)
    _write_new(partial / "summary.json", summary)
    from .price_candidate_audit import _git_state
    project = Path(__file__).resolve().parents[3]
    commit, clean = _git_state(project)
    _write_new(partial / "run_manifest.json", {
        "schema": POLICY_SCHEMA, "engine": POLICY_VERSION, "completed_at": now_utc(),
        "code_commit": commit, "working_tree_clean": clean,
        "policy_sha256": policy_sha256,
        "audit_manifest_sha256": root["audit_manifest_sha256"],
        "files": _files_sha256(partial),
        "summary_sha256": _sha256(partial / "summary.json"), "safety": SAFETY,
    })
    os.replace(partial, output)
    return summary
