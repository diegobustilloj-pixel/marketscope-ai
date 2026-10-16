"""Resumable, read-only recovery of near-cutoff CLOB price history.

This collector consumes only the sealed ``STALE_CANDIDATE`` queue.  It asks
Polymarket's public CLOB price-history endpoint for the original permitted
time window at one-minute fidelity, stores response bytes verbatim, and
classifies possible recovery candidates.  A candidate remains evidence only:
this module cannot write closing marks or mutate a basis bundle.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from . import SAFETY
from .closing_mark_gap_queue import GAP_QUEUE_VERSION, STALE
from .common import EvidenceError, canonical, digest, now_utc, uint, validate_exact
from .price_candidate_audit import _files_sha256, _git_state, _load_json, _sha256
from .price_history_probe import (
    CTF_CONTRACT,
    MAX_RESPONSE_BYTES,
    SAFE_RESPONSE_HEADERS,
    _asset_parts,
    _write_new,
    _write_raw_new,
)
from .price_mark_policy import _verify_sealed_directory


RECOVERY_SCHEMA = 1
RECOVERY_VERSION = "polyledger-stale-clob-history-recovery/1"
BATCH_VERSION = "polyledger-stale-clob-history-batch/1"
CLOB_HISTORY_ENDPOINT = "https://clob.polymarket.com/prices-history"
RECOVERED = "FRESH_RECOVERY_CANDIDATE"
NO_WINDOW_OBSERVATION = "NO_OBSERVATION_IN_ALLOWED_WINDOW"
BOUNDARY_ONLY = "BOUNDARY_PRICE_ONLY"
MALFORMED = "MALFORMED_RESPONSE"
SOURCE_REJECTED = "SOURCE_HTTP_REJECTED"
REQUEST_ERROR = "REQUEST_ERROR"
ALLOWED_RESULT_STATUSES = {
    RECOVERED,
    NO_WINDOW_OBSERVATION,
    BOUNDARY_ONLY,
    MALFORMED,
    SOURCE_REJECTED,
    REQUEST_ERROR,
}
MAX_BATCH_SIZE = 100
MAX_WORKERS = 16


class StaleHistoryRequestError(EvidenceError):
    """A bounded public CLOB request exhausted its safe retries."""

    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


class OfficialClobHistoryClient:
    """Restricted GET-only client for the official public CLOB history API."""

    def __init__(self, *, timeout_seconds: float = 20.0, retries: int = 3,
                 max_response_bytes: int = MAX_RESPONSE_BYTES):
        if (isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not 1 <= timeout_seconds <= 60
                or type(retries) is not int or not 1 <= retries <= 3
                or type(max_response_bytes) is not int
                or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES):
            raise EvidenceError("Stale-history transport bounds are invalid")
        self.timeout_seconds = float(timeout_seconds)
        self.retries = retries
        self.max_response_bytes = max_response_bytes
        self._opener = urllib.request.build_opener(_NoRedirect())

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlsplit(url)
        expected = urlsplit(CLOB_HISTORY_ENDPOINT)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.hostname != expected.hostname or parsed.path != expected.path
                or parsed.port not in {None, 443}):
            raise EvidenceError("Stale-history request is outside the official allowlist")

    @staticmethod
    def _safe_headers(headers) -> dict:
        result = {}
        for key in SAFE_RESPONSE_HEADERS:
            value = headers.get(key)
            if value is not None:
                result[key] = str(value)[:300]
        return result

    def _read_bounded(self, response, attempts: list[dict], attempt: int,
                      latency_ms: int) -> bytes:
        body = response.read(self.max_response_bytes + 1)
        if len(body) > self.max_response_bytes:
            attempts.append({
                "attempt": attempt,
                "status": int(response.status),
                "latency_ms": latency_ms,
                "error": "RESPONSE_TOO_LARGE",
            })
            raise StaleHistoryRequestError(
                "Stale-history response exceeds byte limit", attempts,
            )
        return body

    def fetch(self, url: str) -> dict:
        self._validate_url(url)
        attempts = []
        for attempt in range(1, self.retries + 1):
            started = time.monotonic()
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "polyledger-p0/stale-history-recovery",
                },
                method="GET",
            )
            try:
                with self._opener.open(request, timeout=self.timeout_seconds) as response:
                    latency_ms = int((time.monotonic() - started) * 1000)
                    body = self._read_bounded(response, attempts, attempt, latency_ms)
                    headers = self._safe_headers(response.headers)
                    attempts.append({
                        "attempt": attempt,
                        "status": int(response.status),
                        "latency_ms": latency_ms,
                        "response_headers": headers,
                    })
                    return {
                        "body": body,
                        "status": int(response.status),
                        "response_headers": headers,
                        "attempts": attempts,
                    }
            except StaleHistoryRequestError:
                raise
            except urllib.error.HTTPError as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                headers = self._safe_headers(exc.headers)
                retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
                attempt_row = {
                    "attempt": attempt,
                    "status": int(exc.code),
                    "latency_ms": latency_ms,
                    "response_headers": headers,
                    "error": "HTTP_" + str(exc.code),
                }
                if not retryable:
                    body = self._read_bounded(exc, attempts, attempt, latency_ms)
                    attempts.append(attempt_row)
                    return {
                        "body": body,
                        "status": int(exc.code),
                        "response_headers": headers,
                        "attempts": attempts,
                    }
                attempts.append(attempt_row)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                attempts.append({
                    "attempt": attempt,
                    "status": None,
                    "latency_ms": latency_ms,
                    "error": type(exc).__name__,
                })
            if attempt < self.retries:
                time.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))
                continue
            raise StaleHistoryRequestError(
                "Official CLOB stale-history request failed", attempts,
            )
        raise AssertionError("Bounded stale-history transport exhausted unexpectedly")


def _error_text(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text[:300]}"


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _reject_nonfinite(_: str):
    raise EvidenceError("CLOB price-history response contains a non-finite number")


def parse_clob_window(raw: bytes, *, start_timestamp: int,
                      cutoff_timestamp: int, fidelity_minutes: int) -> dict:
    """Validate one CLOB history response and select only an in-window point."""
    try:
        payload = json.loads(
            raw.decode("utf-8"), parse_float=str, parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, EvidenceError) as exc:
        return {"status": MALFORMED, "detail": _error_text(exc)}
    if not isinstance(payload, dict) or not isinstance(payload.get("history"), list):
        return {"status": MALFORMED, "detail": "response has no history array"}
    by_time: dict[int, str] = {}
    before_window = 0
    after_cutoff = 0
    boundary_prices = 0
    for point in payload["history"]:
        if not isinstance(point, dict):
            return {"status": MALFORMED, "detail": "history point is not an object"}
        try:
            timestamp = uint(point.get("t"))
            raw_price = point.get("p")
            if type(raw_price) not in {int, str}:
                raise EvidenceError("history price is not an exact JSON number")
            price = Decimal(str(raw_price))
            if not price.is_finite() or not Decimal(0) <= price <= Decimal(1):
                raise EvidenceError("history price is outside [0,1]")
            price_text = _decimal_text(price)
        except (EvidenceError, InvalidOperation, ValueError) as exc:
            return {"status": MALFORMED, "detail": _error_text(exc)}
        if timestamp < start_timestamp:
            before_window += 1
            continue
        if timestamp > cutoff_timestamp:
            after_cutoff += 1
            continue
        if price in {Decimal(0), Decimal(1)}:
            boundary_prices += 1
            continue
        prior = by_time.get(timestamp)
        if prior is not None and prior != price_text:
            return {"status": MALFORMED, "detail": "conflicting prices at one timestamp"}
        by_time[timestamp] = price_text
    diagnostics = {
        "points_before_window": before_window,
        "points_after_cutoff": after_cutoff,
        "boundary_price_points": boundary_prices,
        "eligible_interior_points": len(by_time),
    }
    if by_time:
        timestamp = max(by_time)
        return {
            "status": RECOVERED,
            "observation": {
                "timestamp": timestamp,
                "price": by_time[timestamp],
                "age_seconds": cutoff_timestamp - timestamp,
                "source_fidelity_seconds": fidelity_minutes * 60,
            },
            "diagnostics": diagnostics,
        }
    if boundary_prices:
        return {"status": BOUNDARY_ONLY, "diagnostics": diagnostics}
    return {"status": NO_WINDOW_OBSERVATION, "diagnostics": diagnostics}


def _request_url(token_id: str, start_timestamp: int, cutoff_timestamp: int,
                 fidelity_minutes: int) -> str:
    return CLOB_HISTORY_ENDPOINT + "?" + urlencode({
        "market": token_id,
        "startTs": str(start_timestamp),
        "endTs": str(cutoff_timestamp),
        "fidelity": str(fidelity_minutes),
    })


def _atomic_json(path: Path, value) -> None:
    validate_exact(value)
    temporary = path.with_name(path.name + ".new")
    if temporary.exists():
        raise EvidenceError("Stale-history checkpoint temporary file already exists")
    with temporary.open("x", encoding="utf-8") as target:
        target.write(canonical(value) + "\n")
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


def _load_stale_queue(gap_queue: Path) -> tuple[list[dict], dict, dict, str]:
    manifest_sha = _sha256(gap_queue / "run_manifest.json")
    configuration, summary, manifest = _verify_sealed_directory(
        gap_queue,
        expected_manifest_sha256=manifest_sha,
        expected_engine=GAP_QUEUE_VERSION,
    )
    rows = _load_json(gap_queue / "stale_candidates.json")
    if not isinstance(rows, list):
        raise EvidenceError("Stale-candidate queue is invalid")
    seen_assets = set()
    seen_queue_sequences = set()
    normalized = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("asset"), str):
            raise EvidenceError("Stale-candidate row is invalid")
        asset, contract, token_id = _asset_parts(row["asset"])
        quantity = uint(row.get("quantity_atomic"))
        queue_sequence = uint(row.get("queue_sequence"))
        source_sequence = uint(row.get("sequence"))
        cutoff = uint(row.get("cutoff_timestamp"))
        maximum_age = uint(row.get("maximum_age_seconds"))
        observation = row.get("diagnostic_observation_only")
        if (contract != CTF_CONTRACT or quantity == 0 or maximum_age == 0
                or row.get("category") != STALE
                or row.get("route") != "RECOVER_FRESH_HISTORICAL_PRICE"
                or row.get("eligible_as_closing_mark") is not False
                or not isinstance(observation, dict)
                or uint(observation.get("age_seconds")) <= maximum_age
                or cutoff - uint(observation.get("timestamp"))
                != uint(observation.get("age_seconds"))
                or asset in seen_assets or queue_sequence in seen_queue_sequences):
            raise EvidenceError("Stale-candidate row conflicts with recovery policy")
        seen_assets.add(asset)
        seen_queue_sequences.add(queue_sequence)
        normalized.append({
            "asset": asset,
            "token_id": token_id,
            "quantity_atomic": quantity,
            "source_sequence": source_sequence,
            "source_queue_sequence": queue_sequence,
            "cutoff_timestamp": cutoff,
            "maximum_age_seconds": maximum_age,
            "original_stale_observation": observation,
        })
    normalized.sort(key=lambda row: row["source_queue_sequence"])
    expected = uint(summary.get("category_counts", {}).get(STALE))
    if (len(normalized) != expected
            or manifest.get("gap_queue_digest") is None
            or len(normalized) != len(seen_assets)):
        raise EvidenceError("Stale-candidate queue does not reconcile with its seal")
    return normalized, configuration, summary, manifest_sha


def _validate_runtime(*, batch_size: int, workers: int, retries: int,
                      timeout_seconds: float, fidelity_minutes: int,
                      batch_delay_seconds: float, max_batches: int | None) -> None:
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise EvidenceError(f"Stale-history batch size must be 1..{MAX_BATCH_SIZE}")
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise EvidenceError(f"Stale-history workers must be 1..{MAX_WORKERS}")
    if type(retries) is not int or not 1 <= retries <= 3:
        raise EvidenceError("Stale-history retries must be 1..3")
    if (isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= timeout_seconds <= 60):
        raise EvidenceError("Stale-history timeout must be 1..60 seconds")
    if type(fidelity_minutes) is not int or not 1 <= fidelity_minutes <= 60:
        raise EvidenceError("Stale-history fidelity must be 1..60 minutes")
    if (isinstance(batch_delay_seconds, bool)
            or not isinstance(batch_delay_seconds, (int, float))
            or not 0 <= batch_delay_seconds <= 60):
        raise EvidenceError("Stale-history batch delay must be 0..60 seconds")
    if max_batches is not None and (type(max_batches) is not int or max_batches < 1):
        raise EvidenceError("Stale-history max batches must be positive")


def _initial_configuration(gap_queue: Path, rows: list[dict], manifest_sha: str,
                           *, code_commit: str, clean: bool, batch_size: int,
                           workers: int, retries: int, timeout_seconds: float,
                           fidelity_minutes: int, batch_delay_seconds: float) -> dict:
    return {
        "schema": RECOVERY_SCHEMA,
        "engine": RECOVERY_VERSION,
        "created_at": now_utc(),
        "code_commit": code_commit,
        "working_tree_clean": clean,
        "gap_queue_path": str(gap_queue),
        "gap_queue_manifest_sha256": manifest_sha,
        "stale_candidates_sha256": _sha256(gap_queue / "stale_candidates.json"),
        "stale_candidates_digest": digest(rows),
        "stale_candidates": len(rows),
        "official_source": {
            "endpoint": CLOB_HISTORY_ENDPOINT,
            "request_shape": "GET market + startTs + endTs + fidelity",
            "authentication": "NONE_PUBLIC_READ_ONLY",
            "raw_response_storage": "verbatim-bytes",
        },
        "window_policy": {
            "start_timestamp": "cutoff_timestamp - original maximum_age_seconds",
            "end_timestamp": "original cutoff_timestamp",
            "fidelity_minutes": fidelity_minutes,
            "strict_interior_price": True,
            "automatic_promotion": False,
        },
        "execution": {
            "batch_size": batch_size,
            "workers": workers,
            "retries": retries,
            "timeout_seconds": str(Decimal(str(timeout_seconds))),
            "batch_delay_seconds": str(Decimal(str(batch_delay_seconds))),
        },
        "integration": "NONE; recovered points remain candidates for a new audit",
        "safety": SAFETY,
    }


def _new_state(configuration: dict) -> dict:
    return {
        "schema": RECOVERY_SCHEMA,
        "engine": RECOVERY_VERSION,
        "status": "RUNNING",
        "code_commit": configuration["code_commit"],
        "started_at": now_utc(),
        "updated_at": now_utc(),
        "total_stale_candidates": configuration["stale_candidates"],
        "next_index": 0,
        "batches_completed": 0,
        "aggregate_status_counts": {},
        "batches": [],
        "integration": {
            "closing_marks_written": 0,
            "basis_bundle_modified": False,
            "gap_queue_modified": False,
            "status": "NOT_INTEGRATED",
        },
        "safety": SAFETY,
    }


def _fetch_one(client, row: dict, fidelity_minutes: int) -> dict:
    start = row["cutoff_timestamp"] - row["maximum_age_seconds"]
    url = _request_url(row["token_id"], start, row["cutoff_timestamp"], fidelity_minutes)
    base = {
        **row,
        "window_start_timestamp": start,
        "window_end_timestamp": row["cutoff_timestamp"],
        "fidelity_minutes": fidelity_minutes,
        "query_url": url,
        "request_sha256": hashlib.sha256(
            ("GET\n" + url + "\naccept:application/json\n").encode("utf-8")
        ).hexdigest(),
        "started_at": now_utc(),
    }
    try:
        response = client.fetch(url)
    except StaleHistoryRequestError as exc:
        return {
            **base,
            "received_at": now_utc(),
            "status": REQUEST_ERROR,
            "detail": _error_text(exc),
            "attempts": exc.attempts,
        }
    except Exception as exc:  # Defensive boundary for injected/test transports.
        return {
            **base,
            "received_at": now_utc(),
            "status": REQUEST_ERROR,
            "detail": _error_text(exc),
            "attempts": [],
        }
    return {**base, "received_at": now_utc(), "_response": response}


def _run_batch(output: Path, configuration: dict, rows: list[dict], *,
               start_index: int, client, workers: int,
               fidelity_minutes: int) -> dict:
    end_index = start_index + len(rows)
    batch_id = f"batch_{start_index + 1:05d}_{end_index:05d}"
    batches_root = output / "batches"
    batch = batches_root / batch_id
    partial = batches_root / (batch_id + ".partial")
    if batch.exists() or partial.exists():
        raise EvidenceError("Stale-history batch output or partial already exists")
    partial.mkdir(parents=True)
    responses = partial / "responses"
    responses.mkdir()
    request_rows = []
    for row in rows:
        start = row["cutoff_timestamp"] - row["maximum_age_seconds"]
        url = _request_url(row["token_id"], start, row["cutoff_timestamp"], fidelity_minutes)
        request_rows.append({
            "asset": row["asset"],
            "token_id": row["token_id"],
            "source_queue_sequence": row["source_queue_sequence"],
            "window_start_timestamp": start,
            "window_end_timestamp": row["cutoff_timestamp"],
            "maximum_age_seconds": row["maximum_age_seconds"],
            "fidelity_minutes": fidelity_minutes,
            "query_url": url,
        })
    _write_new(partial / "configuration.json", {
        "schema": RECOVERY_SCHEMA,
        "engine": BATCH_VERSION,
        "batch_id": batch_id,
        "start_index": start_index,
        "end_index_exclusive": end_index,
        "gap_queue_manifest_sha256": configuration["gap_queue_manifest_sha256"],
        "stale_candidates_sha256": configuration["stale_candidates_sha256"],
        "official_source": configuration["official_source"],
        "window_policy": configuration["window_policy"],
        "integration": "NONE; batch output is recovery evidence only",
        "safety": SAFETY,
    })
    _write_new(partial / "requests.json", request_rows)
    fetched: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(rows))) as executor:
        futures = {
            executor.submit(_fetch_one, client, row, fidelity_minutes): index
            for index, row in enumerate(rows)
        }
        for future in as_completed(futures):
            fetched[futures[future]] = future.result()
    results = []
    for index in range(len(rows)):
        result = fetched[index]
        response = result.pop("_response", None)
        if response is not None:
            raw = response.get("body")
            status_code = response.get("status")
            if (not isinstance(raw, bytes) or type(status_code) is not int
                    or not isinstance(response.get("attempts"), list)
                    or not isinstance(response.get("response_headers"), dict)):
                result.update({
                    "status": REQUEST_ERROR,
                    "detail": "Injected transport returned an invalid response envelope",
                    "attempts": [],
                })
            else:
                raw_name = f"{start_index + index + 1:05d}.raw.json"
                _write_raw_new(responses / raw_name, raw)
                result.update({
                    "http_status": status_code,
                    "response_headers": response["response_headers"],
                    "attempts": response["attempts"],
                    "raw_response_file": "responses/" + raw_name,
                    "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
                })
                if status_code == 200:
                    result.update(parse_clob_window(
                        raw,
                        start_timestamp=result["window_start_timestamp"],
                        cutoff_timestamp=result["window_end_timestamp"],
                        fidelity_minutes=fidelity_minutes,
                    ))
                else:
                    result.update({
                        "status": SOURCE_REJECTED,
                        "detail": "Official CLOB history endpoint returned HTTP "
                        + str(status_code),
                    })
        if result.get("status") not in ALLOWED_RESULT_STATUSES:
            raise EvidenceError("Stale-history batch produced an unsupported status")
        result["eligible_as_closing_mark"] = False
        results.append(result)
    _write_new(partial / "results.json", results)
    counts = Counter(row["status"] for row in results)
    summary = {
        "schema": RECOVERY_SCHEMA,
        "engine": BATCH_VERSION,
        "status": "BATCH_COMPLETE_EVIDENCE_NOT_INTEGRATED",
        "batch_id": batch_id,
        "coverage": {
            "start_index": start_index,
            "end_index_exclusive": end_index,
            "attempted": len(results),
        },
        "status_counts": dict(sorted(counts.items())),
        "recovered_candidates": counts[RECOVERED],
        "integration": {
            "closing_marks_written": 0,
            "basis_bundle_modified": False,
            "status": "NOT_INTEGRATED",
        },
        "safety": SAFETY,
    }
    _write_new(partial / "summary.json", summary)
    _write_new(partial / "run_manifest.json", {
        "schema": RECOVERY_SCHEMA,
        "engine": BATCH_VERSION,
        "completed_at": now_utc(),
        "code_commit": configuration["code_commit"],
        "working_tree_clean": configuration["working_tree_clean"],
        "gap_queue_manifest_sha256": configuration["gap_queue_manifest_sha256"],
        "files": _files_sha256(partial),
        "summary_sha256": _sha256(partial / "summary.json"),
        "results_digest": digest(results),
        "safety": SAFETY,
    })
    os.replace(partial, batch)
    return {
        "batch_id": batch_id,
        "path": str(batch),
        "manifest_sha256": _sha256(batch / "run_manifest.json"),
        "start_index": start_index,
        "end_index_exclusive": end_index,
        "attempted": len(results),
        "status_counts": dict(sorted(counts.items())),
        "recovered_candidates": counts[RECOVERED],
    }


def _verify_batch(receipt: dict, configuration: dict) -> tuple[list[dict], dict]:
    batch = Path(receipt.get("path", "")).resolve()
    batch_configuration, summary, manifest = _verify_sealed_directory(
        batch,
        expected_manifest_sha256=receipt.get("manifest_sha256"),
        expected_engine=BATCH_VERSION,
    )
    results = _load_json(batch / "results.json")
    requests = _load_json(batch / "requests.json")
    if not isinstance(results, list) or not isinstance(requests, list):
        raise EvidenceError("Stale-history batch payload is invalid")
    counts = Counter(row.get("status") for row in results if isinstance(row, dict))
    start = uint(receipt.get("start_index"))
    end = uint(receipt.get("end_index_exclusive"))
    if (batch_configuration.get("gap_queue_manifest_sha256")
            != configuration["gap_queue_manifest_sha256"]
            or batch_configuration.get("stale_candidates_sha256")
            != configuration["stale_candidates_sha256"]
            or uint(batch_configuration.get("start_index")) != start
            or uint(batch_configuration.get("end_index_exclusive")) != end
            or len(results) != len(requests) or len(results) != end - start
            or summary.get("status") != "BATCH_COMPLETE_EVIDENCE_NOT_INTEGRATED"
            or summary.get("status_counts") != dict(sorted(counts.items()))
            or summary.get("integration") != {
                "closing_marks_written": 0,
                "basis_bundle_modified": False,
                "status": "NOT_INTEGRATED",
            }
            or manifest.get("results_digest") != digest(results)
            or receipt.get("status_counts") != dict(sorted(counts.items()))
            or uint(receipt.get("attempted")) != len(results)
            or uint(receipt.get("recovered_candidates")) != counts[RECOVERED]):
        raise EvidenceError("Stale-history batch does not reconcile with its receipt")
    return results, summary


def _verify_state(output: Path, configuration: dict, state: dict) -> tuple[list[dict], Counter]:
    if (state.get("engine") != RECOVERY_VERSION
            or state.get("code_commit") != configuration["code_commit"]
            or uint(state.get("total_stale_candidates")) != configuration["stale_candidates"]
            or state.get("integration") != {
                "closing_marks_written": 0,
                "basis_bundle_modified": False,
                "gap_queue_modified": False,
                "status": "NOT_INTEGRATED",
            }
            or not isinstance(state.get("batches"), list)):
        raise EvidenceError("Stale-history checkpoint is invalid")
    cursor = 0
    all_results = []
    aggregate: Counter[str] = Counter()
    for receipt in state["batches"]:
        if uint(receipt.get("start_index")) != cursor:
            raise EvidenceError("Stale-history batches have a gap or overlap")
        results, _ = _verify_batch(receipt, configuration)
        cursor = uint(receipt.get("end_index_exclusive"))
        all_results.extend(results)
        aggregate.update(row["status"] for row in results)
    if (cursor != uint(state.get("next_index"))
            or len(state["batches"]) != uint(state.get("batches_completed"))
            or dict(sorted(aggregate.items())) != state.get("aggregate_status_counts")):
        raise EvidenceError("Stale-history checkpoint does not reproduce from sealed batches")
    return all_results, aggregate


def _state_receipt(output: Path, state: dict) -> dict:
    return {
        "schema": RECOVERY_SCHEMA,
        "engine": RECOVERY_VERSION,
        "status": state["status"],
        "output": str(output),
        "progress": {
            "completed": state["next_index"],
            "total": state["total_stale_candidates"],
            "remaining": state["total_stale_candidates"] - state["next_index"],
            "batches_completed": state["batches_completed"],
        },
        "status_counts": state["aggregate_status_counts"],
        "integration": state["integration"],
        "safety": SAFETY,
    }


def _finalize(output: Path, configuration: dict, state: dict,
              all_results: list[dict], aggregate: Counter) -> dict:
    recovered = []
    unresolved = []
    batch_by_range = {
        (row["start_index"], row["end_index_exclusive"]): row
        for row in state["batches"]
    }
    for index, result in enumerate(all_results):
        receipt = next(
            row for (start, end), row in batch_by_range.items() if start <= index < end
        )
        provenance = {
            "batch_id": receipt["batch_id"],
            "batch_path": receipt["path"],
            "batch_manifest_sha256": receipt["manifest_sha256"],
            "raw_response_file": result.get("raw_response_file"),
            "raw_response_sha256": result.get("raw_response_sha256"),
            "request_sha256": result.get("request_sha256"),
        }
        if result["status"] == RECOVERED:
            recovered.append({
                "asset": result["asset"],
                "token_id": result["token_id"],
                "quantity_atomic": result["quantity_atomic"],
                "source_queue_sequence": result["source_queue_sequence"],
                "cutoff_timestamp": result["cutoff_timestamp"],
                "maximum_age_seconds": result["maximum_age_seconds"],
                "observation": result["observation"],
                "source": "OFFICIAL_CLOB_PRICE_HISTORY_WINDOW",
                "provenance": provenance,
                "eligible_as_closing_mark": False,
            })
        else:
            if result["status"] == NO_WINDOW_OBSERVATION:
                route = "ALTERNATE_ARCHIVE_REQUIRED"
            elif result["status"] == REQUEST_ERROR:
                route = "RETRY_OFFICIAL_CLOB_HISTORY"
            elif result["status"] == SOURCE_REJECTED:
                route = "REVIEW_TOKEN_CLOB_AVAILABILITY"
            else:
                route = "MANUAL_SOURCE_CONTRACT_REVIEW"
            unresolved.append({
                "asset": result["asset"],
                "token_id": result["token_id"],
                "quantity_atomic": result["quantity_atomic"],
                "source_queue_sequence": result["source_queue_sequence"],
                "status": result["status"],
                "next_route": route,
                "provenance": provenance,
                "eligible_as_closing_mark": False,
            })
    if len(recovered) + len(unresolved) != len(all_results):
        raise EvidenceError("Stale-history final partition does not reconcile")
    _write_new(output / "recovered_candidates.json", recovered)
    _write_new(output / "unresolved_queue.json", unresolved)
    review_reasons = [
        "RECOVERED_POINTS_REQUIRE_NEW_IDENTITY_AND_POLICY_AUDIT",
        "NO_RECOVERY_CANDIDATE_IS_AN_APPROVED_MARK",
        "EXTERNAL_FLOW_VALUE_EVIDENCE_REMAINS_OPEN",
        "INDEPENDENT_ACCOUNTING_REPORT_MISSING",
    ]
    if unresolved:
        review_reasons.insert(1, "UNRESOLVED_STALE_QUEUE_REMAINS")
    summary = {
        "schema": RECOVERY_SCHEMA,
        "engine": RECOVERY_VERSION,
        "status": "RECOVERY_COMPLETE_EVIDENCE_NOT_INTEGRATED",
        "coverage": {
            "stale_candidates": configuration["stale_candidates"],
            "attempted": len(all_results),
            "batches": len(state["batches"]),
            "remaining_unattempted": configuration["stale_candidates"] - len(all_results),
        },
        "status_counts": dict(sorted(aggregate.items())),
        "recovered_candidates": len(recovered),
        "unresolved_candidates": len(unresolved),
        "integration": {
            "closing_marks_written": 0,
            "basis_bundle_modified": False,
            "gap_queue_modified": False,
            "recovered_candidates_promoted": 0,
            "freshness_threshold_modified": False,
            "status": "NOT_INTEGRATED",
        },
        "review_gate": {
            "status": "BLOCKED",
            "reasons": review_reasons,
        },
        "safety": SAFETY,
    }
    _write_new(output / "summary.json", summary)
    state["status"] = "COMPLETE"
    state["updated_at"] = now_utc()
    _atomic_json(output / "state.json", state)
    batch_manifests = {
        receipt["batch_id"]: receipt["manifest_sha256"] for receipt in state["batches"]
    }
    _write_new(output / "run_manifest.json", {
        "schema": RECOVERY_SCHEMA,
        "engine": RECOVERY_VERSION,
        "completed_at": now_utc(),
        "code_commit": configuration["code_commit"],
        "working_tree_clean": configuration["working_tree_clean"],
        "gap_queue_manifest_sha256": configuration["gap_queue_manifest_sha256"],
        "batch_manifests": batch_manifests,
        "files": _files_sha256(output),
        "summary_sha256": _sha256(output / "summary.json"),
        "recovered_candidates_digest": digest(recovered),
        "unresolved_queue_digest": digest(unresolved),
        "safety": SAFETY,
    })
    return summary


def _verified_complete(output: Path) -> dict:
    configuration, summary, manifest = _verify_sealed_directory(
        output,
        expected_manifest_sha256=_sha256(output / "run_manifest.json"),
        expected_engine=RECOVERY_VERSION,
    )
    state = _load_json(output / "state.json")
    all_results, aggregate = _verify_state(output, configuration, state)
    recovered = _load_json(output / "recovered_candidates.json")
    unresolved = _load_json(output / "unresolved_queue.json")
    expected_batch_manifests = {
        receipt["batch_id"]: receipt["manifest_sha256"] for receipt in state["batches"]
    }
    if (not isinstance(recovered, list) or not isinstance(unresolved, list)
            or state.get("status") != "COMPLETE"
            or uint(state.get("next_index")) != configuration.get("stale_candidates")
            or len(all_results) != configuration.get("stale_candidates")
            or len(recovered) + len(unresolved) != len(all_results)
            or len(recovered) != summary.get("recovered_candidates")
            or len(unresolved) != summary.get("unresolved_candidates")
            or dict(sorted(aggregate.items())) != summary.get("status_counts")
            or manifest.get("batch_manifests") != expected_batch_manifests
            or manifest.get("recovered_candidates_digest") != digest(recovered)
            or manifest.get("unresolved_queue_digest") != digest(unresolved)):
        raise EvidenceError("Completed stale-history recovery does not reconcile")
    return summary


def recover_stale_clob_history(gap_queue: Path, output: Path, *,
                               batch_size: int = 20, workers: int = 8,
                               retries: int = 3, timeout_seconds: float = 20.0,
                               fidelity_minutes: int = 1,
                               batch_delay_seconds: float = 0.1,
                               max_batches: int | None = None,
                               resume: bool = False, client=None,
                               require_clean_git: bool = True) -> dict:
    """Capture the original allowed window for every sealed stale candidate."""
    _validate_runtime(
        batch_size=batch_size, workers=workers, retries=retries,
        timeout_seconds=timeout_seconds, fidelity_minutes=fidelity_minutes,
        batch_delay_seconds=batch_delay_seconds, max_batches=max_batches,
    )
    gap_queue = Path(gap_queue).resolve()
    output = Path(output).resolve()
    project = Path(__file__).resolve().parents[3]
    commit, clean = _git_state(project)
    if require_clean_git and (commit == "unavailable" or not clean):
        raise EvidenceError("Stale-history recovery requires a clean Git commit")
    rows, _, _, gap_manifest_sha = _load_stale_queue(gap_queue)
    expected_configuration = _initial_configuration(
        gap_queue, rows, gap_manifest_sha, code_commit=commit, clean=clean,
        batch_size=batch_size, workers=workers, retries=retries,
        timeout_seconds=timeout_seconds, fidelity_minutes=fidelity_minutes,
        batch_delay_seconds=batch_delay_seconds,
    )
    if output.exists():
        if not resume:
            raise EvidenceError("Stale-history recovery output exists; use --resume")
        configuration = _load_json(output / "configuration.json")
        state = _load_json(output / "state.json")
        comparable = dict(expected_configuration)
        comparable["created_at"] = configuration.get("created_at")
        if configuration != comparable:
            raise EvidenceError("Stale-history resume configuration changed")
        if (output / "run_manifest.json").is_file():
            return _verified_complete(output)
    else:
        if resume:
            raise EvidenceError("Stale-history recovery cannot resume a missing output")
        output.mkdir(parents=True)
        (output / "batches").mkdir()
        configuration = expected_configuration
        state = _new_state(configuration)
        _write_new(output / "configuration.json", configuration)
        _write_new(output / "state.json", state)
    if any((output / "batches").glob("*.partial")):
        raise EvidenceError("Stale-history recovery has an incomplete batch requiring inspection")
    all_results, aggregate = _verify_state(output, configuration, state)
    if state["next_index"] == len(rows):
        return _finalize(output, configuration, state, all_results, aggregate)
    if client is None:
        client = OfficialClobHistoryClient(
            timeout_seconds=timeout_seconds, retries=retries,
        )
    state["status"] = "RUNNING"
    state["updated_at"] = now_utc()
    _atomic_json(output / "state.json", state)
    completed_this_run = 0
    while state["next_index"] < len(rows):
        if max_batches is not None and completed_this_run >= max_batches:
            state["status"] = "PAUSED_BATCH_LIMIT"
            state["updated_at"] = now_utc()
            _atomic_json(output / "state.json", state)
            return _state_receipt(output, state)
        start = state["next_index"]
        selected = rows[start:start + batch_size]
        receipt = _run_batch(
            output, configuration, selected, start_index=start,
            client=client, workers=workers, fidelity_minutes=fidelity_minutes,
        )
        state["batches"].append(receipt)
        state["next_index"] = receipt["end_index_exclusive"]
        state["batches_completed"] = len(state["batches"])
        aggregate.update(receipt["status_counts"])
        state["aggregate_status_counts"] = dict(sorted(aggregate.items()))
        state["updated_at"] = now_utc()
        _atomic_json(output / "state.json", state)
        completed_this_run += 1
        if (state["next_index"] < len(rows) and batch_delay_seconds
                and (max_batches is None or completed_this_run < max_batches)):
            time.sleep(batch_delay_seconds)
    all_results, aggregate = _verify_state(output, configuration, state)
    if (_sha256(gap_queue / "run_manifest.json") != gap_manifest_sha
            or _sha256(gap_queue / "stale_candidates.json")
            != configuration["stale_candidates_sha256"]):
        raise EvidenceError("Sealed stale-candidate queue changed during recovery")
    return _finalize(output, configuration, state, all_results, aggregate)


def stale_history_recovery_status(output: Path) -> dict:
    """Read and verify a recovery checkpoint without network access."""
    output = Path(output).resolve()
    if not output.is_dir():
        raise EvidenceError("Stale-history recovery output is missing")
    if (output / "run_manifest.json").is_file():
        summary = _verified_complete(output)
        return {
            "status": "COMPLETE",
            "output": str(output),
            "coverage": summary["coverage"],
            "status_counts": summary["status_counts"],
            "recovered_candidates": summary["recovered_candidates"],
            "unresolved_candidates": summary["unresolved_candidates"],
            "integration": summary["integration"],
            "safety": SAFETY,
        }
    configuration = _load_json(output / "configuration.json")
    state = _load_json(output / "state.json")
    _verify_state(output, configuration, state)
    return _state_receipt(output, state)
