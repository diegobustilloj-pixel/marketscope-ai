"""Archive-indexer fallback for the PolyLedger wallet history backfill.

Polygon public RPCs may retain block headers while pruning old logs and
receipts.  Blockscout exposes address-scoped token transfers and transaction
logs without wallet authentication.  This module captures that evidence with
cursor proofs and closes every discovered transaction over all of its logs.

An indexer that does not attest complete indexing can supply evidence, but can
never make the P0 coverage gate pass by itself.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from polymarket_bot.polyledger import build_url

from . import SAFETY, VERSION
from .acquire import ExactPublicClient, ReadOnlyRPC
from .common import EvidenceError, address, canonical, digest, hex_bytes, now_utc, uint
from .history_backfill import CHAIN_ORIGIN_FIRST_BLOCK

DEFAULT_BASE_URL = "https://polygon.blockscout.com/api/v2"
INITIAL_CURSOR_INDEX = 2_147_483_647
DEFAULT_LOG_SHARD_SIZE = 25
DEFAULT_LOG_WORKERS = 8


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
            raise EvidenceError(f"Retained Blockscout evidence conflicts with {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.partial")
    with temporary.open("x", encoding="utf-8") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


class BlockscoutClient:
    """Narrow GET-only client for one public Polygon Blockscout instance."""

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, transport=None):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "polygon.blockscout.com"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/api/v2"
        ):
            raise EvidenceError("Exact public Polygon Blockscout v2 HTTPS endpoint required")
        self.base_url = base_url.rstrip("/")
        self.transport = transport or ExactPublicClient(
            timeout_seconds=90, retries=5,
            user_agent="PolyLedger-P0/0.1 read-only historical backfill",
        ).get_json

    def get(self, path: str, params: dict | None = None):
        if not re.fullmatch(r"/[A-Za-z0-9_/-]+", path) or ".." in path:
            raise EvidenceError("Unsupported Blockscout path")
        url = build_url(self.base_url, path, params)
        try:
            result = self.transport(url)
        except Exception as exc:
            raise EvidenceError("Public Blockscout request failed") from exc
        if not isinstance(result, dict):
            raise EvidenceError("Public Blockscout returned a non-object")
        return result


def _party_hash(value: dict | None) -> str:
    if not isinstance(value, dict) or not value.get("hash"):
        raise EvidenceError("Blockscout transfer is missing a participant")
    return address(value["hash"])


def _compact_transfer(item: dict, contract: dict, wallet: str, last_block: int) -> dict:
    token = item.get("token") or {}
    token_address = address(token.get("address_hash"))
    if token_address != address(contract["address"]):
        raise EvidenceError("Blockscout transfer escaped the requested token contract")
    expected_type = "ERC-20" if contract["token_standard"] == "erc20" else "ERC-1155"
    if item.get("token_type", token.get("type")) != expected_type:
        raise EvidenceError("Blockscout transfer token standard mismatch")
    sender, receiver = _party_hash(item.get("from")), _party_hash(item.get("to"))
    if wallet not in {sender, receiver}:
        raise EvidenceError("Blockscout transfer does not involve the requested wallet")
    block_number = uint(item["block_number"])
    if block_number > last_block:
        raise EvidenceError("Blockscout transfer lies after the frozen accounting cut")
    total = item.get("total") or {}
    value = uint(total["value"])
    token_id = total.get("token_id")
    if expected_type == "ERC-1155":
        token_id = str(uint(token_id))
    elif token_id is not None:
        raise EvidenceError("ERC-20 transfer unexpectedly carries a token id")
    return {
        "block_hash": hex_bytes(item["block_hash"], 32),
        "block_number": block_number,
        "from": sender,
        "log_index": uint(item["log_index"]),
        "timestamp": item["timestamp"],
        "to": receiver,
        "token_contract": token_address,
        "token_standard": contract["token_standard"],
        "token_id": token_id,
        "transaction_hash": hex_bytes(item["transaction_hash"], 32),
        "value": value,
    }


def _next_cursor(value, contract: dict) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or "block_number" not in value or "index" not in value:
        raise EvidenceError("Blockscout pagination cursor is incomplete")
    result = {"block_number": uint(value["block_number"]), "index": uint(value["index"])}
    if value.get("token") is not None and address(value["token"]) != address(contract["address"]):
        raise EvidenceError("Blockscout pagination changed the token filter")
    return result


def _transfer_params(contract: dict, cursor: dict) -> dict:
    return {
        "type": "ERC-20" if contract["token_standard"] == "erc20" else "ERC-1155",
        "token": address(contract["address"]),
        "block_number": cursor["block_number"],
        "index": cursor["index"],
    }


def _read_contract_pages(directory: Path, contract: dict, wallet: str, last_block: int,
                         configuration_hash: str) -> tuple[list[Path], dict | None, bool]:
    paths, cursor, complete = [], {"block_number": last_block, "index": INITIAL_CURSOR_INDEX}, False
    retained_paths = sorted(directory.glob("*.json"))
    for retained_index, path in enumerate(retained_paths):
        page = json.loads(path.read_text(encoding="utf-8"))
        if page.get("configuration_hash") != configuration_hash or page.get("request_cursor") != cursor:
            raise EvidenceError("Retained Blockscout transfer pages break the cursor chain")
        for item in page.get("items", []):
            _compact_transfer({
                "token": {"address_hash": item["token_contract"],
                          "type": "ERC-20" if item["token_standard"] == "erc20" else "ERC-1155"},
                "token_type": "ERC-20" if item["token_standard"] == "erc20" else "ERC-1155",
                "from": {"hash": item["from"]}, "to": {"hash": item["to"]},
                "block_number": item["block_number"], "block_hash": item["block_hash"],
                "log_index": item["log_index"], "timestamp": item["timestamp"],
                "transaction_hash": item["transaction_hash"],
                "total": {"value": item["value"], "token_id": item["token_id"]},
            }, contract, wallet, last_block)
        cursor = page.get("next_cursor")
        paths.append(path)
        if cursor is None:
            complete = True
            if retained_index != len(retained_paths) - 1:
                raise EvidenceError("Retained Blockscout pages continue after a terminal cursor")
            break
    return paths, cursor, complete


def _capture_transfer_pages(
    client: BlockscoutClient, root: Path, scope: dict, wallet: str, last_block: int,
    configuration_hash: str, max_pages: int | None, progress=None,
) -> tuple[list[Path], dict[str, bool]]:
    all_paths, completed, pages_now = [], {}, 0
    for contract in scope["balance_contracts"]:
        contract_address = address(contract["address"])
        directory = root / contract_address
        directory.mkdir(parents=True, exist_ok=True)
        paths, cursor, done = _read_contract_pages(
            directory, contract, wallet, last_block, configuration_hash
        )
        all_paths.extend(paths)
        page_number = len(paths)
        while not done:
            if max_pages is not None and pages_now >= max_pages:
                completed[contract_address] = False
                return all_paths, completed
            params = _transfer_params(contract, cursor)
            payload = client.get(f"/addresses/{wallet}/token-transfers", params)
            raw_items = payload.get("items")
            if not isinstance(raw_items, list):
                raise EvidenceError("Blockscout transfer page has no item list")
            items = [_compact_transfer(item, contract, wallet, last_block) for item in raw_items]
            next_cursor = _next_cursor(payload.get("next_page_params"), contract)
            if next_cursor is not None and (
                next_cursor["block_number"], next_cursor["index"]
            ) >= (cursor["block_number"], cursor["index"]):
                raise EvidenceError("Blockscout transfer cursor did not move backwards")
            page = {
                "schema": 1,
                "configuration_hash": configuration_hash,
                "contract": contract_address,
                "request_cursor": cursor,
                "next_cursor": next_cursor,
                "items": items,
                "source_response_hash": digest(payload),
            }
            path = directory / f"{page_number:09d}.json"
            _write_once(path, page)
            paths.append(path)
            all_paths.append(path)
            pages_now += 1
            page_number += 1
            cursor = next_cursor
            done = cursor is None
            if progress:
                progress({
                    "status": "BLOCKSCOUT_TRANSFERS", "contract": contract_address,
                    "page": page_number, "items": len(items), "complete": done,
                })
        completed[contract_address] = True
    return all_paths, completed


def _all_transfers(paths: list[Path]) -> list[dict]:
    result = []
    for path in paths:
        result.extend(json.loads(path.read_text(encoding="utf-8"))["items"])
    return result


def _compact_log(item: dict, transaction_hash: str, position: int) -> dict:
    topics = item.get("topics")
    if not isinstance(topics, list):
        raise EvidenceError("Blockscout transaction log has no topics")
    topics = [hex_bytes(value, 32) for value in topics if value is not None]
    if hex_bytes(item["transaction_hash"], 32) != transaction_hash:
        raise EvidenceError("Blockscout log belongs to another transaction")
    return {
        "address": address((item.get("address") or {})["hash"]),
        "blockNumber": hex(uint(item["block_number"])),
        "blockHash": hex_bytes(item["block_hash"], 32),
        "transactionHash": transaction_hash,
        "transactionIndex": hex(position),
        "logIndex": hex(uint(item["index"])),
        "topics": topics,
        "data": hex_bytes(item["data"]),
        "removed": False,
    }


def _closure_from_rpc_receipt(
    receipt_rpc_url: str, receipt: dict, transaction_hash: str,
    info: dict | None = None,
) -> dict:
    """Validate one full receipt and normalize its logs as immutable evidence."""
    if (
        hex_bytes(receipt["transactionHash"], 32) != transaction_hash
        or uint(receipt["status"]) != 1
    ):
        raise EvidenceError("Fallback RPC transaction identity/status mismatch")
    block_number = uint(receipt["blockNumber"])
    position = uint(receipt["transactionIndex"])
    receipt_block_hash = hex_bytes(receipt["blockHash"], 32)
    if info is not None and (
        block_number != uint(info["block_number"])
        or position != uint(info["position"])
    ):
        raise EvidenceError("Fallback RPC receipt conflicts with Blockscout transaction")
    raw_logs = receipt.get("logs")
    if not isinstance(raw_logs, list) or not raw_logs:
        raise EvidenceError("Seed transaction has no Blockscout or fallback RPC logs")
    logs, log_indexes = [], set()
    for item in raw_logs:
        if item.get("removed") is True:
            raise EvidenceError("Fallback RPC receipt contains a removed log")
        if (
            hex_bytes(item["transactionHash"], 32) != transaction_hash
            or uint(item["blockNumber"]) != block_number
            or uint(item["transactionIndex"]) != position
            or hex_bytes(item["blockHash"], 32) != receipt_block_hash
        ):
            raise EvidenceError("Fallback RPC receipt log identity mismatch")
        log_index = uint(item["logIndex"])
        if log_index in log_indexes:
            raise EvidenceError("Fallback RPC receipt contains a duplicate log index")
        log_indexes.add(log_index)
        logs.append({
            "address": address(item["address"]),
            "blockNumber": hex(block_number),
            "blockHash": hex_bytes(item["blockHash"], 32),
            "transactionHash": transaction_hash,
            "transactionIndex": hex(position),
            "logIndex": hex(log_index),
            "topics": [hex_bytes(value, 32) for value in item["topics"]],
            "data": hex_bytes(item["data"]),
            "removed": False,
        })
    if len({row["blockHash"] for row in logs}) != 1:
        raise EvidenceError("Fallback RPC receipt spans multiple block hashes")
    result = {
        "transaction_hash": transaction_hash,
        "block_number": block_number,
        "block_hash": receipt_block_hash,
        "transaction_index": position,
        "timestamp": info.get("timestamp") if info else None,
        "method": info.get("method") if info else None,
        "logs": logs,
        "log_page_response_hashes": [],
        "log_source": "rpc:" + str(urlsplit(receipt_rpc_url).hostname),
        "receipt_response_hash": digest(receipt),
    }
    if info is not None:
        result["transaction_response_hash"] = digest(info)
    return result


def _rpc_receipt_closure(
    receipt_rpc_url: str, info: dict, transaction_hash: str,
) -> dict:
    """Recover a complete receipt when Blockscout exposes a transfer but no logs."""
    receipt = ReadOnlyRPC(url=receipt_rpc_url).call(
        "eth_getTransactionReceipt", [transaction_hash]
    )
    return _closure_from_rpc_receipt(
        receipt_rpc_url, receipt, transaction_hash, info
    )


def _rpc_receipt_batch(receipt_rpc_url: str, transactions: list[str]) -> list[dict]:
    """Fetch up to ten complete receipts in one read-only JSON-RPC request."""
    if not 1 <= len(transactions) <= 10:
        raise EvidenceError("Receipt RPC batch must contain 1..10 transactions")
    receipts = ReadOnlyRPC(url=receipt_rpc_url).batch([
        ("eth_getTransactionReceipt", [transaction_hash])
        for transaction_hash in transactions
    ])
    return [
        _closure_from_rpc_receipt(receipt_rpc_url, receipt, transaction_hash)
        for transaction_hash, receipt in zip(transactions, receipts, strict=True)
    ]


def _transaction_closure(
    client: BlockscoutClient, transaction_hash: str, *, receipt_rpc_url: str,
) -> dict:
    info = client.get(f"/transactions/{transaction_hash}")
    if hex_bytes(info["hash"], 32) != transaction_hash or info.get("status") != "ok":
        raise EvidenceError("Blockscout transaction identity/status mismatch")
    position, block_number = uint(info["position"]), uint(info["block_number"])
    cursor, page_hashes, logs = None, [], {}
    while True:
        payload = client.get(f"/transactions/{transaction_hash}/logs", cursor)
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise EvidenceError("Blockscout transaction log page has no item list")
        for item in raw_items:
            raw = _compact_log(item, transaction_hash, position)
            if uint(raw["blockNumber"]) != block_number:
                raise EvidenceError("Blockscout transaction/log block mismatch")
            index = uint(raw["logIndex"])
            if index in logs and canonical(logs[index]) != canonical(raw):
                raise EvidenceError("Blockscout returned conflicting transaction logs")
            logs[index] = raw
        page_hashes.append(digest(payload))
        next_value = payload.get("next_page_params")
        if next_value is None:
            break
        if not isinstance(next_value, dict):
            raise EvidenceError("Blockscout log pagination cursor is invalid")
        next_cursor = {key: value for key, value in next_value.items()
                       if key in {"index", "items_count", "block_number"}}
        if not next_cursor or next_cursor == cursor:
            raise EvidenceError("Blockscout log pagination did not advance")
        cursor = next_cursor
    if not logs:
        return _rpc_receipt_closure(receipt_rpc_url, info, transaction_hash)
    ordered = [logs[index] for index in sorted(logs)]
    block_hashes = {row["blockHash"] for row in ordered}
    if len(block_hashes) != 1:
        raise EvidenceError("One Blockscout transaction spans multiple block hashes")
    return {
        "transaction_hash": transaction_hash,
        "block_number": block_number,
        "block_hash": next(iter(block_hashes)),
        "transaction_index": position,
        "timestamp": info.get("timestamp"),
        "method": info.get("method"),
        "logs": ordered,
        "transaction_response_hash": digest(info),
        "log_page_response_hashes": page_hashes,
    }


def _closure_path(directory: Path, offset: int, stop: int) -> Path:
    return directory / f"{offset:09d}-{stop - 1:09d}.json"


def _read_closures(directory: Path, transactions: list[str], shard_size: int,
                   configuration_hash: str) -> tuple[int, list[Path]]:
    captured, paths = 0, []
    for offset in range(0, len(transactions), shard_size):
        stop = min(len(transactions), offset + shard_size)
        path = _closure_path(directory, offset, stop)
        if not path.exists():
            break
        shard = json.loads(path.read_text(encoding="utf-8"))
        expected = transactions[offset:stop]
        if shard.get("configuration_hash") != configuration_hash or shard.get("transactions") != expected:
            raise EvidenceError("Retained Blockscout log shard breaks the transaction index")
        if len(shard.get("closures", [])) != len(expected):
            raise EvidenceError("Retained Blockscout log shard is incomplete")
        captured += len(expected)
        paths.append(path)
    if set(paths) != set(directory.glob("*.json")):
        raise EvidenceError("Retained Blockscout log shards are non-contiguous")
    return captured, paths


def _closure_log_keys(paths: list[Path]) -> tuple[set[tuple[str, int]], int]:
    keys, count = set(), 0
    for path in paths:
        shard = json.loads(path.read_text(encoding="utf-8"))
        for closure in shard["closures"]:
            for raw in closure["logs"]:
                count += 1
                keys.add((hex_bytes(raw["transactionHash"], 32), uint(raw["logIndex"])))
    return keys, count


def backfill_wallet_blockscout(
    *, output: Path, wallet: str, identity_path: Path, scope_path: Path,
    rpc: ReadOnlyRPC | None = None, client: BlockscoutClient | None = None,
    first_block: int = CHAIN_ORIGIN_FIRST_BLOCK, last_block: int | None = None,
    confirmations: int = 200, log_shard_size: int = DEFAULT_LOG_SHARD_SIZE,
    max_transfer_pages: int | None = None, max_log_shards: int | None = None,
    log_workers: int = DEFAULT_LOG_WORKERS,
    receipt_rpc_url: str | None = None,
    receipt_batch_size: int = 0,
    receipt_batch_workers: int = 4,
    progress=None,
) -> dict:
    """Advance a no-key archive-indexer capture without inferring accounting."""
    wallet, first_block = address(wallet), uint(first_block)
    confirmations, log_shard_size = uint(confirmations), uint(log_shard_size)
    log_workers = uint(log_workers)
    receipt_batch_size = uint(receipt_batch_size)
    receipt_batch_workers = uint(receipt_batch_workers)
    if first_block != CHAIN_ORIGIN_FIRST_BLOCK:
        raise EvidenceError("Blockscout lifetime capture must begin at Polygon block 1")
    if not 1 <= log_shard_size <= 100:
        raise EvidenceError("Blockscout log shard size must be 1..100")
    if not 1 <= log_workers <= 32:
        raise EvidenceError("Blockscout log workers must be 1..32")
    if not 0 <= receipt_batch_size <= 10:
        raise EvidenceError("Receipt RPC batch size must be 0..10")
    if not 1 <= receipt_batch_workers <= 8:
        raise EvidenceError("Receipt RPC batch workers must be 1..8")
    if receipt_batch_size and not receipt_rpc_url:
        raise EvidenceError("Receipt RPC batching requires an explicit public RPC URL")
    if max_transfer_pages is not None:
        max_transfer_pages = uint(max_transfer_pages)
    if max_log_shards is not None:
        max_log_shards = uint(max_log_shards)
    output = Path(output).resolve()
    if output.exists():
        return json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))["result"]
    partial = output.with_name(output.name + ".partial")
    partial.mkdir(parents=True, exist_ok=True)
    identity_path, scope_path = Path(identity_path).resolve(), Path(scope_path).resolve()
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    if identity.get("verification_status") != "VERIFIED" or address(identity["verified_proxy_wallet"]) != wallet:
        raise EvidenceError("Identity artifact does not verify the Blockscout wallet")
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    if uint(scope.get("schema")) != 1 or uint(scope.get("chain")) != 137:
        raise EvidenceError("Blockscout historical scope must be schema 1 on Polygon")
    for contract in scope.get("balance_contracts", []):
        address(contract["address"])
        if contract.get("token_standard") not in {"erc20", "erc1155"}:
            raise EvidenceError("Blockscout scope has an unsupported token standard")
    rpc, client = rpc or ReadOnlyRPC(), client or BlockscoutClient()
    # Receipt fallback is operational rather than evidentiary: every recovered
    # response is hashed into its closure, while the frozen capture identity is
    # unchanged. Constructing a client here validates the HTTPS URL up front.
    receipt_rpc_url = ReadOnlyRPC(url=receipt_rpc_url).url if receipt_rpc_url else rpc.url
    if uint(rpc.call("eth_chainId", [])) != 137:
        raise EvidenceError("Blockscout anchor RPC is not Polygon")
    head = uint(rpc.call("eth_blockNumber", []))
    config_path = partial / "configuration.json"
    retained = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else None
    if last_block is None:
        last_block = uint(retained["last_block"]) if retained else head - confirmations
    else:
        last_block = uint(last_block)
    if head <= confirmations or last_block > head - confirmations:
        raise EvidenceError("Blockscout accounting cut is inside the confirmation buffer")
    configuration = {
        "schema": 1, "chain": 137, "wallet": wallet,
        "first_block": first_block, "last_block": last_block,
        "confirmations": confirmations, "log_shard_size": log_shard_size,
        "blockscout_source": client.base_url,
        "anchor_rpc_source": "rpc:" + str(urlsplit(rpc.url).hostname),
        "identity_sha256": _sha256(identity_path), "scope_sha256": _sha256(scope_path),
        "scope": scope["scope"], "safety": SAFETY,
    }
    configuration_hash = digest(configuration)
    _write_once(config_path, configuration)
    transfer_paths, completed_contracts = _capture_transfer_pages(
        client, partial / "transfer_pages", scope, wallet, last_block,
        configuration_hash, max_transfer_pages, progress,
    )
    transfer_complete = len(completed_contracts) == len(scope["balance_contracts"]) and all(completed_contracts.values())
    transfers = _all_transfers(transfer_paths)
    transactions = sorted({row["transaction_hash"] for row in transfers})
    closure_dir = partial / "transaction_log_shards"
    closure_dir.mkdir(exist_ok=True)
    captured_logs, closure_paths = _read_closures(
        closure_dir, transactions, log_shard_size, configuration_hash
    )
    if transfer_complete:
        _write_once(partial / "transactions.json", transactions)
        shards_now = 0
        if receipt_batch_size:
            with ThreadPoolExecutor(
                max_workers=receipt_batch_workers,
                thread_name_prefix="polyledger-receipt-batch",
            ) as executor:
                offset = captured_logs
                while offset < len(transactions):
                    if max_log_shards is not None and shards_now >= max_log_shards:
                        break
                    available_shards = receipt_batch_workers * 4
                    if max_log_shards is not None:
                        available_shards = min(
                            available_shards, max_log_shards - shards_now
                        )
                    window_stop = min(
                        len(transactions),
                        offset + available_shards * log_shard_size,
                    )
                    window = transactions[offset:window_stop]
                    batches = [
                        window[start:start + receipt_batch_size]
                        for start in range(0, len(window), receipt_batch_size)
                    ]
                    closures = [
                        closure
                        for batch_closures in executor.map(
                            lambda batch: _rpc_receipt_batch(receipt_rpc_url, batch),
                            batches,
                        )
                        for closure in batch_closures
                    ]
                    if len(closures) != len(window):
                        raise EvidenceError("Receipt RPC batch closure is incomplete")
                    for start in range(0, len(window), log_shard_size):
                        group = window[start:start + log_shard_size]
                        group_closures = closures[start:start + len(group)]
                        group_offset = offset + start
                        stop = group_offset + len(group)
                        for completed in range(1, len(group_closures) + 1):
                            if progress:
                                progress({
                                    "status": "RPC_TRANSACTION_RECEIPTS",
                                    "transactions": group_offset + completed,
                                    "transactions_total": len(transactions),
                                })
                        path = _closure_path(closure_dir, group_offset, stop)
                        _write_once(path, {
                            "schema": 1, "configuration_hash": configuration_hash,
                            "transactions": group, "closures": group_closures,
                        })
                        closure_paths.append(path)
                        captured_logs += len(group)
                        shards_now += 1
                    offset = window_stop
        else:
            with ThreadPoolExecutor(
                max_workers=log_workers,
                thread_name_prefix="polyledger-blockscout",
            ) as executor:
                for offset in range(captured_logs, len(transactions), log_shard_size):
                    if max_log_shards is not None and shards_now >= max_log_shards:
                        break
                    stop = min(len(transactions), offset + log_shard_size)
                    group = transactions[offset:stop]
                    closures = []
                    # executor.map preserves the frozen transaction order even though
                    # the public GET requests run concurrently.
                    for closure in executor.map(
                        lambda tx: _transaction_closure(
                            client, tx, receipt_rpc_url=receipt_rpc_url
                        ),
                        group,
                    ):
                        closures.append(closure)
                        if progress:
                            progress({
                                "status": "BLOCKSCOUT_TRANSACTION_LOGS",
                                "transactions": offset + len(closures),
                                "transactions_total": len(transactions),
                            })
                    path = _closure_path(closure_dir, offset, stop)
                    _write_once(path, {
                        "schema": 1, "configuration_hash": configuration_hash,
                        "transactions": group, "closures": closures,
                    })
                    closure_paths.append(path)
                    captured_logs += len(group)
                    shards_now += 1
    closure_complete = transfer_complete and captured_logs == len(transactions)
    result = {
        "version": VERSION,
        "status": "IN_PROGRESS",
        "configuration": configuration,
        "progress": {
            "contracts_complete": sum(completed_contracts.values()),
            "contracts_total": len(scope["balance_contracts"]),
            "transfer_pages": len(transfer_paths),
            "transfers": len(transfers),
            "transactions_discovered": len(transactions),
            "transactions_closed": captured_logs,
        },
        "coverage": {
            "starts_at_chain_origin": True,
            "transfer_pagination_complete": transfer_complete,
            "full_transaction_log_closure": closure_complete,
            "indexer_reports_complete": False,
            "anchor_match": False,
            "raw_actions_complete": False,
        },
        "working_directory": str(partial), "safety": SAFETY,
        "ledger_approval": False,
    }
    if closure_complete:
        seed_keys = {(row["transaction_hash"], row["log_index"]) for row in transfers}
        closure_keys, log_count = _closure_log_keys(closure_paths)
        missing = seed_keys - closure_keys
        if missing:
            raise EvidenceError("Blockscout transfer seeds are absent from transaction log closure")
        index_status = client.get("/main-page/indexing-status")
        indexer_complete = index_status.get("finished_indexing") is True and index_status.get("finished_indexing_blocks") is True
        opening_anchor, closing_anchor = rpc.block(first_block - 1), rpc.block(last_block)
        explorer_closing = client.get(f"/blocks/{last_block}")
        anchor_match = (
            uint(explorer_closing["height"]) == last_block
            and hex_bytes(explorer_closing["hash"], 32) == hex_bytes(closing_anchor["hash"], 32)
        )
        raw_complete = indexer_complete and anchor_match
        result["status"] = "RAW_INDEXER_CAPTURE_COMPLETE" if raw_complete else "CAPTURED_BLOCKED"
        result["coverage"].update({
            "indexer_reports_complete": indexer_complete,
            "anchor_match": anchor_match,
            "raw_actions_complete": raw_complete,
        })
        result["indexer_status"] = index_status
        result["anchors"] = {
            "opening": {key: opening_anchor[key] for key in ("number", "hash", "parentHash", "timestamp")},
            "closing": {key: closing_anchor[key] for key in ("number", "hash", "parentHash", "timestamp")},
        }
        result["transaction_logs"] = log_count
        result["transfer_manifest"] = [
            {"file": str(path.relative_to(partial)), "sha256": _sha256(path)} for path in transfer_paths
        ]
        result["transaction_log_manifest"] = [
            {"file": path.name, "sha256": _sha256(path)} for path in closure_paths
        ]
        result["capture_hash"] = digest({
            "configuration": configuration,
            "transfers": result["transfer_manifest"],
            "logs": result["transaction_log_manifest"],
            "anchors": result["anchors"],
        })
        result["working_directory"] = str(output)
        _write_once(partial / "run_manifest.json", {
            "schema": 1, "completed_at": now_utc(), "result": result, "safety": SAFETY,
        })
        os.rename(partial, output)
    return result
