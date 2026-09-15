"""Close receipt gaps discovered by an independent lifetime wallet scan.

This stage fetches only transactions that the source indexer omitted.  Every
full receipt is retained in atomic, resumable shards and must reproduce the
wallet balance logs already sealed by the continuous crosscheck.  It does not
normalize actions, calculate basis, value positions, or enable execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from . import SAFETY
from .blockscout_backfill import _rpc_receipt_batch
from .common import EvidenceError, address, canonical, digest, hex_bytes, now_utc, uint
from .lifetime_inventory import _load_source

CLOSURE_VERSION = "lifetime-receipt-gap-closure/1"
DEFAULT_SHARD_SIZE = 100
DEFAULT_BATCH_SIZE = 10
DEFAULT_WORKERS = 8


def _sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _write_once(path: Path, value) -> None:
    content = canonical(value) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise EvidenceError(f"Retained receipt-gap evidence conflicts with {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.partial")
    with temporary.open("x", encoding="utf-8") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


def _shard_path(directory: Path, offset: int, stop: int) -> Path:
    return directory / f"{offset:09d}-{stop - 1:09d}.json"


def _retained_shards(directory: Path, transactions: list[str], shard_size: int,
                     configuration_hash: str) -> tuple[int, list[Path]]:
    captured, paths = 0, []
    for offset in range(0, len(transactions), shard_size):
        stop = min(len(transactions), offset + shard_size)
        path = _shard_path(directory, offset, stop)
        if not path.exists():
            break
        value = json.loads(path.read_text(encoding="utf-8"))
        expected = transactions[offset:stop]
        if (value.get("configuration_hash") != configuration_hash
                or value.get("transactions") != expected
                or len(value.get("closures", [])) != len(expected)):
            raise EvidenceError("Retained receipt-gap shard breaks the frozen index")
        captured += len(expected)
        paths.append(path)
    if set(paths) != set(directory.glob("*.json")):
        raise EvidenceError("Retained receipt-gap shards are non-contiguous")
    return captured, paths


def _expected_gap(source_transactions: set[str], logs: list[dict],
                  declared_extra_logs: int) -> tuple[list[str], dict[str, list[dict]]]:
    expected = defaultdict(list)
    extra_logs = 0
    for raw in logs:
        transaction = hex_bytes(raw["transactionHash"], 32)
        if transaction in source_transactions:
            continue
        expected[transaction].append(raw)
        extra_logs += 1
    if extra_logs != declared_extra_logs:
        raise EvidenceError(
            "Crosscheck extras are not isolated to transactions omitted by the source"
        )
    transactions = sorted(expected)
    for transaction in transactions:
        expected[transaction].sort(key=lambda row: uint(row["logIndex"]))
    return transactions, dict(expected)


def _validate_closure(closure: dict, transaction: str,
                      expected_logs: list[dict]) -> dict:
    if (hex_bytes(closure.get("transaction_hash"), 32) != transaction
            or not isinstance(closure.get("logs"), list)
            or not closure["logs"]):
        raise EvidenceError("Receipt-gap closure identity/logs are invalid")
    actual = {}
    for raw in closure["logs"]:
        if hex_bytes(raw["transactionHash"], 32) != transaction:
            raise EvidenceError("Receipt-gap closure contains another transaction")
        key = uint(raw["logIndex"])
        if key in actual:
            raise EvidenceError("Receipt-gap closure contains duplicate log indexes")
        actual[key] = raw
    for expected in expected_logs:
        index = uint(expected["logIndex"])
        if index not in actual or canonical(actual[index]) != canonical(expected):
            raise EvidenceError("Receipt does not reproduce a sealed crosscheck log")
    return closure


def _fetch_with_retries(rpc_url: str, transactions: list[str],
                        expected: dict[str, list[dict]], retries: int,
                        fetch_batch) -> list[dict]:
    last_error = None
    for attempt in range(retries):
        try:
            closures = fetch_batch(rpc_url, transactions)
            if len(closures) != len(transactions):
                raise EvidenceError("Receipt RPC returned an incomplete batch")
            return [
                _validate_closure(closure, transaction, expected[transaction])
                for transaction, closure in zip(transactions, closures, strict=True)
            ]
        except EvidenceError as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(min(8, 2 ** attempt))
    raise EvidenceError(
        "Receipt gap remained unavailable after retries: " + str(last_error)
    ) from last_error


def close_lifetime_receipt_gap(
    *, crosscheck: Path, source_capture: Path, scope_path: Path, wallet: str,
    rpc_url: str, output: Path, shard_size: int = DEFAULT_SHARD_SIZE,
    batch_size: int = DEFAULT_BATCH_SIZE, workers: int = DEFAULT_WORKERS,
    retries: int = 5, max_shards: int | None = None, progress=None,
    fetch_batch=None,
) -> dict:
    """Fetch and seal only full receipts omitted by the source indexer."""
    crosscheck, source_capture, scope_path, output = (
        Path(crosscheck).resolve(), Path(source_capture).resolve(),
        Path(scope_path).resolve(), Path(output).resolve()
    )
    wallet = address(wallet)
    shard_size, batch_size, workers, retries = (
        uint(shard_size), uint(batch_size), uint(workers), uint(retries)
    )
    if not 1 <= shard_size <= 1000 or not 1 <= batch_size <= 10:
        raise EvidenceError("Invalid receipt-gap shard or batch size")
    if not 1 <= workers <= 16 or not 1 <= retries <= 10:
        raise EvidenceError("Invalid receipt-gap workers or retries")
    if max_shards is not None:
        max_shards = uint(max_shards)
    parsed = urlsplit(rpc_url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise EvidenceError("Receipt-gap closure requires an HTTPS RPC without inline credentials/query")
    source_manifest_path = source_capture / "run_manifest.json"
    transactions_path = source_capture / "transactions.json"
    if not source_manifest_path.is_file() or not transactions_path.is_file():
        raise EvidenceError("Sealed source capture is missing")
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    source_hash = source_manifest.get("result", {}).get("capture_hash")
    crosscheck_summary, _, logs = _load_source(crosscheck, scope_path, wallet)
    if not source_hash or crosscheck_summary.get("source_capture_hash") != source_hash:
        raise EvidenceError("Source capture differs from the lifetime crosscheck")
    raw_source_transactions = json.loads(transactions_path.read_text(encoding="utf-8"))
    if not isinstance(raw_source_transactions, list):
        raise EvidenceError("Source transaction index is invalid")
    source_transactions = {hex_bytes(item, 32) for item in raw_source_transactions}
    if len(source_transactions) != len(raw_source_transactions):
        raise EvidenceError("Source transaction index contains duplicates")
    transactions, expected = _expected_gap(
        source_transactions, logs,
        uint(crosscheck_summary["comparison"]["extra_count"]),
    )
    del logs
    if not transactions:
        raise EvidenceError("Lifetime crosscheck has no omitted transactions to close")

    partial = output.with_name(output.name + ".partial")
    if output.exists():
        raise EvidenceError("Receipt-gap output already exists")
    partial.mkdir(parents=True, exist_ok=True)
    configuration = {
        "schema": 1, "version": CLOSURE_VERSION, "chain": 137, "wallet": wallet,
        "crosscheck": str(crosscheck), "crosscheck_summary_sha256": _sha256(crosscheck / "summary.json"),
        "source_capture": str(source_capture), "source_capture_hash": source_hash,
        "source_transactions_sha256": _sha256(transactions_path),
        "scope": str(scope_path), "scope_sha256": _sha256(scope_path),
        "rpc_source": "rpc:" + str(parsed.hostname), "shard_size": shard_size,
        "batch_size": batch_size, "workers": workers,
        "transactions": len(transactions), "transactions_hash": digest(transactions),
        "expected_wallet_logs": sum(map(len, expected.values())), "safety": SAFETY,
    }
    configuration_hash = digest(configuration)
    _write_once(partial / "configuration.json", configuration)
    _write_once(partial / "transactions.json", transactions)
    shard_dir = partial / "receipt_shards"
    shard_dir.mkdir(exist_ok=True)
    captured, paths = _retained_shards(
        shard_dir, transactions, shard_size, configuration_hash
    )
    fetch_batch = fetch_batch or _rpc_receipt_batch
    shards_now = 0
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="polyledger-gap-receipts") as executor:
        while captured < len(transactions):
            if max_shards is not None and shards_now >= max_shards:
                break
            stop = min(len(transactions), captured + shard_size)
            group = transactions[captured:stop]
            batches = [group[offset:offset + batch_size]
                       for offset in range(0, len(group), batch_size)]
            futures = [executor.submit(
                _fetch_with_retries, rpc_url, batch, expected, retries, fetch_batch
            ) for batch in batches]
            closures = [closure for future in futures for closure in future.result()]
            value = {"schema": 1, "configuration_hash": configuration_hash,
                     "transactions": group, "closures": closures}
            path = _shard_path(shard_dir, captured, stop)
            _write_once(path, value)
            paths.append(path)
            captured = stop
            shards_now += 1
            if progress:
                progress({"stage": "receipt_gap", "transactions": captured,
                          "transactions_total": len(transactions),
                          "percent": str(captured * 10000 // len(transactions) / 100),
                          "shards": len(paths)})

    if captured < len(transactions):
        return {
            "schema": 1, "version": CLOSURE_VERSION, "status": "IN_PROGRESS",
            "progress": {"transactions": captured, "transactions_total": len(transactions),
                         "receipt_shards": len(paths)},
            "working_directory": str(partial), "bundle_created": False,
            "ledger_approval": False, "safety": SAFETY,
        }

    receipt_logs = 0
    reproduced_logs = 0
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        for closure in value["closures"]:
            transaction = closure["transaction_hash"]
            receipt_logs += len(closure["logs"])
            reproduced_logs += len(expected[transaction])
            _validate_closure(closure, transaction, expected[transaction])
    result = {
        "schema": 1, "version": CLOSURE_VERSION, "status": "RECEIPTS_CLOSED",
        "wallet": wallet,
        "range": crosscheck_summary["range"],
        "progress": {"source_transactions": len(source_transactions),
                     "gap_transactions": len(transactions),
                     "gap_transactions_closed": captured,
                     "union_transactions": len(source_transactions) + len(transactions),
                     "receipt_shards": len(paths), "full_receipt_logs": receipt_logs,
                     "crosscheck_wallet_logs_reproduced": reproduced_logs},
        "coverage": {"every_gap_transaction_closed": True,
                     "every_crosscheck_extra_log_reproduced":
                         reproduced_logs == uint(crosscheck_summary["comparison"]["extra_count"]),
                     "source_capture_untouched": True,
                     "bundle_created": False, "raw_actions_complete": False},
        "transactions_hash": digest(transactions),
        "receipt_manifest": [{"file": path.name, "sha256": _sha256(path)} for path in paths],
        "next_gate": "BUILD_REVIEWED_ACTION_BUNDLE", "ledger_approval": False,
        "execution_allowed": False, "safety": SAFETY,
    }
    _write_once(partial / "summary.json", result)
    project = Path(__file__).resolve().parents[3]
    try:
        git = ["git", "-c", "safe.directory=" + str(project).replace("\\", "/"),
               "-C", str(project)]
        commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
        clean = not subprocess.check_output(git + ["status", "--porcelain"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit, clean = "unavailable", False
    _write_once(partial / "run_manifest.json", {
        "schema": 1, "version": CLOSURE_VERSION, "completed_at": now_utc(),
        "code_commit": commit, "working_tree_clean": clean,
        "configuration_sha256": _sha256(partial / "configuration.json"),
        "transactions_sha256": _sha256(partial / "transactions.json"),
        "summary_sha256": _sha256(partial / "summary.json"),
        "status": result["status"], "safety": SAFETY,
    })
    os.replace(partial, output)
    return result
