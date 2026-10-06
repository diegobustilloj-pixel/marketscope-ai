"""Bounded, sealed probe of Polymarket's official historical price source.

This module is deliberately narrower than a valuation engine.  It anchors a
small deterministic CTF-token sample to the exact closing block of a sealed
basis bundle, saves the unmodified public API bytes, and records whether each
response contains an observation at or before that block's timestamp.  It
does not add marks to a bundle, calculate PnL, or enable execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from . import SAFETY
from .common import EvidenceError, address, canonical, digest, hex_bytes, now_utc, uint, validate_exact


PROBE_SCHEMA = 1
PROBE_VERSION = "polyledger-price-history-probe/1"
PRICE_HISTORY_ENDPOINT = "https://data-api.polymarket.com/v2/prices-history"
CTF_CONTRACT = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
COMBO_CONTRACT = "0x006f54f7f9a22e0000cc2ab60031000000ae9fef"
PUSD_CONTRACT = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
MAX_SAMPLE_SIZE = 20
SELECTION_POLICY = "first-sorted-ctf-closing-mark-request-v1"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
SAFE_RESPONSE_HEADERS = ("content-type", "content-length", "retry-after", "date")


class PriceHistoryRequestError(EvidenceError):
    """Bounded public-source error retaining non-sensitive attempt metadata."""

    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 - stdlib signature
        return None


class OfficialPriceHistoryClient:
    """Restricted GET-only transport for the single documented point-in-time API.

    It accepts no credentials or arbitrary hosts, follows no redirects, records
    a small safe subset of response headers, and retains only bounded response
    bytes.  It contains no wallet, order, signing, or execution capability.
    """

    def __init__(self, *, timeout_seconds: float = 20.0, retries: int = 2,
                 max_response_bytes: int = MAX_RESPONSE_BYTES):
        if not 1 <= retries <= 3 or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise EvidenceError("Price-history transport bounds are invalid")
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.max_response_bytes = max_response_bytes
        self._opener = urllib.request.build_opener(_NoRedirect())

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlsplit(url)
        expected = urlsplit(PRICE_HISTORY_ENDPOINT)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.hostname != expected.hostname or parsed.path != expected.path
                or parsed.port not in {None, 443}):
            raise EvidenceError("Price-history request is outside the official allowlist")

    @staticmethod
    def _safe_headers(headers) -> dict:
        result = {}
        for key in SAFE_RESPONSE_HEADERS:
            value = headers.get(key)
            if value is not None:
                result[key] = str(value)[:300]
        return result

    def fetch(self, url: str) -> dict:
        self._validate_url(url)
        attempts = []
        for attempt in range(1, self.retries + 1):
            started = time.monotonic()
            request = urllib.request.Request(
                url, headers={"Accept": "application/json", "User-Agent": "polyledger-p0/price-probe"},
                method="GET",
            )
            try:
                with self._opener.open(request, timeout=self.timeout_seconds) as response:
                    body = response.read(self.max_response_bytes + 1)
                    latency_ms = int((time.monotonic() - started) * 1000)
                    if len(body) > self.max_response_bytes:
                        attempts.append({"attempt": attempt, "status": int(response.status),
                                         "latency_ms": latency_ms, "error": "RESPONSE_TOO_LARGE"})
                        raise PriceHistoryRequestError("Price-history response exceeds byte limit", attempts)
                    attempts.append({"attempt": attempt, "status": int(response.status),
                                     "latency_ms": latency_ms,
                                     "response_headers": self._safe_headers(response.headers)})
                    return {"body": body, "status": int(response.status),
                            "response_headers": self._safe_headers(response.headers),
                            "attempts": attempts}
            except PriceHistoryRequestError:
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
            raise PriceHistoryRequestError("Official price-history request failed", attempts)
        raise AssertionError("Bounded price-history transport exhausted unexpectedly")


def _sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _write_new(path: Path, value) -> None:
    validate_exact(value)
    with path.open("x", encoding="utf-8") as target:
        target.write(canonical(value) + "\n")
        target.flush()
        os.fsync(target.fileno())


def _write_raw_new(path: Path, value: bytes) -> None:
    """Store public response bytes verbatim, rather than reserializing JSON."""
    with path.open("xb") as target:
        target.write(value)
        target.flush()
        os.fsync(target.fileno())


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _asset_parts(asset: str) -> tuple[str, str, str]:
    if not isinstance(asset, str):
        raise EvidenceError("Closing inventory asset must be a string")
    pieces = asset.split(":")
    if len(pieces) != 3 or uint(pieces[0]) != 137:
        raise EvidenceError("Closing inventory asset is not normalized Polygon namespace")
    contract = address(pieces[1])
    token = pieces[2]
    if token != "erc20":
        token = str(uint(token))
    normalized = f"137:{contract}:{token}"
    if normalized != asset:
        raise EvidenceError("Closing inventory asset is not normalized")
    return normalized, contract, token


def _load_validated_mark_queue(gap_path: Path, *, bundle_path: Path, bundle: dict) -> tuple[dict[str, int], dict]:
    """Verify the sealed evidence-gap queue before it can drive a probe."""
    required = (
        "configuration.json", "closing_mark_requests.json", "summary.json", "run_manifest.json",
    )
    if not gap_path.is_dir() or not all((gap_path / name).is_file() for name in required):
        raise EvidenceError("Price probe requires a completed sealed evidence-gap directory")
    try:
        configuration = json.loads((gap_path / "configuration.json").read_text(encoding="utf-8"))
        requests = json.loads((gap_path / "closing_mark_requests.json").read_text(encoding="utf-8"))
        summary = json.loads((gap_path / "summary.json").read_text(encoding="utf-8"))
        manifest = json.loads((gap_path / "run_manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("Price-probe evidence-gap files are not valid JSON") from exc
    validate_exact(configuration)
    validate_exact(requests)
    validate_exact(summary)
    validate_exact(manifest)
    declared_files = manifest.get("files")
    if not isinstance(declared_files, dict):
        raise EvidenceError("Evidence-gap manifest file hashes are missing")
    for name, expected_hash in declared_files.items():
        path = gap_path / name
        if (not isinstance(name, str) or not path.is_file() or not isinstance(expected_hash, str)
                or _sha256(path) != expected_hash):
            raise EvidenceError("Evidence-gap manifest file hash mismatch")
    input_sha256 = _sha256(bundle_path)
    if (configuration.get("input_sha256") != input_sha256
            or configuration.get("input_digest") != digest(bundle)
            or manifest.get("input_sha256") != input_sha256
            or uint(summary.get("schema")) != 1):
        raise EvidenceError("Evidence-gap queue is not linked to this sealed basis bundle")
    request_hash = digest(requests)
    if (summary.get("request_hashes", {}).get("closing_mark_requests") != request_hash
            or uint(summary.get("closing_marks", {}).get("requests")) != len(requests)):
        raise EvidenceError("Evidence-gap closing-mark request hash mismatch")
    rows: dict[str, int] = {}
    for row in requests:
        if not isinstance(row, dict) or not isinstance(row.get("asset"), str):
            raise EvidenceError("Evidence-gap closing-mark request is invalid")
        asset, _, _ = _asset_parts(row["asset"])
        quantity = uint(row.get("quantity_atomic"))
        required_reasons = row.get("required")
        if (asset in rows or not isinstance(required_reasons, list)
                or "CLOSING_MARK_MISSING" not in required_reasons):
            raise EvidenceError("Evidence-gap closing-mark request has invalid scope")
        rows[asset] = quantity
    return rows, {
        "path": str(gap_path),
        "configuration_sha256": _sha256(gap_path / "configuration.json"),
        "summary_sha256": _sha256(gap_path / "summary.json"),
        "manifest_sha256": _sha256(gap_path / "run_manifest.json"),
        "closing_mark_requests_sha256": _sha256(gap_path / "closing_mark_requests.json"),
        "closing_mark_requests_digest": request_hash,
        "closing_mark_requests": len(rows),
    }


def select_ctf_probe_assets(bundle: dict, *, closing_mark_requests: dict[str, int],
                            sample_size: int) -> tuple[list[dict], dict]:
    """Select the first canonical CTF queue rows and account for exclusions."""
    validate_exact(bundle)
    if type(sample_size) is not int or not 1 <= sample_size <= MAX_SAMPLE_SIZE:
        raise EvidenceError(f"Price probe sample size must be 1..{MAX_SAMPLE_SIZE}")
    if uint(bundle.get("schema")) != 1 or uint(bundle.get("chain")) != 137:
        raise EvidenceError("Price probe only supports schema 1 Polygon basis bundles")
    closing = bundle.get("closing")
    quote_asset = bundle.get("quote_asset")
    if not isinstance(closing, dict) or not isinstance(closing.get("balances"), dict):
        raise EvidenceError("Basis bundle has no closing balance snapshot")
    if not isinstance(quote_asset, str):
        raise EvidenceError("Basis bundle quote asset is invalid")

    eligible: list[dict] = []
    exclusions: Counter[str] = Counter()
    positive_non_quote = 0
    for raw_asset, raw_quantity in sorted(closing["balances"].items()):
        quantity = uint(raw_quantity)
        asset, contract, token = _asset_parts(raw_asset)
        if not quantity or asset == quote_asset:
            continue
        positive_non_quote += 1
        if contract == CTF_CONTRACT:
            queued_quantity = closing_mark_requests.get(asset)
            if queued_quantity is None:
                exclusions["CTF_NOT_IN_CLOSING_MARK_QUEUE"] += 1
            elif queued_quantity != quantity:
                raise EvidenceError("Closing-mark queue quantity differs from basis bundle closing inventory")
            else:
                eligible.append({"asset": asset, "token_id": token, "quantity_atomic": quantity})
        elif contract == COMBO_CONTRACT:
            exclusions["COMBO_POSITION_NOT_CLOB_OUTCOME"] += 1
        elif contract == PUSD_CONTRACT:
            exclusions["PUSD_COLLATERAL_NOT_CLOB_OUTCOME"] += 1
        else:
            exclusions["NON_CTF_ASSET_NOT_CLOB_OUTCOME"] += 1

    if not eligible:
        raise EvidenceError("Closing inventory has no positive CTF outcome eligible for price-history probe")
    chosen = eligible[:sample_size]
    rows = [
        {
            "sequence": index,
            "asset": row["asset"],
            "token_id": row["token_id"],
            "quantity_atomic": row["quantity_atomic"],
            "source_eligibility": "CTF_OUTCOME_TOKEN",
        }
        for index, row in enumerate(chosen, 1)
    ]
    coverage = {
        "positive_non_quote_assets": positive_non_quote,
        "eligible_ctf_outcomes": len(eligible),
        "selected_ctf_outcomes": len(rows),
        "excluded_assets_by_reason": dict(sorted(exclusions.items())),
        "selection_policy": SELECTION_POLICY,
    }
    return rows, coverage


def _request_url(token_id: str, cutoff_timestamp: int) -> str:
    return PRICE_HISTORY_ENDPOINT + "?" + urlencode({
        "token_id": token_id,
        "as_of": str(cutoff_timestamp),
    })


def _reject_nonfinite(_: str):
    raise EvidenceError("Price-history response contains a non-finite JSON number")


def _point_value(item: dict, short_key: str, long_key: str):
    short_present, long_present = short_key in item, long_key in item
    if not short_present and not long_present:
        raise EvidenceError(f"Price-history point has no {short_key}/{long_key}")
    if short_present and long_present and item[short_key] != item[long_key]:
        raise EvidenceError(f"Price-history point conflicts on {short_key}/{long_key}")
    return item[short_key] if short_present else item[long_key]


def parse_history_observation(raw: bytes, *, cutoff_timestamp: int) -> dict:
    """Validate the response shape and retain only a non-future observation.

    A returned value is still only *candidate evidence*.  This parser never
    promotes it into a closing mark or changes the input bundle.
    """
    try:
        payload = json.loads(
            raw.decode("utf-8"), parse_float=str, parse_constant=_reject_nonfinite
        )
    except (UnicodeDecodeError, json.JSONDecodeError, EvidenceError) as exc:
        return {"status": "MALFORMED_RESPONSE", "detail": _error_text(exc)}
    if not isinstance(payload, dict) or not isinstance(payload.get("history"), list):
        return {"status": "MALFORMED_RESPONSE", "detail": "history array is missing"}

    by_time: dict[int, str] = {}
    future_points = 0
    for item in payload["history"]:
        if not isinstance(item, dict):
            return {"status": "MALFORMED_RESPONSE", "detail": "history item is not an object"}
        try:
            timestamp = uint(_point_value(item, "t", "timestamp"))
            raw_price = _point_value(item, "p", "price")
            if type(raw_price) not in {int, str}:
                raise EvidenceError("Price-history price is not an exact number")
            price = Decimal(str(raw_price))
            if not price.is_finite() or not Decimal(0) <= price <= Decimal(1):
                raise EvidenceError("Price-history price is outside [0,1]")
            price_text = _decimal_text(price)
        except (EvidenceError, InvalidOperation, ValueError) as exc:
            return {"status": "MALFORMED_RESPONSE", "detail": _error_text(exc)}
        if timestamp > cutoff_timestamp:
            future_points += 1
            continue
        prior = by_time.get(timestamp)
        if prior is not None and prior != price_text:
            return {"status": "MALFORMED_RESPONSE", "detail": "conflicting prices at one timestamp"}
        by_time[timestamp] = price_text

    if future_points:
        return {
            "status": "FUTURE_OBSERVATION_REJECTED",
            "future_points": future_points,
            "eligible_points": len(by_time),
        }
    if not by_time:
        return {"status": "NO_OBSERVATION", "eligible_points": 0}
    timestamp = max(by_time)
    return {
        "status": "OBSERVATION_AT_OR_BEFORE_CUT",
        "observation": {
            "timestamp": timestamp,
            "price": by_time[timestamp],
            "age_seconds": cutoff_timestamp - timestamp,
        },
        "eligible_points": len(by_time),
    }


def _error_text(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text[:300]}"


def _git_state(project: Path) -> tuple[str, bool]:
    try:
        git = ["git", "-c", "safe.directory=" + str(project).replace("\\", "/"), "-C", str(project)]
        commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
        clean = not subprocess.check_output(git + ["status", "--porcelain"], text=True).strip()
        return commit, clean
    except (OSError, subprocess.CalledProcessError):
        return "unavailable", False


def _files_sha256(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "run_manifest.json"
    }


def _verify_closing_header(rpc, *, closing: dict) -> tuple[dict, int]:
    block_number = uint(closing.get("block_number"))
    expected_hash = hex_bytes(closing.get("block_hash"), 32)
    if uint(rpc.call("eth_chainId", [])) != 137:
        raise EvidenceError("Price-probe RPC is not Polygon chain 137")
    header = rpc.block(block_number)
    if not isinstance(header, dict):
        raise EvidenceError("Price-probe RPC returned a malformed block header")
    if uint(header.get("number")) != block_number:
        raise EvidenceError("Price-probe RPC returned the wrong closing block number")
    if hex_bytes(header.get("hash"), 32) != expected_hash:
        raise EvidenceError("Price-probe RPC closing block hash differs from sealed basis bundle")
    timestamp = uint(header.get("timestamp"))
    if timestamp == 0:
        raise EvidenceError("Price-probe closing block timestamp must be positive")
    return header, timestamp


def _verify_dual_closing_header(primary_rpc, secondary_rpc, *, closing: dict) -> tuple[dict, dict, int]:
    primary_header, primary_timestamp = _verify_closing_header(primary_rpc, closing=closing)
    secondary_header, secondary_timestamp = _verify_closing_header(secondary_rpc, closing=closing)
    if primary_timestamp != secondary_timestamp:
        raise EvidenceError("Independent Polygon RPCs disagree on closing block timestamp")
    if hex_bytes(primary_header["hash"], 32) != hex_bytes(secondary_header["hash"], 32):
        raise EvidenceError("Independent Polygon RPCs disagree on closing block hash")
    if primary_timestamp < 2:
        raise EvidenceError("Price-probe closing timestamp cannot support a pre-block cutoff")
    # Polygon block timestamps are second-granular.  Use the preceding second
    # so a trade that happened later within the closing block cannot leak into
    # a historical candidate mark.
    return primary_header, secondary_header, primary_timestamp - 1


def _rpc_host(rpc) -> str:
    url = getattr(rpc, "url", None)
    if not isinstance(url, str) or not url:
        raise EvidenceError("Price-probe RPC has no declared HTTPS URL")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise EvidenceError("Price-probe RPC URL must be public HTTPS")
    return parsed.hostname.lower()


def build_price_history_probe_file(
    bundle_path: Path,
    evidence_gaps: Path,
    output: Path,
    *,
    rpc,
    secondary_rpc,
    client: OfficialPriceHistoryClient,
    sample_size: int = MAX_SAMPLE_SIZE,
    max_age_seconds: int,
) -> dict:
    """Run one bounded price-source probe into a new sealed local directory."""
    bundle_path, evidence_gaps, output = (Path(bundle_path).resolve(), Path(evidence_gaps).resolve(),
                                          Path(output).resolve())
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists():
        raise EvidenceError("Price-probe output or partial directory already exists")
    if type(max_age_seconds) is not int or max_age_seconds < 0:
        raise EvidenceError("Price-probe maximum age must be a non-negative whole number of seconds")
    primary_rpc_host, secondary_rpc_host = _rpc_host(rpc), _rpc_host(secondary_rpc)
    if primary_rpc_host == secondary_rpc_host:
        raise EvidenceError("Price-probe requires two distinct Polygon RPC hosts")
    input_sha256 = _sha256(bundle_path)
    try:
        bundle = json.loads(
            bundle_path.read_text(encoding="utf-8"), parse_float=str, parse_constant=_reject_nonfinite
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, EvidenceError) as exc:
        raise EvidenceError("Price-probe basis bundle is not exact JSON") from exc
    validate_exact(bundle)
    closing_mark_requests, gap_evidence = _load_validated_mark_queue(
        evidence_gaps, bundle_path=bundle_path, bundle=bundle
    )
    selected, coverage = select_ctf_probe_assets(
        bundle, closing_mark_requests=closing_mark_requests, sample_size=sample_size
    )
    closing = bundle["closing"]
    expected_block = {
        "number": uint(closing["block_number"]),
        "hash": hex_bytes(closing["block_hash"], 32),
    }
    partial.mkdir(parents=True)
    _write_new(partial / "configuration.json", {
        "schema": PROBE_SCHEMA,
        "engine": PROBE_VERSION,
        "input_path": str(bundle_path),
        "input_sha256": input_sha256,
        "input_digest": digest(bundle),
        "evidence_gap_queue": gap_evidence,
        "closing_block": expected_block,
        "sample_size_requested": sample_size,
        "maximum_observation_age_seconds": max_age_seconds,
        "selection_policy": SELECTION_POLICY,
        "block_anchor": {
            "primary_rpc_host": primary_rpc_host,
            "secondary_rpc_host": secondary_rpc_host,
            "required_agreement": ["chain_id", "block_number", "block_hash", "block_timestamp"],
        },
        "official_source": {
            "endpoint": PRICE_HISTORY_ENDPOINT,
            "request_shape": "GET token_id + as_of",
            "raw_response_storage": "verbatim-bytes",
        },
        "integration": "NONE; probe evidence is not a closing mark",
        "safety": SAFETY,
    })
    try:
        primary_header, secondary_header, cutoff_timestamp = _verify_dual_closing_header(
            rpc, secondary_rpc, closing=closing
        )
    except Exception as exc:
        _write_new(partial / "failure.json", {
            "status": "BLOCKED_HEADER_UNAVAILABLE_OR_MISMATCH",
            "error": _error_text(exc),
            "expected_closing_block": expected_block,
            "safety": SAFETY,
        })
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError("Price-probe closing-header capture failed") from exc
    _write_new(partial / "closing_block_header.json", {
        "source": "two-independent-read-only-polygon-rpcs",
        "primary_header": primary_header,
        "secondary_header": secondary_header,
        "block_timestamp": cutoff_timestamp + 1,
        "cutoff_timestamp": cutoff_timestamp,
        "cutoff_rule": "closing-block-timestamp-minus-one-second",
        "expected_hash": expected_block["hash"],
    })
    _write_new(partial / "requests.json", {
        "sample": selected,
        "coverage": coverage,
        "cutoff_timestamp": cutoff_timestamp,
        "request_url_template": PRICE_HISTORY_ENDPOINT + "?token_id={token_id}&as_of={cutoff_timestamp}",
    })

    responses = partial / "responses"
    responses.mkdir()
    results = []
    for row in selected:
        url = _request_url(row["token_id"], cutoff_timestamp)
        result = {
            **row,
            "request_id": f"price-v2-as-of-{row['sequence']:02d}",
            "query_url": url,
            "request_sha256": hashlib.sha256(
                ("GET\n" + url + "\naccept:application/json\n").encode("utf-8")
            ).hexdigest(),
            "started_at": now_utc(),
            "cutoff_timestamp": cutoff_timestamp,
        }
        try:
            response = client.fetch(url)
        except PriceHistoryRequestError as exc:
            result.update({"received_at": now_utc(), "status": "REQUEST_ERROR", "detail": _error_text(exc),
                           "attempts": exc.attempts})
        else:
            result["received_at"] = now_utc()
            raw = response["body"]
            raw_name = f"{row['sequence']:02d}_{row['token_id']}.raw.json"
            raw_path = responses / raw_name
            _write_raw_new(raw_path, raw)
            result.update({
                "http_status": response["status"],
                "response_headers": response["response_headers"],
                "attempts": response["attempts"],
                "raw_response_file": "responses/" + raw_name,
                "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
                **parse_history_observation(raw, cutoff_timestamp=cutoff_timestamp),
            })
            if result["status"] == "OBSERVATION_AT_OR_BEFORE_CUT":
                age = result["observation"]["age_seconds"]
                result["status"] = ("FRESH_CANDIDATE" if age <= max_age_seconds
                                    else "STALE_CANDIDATE")
        results.append(result)
    _write_new(partial / "request_results.json", results)

    status_counts = Counter(row["status"] for row in results)
    usable = [row for row in results if row["status"] == "FRESH_CANDIDATE"]
    summary = {
        "schema": PROBE_SCHEMA,
        "engine": PROBE_VERSION,
        "status": "PROBE_COMPLETE_EVIDENCE_PENDING_REVIEW",
        "closing_block": {**expected_block, "timestamp": cutoff_timestamp + 1,
                          "as_of_cutoff_timestamp": cutoff_timestamp,
                          "cutoff_rule": "closing-block-timestamp-minus-one-second"},
        "source": {"endpoint": PRICE_HISTORY_ENDPOINT, "mode": "GET token_id + as_of"},
        "sample": coverage,
        "maximum_observation_age_seconds": max_age_seconds,
        "responses": {
            "attempted": len(selected),
            "raw_saved": sum("raw_response_file" in row for row in results),
            "status_counts": dict(sorted(status_counts.items())),
            "fresh_candidates_at_or_before_cut": len(usable),
        },
        "integration": {
            "closing_marks_written": 0,
            "basis_bundle_modified": False,
            "status": "NOT_INTEGRATED",
        },
        "review_gate": {
            "status": "BLOCKED",
            "reasons": [
                "PROBE_OUTPUT_REQUIRES_REVIEW",
                "CLOSING_MARK_EVIDENCE_NOT_YET_INTEGRATED",
                "EXTERNAL_FLOW_VALUE_EVIDENCE_REMAINS_OPEN",
                "INDEPENDENT_ACCOUNTING_REPORT_MISSING",
            ],
        },
        "safety": SAFETY,
    }
    _write_new(partial / "summary.json", summary)
    project = Path(__file__).resolve().parents[3]
    commit, clean = _git_state(project)
    _write_new(partial / "run_manifest.json", {
        "schema": PROBE_SCHEMA,
        "engine": PROBE_VERSION,
        "completed_at": now_utc(),
        "code_commit": commit,
        "working_tree_clean": clean,
        "input_path": str(bundle_path),
        "input_sha256": input_sha256,
        "files": _files_sha256(partial),
        "summary_sha256": _sha256(partial / "summary.json"),
        "safety": SAFETY,
    })
    os.replace(partial, output)
    return summary
