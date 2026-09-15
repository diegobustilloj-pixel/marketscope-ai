"""Reconstruct and reconcile a point-in-time wallet inventory from a lifetime scan.

The input is the sealed continuous ``eth_getLogs`` crosscheck.  Starting from a
proved zero wallet origin, standard ERC-20/ERC-1155 transfers are folded into a
closing inventory and compared with exact-block ``balanceOf`` calls.  This is
an inventory proof only: transaction receipts and semantic action mappings are
still required for historical cost basis and PnL.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from eth_abi import decode, encode
from eth_hash.auto import keccak

from . import SAFETY
from .acquire import ReadOnlyRPC
from .common import EvidenceError, address, canonical, digest, hex_bytes, now_utc, uint
from .reconcile import independent_transfer_balances

INVENTORY_VERSION = "lifetime-wallet-inventory/1"
BALANCE_BATCH_SIZE = 100


def _sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _write_new(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as target:
        target.write(canonical(value) + "\n")
        target.flush()
        os.fsync(target.fileno())


def _load_source(crosscheck: Path, scope_path: Path, wallet: str) -> tuple[dict, dict, list[dict]]:
    summary_path = crosscheck / "summary.json"
    configuration_path = crosscheck / "configuration.json"
    manifest_path = crosscheck / "run_manifest.json"
    if not all(path.is_file() for path in (summary_path, configuration_path, manifest_path)):
        raise EvidenceError("Completed lifetime crosscheck files are missing")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (_sha256(summary_path) != manifest.get("summary_sha256")
            or _sha256(configuration_path) != manifest.get("configuration_sha256")):
        raise EvidenceError("Lifetime crosscheck manifest hash mismatch")
    if (uint(summary.get("schema")) != 1 or uint(configuration.get("schema")) != 1
            or address(summary.get("wallet")) != wallet
            or address(configuration.get("wallet")) != wallet):
        raise EvidenceError("Lifetime crosscheck identity mismatch")
    coverage = summary.get("coverage", {})
    origin = summary.get("origin", {})
    progress = summary.get("progress", {})
    if (coverage.get("continuous_blocks") is not True
            or coverage.get("starts_at_wallet_origin") is not True
            or origin.get("zero_opening_proven") is not True
            or uint(progress.get("ranges")) != uint(progress.get("ranges_total"))):
        raise EvidenceError("Lifetime crosscheck does not prove continuous zero-origin coverage")
    if uint(configuration.get("chain")) != 137:
        raise EvidenceError("Lifetime inventory only supports the Polygon capture")

    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    if uint(scope.get("schema")) != 1 or uint(scope.get("chain")) != 137:
        raise EvidenceError("Lifetime inventory scope must be schema 1 on Polygon")
    contracts = scope.get("balance_contracts")
    if not isinstance(contracts, list) or not contracts:
        raise EvidenceError("Lifetime inventory scope has no balance contracts")
    registered = {}
    for row in contracts:
        contract = address(row["address"])
        standard = row.get("token_standard")
        if contract in registered or standard not in {"erc20", "erc1155"}:
            raise EvidenceError("Lifetime inventory scope has invalid contracts")
        registered[contract] = {**row, "address": contract}

    declared = summary.get("coverage_manifest")
    if not isinstance(declared, list) or not declared:
        raise EvidenceError("Lifetime crosscheck has no coverage manifest")
    shard_dir = crosscheck / "coverage_shards"
    actual_names = {path.name for path in shard_dir.glob("*.json")}
    declared_names = {row["file"] for row in declared}
    if actual_names != declared_names or len(declared_names) != len(declared):
        raise EvidenceError("Lifetime coverage shard set differs from its manifest")
    expected_block = uint(summary["range"]["start_block"])
    logs, seen = [], {}
    for row in declared:
        path = shard_dir / row["file"]
        if _sha256(path) != row["sha256"]:
            raise EvidenceError("Lifetime coverage shard hash mismatch")
        shard = json.loads(path.read_text(encoding="utf-8"))
        range_ = shard.get("range", {})
        first, last = uint(range_["first_block"]), uint(range_["last_block"])
        if (shard.get("configuration_hash") != summary.get("configuration_hash")
                or first != expected_block or last < first):
            raise EvidenceError("Lifetime coverage shards are not contiguous")
        expected_block = last + 1
        prior_order = None
        for raw in shard.get("logs", []):
            contract = address(raw["address"])
            if contract not in registered:
                raise EvidenceError("Lifetime log references an undeclared contract")
            order = (uint(raw["blockNumber"]), uint(raw["transactionIndex"]),
                     uint(raw["logIndex"]), hex_bytes(raw["transactionHash"], 32))
            if not first <= order[0] <= last or (prior_order is not None and order < prior_order):
                raise EvidenceError("Lifetime coverage shard log order/range mismatch")
            prior_order = order
            key = (hex_bytes(raw["blockHash"], 32), order[3], order[2])
            fingerprint = digest(raw)
            if key in seen:
                if seen[key] != fingerprint:
                    raise EvidenceError("Lifetime coverage contains conflicting duplicate logs")
                continue
            seen[key] = fingerprint
            logs.append(raw)
    if expected_block - 1 != uint(summary["range"]["end_block"]):
        raise EvidenceError("Lifetime coverage does not reach the declared closing block")
    if len(logs) != uint(summary["comparison"]["rpc_wallet_balance_logs"]):
        raise EvidenceError("Lifetime log count differs from the sealed summary")
    return summary, {"scope": scope, "contracts": registered}, logs


def _token_ids(logs: list[dict], contracts: dict) -> dict[str, set[int]]:
    single = "0x" + keccak(b"TransferSingle(address,address,address,uint256,uint256)").hex()
    batch = "0x" + keccak(b"TransferBatch(address,address,address,uint256[],uint256[])").hex()
    result: dict[str, set[int]] = defaultdict(set)
    for raw in logs:
        contract = address(raw["address"])
        if contracts[contract]["token_standard"] != "erc1155":
            continue
        topics = [hex_bytes(item, 32) for item in raw.get("topics", [])]
        data = bytes.fromhex(hex_bytes(raw["data"])[2:])
        if topics and topics[0] == single:
            if len(data) != 64:
                raise EvidenceError("Malformed lifetime TransferSingle")
            result[contract].add(int.from_bytes(data[:32], "big"))
        elif topics and topics[0] == batch:
            ids, quantities = decode(["uint256[]", "uint256[]"], data, strict=True)
            if len(ids) != len(quantities):
                raise EvidenceError("Malformed lifetime TransferBatch")
            result[contract].update(map(int, ids))
        else:
            raise EvidenceError("Unexpected event in ERC-1155 lifetime evidence")
    return result


def _state_calls(wallet: str, contracts: dict, ids_by_contract: dict[str, set[int]]) -> list[dict]:
    calls = []
    erc20_selector = "0x" + keccak(b"balanceOf(address)")[:4].hex()
    batch_selector = "0x" + keccak(b"balanceOfBatch(address[],uint256[])")[:4].hex()
    for contract, row in sorted(contracts.items()):
        if row["token_standard"] == "erc20":
            calls.append({"to": contract,
                          "data": erc20_selector + encode(["address"], [wallet]).hex(),
                          "assets": [f"137:{contract}:erc20"], "ids": []})
            continue
        ids = sorted(ids_by_contract.get(contract, set()))
        for offset in range(0, len(ids), BALANCE_BATCH_SIZE):
            group = ids[offset:offset + BALANCE_BATCH_SIZE]
            calls.append({
                "to": contract,
                "data": batch_selector + encode(
                    ["address[]", "uint256[]"], [[wallet] * len(group), group]
                ).hex(),
                "assets": [f"137:{contract}:{token}" for token in group],
                "ids": group,
            })
    return calls


def _state_balances(rpc: ReadOnlyRPC, *, wallet: str, block_number: int,
                    block_hash: str, contracts: dict,
                    ids_by_contract: dict[str, set[int]]) -> dict:
    if uint(rpc.call("eth_chainId", [])) != 137:
        raise EvidenceError("Inventory state RPC is not Polygon")
    anchored = rpc.block(block_number)
    if hex_bytes(anchored["hash"], 32) != block_hash:
        raise EvidenceError("Inventory state RPC disagrees with the frozen closing block")
    pinned = {"blockHash": block_hash, "requireCanonical": True}
    number_tag = hex(block_number)
    calls = _state_calls(wallet, contracts, ids_by_contract)
    balances, errors, methods = {}, [], set()
    for offset in range(0, len(calls), 10):
        group = calls[offset:offset + 10]
        requests = [("eth_call", [{"to": item["to"], "data": item["data"]}, pinned])
                    for item in group]
        try:
            payloads = rpc.batch(requests)
            methods.add("eip1898-block-hash")
        except EvidenceError:
            payloads = []
            for item in group:
                try:
                    payloads.append(rpc.call("eth_call", [
                        {"to": item["to"], "data": item["data"]}, number_tag
                    ]))
                    methods.add("block-number-with-hash-recheck")
                except EvidenceError:
                    payloads.append(None)
                    errors.extend(item["assets"])
        for item, payload in zip(group, payloads, strict=True):
            if payload is None:
                continue
            try:
                raw = bytes.fromhex(hex_bytes(payload)[2:])
                values = ([int.from_bytes(raw, "big")] if not item["ids"]
                          else list(decode(["uint256[]"], raw, strict=True)[0]))
                if len(values) != len(item["assets"]):
                    raise EvidenceError("Exact-block balance result length mismatch")
                balances.update(zip(item["assets"], map(uint, values)))
            except (EvidenceError, ValueError, OverflowError):
                errors.extend(item["assets"])
    if "block-number-with-hash-recheck" in methods:
        if hex_bytes(rpc.block(block_number)["hash"], 32) != block_hash:
            raise EvidenceError("Reorg during number-pinned inventory reads")
    return {"block_number": block_number, "block_hash": block_hash,
            "balances": balances, "errors": sorted(set(errors)),
            "calls": len(calls), "pinning_methods": sorted(methods),
            "source": "rpc:" + str(urlsplit(rpc.url).hostname)}


def _quantity_text(value: int) -> str:
    whole, fraction = divmod(value, 1_000_000)
    return f"{whole}.{fraction:06d}"


def reconstruct_lifetime_inventory(*, crosscheck: Path, scope_path: Path,
                                   wallet: str, state_rpc: ReadOnlyRPC) -> tuple[dict, list[dict], dict]:
    """Return summary, nonzero inventory, and exact-block reconciliation."""
    crosscheck, scope_path = Path(crosscheck).resolve(), Path(scope_path).resolve()
    wallet = address(wallet)
    source, registry, logs = _load_source(crosscheck, scope_path, wallet)
    contracts = registry["contracts"]
    token_contracts = {key for key, row in contracts.items()
                       if row["token_standard"] == "erc1155"}
    cash_contracts = {key for key, row in contracts.items()
                      if row["token_standard"] == "erc20"}
    folded = independent_transfer_balances(
        logs, wallet, {}, 137, token_contracts, cash_contracts
    )
    ids_by_contract = _token_ids(logs, contracts)
    block_number = uint(source["range"]["end_block"])
    configuration = json.loads((crosscheck / "configuration.json").read_text(encoding="utf-8"))
    block_hash = hex_bytes(configuration["closing_block_hash"], 32)
    state = _state_balances(
        state_rpc, wallet=wallet, block_number=block_number, block_hash=block_hash,
        contracts=contracts, ids_by_contract=ids_by_contract,
    )
    assets = set(folded) | set(state["balances"])
    mismatches = [
        {"asset": asset, "folded_atomic": folded.get(asset, 0),
         "state_atomic": state["balances"].get(asset)}
        for asset in sorted(assets)
        if (asset not in state["errors"]
            and folded.get(asset, 0) != state["balances"].get(asset))
    ]
    inventory = []
    by_contract = defaultdict(lambda: {"assets": 0, "quantity_atomic": 0})
    for asset, quantity in sorted(folded.items()):
        if not quantity:
            continue
        _, contract, token = asset.split(":", 2)
        row = contracts[contract]
        inventory.append({
            "asset": asset, "contract": contract, "contract_label": row["label"],
            "token_standard": row["token_standard"], "token": token,
            "quantity_atomic": quantity, "quantity_6dp": _quantity_text(quantity),
        })
        by_contract[contract]["assets"] += 1
        by_contract[contract]["quantity_atomic"] += quantity
    event_counts = Counter(hex_bytes(raw["topics"][0], 32) for raw in logs)
    blockers = []
    if state["errors"]:
        blockers.append("EXACT_BLOCK_BALANCE_READS_INCOMPLETE")
    if mismatches:
        blockers.append("FOLDED_INVENTORY_STATE_MISMATCH")
    report = {
        "schema": 1, "version": INVENTORY_VERSION,
        "status": "INVENTORY_RECONCILED" if not blockers else "INVENTORY_BLOCKED",
        "wallet": wallet, "chain": 137,
        "range": {**source["range"], "closing_block_hash": block_hash,
                  "zero_opening_proven": True},
        "source": {"crosscheck": str(crosscheck),
                   "crosscheck_summary_sha256": _sha256(crosscheck / "summary.json"),
                   "wallet_balance_logs": len(logs),
                   "continuous_blocks": True,
                   "blockscout_logs_omitted": uint(source["comparison"]["extra_count"])},
        "inventory": {"nonzero_assets": len(inventory),
                      "erc20_assets": sum(row["token_standard"] == "erc20" for row in inventory),
                      "erc1155_assets": sum(row["token_standard"] == "erc1155" for row in inventory),
                      "by_contract": {
                          contract: {"label": contracts[contract]["label"], **counts,
                                     "quantity_6dp": _quantity_text(counts["quantity_atomic"])}
                          for contract, counts in sorted(by_contract.items())
                      },
                      "inventory_hash": digest(inventory)},
        "events": {"logs": len(logs), "by_topic": dict(sorted(event_counts.items()))},
        "reconciliation": {"status": "MATCH" if not state["errors"] and not mismatches else "BLOCKED",
                           "assets_checked": len(assets), "state_calls": state["calls"],
                           "state_source": state["source"],
                           "pinning_methods": state["pinning_methods"],
                           "read_error_count": len(state["errors"]),
                           "read_error_sample": state["errors"][:20],
                           "mismatch_count": len(mismatches),
                           "mismatch_sample": mismatches[:20],
                           "mismatch_hash": digest(mismatches)},
        "cost_basis": {"available": False,
                       "reason": "Full transaction receipts and reviewed semantic action mappings are still required"},
        "blockers": blockers, "ledger_approval": False,
        "execution_allowed": False, "safety": SAFETY,
    }
    return report, inventory, state


def build_lifetime_inventory_file(*, crosscheck: Path, scope_path: Path,
                                  wallet: str, state_rpc: ReadOnlyRPC,
                                  output: Path) -> dict:
    """Build a sealed inventory report in a new output directory."""
    crosscheck, scope_path, output = (Path(crosscheck).resolve(), Path(scope_path).resolve(),
                                      Path(output).resolve())
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists():
        raise EvidenceError("Lifetime inventory output or partial directory already exists")
    partial.mkdir(parents=True)
    report, inventory, state = reconstruct_lifetime_inventory(
        crosscheck=crosscheck, scope_path=scope_path, wallet=wallet, state_rpc=state_rpc
    )
    configuration = {
        "schema": 1, "version": INVENTORY_VERSION, "wallet": address(wallet),
        "crosscheck": str(crosscheck), "scope": str(scope_path),
        "crosscheck_summary_sha256": _sha256(crosscheck / "summary.json"),
        "scope_sha256": _sha256(scope_path), "state_source": state["source"],
        "safety": SAFETY,
    }
    _write_new(partial / "configuration.json", configuration)
    _write_new(partial / "inventory.json", inventory)
    _write_new(partial / "state_balances.json", state)
    _write_new(partial / "summary.json", report)
    project = Path(__file__).resolve().parents[3]
    try:
        git = ["git", "-c", "safe.directory=" + str(project).replace("\\", "/"),
               "-C", str(project)]
        commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
        clean = not subprocess.check_output(git + ["status", "--porcelain"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit, clean = "unavailable", False
    files = {path.name: _sha256(path) for path in sorted(partial.glob("*.json"))}
    _write_new(partial / "run_manifest.json", {
        "schema": 1, "version": INVENTORY_VERSION, "completed_at": now_utc(),
        "code_commit": commit, "working_tree_clean": clean, "files": files,
        "summary_hash": digest(report), "status": report["status"], "safety": SAFETY,
    })
    os.replace(partial, output)
    return report
