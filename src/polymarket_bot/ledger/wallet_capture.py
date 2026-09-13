"""Resumable 24-hour public-evidence capture for one known wallet.

The capture combines paginated Data API activity with wallet-indexed Polygon
logs and full receipts for the union of discovered transactions. It captures
scoped start/end balances, but never invents opening basis or independent PnL.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from eth_abi import decode, encode
from eth_hash.auto import keccak

from polymarket_bot.polyledger import deduplicate_rows, fetch_activity_window

from . import SAFETY, VERSION
from .acquire import ExactPublicClient, ReadOnlyRPC
from .common import EvidenceError, address, canonical, digest, hex_bytes, now_utc, uint
from .decoders import event_topic
from .reconcile import independent_transfer_balances
from .registry import BEACON_SLOT, IMPLEMENTATION_SLOT, code_hash

WINDOW_SECONDS = 86_400
QUERY_BLOCKS = 10_000
EVENTS = {
    "clob_v2_ctf": {"OrderFilled"},
    "ctf": {"TransferSingle", "TransferBatch", "PositionSplit", "PositionsMerge", "PayoutRedemption"},
    "negrisk": {"PositionSplit", "PositionsMerge", "PositionsConverted", "PayoutRedemption"},
    "collateral": {"Transfer"},
    "combo_v2": {"TransferSingle", "TransferBatch"},
}
PARTICIPANTS = {"maker", "taker", "stakeholder", "redeemer", "from", "to", "operator"}
TRANSFER_TOPICS = {
    "erc20": "0x" + keccak(b"Transfer(address,address,uint256)").hex(),
    "single": "0x" + keccak(b"TransferSingle(address,address,address,uint256,uint256)").hex(),
    "batch": "0x" + keccak(b"TransferBatch(address,address,address,uint256[],uint256[])").hex(),
}


def _save(path: Path, value) -> None:
    content = canonical(value) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise EvidenceError(f"Retained capture conflicts with {path.name}")
        return
    temporary = path.with_name(path.name + f".{os.getpid()}.partial")
    with temporary.open("x", encoding="utf-8") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _compact_block(value: dict) -> dict:
    return {key: value[key] for key in ("number", "hash", "parentHash", "timestamp")}


def _first_block_at_or_after(rpc: ReadOnlyRPC, target: int, end: int) -> int:
    cache = {}
    def timestamp(number: int) -> int:
        if number not in cache:
            cache[number] = uint(rpc.block(number)["timestamp"])
        return cache[number]
    low = max(0, end - 100_000)
    step = 100_000
    while low and timestamp(low) >= target:
        step *= 2
        low = max(0, end - step)
    if timestamp(end) < target:
        raise EvidenceError("Finalized block precedes requested capture window")
    while low < end:
        middle = (low + end) // 2
        if timestamp(middle) < target:
            low = middle + 1
        else:
            end = middle
    return low


def _abis(catalog_path: Path, combo_path: Path) -> dict[str, list[dict]]:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))["families"]
    result = {family: catalog[family]["abi"] for family in EVENTS if family != "combo_v2"}
    result["combo_v2"] = json.loads(combo_path.read_text(encoding="utf-8"))["abi"]
    return result


def _filters(deployments: dict, abis: dict, wallet: str) -> list[dict]:
    wallet_topic = "0x" + encode(["address"], [wallet]).hex()
    result = []
    for contract in deployments["contracts"]:
        family = contract["family"]
        wanted = EVENTS.get(family, set())
        for event in abis[family]:
            if event.get("type") != "event" or event.get("name") not in wanted:
                continue
            indexed = [item for item in event["inputs"] if item["indexed"]]
            for offset, item in enumerate(indexed, 1):
                if item["type"] == "address" and item["name"] in PARTICIPANTS:
                    topics = [event_topic(event)] + [None] * len(indexed)
                    topics[offset] = wallet_topic
                    result.append({"contract": address(contract["address"]), "label": contract["label"],
                                   "family": family, "event": event["name"], "field": item["name"],
                                   "topic_index": offset, "topics": topics})
    return result


def _seed_logs(rpc: ReadOnlyRPC, filters: list[dict], first: int, last: int, progress=None) -> tuple[list[dict], list[dict]]:
    calls, metadata = [], []
    for item in filters:
        for start in range(first, last + 1, QUERY_BLOCKS):
            stop = min(last, start + QUERY_BLOCKS - 1)
            calls.append(("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(stop),
                                             "address": item["contract"], "topics": item["topics"]}]))
            metadata.append((item, start, stop))
    logs, matches = {}, {}
    for offset in range(0, len(calls), 10):
        group = calls[offset:offset + 10]
        rows = rpc.batch(group)
        for payload, (item, start, stop) in zip(rows, metadata[offset:offset + 10]):
            if not isinstance(payload, list):
                raise EvidenceError("eth_getLogs returned a non-list payload")
            for raw in payload:
                number = uint(raw["blockNumber"])
                topics = raw.get("topics") or []
                if (not start <= number <= stop or address(raw["address"]) != item["contract"]
                        or raw.get("removed") is True or len(topics) <= item["topic_index"]
                        or hex_bytes(topics[item["topic_index"]], 32) != hex_bytes(item["topics"][item["topic_index"]], 32)):
                    raise EvidenceError("RPC returned a log outside the wallet-indexed query")
                key = (hex_bytes(raw["transactionHash"], 32), uint(raw["logIndex"]))
                logs[key] = raw
                matches.setdefault(key, set()).add(item["label"] + ":" + item["event"] + ":" + item["field"])
        if progress:
            progress({"status": "SEED_LOGS", "queries": min(offset + 10, len(calls)), "total": len(calls)})
    ordered = [logs[key] for key in sorted(logs, key=lambda value: (uint(logs[value]["blockNumber"]), value[1]))]
    evidence = [{"transaction_hash": key[0], "log_index": key[1], "matched_filters": sorted(matches[key])}
                for key in sorted(matches)]
    return ordered, evidence


def _receipts(rpc: ReadOnlyRPC, transactions: list[str], directory: Path, first: int, last: int, progress=None) -> list[dict]:
    directory.mkdir(exist_ok=True)
    results = {}
    for offset in range(0, len(transactions), 10):
        group = transactions[offset:offset + 10]
        missing = [tx for tx in group if not (directory / f"{tx}.json").exists()]
        if missing:
            payloads = rpc.batch([("eth_getTransactionReceipt", [tx]) for tx in missing])
            for tx, receipt in zip(missing, payloads):
                if (hex_bytes(receipt["transactionHash"], 32) != tx
                        or not first <= uint(receipt["blockNumber"]) <= last):
                    raise EvidenceError("Receipt identity or capture range mismatch")
                _save(directory / f"{tx}.json", receipt)
        for tx in group:
            receipt = json.loads((directory / f"{tx}.json").read_text(encoding="utf-8"))
            if hex_bytes(receipt["transactionHash"], 32) != tx:
                raise EvidenceError("Retained receipt identity mismatch")
            results[tx] = receipt
        if progress:
            progress({"status": "RECEIPTS", "captured": min(offset + 10, len(transactions)),
                      "total": len(transactions)})
    return [results[tx] for tx in transactions]


def _block_headers(rpc: ReadOnlyRPC, receipts: list[dict], first_header: dict, end_header: dict) -> list[dict]:
    numbers = sorted({uint(row["blockNumber"]) for row in receipts} | {uint(first_header["number"]), uint(end_header["number"])})
    headers = {}
    for offset in range(0, len(numbers), 10):
        group = numbers[offset:offset + 10]
        payloads = rpc.batch([("eth_getBlockByNumber", [hex(number), False]) for number in group])
        for number, block in zip(group, payloads):
            if uint(block["number"]) != number:
                raise EvidenceError("Wrong block returned while sealing receipts")
            headers[number] = _compact_block(block)
    for receipt in receipts:
        if headers[uint(receipt["blockNumber"])]["hash"] != receipt["blockHash"]:
            raise EvidenceError("Receipt block hash differs from sealed header")
    return [headers[number] for number in numbers]


def _balance_calls(wallet: str, deployments: dict, token_ids: list[int]) -> list[tuple[str, str]]:
    result = []
    for contract in deployments["contracts"]:
        standard = contract.get("token_standard")
        contract_address = address(contract["address"])
        if standard == "erc20":
            data = "0x" + keccak(b"balanceOf(address)")[:4].hex() + encode(["address"], [wallet]).hex()
            result.append((f"137:{contract_address}:erc20", data))
        elif standard == "erc1155":
            selector = "0x" + keccak(b"balanceOf(address,uint256)")[:4].hex()
            for token in token_ids:
                result.append((f"137:{contract_address}:{token}", selector + encode(["address", "uint256"], [wallet, token]).hex()))
    return result


def _token_ids(logs: list[dict], token_contracts: set[str]) -> set[int]:
    result = set()
    for raw in logs:
        topics = raw.get("topics") or []
        if address(raw["address"]) not in token_contracts or not topics:
            continue
        topic = topics[0].lower()
        data = bytes.fromhex(hex_bytes(raw["data"])[2:])
        if topic == TRANSFER_TOPICS["single"]:
            if len(data) != 64:
                raise EvidenceError("Malformed captured TransferSingle")
            result.add(int.from_bytes(data[:32], "big"))
        elif topic == TRANSFER_TOPICS["batch"]:
            ids, amounts = decode(["uint256[]", "uint256[]"], data, strict=True)
            if len(ids) != len(amounts):
                raise EvidenceError("Malformed captured TransferBatch")
            result.update(ids)
    return result


def _wallet_balance_logs(receipts: list[dict], wallet: str, token_contracts: set[str],
                         cash_contracts: set[str]) -> list[dict]:
    wallet_word = wallet[2:].lower()
    result = []
    for receipt in receipts:
        for raw in receipt.get("logs", []):
            contract = address(raw["address"])
            topics = raw.get("topics") or []
            if not topics:
                continue
            topic = topics[0].lower()
            participant_topics = []
            if contract in cash_contracts and topic == TRANSFER_TOPICS["erc20"] and len(topics) == 3:
                participant_topics = topics[1:3]
            elif (contract in token_contracts and topic in {TRANSFER_TOPICS["single"], TRANSFER_TOPICS["batch"]}
                  and len(topics) == 4):
                participant_topics = topics[2:4]
            if any(value[-40:].lower() == wallet_word for value in participant_topics):
                result.append(raw)
    return result


def _balances(rpc: ReadOnlyRPC, block: dict, wallet: str, calls: list[tuple[str, str]]) -> dict:
    pinned = {"blockHash": hex_bytes(block["hash"], 32), "requireCanonical": True}
    number_pinned = hex(uint(block["number"]))
    values, errors, methods = {}, [], set()
    for offset in range(0, len(calls), 10):
        group = calls[offset:offset + 10]
        rpc_calls = [("eth_call", [{"to": asset.split(":")[1], "data": data}, pinned])
                     for asset, data in group]
        try:
            payloads = rpc.batch(rpc_calls)
            methods.add("eip1898-block-hash")
        except EvidenceError:
            # Some public Polygon RPCs reject EIP-1898. A numbered block is
            # acceptable only with canonical hash checks before and after.
            numbered_calls = [(method, [params[0], number_pinned]) for method, params in rpc_calls]
            try:
                payloads = rpc.batch(numbered_calls)
                methods.add("block-number-with-hash-recheck")
            except EvidenceError:
                payloads = []
                for (asset, _), call in zip(group, numbered_calls):
                    try:
                        payloads.append(rpc.call(*call))
                        methods.add("block-number-with-hash-recheck")
                    except EvidenceError:
                        payloads.append(None)
                        errors.append(asset)
        for (asset, _), value in zip(group, payloads):
            if value is not None:
                try:
                    values[asset] = int(hex_bytes(value, 32), 16)
                except (EvidenceError, ValueError):
                    errors.append(asset)
    return {"chain": 137, "wallet": wallet, "block_number": uint(block["number"]),
            "block_hash": block["hash"], "balances": values, "errors": sorted(set(errors)),
            "pinning_methods": sorted(methods), "complete_for_declared_scope": not errors}


def _observe_contract(rpc: ReadOnlyRPC, contract: dict, block: dict) -> dict:
    try:
        result = rpc.observe_contract(contract, block)
        result["pinning_method"] = "eip1898-block-hash"
        return result
    except EvidenceError:
        tag = hex(uint(block["number"]))
        contract_address = address(contract["address"])
        result = {"chain": uint(rpc.call("eth_chainId", [])), "address": contract_address,
                  "block_number": uint(block["number"]), "block_hash": block["hash"],
                  "source": "rpc:" + str(urlsplit(rpc.url).hostname),
                  "code": rpc.call("eth_getCode", [contract_address, tag]),
                  "implementation_slot": rpc.call("eth_getStorageAt", [contract_address, IMPLEMENTATION_SLOT, tag]),
                  "beacon_slot": rpc.call("eth_getStorageAt", [contract_address, BEACON_SLOT, tag]),
                  "pinning_method": "block-number-with-hash-recheck"}
        if contract.get("proxy_kind", "direct") != "direct":
            if contract["proxy_kind"] != "eip1967":
                raise EvidenceError("24h capture supports direct and EIP-1967 observations")
            implementation = "0x" + hex_bytes(result["implementation_slot"], 32)[-40:]
            result["implementation_code"] = rpc.call("eth_getCode", [implementation, tag])
        if rpc.block(uint(block["number"]))["hash"] != block["hash"]:
            raise EvidenceError("Reorg during number-pinned contract observation")
        return result


def capture_wallet_24h(*, output: Path, wallet: str, identity_path: Path, deployments_path: Path,
                       catalog_path: Path, combo_path: Path, validation_path: Path,
                       rpc: ReadOnlyRPC | None = None, state_rpc: ReadOnlyRPC | None = None,
                       data_client=None, confirmations: int = 200, progress=None) -> dict:
    wallet, confirmations = address(wallet), uint(confirmations)
    output = Path(output).resolve()
    if output.exists():
        raise EvidenceError("Capture output already exists")
    partial = output.with_name(output.name + ".partial")
    partial.mkdir(parents=True, exist_ok=True)
    identity_path, deployments_path = Path(identity_path).resolve(), Path(deployments_path).resolve()
    catalog_path, combo_path = Path(catalog_path).resolve(), Path(combo_path).resolve()
    validation_path = Path(validation_path).resolve()
    input_paths = {"identity": identity_path, "deployments": deployments_path, "abi_catalog": catalog_path,
                   "combo_abi": combo_path, "validation": validation_path}
    input_hashes = {name: _sha256(path) for name, path in input_paths.items()}
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    if identity.get("verification_status") != "VERIFIED" or address(identity["verified_proxy_wallet"]) != wallet:
        raise EvidenceError("Identity artifact does not verify the requested wallet")
    deployments = json.loads(deployments_path.read_text(encoding="utf-8"))
    if uint(deployments["chain"]) != 137:
        raise EvidenceError("Wallet capture deployments must target Polygon")
    rpc = rpc or ReadOnlyRPC()
    state_rpc = state_rpc or rpc
    data_client = data_client or ExactPublicClient()
    config_file = partial / "configuration.json"
    if config_file.exists():
        configuration = json.loads(config_file.read_text(encoding="utf-8"))
        if (configuration["wallet"] != wallet or configuration["confirmations"] != confirmations
                or configuration["deployments_hash"] != digest(deployments)
                or configuration.get("input_hashes") != input_hashes
                or configuration.get("rpc_source") != rpc.url
                or configuration.get("state_rpc_source", configuration.get("rpc_source")) != state_rpc.url):
            raise EvidenceError("Resume configuration differs from retained 24h capture")
        first, last = configuration["first_block"], configuration["last_block"]
        first_header, end_header = rpc.block(first), rpc.block(last)
    else:
        head = uint(rpc.call("eth_blockNumber", []))
        if head <= confirmations:
            raise EvidenceError("Confirmation buffer exceeds chain head")
        last = head - confirmations
        end_header = rpc.block(last)
        end_time = uint(end_header["timestamp"])
        first = _first_block_at_or_after(rpc, end_time - WINDOW_SECONDS, last)
        first_header = rpc.block(first)
        configuration = {"schema": 1, "chain": 137, "wallet": wallet, "confirmations": confirmations,
                         "first_block": first, "last_block": last,
                         "start_timestamp": uint(first_header["timestamp"]), "end_timestamp": end_time,
                         "requested_seconds": WINDOW_SECONDS, "rpc_source": rpc.url,
                         "state_rpc_source": state_rpc.url,
                         "data_source": "https://data-api.polymarket.com/activity",
                         "deployments_hash": digest(deployments), "input_hashes": input_hashes,
                         "safety": SAFETY}
        _save(config_file, configuration)
    opening_header = rpc.block(first - 1)
    if (state_rpc.block(first - 1)["hash"] != opening_header["hash"]
            or state_rpc.block(last)["hash"] != end_header["hash"]):
        raise EvidenceError("Acquisition and state RPCs disagree on capture anchors")

    activity_file = partial / "activity.json"
    audit_file = partial / "activity_pages.json"
    if activity_file.exists():
        activity = json.loads(activity_file.read_text(encoding="utf-8"))
        audits = json.loads(audit_file.read_text(encoding="utf-8"))
    else:
        activity, page_rows = fetch_activity_window(data_client, wallet, configuration["start_timestamp"],
                                                     configuration["end_timestamp"])
        activity = deduplicate_rows(activity)
        audits = [row.as_dict() for row in page_rows]
        _save(activity_file, activity)
        _save(audit_file, audits)

    abis = _abis(catalog_path, combo_path)
    filters = _filters(deployments, abis, wallet)
    seed_file, match_file = partial / "seed_logs.json", partial / "seed_matches.json"
    if seed_file.exists():
        seed_logs = json.loads(seed_file.read_text(encoding="utf-8"))
        seed_matches = json.loads(match_file.read_text(encoding="utf-8"))
    else:
        seed_logs, seed_matches = _seed_logs(rpc, filters, first, last, progress)
        _save(seed_file, seed_logs)
        _save(match_file, seed_matches)
    api_transactions = {hex_bytes(row["transactionHash"], 32) for row in activity if row.get("transactionHash")}
    seed_transactions = {hex_bytes(row["transactionHash"], 32) for row in seed_logs}
    transactions = sorted(api_transactions | seed_transactions)
    receipts = _receipts(rpc, transactions, partial / "receipts", first, last, progress)
    headers = _block_headers(rpc, receipts, first_header, end_header)
    _save(partial / "block_headers.json", headers)

    receipt_logs = {(hex_bytes(log["transactionHash"], 32), uint(log["logIndex"]))
                    for receipt in receipts for log in receipt.get("logs", [])}
    missing_seeds = [(hex_bytes(log["transactionHash"], 32), uint(log["logIndex"])) for log in seed_logs
                     if (hex_bytes(log["transactionHash"], 32), uint(log["logIndex"])) not in receipt_logs]
    if missing_seeds:
        raise EvidenceError("Wallet-indexed logs are missing from their transaction receipts")

    token_contracts = {address(row["address"]) for row in deployments["contracts"]
                       if row.get("token_standard") == "erc1155"}
    cash_contracts = {address(row["address"]) for row in deployments["contracts"]
                      if row.get("token_standard") == "erc20"}
    token_ids = {uint(row["asset"]) for row in activity if str(row.get("asset") or "").isdigit()}
    token_ids |= _token_ids(seed_logs, token_contracts)
    token_ids = sorted(token_ids)
    balance_calls = _balance_calls(wallet, deployments, token_ids)
    opening_file, closing_file = partial / "opening_balances.json", partial / "closing_balances.json"
    if opening_file.exists() and closing_file.exists():
        opening_balances = json.loads(opening_file.read_text(encoding="utf-8"))
        closing_balances = json.loads(closing_file.read_text(encoding="utf-8"))
    else:
        opening_balances = _balances(state_rpc, opening_header, wallet, balance_calls)
        closing_balances = _balances(state_rpc, end_header, wallet, balance_calls)
        _save(opening_file, opening_balances)
        _save(closing_file, closing_balances)

    scoped_receipt_logs = _wallet_balance_logs(receipts, wallet, token_contracts, cash_contracts)
    balance_mismatches = []
    if not opening_balances["errors"] and not closing_balances["errors"]:
        folded = independent_transfer_balances(scoped_receipt_logs, wallet, opening_balances["balances"],
                                                137, token_contracts, cash_contracts)
        assets = set(opening_balances["balances"]) | set(closing_balances["balances"]) | set(folded)
        balance_mismatches = [{"asset": asset, "opening": opening_balances["balances"].get(asset),
                              "folded": folded.get(asset, 0),
                              "closing": closing_balances["balances"].get(asset)}
                             for asset in sorted(assets)
                             if folded.get(asset, 0) != closing_balances["balances"].get(asset)]
    balance_reconciliation = {"status": "MATCH" if not balance_mismatches
                              and not opening_balances["errors"] and not closing_balances["errors"] else "BLOCKED",
                              "receipt_transfer_logs": len(scoped_receipt_logs),
                              "assets": len(balance_calls), "mismatches": balance_mismatches}
    _save(partial / "balance_reconciliation.json", balance_reconciliation)

    observations, observation_errors = [], []
    for contract in deployments["contracts"]:
        try:
            observations.append(_observe_contract(state_rpc, contract, end_header))
        except EvidenceError:
            observation_errors.append({"label": contract["label"], "address": address(contract["address"]),
                                       "code": "EXACT_BLOCK_OBSERVATION_UNAVAILABLE"})
    _save(partial / "contract_observations.json", observations)
    _save(partial / "contract_observation_errors.json", observation_errors)
    validation = json.loads(validation_path.read_text(encoding="utf-8"))["contract_source_qualification"]
    by_address = {address(row["address"]): row for row in observations}
    ctf_address = address(validation["ctf"]["address"])
    combo_address = address(validation["combo"]["proxy"])
    ctf_observation = by_address.get(ctf_address)
    combo_observation = by_address.get(combo_address)
    ctf_continues = bool(ctf_observation and
                         code_hash(ctf_observation["code"]) == validation["ctf"]["runtime_code_hash"])
    combo_continues = bool(
        combo_observation
        and code_hash(combo_observation["code"]) == validation["combo"]["proxy_code_hash"]
        and code_hash(combo_observation["implementation_code"])
        == validation["combo"]["implementation_code_hash"]
        and int(combo_observation["implementation_slot"], 16)
        == int(validation["combo"]["implementation"], 16)
    )
    if (rpc.block(first - 1)["hash"] != opening_header["hash"]
            or rpc.block(last)["hash"] != end_header["hash"]):
        raise EvidenceError("Reorg during 24h capture finalization")

    blockers = ["OPENING_BASIS_MISSING", "FULL_WALLET_ASSET_UNIVERSE_NOT_PROVEN",
                "CLOB_ORDER_SNAPSHOT_MISSING", "INDEPENDENT_PNL_PIPELINE_MISSING",
                "DEPLOYMENTS_NOT_ALL_SOURCE_QUALIFIED",
                "COMBO_MODULE_ECONOMICS_AND_FULL_VECTORS_PENDING"]
    acquisition_host = urlsplit(rpc.url).hostname
    state_host = urlsplit(state_rpc.url).hostname
    if acquisition_host == state_host:
        blockers.append("SINGLE_RPC_PROVIDER")
    if opening_balances["errors"] or closing_balances["errors"]:
        blockers.append("SCOPED_BALANCE_READS_INCOMPLETE")
    if balance_reconciliation["status"] != "MATCH":
        blockers.append("SCOPED_BALANCE_RECONCILIATION_MISMATCH")
    if observation_errors:
        blockers.append("CONTRACT_OBSERVATIONS_INCOMPLETE")
    if not ctf_observation or not combo_observation:
        blockers.append("QUALIFIED_CONTRACT_OBSERVATION_MISSING")
    elif not ctf_continues or not combo_continues:
        blockers.append("QUALIFIED_CONTRACT_RUNTIME_CHANGED")
    receipt_log_count = sum(len(row.get("logs", [])) for row in receipts)
    summary = {"schema": 1, "version": VERSION, "status": "CAPTURED_BLOCKED", "wallet": wallet,
               "window": {"first_block": first, "last_block": last,
                          "start_timestamp": configuration["start_timestamp"],
                          "end_timestamp": configuration["end_timestamp"],
                          "duration_seconds": configuration["end_timestamp"] - configuration["start_timestamp"]},
               "activity": {"rows": len(activity), "pages": len(audits),
                            "transactions": len(api_transactions),
                            "without_transaction": sum(not row.get("transactionHash") for row in activity)},
               "onchain": {"filters": len(filters), "seed_logs": len(seed_logs),
                           "seed_transactions": len(seed_transactions), "receipts": len(receipts),
                           "receipt_logs": receipt_log_count, "sealed_block_headers": len(headers)},
               "balances": {"assets": len(balance_calls), "opening_block": uint(opening_header["number"]),
                            "closing_block": uint(end_header["number"]),
                            "scope_complete": not opening_balances["errors"] and not closing_balances["errors"],
                            "opening_errors": len(opening_balances["errors"]),
                            "closing_errors": len(closing_balances["errors"]),
                            "opening_pinning": opening_balances.get("pinning_methods", []),
                            "closing_pinning": closing_balances.get("pinning_methods", []),
                            "wallet_universe_complete": False},
               "balance_reconciliation": {"status": balance_reconciliation["status"],
                                          "receipt_transfer_logs": len(scoped_receipt_logs),
                                          "mismatches": len(balance_mismatches)},
               "contracts": {"declared": len(deployments["contracts"]), "observed": len(observations),
                             "observation_errors": len(observation_errors),
                             "pinning_methods": sorted({row["pinning_method"] for row in observations}),
                             "ctf_runtime_continues": ctf_continues,
                             "combo_runtime_continues": combo_continues, "all_source_qualified": False},
               "sources": {"activity": "data-api.polymarket.com", "acquisition_rpc": acquisition_host,
                           "state_rpc": state_host, "independent_rpc_hosts": acquisition_host != state_host},
               "blockers": sorted(blockers), "p0_exit_allowed": False, "execution_allowed": False,
               "safety": SAFETY}
    _save(partial / "summary.json", summary)
    project = Path(__file__).resolve().parents[3]
    try:
        git = ["git", "-c", "safe.directory=" + str(project).replace("\\", "/"), "-C", str(project)]
        commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
        clean = not subprocess.check_output(git + ["status", "--porcelain"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unavailable"
        clean = False
    code_paths = [Path(__file__).resolve(), Path(__file__).resolve().with_name("acquire.py"),
                  Path(__file__).resolve().with_name("reconcile.py")]
    source_hashes = {str(path.relative_to(project)).replace("\\", "/"): _sha256(path) for path in code_paths}
    evidence_files = sorted(path for path in partial.rglob("*.json") if path.name != "run_manifest.json")
    manifest = {"schema": 1, "completed_at": now_utc(), "code_commit": commit,
                "working_tree_clean": clean, "source_hashes": source_hashes, "input_hashes": input_hashes,
                "files": {str(path.relative_to(partial)).replace("\\", "/"): _sha256(path) for path in evidence_files},
                "summary_hash": digest(summary), "p0_exit": "BLOCKED", "safety": SAFETY}
    _save(partial / "run_manifest.json", manifest)
    os.rename(partial, output)
    return summary
