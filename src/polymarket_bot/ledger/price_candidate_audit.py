"""Audit fresh price-history candidates without integrating any price mark.

The audit is intentionally a read-only evidence step between a bounded price
probe and any future bundle rebuild.  It validates the probe's hashes and raw
price bytes, asks only documented public market-identity endpoints for the
candidate token IDs, and records whether the mappings agree.  A successful
audit is an eligibility finding for human policy review, never a mutation of
``closing_marks`` or an approval of P0.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from . import SAFETY
from .common import EvidenceError, canonical, hex_bytes, now_utc, uint, validate_exact
from .price_history_probe import (
    CTF_CONTRACT,
    MAX_RESPONSE_BYTES,
    PRICE_HISTORY_ENDPOINT,
    SAFE_RESPONSE_HEADERS,
    _error_text,
    _sha256,
    _write_new,
    _write_raw_new,
    parse_history_observation,
)


AUDIT_SCHEMA = 1
AUDIT_VERSION = "polyledger-price-candidate-audit/1"
CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
MAX_CANDIDATES = 20


class CandidateAuditRequestError(EvidenceError):
    """A bounded public request failed, with safe per-attempt evidence."""

    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 - stdlib signature
        return None


class OfficialCandidateMetadataClient:
    """GET-only transport restricted to three documented identity lookups."""

    def __init__(self, *, timeout_seconds: float = 20.0, retries: int = 2,
                 max_response_bytes: int = MAX_RESPONSE_BYTES):
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 60
                or type(retries) is not int or not 1 <= retries <= 3
                or type(max_response_bytes) is not int
                or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES):
            raise EvidenceError("Candidate-audit transport bounds are invalid")
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.max_response_bytes = max_response_bytes
        self._opener = urllib.request.build_opener(_NoRedirect())

    @staticmethod
    def _safe_headers(headers) -> dict:
        result = {}
        for key in SAFE_RESPONSE_HEADERS:
            value = headers.get(key)
            if value is not None:
                result[key] = str(value)[:300]
        return result

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise EvidenceError("Candidate-audit URL has an invalid port") from exc
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment
                or port not in {None, 443}):
            raise EvidenceError("Candidate-audit URL must be credential-free HTTPS")
        host = (parsed.hostname or "").lower()
        def is_condition_id(value: str) -> bool:
            try:
                return hex_bytes(value, 32) == value.lower()
            except EvidenceError:
                return False

        if host == "clob.polymarket.com" and not parsed.query:
            parts = parsed.path.split("/")
            if (len(parts) == 3 and parts[1] == "markets-by-token"
                    and _decimal_uint_string(parts[2])) or (
                len(parts) == 3 and parts[1] == "clob-markets"
                and is_condition_id(parts[2])
            ):
                return
        if host == "gamma-api.polymarket.com" and parsed.path == "/markets":
            query = parse_qs(parsed.query, keep_blank_values=True)
            values = query.get("condition_ids")
            if (set(query) == {"condition_ids", "limit"} and values is not None and len(values) == 1
                    and is_condition_id(values[0])
                    and query.get("limit") == ["10"]):
                return
        raise EvidenceError("Candidate-audit request is outside official endpoint allowlist")

    def fetch(self, url: str) -> dict:
        self._validate_url(url)
        attempts = []
        for attempt in range(1, self.retries + 1):
            started = time.monotonic()
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "polyledger-p0/candidate-audit"},
                method="GET",
            )
            try:
                with self._opener.open(request, timeout=self.timeout_seconds) as response:
                    body = response.read(self.max_response_bytes + 1)
                    latency_ms = int((time.monotonic() - started) * 1000)
                    if len(body) > self.max_response_bytes:
                        attempts.append({"attempt": attempt, "status": int(response.status),
                                         "latency_ms": latency_ms, "error": "RESPONSE_TOO_LARGE"})
                        raise CandidateAuditRequestError("Candidate-audit response exceeds byte limit", attempts)
                    headers = self._safe_headers(response.headers)
                    attempts.append({"attempt": attempt, "status": int(response.status),
                                     "latency_ms": latency_ms, "response_headers": headers})
                    return {"body": body, "status": int(response.status),
                            "response_headers": headers, "attempts": attempts}
            except CandidateAuditRequestError:
                raise
            except urllib.error.HTTPError as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                attempts.append({"attempt": attempt, "status": int(exc.code), "latency_ms": latency_ms,
                                 "response_headers": self._safe_headers(exc.headers),
                                 "error": "HTTP_" + str(exc.code)})
                retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                attempts.append({"attempt": attempt, "status": None, "latency_ms": latency_ms,
                                 "error": type(exc).__name__})
                retryable = True
            if retryable and attempt < self.retries:
                time.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))
                continue
            raise CandidateAuditRequestError("Official candidate-metadata request failed", attempts)
        raise AssertionError("Bounded candidate-audit transport exhausted unexpectedly")


def _reject_nonfinite(_: str):
    raise EvidenceError("Candidate-audit JSON contains non-finite number")


def _decimal_uint_string(value) -> bool:
    try:
        return isinstance(value, str) and str(uint(value)) == value
    except EvidenceError:
        return False


def _load_json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_float=str,
                           parse_constant=_reject_nonfinite)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, EvidenceError) as exc:
        raise EvidenceError(f"Candidate-audit JSON is invalid: {path.name}") from exc
    validate_exact(value)
    return value


def _files_sha256(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "run_manifest.json"
    }


def _git_state(project: Path) -> tuple[str, bool]:
    try:
        git = ["git", "-c", "safe.directory=" + str(project).replace("\\", "/"), "-C", str(project)]
        commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
        clean = not subprocess.check_output(git + ["status", "--porcelain"], text=True).strip()
        return commit, clean
    except (OSError, subprocess.CalledProcessError):
        return "unavailable", False


def _safe_relative(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value:
        raise EvidenceError("Probe raw-response path is invalid")
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise EvidenceError("Probe raw-response path leaves probe directory") from exc
    if not candidate.is_file():
        raise EvidenceError("Probe raw-response file is missing")
    return candidate


def _load_verified_probe(probe: Path) -> tuple[dict, list[dict], dict]:
    required = ("configuration.json", "closing_block_header.json", "request_results.json",
                "summary.json", "run_manifest.json")
    if not probe.is_dir() or not all((probe / name).is_file() for name in required):
        raise EvidenceError("Candidate audit requires a completed price-probe directory")
    configuration = _load_json(probe / "configuration.json")
    closing_header = _load_json(probe / "closing_block_header.json")
    results = _load_json(probe / "request_results.json")
    summary = _load_json(probe / "summary.json")
    manifest = _load_json(probe / "run_manifest.json")
    if not isinstance(results, list) or not isinstance(manifest.get("files"), dict):
        raise EvidenceError("Price-probe result or manifest is invalid")
    actual_files = _files_sha256(probe)
    if actual_files != manifest["files"] or manifest.get("summary_sha256") != _sha256(probe / "summary.json"):
        raise EvidenceError("Price-probe manifest hash verification failed")
    if (uint(configuration.get("schema")) != 1 or configuration.get("engine") != "polyledger-price-history-probe/1"
            or uint(summary.get("schema")) != 1 or summary.get("engine") != configuration.get("engine")
            or uint(manifest.get("schema")) != 1 or manifest.get("engine") != configuration.get("engine")
            or summary.get("status") != "PROBE_COMPLETE_EVIDENCE_PENDING_REVIEW"):
        raise EvidenceError("Price-probe contract is not supported for candidate audit")
    max_age = uint(configuration.get("maximum_observation_age_seconds"))
    if uint(summary.get("maximum_observation_age_seconds")) != max_age:
        raise EvidenceError("Price-probe freshness bounds disagree")
    cutoff = uint(summary.get("closing_block", {}).get("as_of_cutoff_timestamp"))
    block_timestamp = uint(summary.get("closing_block", {}).get("timestamp"))
    block_number = uint(summary.get("closing_block", {}).get("number"))
    block_hash = hex_bytes(summary.get("closing_block", {}).get("hash"), 32)
    configured_block = configuration.get("closing_block")
    primary_header = closing_header.get("primary_header")
    secondary_header = closing_header.get("secondary_header")
    if (not isinstance(configured_block, dict) or not isinstance(primary_header, dict)
            or not isinstance(secondary_header, dict)):
        raise EvidenceError("Price-probe closing block evidence is incomplete")
    if (closing_header.get("cutoff_rule") != "closing-block-timestamp-minus-one-second"
            or uint(closing_header.get("block_timestamp")) != block_timestamp
            or uint(closing_header.get("cutoff_timestamp")) != cutoff
            or cutoff + 1 != block_timestamp
            or uint(configured_block.get("number")) != block_number
            or hex_bytes(configured_block.get("hash"), 32) != block_hash
            or hex_bytes(closing_header.get("expected_hash"), 32) != block_hash
            or uint(primary_header.get("number")) != block_number
            or uint(secondary_header.get("number")) != block_number
            or hex_bytes(primary_header.get("hash"), 32) != block_hash
            or hex_bytes(secondary_header.get("hash"), 32) != block_hash
            or uint(primary_header.get("timestamp")) != block_timestamp
            or uint(secondary_header.get("timestamp")) != block_timestamp):
        raise EvidenceError("Price-probe closing block and cutoff evidence disagree")
    fresh = []
    seen_sequences: set[int] = set()
    seen_tokens: set[str] = set()
    for row in results:
        if not isinstance(row, dict) or row.get("status") != "FRESH_CANDIDATE":
            continue
        token = row.get("token_id")
        sequence = uint(row.get("sequence"))
        if sequence in seen_sequences or token in seen_tokens:
            raise EvidenceError("Price-probe fresh candidate set contains a duplicate")
        raw_path = _safe_relative(probe, row.get("raw_response_file"))
        raw = raw_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row.get("raw_response_sha256"):
            raise EvidenceError("Fresh candidate raw response hash mismatch")
        parsed = parse_history_observation(raw, cutoff_timestamp=cutoff)
        if parsed.get("status") != "OBSERVATION_AT_OR_BEFORE_CUT":
            raise EvidenceError("Fresh candidate no longer parses as an eligible observation")
        observation = parsed.get("observation")
        if (not _decimal_uint_string(token) or not isinstance(observation, dict)
                or observation != row.get("observation") or uint(observation["age_seconds"]) > max_age):
            raise EvidenceError("Fresh candidate temporal evidence does not match probe result")
        if (row.get("source_eligibility") != "CTF_OUTCOME_TOKEN"
                or row.get("asset") != f"137:{CTF_CONTRACT}:{token}"):
            raise EvidenceError("Fresh candidate asset is not the declared Polygon CTF token")
        expected_url = PRICE_HISTORY_ENDPOINT + "?" + urlencode({
            "token_id": token, "as_of": str(cutoff),
        })
        expected_request_sha256 = hashlib.sha256(
            ("GET\n" + expected_url + "\naccept:application/json\n").encode("utf-8")
        ).hexdigest()
        if (row.get("query_url") != expected_url or row.get("request_sha256") != expected_request_sha256
                or uint(row.get("cutoff_timestamp")) != cutoff or uint(row.get("http_status")) != 200):
            raise EvidenceError("Fresh candidate query identity or cutoff is invalid")
        seen_sequences.add(sequence)
        seen_tokens.add(token)
        fresh.append({
            "sequence": sequence, "asset": row.get("asset"), "token_id": token,
            "probe_status": row["status"], "observation": observation,
            "price_raw_sha256": row["raw_response_sha256"],
            "price_raw_file": row["raw_response_file"], "query_url": row.get("query_url"),
        })
    if not 1 <= len(fresh) <= MAX_CANDIDATES:
        raise EvidenceError("Price-probe has no bounded fresh-candidate set to audit")
    if uint(summary.get("responses", {}).get("fresh_candidates_at_or_before_cut")) != len(fresh):
        raise EvidenceError("Price-probe summary fresh-candidate count mismatch")
    return {
        "configuration": configuration,
        "summary": summary,
        "manifest_sha256": _sha256(probe / "run_manifest.json"),
        "summary_sha256": _sha256(probe / "summary.json"),
        "request_results_sha256": _sha256(probe / "request_results.json"),
        "cutoff_timestamp": cutoff,
        "maximum_observation_age_seconds": max_age,
    }, fresh, manifest


def _json_payload(raw: bytes, *, label: str):
    try:
        value = json.loads(raw.decode("utf-8"), parse_float=str, parse_constant=_reject_nonfinite)
    except (UnicodeDecodeError, json.JSONDecodeError, EvidenceError) as exc:
        raise EvidenceError(f"{label} response JSON is invalid") from exc
    validate_exact(value)
    return value


def _parse_parent_market(raw: bytes, token_id: str) -> dict:
    payload = _json_payload(raw, label="markets-by-token")
    if not isinstance(payload, dict):
        raise EvidenceError("markets-by-token response is not an object")
    condition = hex_bytes(payload.get("condition_id"), 32)
    primary, secondary = payload.get("primary_token_id"), payload.get("secondary_token_id")
    if not _decimal_uint_string(primary) or not _decimal_uint_string(secondary):
        raise EvidenceError("markets-by-token response has invalid token pair")
    if primary == secondary:
        raise EvidenceError("markets-by-token response has a duplicate token pair")
    if token_id == primary:
        role = "YES"
    elif token_id == secondary:
        role = "NO"
    else:
        raise EvidenceError("markets-by-token response does not contain requested token")
    return {"condition_id": condition, "primary_token_id": primary,
            "secondary_token_id": secondary, "expected_outcome": role}


def _parse_clob_market(raw: bytes, *, token_id: str, parent: dict) -> dict:
    payload = _json_payload(raw, label="clob-markets")
    rows = payload.get("t") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise EvidenceError("clob-markets response has no token list")
    if payload.get("condition_id") is not None and hex_bytes(payload["condition_id"], 32) != parent["condition_id"]:
        raise EvidenceError("clob-markets condition conflicts with markets-by-token")
    mapping = {}
    for row in rows:
        if not isinstance(row, dict) or not _decimal_uint_string(row.get("t")) or not isinstance(row.get("o"), str):
            raise EvidenceError("clob-markets token row is invalid")
        outcome = row["o"].strip().upper()
        if outcome in mapping:
            raise EvidenceError("clob-markets outcome mapping is ambiguous")
        mapping[outcome] = row["t"]
    expected_mapping = {"YES": parent["primary_token_id"], "NO": parent["secondary_token_id"]}
    if mapping != expected_mapping or mapping.get(parent["expected_outcome"]) != token_id:
        raise EvidenceError("clob-markets token pair conflicts with markets-by-token")
    return {"outcome": parent["expected_outcome"], "token_count": len(rows),
            "token_pair": expected_mapping}


def _string_json_list(value, *, label: str) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value, parse_float=str, parse_constant=_reject_nonfinite)
        except (json.JSONDecodeError, EvidenceError) as exc:
            raise EvidenceError(f"Gamma {label} is not a JSON list") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise EvidenceError(f"Gamma {label} is not a string list")
    return value


def _parse_gamma_market(raw: bytes, *, token_id: str, parent: dict) -> dict:
    payload = _json_payload(raw, label="Gamma markets")
    if not isinstance(payload, list):
        raise EvidenceError("Gamma markets response is not a list")
    matches = [row for row in payload if isinstance(row, dict)
               and isinstance(row.get("conditionId"), str)
               and hex_bytes(row["conditionId"], 32) == parent["condition_id"]]
    if len(matches) != 1:
        raise EvidenceError("Gamma markets response does not identify condition exactly once")
    row = matches[0]
    tokens = _string_json_list(row.get("clobTokenIds"), label="clobTokenIds")
    outcomes = _string_json_list(row.get("outcomes"), label="outcomes")
    if len(tokens) != len(outcomes) or tokens.count(token_id) != 1:
        raise EvidenceError("Gamma market does not map requested token to outcome")
    mapping = {}
    for token, outcome_raw in zip(tokens, outcomes, strict=True):
        if not _decimal_uint_string(token):
            raise EvidenceError("Gamma token ID is invalid")
        outcome = outcome_raw.strip().upper()
        if outcome in mapping:
            raise EvidenceError("Gamma outcome mapping is ambiguous")
        mapping[outcome] = token
    expected_mapping = {"YES": parent["primary_token_id"], "NO": parent["secondary_token_id"]}
    if mapping != expected_mapping or mapping.get(parent["expected_outcome"]) != token_id:
        raise EvidenceError("Gamma token pair conflicts with CLOB parent market")
    fields = ("id", "question", "slug", "startDate", "endDate", "closedTime", "active", "closed",
              "negRisk", "resolutionSource")
    return {"outcome": parent["expected_outcome"], "token_pair": expected_mapping,
            "market": {field: row.get(field) for field in fields}}


def _iso_timestamp(value) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return int(parsed.astimezone(timezone.utc).timestamp())
    except ValueError:
        return None


def _current_window_status(market: dict, cutoff_timestamp: int) -> str:
    start, end = _iso_timestamp(market.get("startDate")), _iso_timestamp(market.get("endDate"))
    if start is None or end is None:
        return "CURRENT_METADATA_WINDOW_UNAVAILABLE"
    return ("CUTOFF_WITHIN_CURRENT_DECLARED_WINDOW" if start <= cutoff_timestamp <= end
            else "CUTOFF_OUTSIDE_CURRENT_DECLARED_WINDOW")


def _lookup_local_metadata(path: Path | None, candidates: list[dict], cutoff_timestamp: int) -> dict:
    if path is None or not path.is_file():
        return {"status": "NOT_AVAILABLE", "rows": {}, "path": None, "sha256": None}
    resolved = path.resolve()
    before_sha256 = _sha256(resolved)
    try:
        uri = resolved.as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            source_rows = connection.execute(
                "SELECT condition_id,event_slug,outcomes_json,token_ids_json "
                "FROM market_metadata ORDER BY condition_id,event_slug"
            ).fetchall()
            fetches = dict(connection.execute(
                "SELECT event_slug,fetched_at FROM event_metadata WHERE error IS NULL"
            ).fetchall())
    except (OSError, sqlite3.Error) as exc:
        return {"status": "READ_ERROR", "rows": {}, "path": str(resolved),
                "sha256": before_sha256, "error": _error_text(exc)}
    after_sha256 = _sha256(resolved)
    if before_sha256 != after_sha256:
        return {"status": "UNSTABLE_DURING_READ", "rows": {}, "path": str(resolved),
                "sha256": after_sha256}
    desired = {row["token_id"] for row in candidates}
    observations = {token: [] for token in desired}
    for condition, event_slug, outcomes_raw, tokens_raw in source_rows:
        try:
            outcomes = _string_json_list(outcomes_raw, label="cached outcomes")
            tokens = _string_json_list(tokens_raw, label="cached token IDs")
        except EvidenceError:
            continue
        if len(outcomes) != len(tokens):
            continue
        for token in desired.intersection(tokens):
            fetched_at = fetches.get(event_slug)
            condition_id = None
            try:
                condition_id = hex_bytes(condition, 32)
            except EvidenceError:
                pass
            token_indexes = [index for index, value in enumerate(tokens) if value == token]
            observations[token].append({
                "condition_id": condition_id, "event_slug": event_slug,
                "outcome": outcomes[token_indexes[0]] if len(token_indexes) == 1 else None,
                "token_occurrences": len(token_indexes), "fetched_at": fetched_at,
                "fetched_at_or_before_cutoff": isinstance(fetched_at, int) and fetched_at <= cutoff_timestamp,
            })
    matches = {}
    for token, rows in observations.items():
        if not rows:
            continue
        if (len(rows) != 1 or rows[0]["condition_id"] is None
                or rows[0]["token_occurrences"] != 1 or not isinstance(rows[0]["outcome"], str)):
            matches[token] = {"mapping_status": "CONFLICT_OR_AMBIGUOUS", "row_count": len(rows)}
        else:
            matches[token] = {"mapping_status": "UNIQUE", **rows[0]}
    return {"status": "AVAILABLE", "rows": matches, "path": str(resolved),
            "sha256": after_sha256}


def _request_url(kind: str, value: str) -> str:
    if kind == "parent":
        return CLOB_BASE + "/markets-by-token/" + value
    if kind == "clob":
        return CLOB_BASE + "/clob-markets/" + value
    if kind == "gamma":
        return GAMMA_BASE + "/markets?" + urlencode({"condition_ids": value, "limit": "10"})
    raise EvidenceError("Unknown candidate-audit request kind")


def _capture_request(partial: Path, client, *, request_id: str, kind: str, value: str) -> tuple[dict, bytes | None]:
    url = _request_url(kind, value)
    record = {
        "request_id": request_id,
        "kind": kind,
        "query_url": url,
        "request_sha256": hashlib.sha256(("GET\n" + url + "\naccept:application/json\n").encode("utf-8")).hexdigest(),
        "started_at": now_utc(),
    }
    try:
        response = client.fetch(url)
    except CandidateAuditRequestError as exc:
        record.update({"received_at": now_utc(), "status": "REQUEST_ERROR", "detail": _error_text(exc),
                       "attempts": exc.attempts})
        return record, None
    raw = response["body"]
    filename = request_id + ".raw.json"
    raw_path = partial / "responses" / filename
    _write_raw_new(raw_path, raw)
    record.update({
        "received_at": now_utc(), "status": "HTTP_OK", "http_status": response["status"],
        "response_headers": response["response_headers"], "attempts": response["attempts"],
        "raw_response_file": "responses/" + filename,
        "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
    })
    return record, raw


def _candidate_review(candidate: dict, *, parent: dict | None, clob: dict | None,
                      gamma: dict | None, local: dict | None, cutoff_timestamp: int) -> dict:
    blockers = []
    if parent is None:
        blockers.append("CLOB_PARENT_MARKET_UNVERIFIED")
    if clob is None:
        blockers.append("CLOB_OUTCOME_LABEL_UNVERIFIED")
    if gamma is None:
        blockers.append("CURRENT_GAMMA_MARKET_UNVERIFIED")
    if parent is not None and clob is not None and gamma is not None:
        identity_status = "CURRENT_OFFICIAL_IDENTITY_CONSISTENT"
    else:
        identity_status = "CURRENT_OFFICIAL_IDENTITY_INCOMPLETE"
    local_status = "NOT_AVAILABLE"
    if local is not None:
        if local.get("mapping_status") != "UNIQUE":
            local_status = "AUXILIARY_MAPPING_CONFLICT"
            blockers.append("LOCAL_METADATA_MAPPING_CONFLICT")
        elif (parent is not None and local["condition_id"] == parent["condition_id"]
                and local["outcome"].strip().upper() == parent["expected_outcome"]):
            local_status = ("AUXILIARY_MAPPING_BEFORE_CUTOFF" if local["fetched_at_or_before_cutoff"]
                            else "AUXILIARY_MAPPING_AFTER_CUTOFF")
        else:
            local_status = "AUXILIARY_MAPPING_CONFLICT"
            blockers.append("LOCAL_METADATA_MAPPING_CONFLICT")
    market = gamma.get("market") if gamma else {}
    return {
        "sequence": candidate["sequence"], "asset": candidate["asset"], "token_id": candidate["token_id"],
        "price_candidate": candidate["observation"],
        "probe_raw": {"file": candidate["price_raw_file"], "sha256": candidate["price_raw_sha256"]},
        "current_identity": {
            "status": identity_status,
            "condition_id": parent.get("condition_id") if parent else None,
            "outcome": parent.get("expected_outcome") if parent else None,
            "market": market,
            "current_declared_window_at_cutoff": _current_window_status(market, cutoff_timestamp) if gamma else None,
        },
        "auxiliary_local_metadata": {"status": local_status, "row": local},
        "integration": {
            "status": "PENDING_MARK_POLICY_REVIEW" if not blockers else "NOT_ELIGIBLE_FOR_POLICY_REVIEW",
            "closing_mark_written": False,
            "basis_bundle_modified": False,
            "reasons": sorted(set(blockers + ["AUDIT_DOES_NOT_AUTOMATICALLY_INTEGRATE_MARK"])),
        },
    }


def audit_price_candidates_file(probe: Path, output: Path, *, client: OfficialCandidateMetadataClient,
                                metadata_db: Path | None = None) -> dict:
    """Seal a fresh-candidate identity audit in a brand-new local directory."""
    probe, output = Path(probe).resolve(), Path(output).resolve()
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists():
        raise EvidenceError("Candidate-audit output or partial directory already exists")
    if output.is_relative_to(probe) or partial.is_relative_to(probe):
        raise EvidenceError("Candidate-audit output cannot be inside the sealed probe")
    source, candidates, probe_manifest = _load_verified_probe(probe)
    local_metadata = _lookup_local_metadata(metadata_db, candidates, source["cutoff_timestamp"])
    partial.mkdir(parents=True)
    _write_new(partial / "configuration.json", {
        "schema": AUDIT_SCHEMA,
        "engine": AUDIT_VERSION,
        "probe_path": str(probe),
        "probe_manifest_sha256": source["manifest_sha256"],
        "probe_summary_sha256": source["summary_sha256"],
        "probe_request_results_sha256": source["request_results_sha256"],
        "probe_code_commit": probe_manifest.get("code_commit"),
        "fresh_candidate_count": len(candidates),
        "official_sources": {
            "parent_by_token": CLOB_BASE + "/markets-by-token/{token_id}",
            "clob_market": CLOB_BASE + "/clob-markets/{condition_id}",
            "gamma_market": GAMMA_BASE + "/markets?condition_ids={condition_id}&limit=10",
        },
        "integration": "NONE; audit cannot write closing_marks or modify a basis bundle",
        "safety": SAFETY,
    })
    _write_new(partial / "candidates.json", {
        "cutoff_timestamp": source["cutoff_timestamp"],
        "maximum_observation_age_seconds": source["maximum_observation_age_seconds"],
        "candidates": candidates,
        "local_metadata": {key: value for key, value in local_metadata.items() if key != "rows"},
    })
    (partial / "responses").mkdir()
    requests, reviews = [], []
    for candidate in candidates:
        prefix = f"{candidate['sequence']:02d}_{candidate['token_id']}"
        parent_request, parent_raw = _capture_request(
            partial, client, request_id=prefix + "_parent", kind="parent", value=candidate["token_id"]
        )
        requests.append(parent_request)
        try:
            parent = _parse_parent_market(parent_raw, candidate["token_id"]) if parent_raw else None
        except EvidenceError as exc:
            parent, parent_request["parse_error"] = None, _error_text(exc)
        clob = gamma = None
        if parent is not None:
            clob_request, clob_raw = _capture_request(
                partial, client, request_id=prefix + "_clob", kind="clob", value=parent["condition_id"]
            )
            requests.append(clob_request)
            try:
                clob = _parse_clob_market(
                    clob_raw, token_id=candidate["token_id"], parent=parent
                ) if clob_raw else None
            except EvidenceError as exc:
                clob, clob_request["parse_error"] = None, _error_text(exc)
            gamma_request, gamma_raw = _capture_request(
                partial, client, request_id=prefix + "_gamma", kind="gamma", value=parent["condition_id"]
            )
            requests.append(gamma_request)
            try:
                gamma = _parse_gamma_market(
                    gamma_raw, token_id=candidate["token_id"], parent=parent
                ) if gamma_raw else None
            except EvidenceError as exc:
                gamma, gamma_request["parse_error"] = None, _error_text(exc)
        reviews.append(_candidate_review(
            candidate, parent=parent, clob=clob, gamma=gamma,
            local=local_metadata["rows"].get(candidate["token_id"]),
            cutoff_timestamp=source["cutoff_timestamp"],
        ))
    _write_new(partial / "request_results.json", requests)
    _write_new(partial / "candidate_reviews.json", reviews)
    eligible = sum(row["integration"]["status"] == "PENDING_MARK_POLICY_REVIEW" for row in reviews)
    summary = {
        "schema": AUDIT_SCHEMA,
        "engine": AUDIT_VERSION,
        "status": "AUDIT_COMPLETE_EVIDENCE_PENDING_REVIEW",
        "probe": {"path": str(probe), "cutoff_timestamp": source["cutoff_timestamp"],
                  "fresh_candidates": len(candidates)},
        "requests": {
            "attempted": len(requests),
            "raw_saved": sum("raw_response_file" in row for row in requests),
            "transport_errors": sum(row["status"] != "HTTP_OK" for row in requests),
            "parse_errors": sum("parse_error" in row for row in requests),
        },
        "reviews": {
            "current_official_identity_consistent": sum(
                row["current_identity"]["status"] == "CURRENT_OFFICIAL_IDENTITY_CONSISTENT" for row in reviews
            ),
            "eligible_for_mark_policy_review": eligible,
            "not_eligible": len(reviews) - eligible,
            "auxiliary_local_mapping_before_cutoff": sum(
                row["auxiliary_local_metadata"]["status"] == "AUXILIARY_MAPPING_BEFORE_CUTOFF" for row in reviews
            ),
        },
        "integration": {"closing_marks_written": 0, "basis_bundle_modified": False,
                        "status": "NOT_INTEGRATED"},
        "review_gate": {"status": "BLOCKED", "reasons": [
            "MARK_POLICY_REVIEW_REQUIRED", "CLOSING_MARK_EVIDENCE_NOT_YET_INTEGRATED",
            "EXTERNAL_FLOW_VALUE_EVIDENCE_REMAINS_OPEN", "INDEPENDENT_ACCOUNTING_REPORT_MISSING",
        ]},
        "safety": SAFETY,
    }
    _write_new(partial / "summary.json", summary)
    project = Path(__file__).resolve().parents[3]
    commit, clean = _git_state(project)
    _write_new(partial / "run_manifest.json", {
        "schema": AUDIT_SCHEMA, "engine": AUDIT_VERSION, "completed_at": now_utc(),
        "code_commit": commit, "working_tree_clean": clean, "probe_path": str(probe),
        "probe_manifest_sha256": source["manifest_sha256"], "files": _files_sha256(partial),
        "summary_sha256": _sha256(partial / "summary.json"), "safety": SAFETY,
    })
    os.replace(partial, output)
    return summary
