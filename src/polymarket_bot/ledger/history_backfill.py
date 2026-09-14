"""Resumable lifetime wallet backfill from public Polygon evidence.

This module deliberately separates discovery from accounting.  It scans only
wallet-indexed balance events, closes every discovered transaction over its
full receipt, and seals immutable range/shard files.  It does not infer lots,
prices, basis, or PnL.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from eth_abi import encode
from eth_hash.auto import keccak

from . import SAFETY, VERSION
from .acquire import ReadOnlyRPC
from .common import EvidenceError, address, canonical, digest, hex_bytes, now_utc, uint

CHAIN_ORIGIN_FIRST_BLOCK = 1
DEFAULT_SEGMENT_BLOCKS = 100_000
DEFAULT_MIN_QUERY_BLOCKS = 1_000
DEFAULT_RECEIPT_SHARD_SIZE = 100
TRANSFER = "0x" + keccak(b"Transfer(address,address,uint256)").hex()
TRANSFER_SINGLE = "0x" + keccak(
    b"TransferSingle(address,address,address,uint256,uint256)"
).hex()
TRANSFER_BATCH = "0x" + keccak(
    b"TransferBatch(address,address,address,uint256[],uint256[])"
).hex()


def _sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _write_once(path: Path, value) -> None:
    """Create immutable canonical JSON, accepting an identical retained file."""
    content = canonical(value) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise EvidenceError(f"Retained backfill evidence conflicts with {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.partial")
    with temporary.open("x", encoding="utf-8") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


def _compact_block(block: dict) -> dict:
    return {key: block[key] for key in ("number", "hash", "parentHash", "timestamp")}


def _wallet_topic(wallet: str) -> str:
    return "0x" + encode(["address"], [address(wallet)]).hex()


def discovery_filters(scope: dict, wallet: str) -> list[dict]:
    """Build the minimal balance-bearing wallet filters for the declared scope."""
    if uint(scope.get("schema")) != 1 or uint(scope.get("chain")) != 137:
        raise EvidenceError("Historical scope must be schema 1 on Polygon")
    topic = _wallet_topic(wallet)
    result = []
    seen = set()
    for contract in scope.get("balance_contracts", []):
        contract_address = address(contract["address"])
        standard = contract.get("token_standard")
        if standard == "erc20":
            events = (("Transfer", TRANSFER, 1, "from"), ("Transfer", TRANSFER, 2, "to"))
        elif standard == "erc1155":
            events = (
                ("TransferSingle", TRANSFER_SINGLE, 2, "from"),
                ("TransferSingle", TRANSFER_SINGLE, 3, "to"),
                ("TransferBatch", TRANSFER_BATCH, 2, "from"),
                ("TransferBatch", TRANSFER_BATCH, 3, "to"),
            )
        else:
            raise EvidenceError("Historical balance contract has an unsupported token standard")
        for event, event_topic, topic_index, field in events:
            topics = [event_topic] + [None] * topic_index
            topics[topic_index] = topic
            key = (contract_address, tuple(topics))
            if key in seen:
                raise EvidenceError("Historical scope contains duplicate discovery filters")
            seen.add(key)
            result.append({
                "id": f"{contract['label']}:{event}:{field}",
                "contract": contract_address,
                "token_standard": standard,
                "event": event,
                "field": field,
                "topic_index": topic_index,
                "topics": topics,
            })
    if not result:
        raise EvidenceError("Historical scope has no balance-bearing contracts")
    return result


def _validate_log(raw: dict, query: dict, first: int, last: int, wallet_topic: str) -> None:
    topics = raw.get("topics") or []
    if (
        raw.get("removed") is True
        or address(raw["address"]) != query["contract"]
        or not first <= uint(raw["blockNumber"]) <= last
        or len(topics) <= query["topic_index"]
        or hex_bytes(topics[0], 32) != query["topics"][0]
        or hex_bytes(topics[query["topic_index"]], 32) != wallet_topic
    ):
        raise EvidenceError("RPC returned a log outside the wallet-indexed historical query")
    hex_bytes(raw["transactionHash"], 32)
    uint(raw["logIndex"])


def adaptive_logs(
    rpc: ReadOnlyRPC,
    query: dict,
    first: int,
    last: int,
    *,
    min_query_blocks: int = DEFAULT_MIN_QUERY_BLOCKS,
) -> tuple[list[dict], list[dict]]:
    """Query one filter, bisecting provider-limited ranges without gaps."""
    first, last, min_query_blocks = uint(first), uint(last), uint(min_query_blocks)
    if first > last or min_query_blocks < 1:
        raise EvidenceError("Invalid adaptive historical query range")
    wallet_topic = query["topics"][query["topic_index"]]
    pending = [(first, last)]
    completed, logs = [], {}
    while pending:
        start, stop = pending.pop()
        params = [{
            "fromBlock": hex(start),
            "toBlock": hex(stop),
            "address": query["contract"],
            "topics": query["topics"],
        }]
        try:
            payload = rpc.call("eth_getLogs", params)
            if not isinstance(payload, list):
                raise EvidenceError("eth_getLogs returned a non-list payload")
        except EvidenceError as exc:
            if "pruned" in str(exc).lower() or "archive" in str(exc).lower():
                raise EvidenceError(
                    "Historical provider has pruned this range; an archive RPC/indexer is required"
                ) from exc
            width = stop - start + 1
            if width <= min_query_blocks:
                raise
            middle = (start + stop) // 2
            pending.append((middle + 1, stop))
            pending.append((start, middle))
            continue
        for raw in payload:
            _validate_log(raw, query, start, stop, wallet_topic)
            key = (hex_bytes(raw["transactionHash"], 32), uint(raw["logIndex"]))
            existing = logs.get(key)
            if existing is not None and canonical(existing) != canonical(raw):
                raise EvidenceError("Provider returned conflicting copies of one historical log")
            logs[key] = raw
        completed.append({"first_block": start, "last_block": stop, "logs": len(payload)})
    completed.sort(key=lambda row: row["first_block"])
    expected = first
    for row in completed:
        if row["first_block"] != expected:
            raise EvidenceError("Adaptive historical queries left a block gap")
        expected = row["last_block"] + 1
    if expected != last + 1:
        raise EvidenceError("Adaptive historical queries did not cover the requested range")
    ordered = [logs[key] for key in sorted(
        logs, key=lambda item: (uint(logs[item]["blockNumber"]), item[1], item[0])
    )]
    return ordered, completed


def _segment_ranges(first: int, last: int, blocks: int) -> list[tuple[int, int]]:
    first, last, blocks = uint(first), uint(last), uint(blocks)
    if first > last or blocks < 1 or blocks > 1_000_000:
        raise EvidenceError("Historical segment size/range is invalid")
    return [(start, min(last, start + blocks - 1)) for start in range(first, last + 1, blocks)]


def _segment_path(directory: Path, first: int, last: int) -> Path:
    return directory / f"{first:012d}-{last:012d}.json"


def _validate_retained_segment(rpc: ReadOnlyRPC, segment: dict, configuration_hash: str) -> None:
    if segment.get("configuration_hash") != configuration_hash:
        raise EvidenceError("Retained historical segment belongs to another configuration")
    for key in ("first_anchor", "last_anchor"):
        retained = segment[key]
        current = rpc.block(uint(retained["number"]))
        if current["hash"] != retained["hash"]:
            raise EvidenceError("Canonical chain changed under retained historical evidence")


def _capture_segment(
    rpc: ReadOnlyRPC,
    queries: list[dict],
    first: int,
    last: int,
    configuration_hash: str,
    min_query_blocks: int,
) -> dict:
    first_anchor = _compact_block(rpc.block(first))
    last_anchor = _compact_block(rpc.block(last))
    by_log, matches, query_coverage = {}, {}, []
    for query in queries:
        rows, pieces = adaptive_logs(
            rpc, query, first, last, min_query_blocks=min_query_blocks
        )
        query_coverage.append({"filter_id": query["id"], "pieces": pieces})
        for raw in rows:
            key = (hex_bytes(raw["transactionHash"], 32), uint(raw["logIndex"]))
            existing = by_log.get(key)
            if existing is not None and canonical(existing) != canonical(raw):
                raise EvidenceError("Historical filters returned a conflicting log")
            by_log[key] = raw
            matches.setdefault(key, set()).add(query["id"])
    if rpc.block(first)["hash"] != first_anchor["hash"] or rpc.block(last)["hash"] != last_anchor["hash"]:
        raise EvidenceError("Reorg during historical segment capture")
    ordered_keys = sorted(
        by_log, key=lambda item: (uint(by_log[item]["blockNumber"]), item[1], item[0])
    )
    return {
        "schema": 1,
        "configuration_hash": configuration_hash,
        "range": {"first_block": first, "last_block": last},
        "first_anchor": first_anchor,
        "last_anchor": last_anchor,
        "queries": query_coverage,
        "logs": [by_log[key] for key in ordered_keys],
        "matches": [
            {"transaction_hash": key[0], "log_index": key[1], "filters": sorted(matches[key])}
            for key in ordered_keys
        ],
    }


def _transactions(segment_paths: list[Path]) -> list[str]:
    transactions = set()
    for path in segment_paths:
        segment = json.loads(path.read_text(encoding="utf-8"))
        transactions.update(hex_bytes(row["transactionHash"], 32) for row in segment["logs"])
    return sorted(transactions)


def _receipt_shard_path(directory: Path, offset: int, stop: int) -> Path:
    return directory / f"{offset:09d}-{stop - 1:09d}.json"


def _validate_receipt(receipt: dict, transaction: str, first: int, last: int) -> None:
    if (
        hex_bytes(receipt["transactionHash"], 32) != transaction
        or not first <= uint(receipt["blockNumber"]) <= last
        or receipt.get("blockHash") is None
    ):
        raise EvidenceError("Historical receipt identity or block range mismatch")


def _capture_receipt_shard(
    rpc: ReadOnlyRPC, transactions: list[str], first: int, last: int, configuration_hash: str
) -> dict:
    receipts = []
    for offset in range(0, len(transactions), 10):
        group = transactions[offset:offset + 10]
        payloads = rpc.batch([("eth_getTransactionReceipt", [tx]) for tx in group])
        for tx, receipt in zip(group, payloads):
            _validate_receipt(receipt, tx, first, last)
            receipts.append(receipt)
    return {
        "schema": 1,
        "configuration_hash": configuration_hash,
        "transactions": transactions,
        "receipts": receipts,
    }


def _receipt_progress(receipt_dir: Path, transactions: list[str], shard_size: int,
                      configuration_hash: str, first: int, last: int) -> tuple[int, list[Path]]:
    captured, paths = 0, []
    for offset in range(0, len(transactions), shard_size):
        stop = min(len(transactions), offset + shard_size)
        path = _receipt_shard_path(receipt_dir, offset, stop)
        if not path.exists():
            break
        shard = json.loads(path.read_text(encoding="utf-8"))
        expected = transactions[offset:stop]
        if shard.get("configuration_hash") != configuration_hash or shard.get("transactions") != expected:
            raise EvidenceError("Retained receipt shard belongs to another transaction index")
        if len(shard.get("receipts", [])) != len(expected):
            raise EvidenceError("Retained receipt shard is incomplete")
        for tx, receipt in zip(expected, shard["receipts"]):
            _validate_receipt(receipt, tx, first, last)
        captured += len(expected)
        paths.append(path)
    return captured, paths


def _seed_keys(segment_paths: list[Path]) -> set[tuple[str, int]]:
    result = set()
    for path in segment_paths:
        segment = json.loads(path.read_text(encoding="utf-8"))
        result.update(
            (hex_bytes(row["transactionHash"], 32), uint(row["logIndex"]))
            for row in segment["logs"]
        )
    return result


def _receipt_keys(receipt_paths: list[Path]) -> tuple[set[tuple[str, int]], int]:
    result, logs = set(), 0
    for path in receipt_paths:
        shard = json.loads(path.read_text(encoding="utf-8"))
        for receipt in shard["receipts"]:
            for raw in receipt.get("logs", []):
                logs += 1
                result.add((hex_bytes(raw["transactionHash"], 32), uint(raw["logIndex"])))
    return result, logs


def _result(
    *, configuration: dict, segment_paths: list[Path], total_segments: int,
    transactions: list[str], captured_receipts: int, receipt_paths: list[Path],
    partial: Path, complete: bool,
) -> dict:
    segment_hashes = [{"file": path.name, "sha256": _sha256(path)} for path in segment_paths]
    receipt_hashes = [{"file": path.name, "sha256": _sha256(path)} for path in receipt_paths]
    return {
        "version": VERSION,
        "status": "RAW_HISTORY_CAPTURE_COMPLETE" if complete else "IN_PROGRESS",
        "configuration": configuration,
        "progress": {
            "segments_complete": len(segment_paths),
            "segments_total": total_segments,
            "transactions_discovered": len(transactions),
            "receipts_complete": captured_receipts,
            "receipts_total": len(transactions),
        },
        "coverage": {
            "continuous_wallet_indexed_queries": len(segment_paths) == total_segments,
            "full_receipt_closure": complete,
            "starts_at_chain_origin": configuration["first_block"] == CHAIN_ORIGIN_FIRST_BLOCK,
            "scope": configuration["scope"],
            "basis_or_pnl_inferred": False,
        },
        "segment_manifest": segment_hashes,
        "receipt_manifest": receipt_hashes,
        "working_directory": str(partial),
        "safety": SAFETY,
        "ledger_approval": False,
    }


def backfill_wallet_history(
    *, output: Path, wallet: str, identity_path: Path, scope_path: Path,
    rpc: ReadOnlyRPC | None = None, first_block: int = CHAIN_ORIGIN_FIRST_BLOCK,
    last_block: int | None = None, confirmations: int = 200,
    segment_blocks: int = DEFAULT_SEGMENT_BLOCKS,
    min_query_blocks: int = DEFAULT_MIN_QUERY_BLOCKS,
    receipt_shard_size: int = DEFAULT_RECEIPT_SHARD_SIZE,
    max_segments: int | None = None, max_receipt_shards: int | None = None,
    progress=None,
) -> dict:
    """Advance or complete an immutable, resumable, read-only history capture."""
    wallet, first_block = address(wallet), uint(first_block)
    confirmations = uint(confirmations)
    segment_blocks, min_query_blocks = uint(segment_blocks), uint(min_query_blocks)
    receipt_shard_size = uint(receipt_shard_size)
    if first_block < CHAIN_ORIGIN_FIRST_BLOCK:
        raise EvidenceError("Historical backfill starts at block 1; block 0 is the opening anchor")
    if receipt_shard_size < 1 or receipt_shard_size > 1_000:
        raise EvidenceError("Receipt shard size must be 1..1000")
    if max_segments is not None:
        max_segments = uint(max_segments)
    if max_receipt_shards is not None:
        max_receipt_shards = uint(max_receipt_shards)
    output = Path(output).resolve()
    if output.exists():
        manifest = output / "run_manifest.json"
        if not manifest.exists():
            raise EvidenceError("Completed backfill directory has no manifest")
        return json.loads(manifest.read_text(encoding="utf-8"))["result"]
    partial = output.with_name(output.name + ".partial")
    partial.mkdir(parents=True, exist_ok=True)
    identity_path, scope_path = Path(identity_path).resolve(), Path(scope_path).resolve()
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    if identity.get("verification_status") != "VERIFIED" or address(identity["verified_proxy_wallet"]) != wallet:
        raise EvidenceError("Identity artifact does not verify the historical wallet")
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    queries = discovery_filters(scope, wallet)
    rpc = rpc or ReadOnlyRPC()
    if uint(rpc.call("eth_chainId", [])) != uint(scope["chain"]):
        raise EvidenceError("Historical RPC chain differs from the declared scope")
    head = uint(rpc.call("eth_blockNumber", []))
    retained_configuration_path = partial / "configuration.json"
    retained_configuration = (
        json.loads(retained_configuration_path.read_text(encoding="utf-8"))
        if retained_configuration_path.exists() else None
    )
    if last_block is None:
        if retained_configuration is not None:
            last_block = uint(retained_configuration["last_block"])
        elif head <= confirmations:
            raise EvidenceError("Confirmation buffer exceeds Polygon head")
        else:
            last_block = head - confirmations
    else:
        last_block = uint(last_block)
        if last_block > head - confirmations:
            raise EvidenceError("Historical end block is inside the confirmation buffer")
    ranges = _segment_ranges(first_block, last_block, segment_blocks)
    configuration = {
        "schema": 1,
        "chain": uint(scope["chain"]),
        "wallet": wallet,
        "first_block": first_block,
        "last_block": last_block,
        "confirmations": confirmations,
        "segment_blocks": segment_blocks,
        "min_query_blocks": min_query_blocks,
        "receipt_shard_size": receipt_shard_size,
        "rpc_source": "rpc:" + str(urlsplit(rpc.url).hostname),
        "identity_sha256": _sha256(identity_path),
        "scope_sha256": _sha256(scope_path),
        "scope": scope["scope"],
        "filter_hash": digest(queries),
        "safety": SAFETY,
    }
    configuration_hash = digest(configuration)
    _write_once(retained_configuration_path, configuration)
    segment_dir = partial / "segments"
    segment_dir.mkdir(exist_ok=True)
    segment_paths = []
    captured_now = 0
    for position, (start, stop) in enumerate(ranges, 1):
        path = _segment_path(segment_dir, start, stop)
        if path.exists():
            segment = json.loads(path.read_text(encoding="utf-8"))
            _validate_retained_segment(rpc, segment, configuration_hash)
            segment_paths.append(path)
            continue
        if max_segments is not None and captured_now >= max_segments:
            break
        segment = _capture_segment(
            rpc, queries, start, stop, configuration_hash, min_query_blocks
        )
        _write_once(path, segment)
        segment_paths.append(path)
        captured_now += 1
        if progress:
            progress({
                "status": "HISTORY_DISCOVERY",
                "segment": position,
                "segments_total": len(ranges),
                "first_block": start,
                "last_block": stop,
                "logs": len(segment["logs"]),
            })
    scan_complete = len(segment_paths) == len(ranges)
    transactions = _transactions(segment_paths)
    receipt_dir = partial / "receipt_shards"
    receipt_dir.mkdir(exist_ok=True)
    captured_receipts, receipt_paths = _receipt_progress(
        receipt_dir, transactions, receipt_shard_size, configuration_hash,
        first_block, last_block
    )
    if scan_complete:
        _write_once(partial / "transactions.json", transactions)
        shards_now = 0
        for offset in range(captured_receipts, len(transactions), receipt_shard_size):
            if max_receipt_shards is not None and shards_now >= max_receipt_shards:
                break
            stop = min(len(transactions), offset + receipt_shard_size)
            path = _receipt_shard_path(receipt_dir, offset, stop)
            shard = _capture_receipt_shard(
                rpc, transactions[offset:stop], first_block, last_block, configuration_hash
            )
            _write_once(path, shard)
            receipt_paths.append(path)
            captured_receipts += stop - offset
            shards_now += 1
            if progress:
                progress({
                    "status": "HISTORY_RECEIPTS",
                    "receipts": captured_receipts,
                    "receipts_total": len(transactions),
                })
    complete = scan_complete and captured_receipts == len(transactions)
    result = _result(
        configuration=configuration, segment_paths=segment_paths,
        total_segments=len(ranges), transactions=transactions,
        captured_receipts=captured_receipts, receipt_paths=receipt_paths,
        partial=partial, complete=complete,
    )
    if complete:
        receipt_keys, receipt_log_count = _receipt_keys(receipt_paths)
        missing = _seed_keys(segment_paths) - receipt_keys
        if missing:
            raise EvidenceError("Wallet-indexed seed logs are absent from full receipts")
        result["receipt_logs"] = receipt_log_count
        result["capture_hash"] = digest({
            "configuration": configuration,
            "segments": result["segment_manifest"],
            "receipts": result["receipt_manifest"],
        })
        result["working_directory"] = str(output)
        manifest = {
            "schema": 1,
            "completed_at": now_utc(),
            "result": result,
            "safety": SAFETY,
        }
        _write_once(partial / "run_manifest.json", manifest)
        os.rename(partial, output)
    return result
