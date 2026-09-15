"""Independent, resumable wallet-log crosscheck for a lifetime capture.

The Blockscout backfill discovers transactions through an indexer.  This module
proves discovery coverage independently by scanning every block in which the
audited proxy wallet exists with read-only ``eth_getLogs`` calls.  It keeps
small atomic coverage shards and compares the resulting wallet balance events
byte-for-byte with the full receipts retained by the source capture.

It never connects a wallet, signs, submits a transaction, or calculates PnL.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from eth_abi import encode

from . import SAFETY, VERSION
from .acquire import ReadOnlyRPC
from .common import EvidenceError, address, canonical, digest, hex_bytes, now_utc, uint
from .history_backfill import TRANSFER, TRANSFER_BATCH, TRANSFER_SINGLE

CROSSCHECK_VERSION = "wallet-log-crosscheck/1"
DEFAULT_RANGE_BLOCKS = 100
DEFAULT_SHARD_RANGES = 100
DEFAULT_BATCH_SIZE = 10
DEFAULT_WORKERS = 8
PROXY_PREFIX = "0x363d3d373d3d3d363d73"
PROXY_SUFFIX = "5af43d82803e903d91602b57fd5bf3"


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
            raise EvidenceError(f"Retained crosscheck evidence conflicts with {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.partial")
    with temporary.open("x", encoding="utf-8") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


def _wallet_topic(wallet: str) -> str:
    return "0x" + encode(["address"], [address(wallet)]).hex()


def _query_specs(scope: dict, wallet: str) -> list[dict]:
    contracts = scope.get("balance_contracts")
    if uint(scope.get("schema")) != 1 or uint(scope.get("chain")) != 137:
        raise EvidenceError("Crosscheck scope must be schema 1 on Polygon")
    if not isinstance(contracts, list) or not contracts:
        raise EvidenceError("Crosscheck scope has no balance contracts")
    addresses, standards = [], set()
    for item in contracts:
        contract = address(item["address"])
        if contract in addresses:
            raise EvidenceError("Crosscheck scope contains a duplicate contract")
        standard = item.get("token_standard")
        if standard not in {"erc20", "erc1155"}:
            raise EvidenceError("Unsupported crosscheck token standard")
        addresses.append(contract)
        standards.add(standard)
    topic = _wallet_topic(wallet)
    result = []
    if "erc20" in standards:
        erc20 = [address(item["address"]) for item in contracts
                 if item["token_standard"] == "erc20"]
        result.extend([
            {"id": "erc20:from", "addresses": erc20,
             "event_topics": [TRANSFER], "wallet_topic_index": 1,
             "topics": [TRANSFER, topic]},
            {"id": "erc20:to", "addresses": erc20,
             "event_topics": [TRANSFER], "wallet_topic_index": 2,
             "topics": [TRANSFER, None, topic]},
        ])
    if "erc1155" in standards:
        erc1155 = [address(item["address"]) for item in contracts
                   if item["token_standard"] == "erc1155"]
        events = [TRANSFER_SINGLE, TRANSFER_BATCH]
        result.extend([
            {"id": "erc1155:from", "addresses": erc1155,
             "event_topics": events, "wallet_topic_index": 2,
             "topics": [events, None, topic]},
            {"id": "erc1155:to", "addresses": erc1155,
             "event_topics": events, "wallet_topic_index": 3,
             "topics": [events, None, None, topic]},
        ])
    return result


def _compact_log(raw: dict) -> dict:
    topics = raw.get("topics")
    if not isinstance(topics, list) or not topics:
        raise EvidenceError("Crosscheck log has no topics")
    result = {
        "address": address(raw["address"]),
        "blockHash": hex_bytes(raw["blockHash"], 32),
        "blockNumber": hex(uint(raw["blockNumber"])),
        "data": hex_bytes(raw["data"]),
        "logIndex": hex(uint(raw["logIndex"])),
        "removed": raw.get("removed") is True,
        "topics": [hex_bytes(item, 32) for item in topics],
        "transactionHash": hex_bytes(raw["transactionHash"], 32),
        "transactionIndex": hex(uint(raw["transactionIndex"])),
    }
    if result["removed"]:
        raise EvidenceError("Crosscheck RPC returned a removed log")
    return result


def _matches(log: dict, spec: dict, wallet_topic: str | None = None) -> bool:
    topics = log["topics"]
    topic = wallet_topic or spec["topics"][spec["wallet_topic_index"]]
    return (
        log["address"] in spec["addresses"]
        and len(topics) > spec["wallet_topic_index"]
        and topics[0] in spec["event_topics"]
        and topics[spec["wallet_topic_index"]] == topic
    )


def _validated_result(payload, spec: dict, first: int, last: int) -> list[dict]:
    if not isinstance(payload, list):
        raise EvidenceError("Crosscheck eth_getLogs result is not a list")
    logs = []
    for raw in payload:
        log = _compact_log(raw)
        if not first <= uint(log["blockNumber"]) <= last or not _matches(log, spec):
            raise EvidenceError("Crosscheck RPC returned a log outside its query")
        logs.append(log)
    ordered = sorted(logs, key=lambda row: (
        uint(row["blockNumber"]), uint(row["transactionIndex"]),
        uint(row["logIndex"]), row["transactionHash"],
    ))
    if logs != ordered:
        raise EvidenceError("Crosscheck RPC logs are not canonically ordered")
    return logs


def _fetch_batch(rpc_url: str, work: list[dict], retries: int) -> list[dict]:
    calls = [("eth_getLogs", [{
        "fromBlock": hex(item["first_block"]),
        "toBlock": hex(item["last_block"]),
        "address": item["spec"]["addresses"],
        "topics": item["spec"]["topics"],
    }]) for item in work]
    last_error = None
    for attempt in range(retries):
        try:
            payloads = ReadOnlyRPC(url=rpc_url).batch(calls)
            return [
                {**item, "logs": _validated_result(payload, item["spec"],
                                                     item["first_block"], item["last_block"])}
                for item, payload in zip(work, payloads, strict=True)
            ]
        except EvidenceError as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(min(8, 2 ** attempt))
    raise EvidenceError("Historical RPC batch remained unavailable after retries") from last_error


def _ranges(first: int, last: int, width: int) -> list[tuple[int, int]]:
    return [(start, min(last, start + width - 1))
            for start in range(first, last + 1, width)]


def _shard_path(directory: Path, first: int, last: int) -> Path:
    return directory / f"{first:012d}-{last:012d}.json"


def _retained_shards(directory: Path, configuration_hash: str,
                     first: int, last: int) -> tuple[int, list[Path]]:
    expected, paths = first, []
    for path in sorted(directory.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("configuration_hash") != configuration_hash:
            raise EvidenceError("Retained crosscheck shard has another configuration")
        range_ = value.get("range", {})
        start, stop = uint(range_["first_block"]), uint(range_["last_block"])
        if start != expected or stop < start or stop > last:
            raise EvidenceError("Retained crosscheck shards are non-contiguous")
        expected = stop + 1
        paths.append(path)
    return expected, paths


def _capture_origin(rpc: ReadOnlyRPC, wallet: str, block: int,
                    transaction_hash: str) -> dict:
    if block < 1:
        raise EvidenceError("Wallet origin block must have a parent")
    receipt = rpc.call("eth_getTransactionReceipt", [hex_bytes(transaction_hash, 32)])
    if (hex_bytes(receipt["transactionHash"], 32) != transaction_hash
            or uint(receipt["blockNumber"]) != block or uint(receipt["status"]) != 1):
        raise EvidenceError("Wallet creation receipt does not match the declared origin")
    before = hex_bytes(rpc.call("eth_getCode", [wallet, hex(block - 1)]))
    at = hex_bytes(rpc.call("eth_getCode", [wallet, hex(block)]))
    if before != "0x" or at == "0x":
        raise EvidenceError("Wallet code transition does not prove a zero opening")
    if not (at.startswith(PROXY_PREFIX) and at.endswith(PROXY_SUFFIX)
            and len(at) == 2 + 45 * 2):
        raise EvidenceError("Audited wallet is not the expected minimal proxy runtime")
    implementation = address("0x" + at[len(PROXY_PREFIX):len(PROXY_PREFIX) + 40])
    parent, origin = rpc.block(block - 1), rpc.block(block)
    if origin["parentHash"] != parent["hash"]:
        raise EvidenceError("Wallet origin block is not anchored to its parent")
    return {
        "block_number": block,
        "transaction_hash": transaction_hash,
        "parent_block_hash": hex_bytes(parent["hash"], 32),
        "origin_block_hash": hex_bytes(origin["hash"], 32),
        "code_before": before,
        "code_at": at,
        "implementation": implementation,
        "receipt_hash": digest(receipt),
        "zero_opening_proven": True,
    }


def _source_wallet_logs(capture: Path, manifest: dict, specs: list[dict]) -> dict:
    declared = manifest["result"].get("transaction_log_manifest")
    if not isinstance(declared, list) or not declared:
        raise EvidenceError("Source capture has no transaction-log manifest")
    actual_names = {path.name for path in (capture / "transaction_log_shards").glob("*.json")}
    declared_names = {row["file"] for row in declared}
    if actual_names != declared_names or len(declared_names) != len(declared):
        raise EvidenceError("Source transaction-log shard set differs from its manifest")
    result = {}
    for item in declared:
        path = capture / "transaction_log_shards" / item["file"]
        if _sha256(path) != item["sha256"]:
            raise EvidenceError("Source transaction-log shard hash mismatch")
        value = json.loads(path.read_text(encoding="utf-8"))
        for closure in value["closures"]:
            for raw in closure["logs"]:
                log = _compact_log(raw)
                if not any(_matches(log, spec) for spec in specs):
                    continue
                key = (log["transactionHash"], uint(log["logIndex"]))
                if key in result and canonical(result[key]) != canonical(log):
                    raise EvidenceError("Source capture contains conflicting wallet balance logs")
                result[key] = log
    return result


def _crosscheck_logs(paths: list[Path]) -> dict:
    result = {}
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        for raw in value["logs"]:
            log = _compact_log(raw)
            key = (log["transactionHash"], uint(log["logIndex"]))
            if key in result and canonical(result[key]) != canonical(log):
                raise EvidenceError("Crosscheck shards contain conflicting wallet balance logs")
            result[key] = log
    return result


def crosscheck_lifetime_capture(
    *, capture: Path, output: Path, wallet: str, identity_path: Path,
    scope_path: Path, origin_block: int, origin_transaction: str,
    rpc_url: str, range_blocks: int = DEFAULT_RANGE_BLOCKS,
    shard_ranges: int = DEFAULT_SHARD_RANGES, batch_size: int = DEFAULT_BATCH_SIZE,
    workers: int = DEFAULT_WORKERS, retries: int = 5,
    max_shards: int | None = None, progress=None,
) -> dict:
    """Crosscheck every balance-bearing wallet log from proxy creation to cut."""
    capture, output = Path(capture).resolve(), Path(output).resolve()
    identity_path, scope_path = Path(identity_path).resolve(), Path(scope_path).resolve()
    wallet, origin_block = address(wallet), uint(origin_block)
    origin_transaction = hex_bytes(origin_transaction, 32)
    range_blocks, shard_ranges = uint(range_blocks), uint(shard_ranges)
    batch_size, workers, retries = uint(batch_size), uint(workers), uint(retries)
    if not 1 <= range_blocks <= 100:
        raise EvidenceError("Historical public RPC range must be 1..100 blocks")
    if not 1 <= shard_ranges <= 1000 or not 1 <= batch_size <= 10:
        raise EvidenceError("Invalid crosscheck shard or RPC batch size")
    if not 1 <= workers <= 16 or not 1 <= retries <= 10:
        raise EvidenceError("Invalid crosscheck worker or retry count")
    if max_shards is not None:
        max_shards = uint(max_shards)
    parsed = urlsplit(rpc_url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise EvidenceError("Crosscheck requires an HTTPS RPC without inline credentials/query")
    if not capture.is_dir() or not (capture / "run_manifest.json").is_file():
        raise EvidenceError("Sealed lifetime source capture is missing")
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    if (identity.get("verification_status") != "VERIFIED"
            or address(identity.get("verified_proxy_wallet")) != wallet):
        raise EvidenceError("Identity artifact does not verify the crosscheck wallet")
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    specs = _query_specs(scope, wallet)
    source_manifest_path = capture / "run_manifest.json"
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    source = source_manifest.get("result", {})
    source_coverage = source.get("coverage", {})
    if (source_coverage.get("transfer_pagination_complete") is not True
            or source_coverage.get("full_transaction_log_closure") is not True
            or source_coverage.get("anchor_match") is not True):
        raise EvidenceError("Lifetime source capture is not transaction-closed and anchored")
    if address(source["configuration"]["wallet"]) != wallet:
        raise EvidenceError("Lifetime source capture belongs to another wallet")
    last_block = uint(source["anchors"]["closing"]["number"])
    if origin_block > last_block:
        raise EvidenceError("Wallet origin is after the frozen accounting cut")
    rpc = ReadOnlyRPC(url=rpc_url)
    if uint(rpc.call("eth_chainId", [])) != 137:
        raise EvidenceError("Crosscheck RPC is not Polygon")
    origin = _capture_origin(rpc, wallet, origin_block, origin_transaction)
    closing = rpc.block(last_block)
    if hex_bytes(closing["hash"], 32) != hex_bytes(source["anchors"]["closing"]["hash"], 32):
        raise EvidenceError("Crosscheck RPC disagrees with the frozen closing block")

    partial = output.with_name(output.name + ".partial")
    if output.exists():
        raise EvidenceError("Crosscheck output already exists")
    partial.mkdir(parents=True, exist_ok=True)
    configuration = {
        "schema": 1,
        "version": CROSSCHECK_VERSION,
        "chain": 137,
        "wallet": wallet,
        "origin": origin,
        "last_block": last_block,
        "closing_block_hash": hex_bytes(closing["hash"], 32),
        "range_blocks": range_blocks,
        "shard_ranges": shard_ranges,
        "query_specs": specs,
        "rpc_source": "rpc:" + str(parsed.hostname),
        "source_capture": str(capture),
        "source_capture_hash": source["capture_hash"],
        "source_manifest_sha256": _sha256(source_manifest_path),
        "identity_sha256": _sha256(identity_path),
        "scope_sha256": _sha256(scope_path),
        "safety": SAFETY,
    }
    configuration_hash = digest(configuration)
    _write_once(partial / "configuration.json", configuration)
    shard_dir = partial / "coverage_shards"
    shard_dir.mkdir(exist_ok=True)
    next_block, paths = _retained_shards(
        shard_dir, configuration_hash, origin_block, last_block
    )
    ranges_total = (last_block - origin_block) // range_blocks + 1
    ranges_done = (next_block - origin_block + range_blocks - 1) // range_blocks
    shards_now = 0
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="polyledger-crosscheck") as executor:
        while next_block <= last_block:
            if max_shards is not None and shards_now >= max_shards:
                break
            shard_last = min(last_block, next_block + range_blocks * shard_ranges - 1)
            shard_ranges_list = _ranges(next_block, shard_last, range_blocks)
            work = [
                {"first_block": first, "last_block": stop, "spec": spec}
                for first, stop in shard_ranges_list for spec in specs
            ]
            batches = [work[offset:offset + batch_size]
                       for offset in range(0, len(work), batch_size)]
            fetched = [item for group in executor.map(
                lambda batch: _fetch_batch(rpc_url, batch, retries), batches
            ) for item in group]
            by_range = {(first, stop): [] for first, stop in shard_ranges_list}
            logs = {}
            for item in fetched:
                by_range[(item["first_block"], item["last_block"])].append({
                    "filter_id": item["spec"]["id"],
                    "result_hash": digest(item["logs"]),
                    "logs": len(item["logs"]),
                })
                for log in item["logs"]:
                    key = (log["transactionHash"], uint(log["logIndex"]))
                    if key in logs and canonical(logs[key]) != canonical(log):
                        raise EvidenceError("One crosscheck shard received conflicting logs")
                    logs[key] = log
            ordered_logs = sorted(logs.values(), key=lambda row: (
                uint(row["blockNumber"]), uint(row["transactionIndex"]),
                uint(row["logIndex"]), row["transactionHash"],
            ))
            value = {
                "schema": 1,
                "configuration_hash": configuration_hash,
                "range": {"first_block": next_block, "last_block": shard_last},
                "queries": [
                    {"first_block": first, "last_block": stop,
                     "responses": sorted(by_range[(first, stop)], key=lambda row: row["filter_id"])}
                    for first, stop in shard_ranges_list
                ],
                "logs": ordered_logs,
            }
            path = _shard_path(shard_dir, next_block, shard_last)
            _write_once(path, value)
            paths.append(path)
            next_block = shard_last + 1
            ranges_done += len(shard_ranges_list)
            shards_now += 1
            if progress:
                progress({
                    "status": "RPC_LOG_CROSSCHECK",
                    "last_block": shard_last,
                    "cut_block": last_block,
                    "ranges": ranges_done,
                    "ranges_total": ranges_total,
                    "percent": str((ranges_done * 10000 // ranges_total) / 100),
                })

    if next_block <= last_block:
        return {
            "schema": 1,
            "version": CROSSCHECK_VERSION,
            "status": "IN_PROGRESS",
            "progress": {"last_block": next_block - 1, "cut_block": last_block,
                         "ranges": ranges_done, "ranges_total": ranges_total,
                         "coverage_shards": len(paths)},
            "working_directory": str(partial),
            "raw_actions_complete": False,
            "ledger_approval": False,
            "safety": SAFETY,
        }

    source_logs = _source_wallet_logs(capture, source_manifest, specs)
    rpc_logs = _crosscheck_logs(paths)
    missing = sorted(set(source_logs) - set(rpc_logs))
    extra = sorted(set(rpc_logs) - set(source_logs))
    conflicts = sorted(key for key in set(source_logs) & set(rpc_logs)
                       if canonical(source_logs[key]) != canonical(rpc_logs[key]))
    match = not missing and not extra and not conflicts
    result = {
        "schema": 1,
        "version": CROSSCHECK_VERSION,
        "status": "RPC_CROSSCHECK_COMPLETE" if match else "CROSSCHECK_BLOCKED",
        "wallet": wallet,
        "range": {"start_block": origin_block, "end_block": last_block},
        "origin": origin,
        "progress": {"ranges": ranges_done, "ranges_total": ranges_total,
                     "coverage_shards": len(paths),
                     "rpc_queries": ranges_done * len(specs)},
        "comparison": {
            "source_wallet_balance_logs": len(source_logs),
            "rpc_wallet_balance_logs": len(rpc_logs),
            "missing_count": len(missing),
            "extra_count": len(extra),
            "conflict_count": len(conflicts),
            "missing_sample": [list(key) for key in missing[:20]],
            "extra_sample": [list(key) for key in extra[:20]],
            "conflict_sample": [list(key) for key in conflicts[:20]],
            "exact_match": match,
        },
        "coverage": {
            "starts_at_wallet_origin": origin["zero_opening_proven"],
            "continuous_blocks": True,
            "source_capture_transaction_closed": True,
            "exact_wallet_balance_log_match": match,
            "raw_actions_complete": match,
        },
        "source_capture_hash": source["capture_hash"],
        "configuration_hash": configuration_hash,
        "coverage_manifest": [
            {"file": path.name, "sha256": _sha256(path)} for path in paths
        ],
        "ledger_approval": False,
        "next_gate": "DECODE_NORMALIZE_AND_RECONCILE" if match else "RESOLVE_LOG_MISMATCH",
        "safety": SAFETY,
    }
    _write_once(partial / "summary.json", result)
    try:
        project = Path(__file__).resolve().parents[3]
        git = ["git", "-c", "safe.directory=" + str(project).replace("\\", "/"),
               "-C", str(project)]
        commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
        clean = not subprocess.check_output(git + ["status", "--porcelain"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit, clean = "unavailable", False
    _write_once(partial / "run_manifest.json", {
        "schema": 1,
        "version": CROSSCHECK_VERSION,
        "completed_at": now_utc(),
        "code_commit": commit,
        "working_tree_clean": clean,
        "configuration_sha256": _sha256(partial / "configuration.json"),
        "summary_sha256": _sha256(partial / "summary.json"),
        "status": result["status"],
        "safety": SAFETY,
    })
    os.replace(partial, output)
    return result
