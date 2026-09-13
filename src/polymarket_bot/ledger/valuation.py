"""Supplemental mark-to-market valuation for a sealed wallet capture.

The module never edits the raw capture. It expands the declared token scope,
reads exact-block balances, obtains public CLOB historical reference prices,
and emits a separate evidence pack. Historical marks are not executable book
quotes and the result never authorizes trading.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import duckdb
from eth_abi import decode, encode
from eth_hash.auto import keccak

from polymarket_bot.polyledger import (
    DATA_API,
    build_url,
    fetch_all_positions,
    fetch_offset_pages,
    stable_json,
)

from . import SAFETY, VERSION
from .acquire import ExactPublicClient, ReadOnlyRPC, atomic_decimal
from .common import EvidenceError, address, canonical, digest, hex_bytes, now_utc, uint
from .wallet_capture import TRANSFER_TOPICS

CLOB = "https://clob.polymarket.com"
SCALE = Decimal(1_000_000)
BALANCE_BATCH_SIZE = 100
PRICE_LOOKBACK_SECONDS = 3_600
MAX_MARK_AGE_SECONDS = 900


def _sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = canonical(value) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise EvidenceError(f"Supplemental evidence conflicts with {path.name}")
        return
    temporary = path.with_name(path.name + f".{os.getpid()}.partial")
    with temporary.open("x", encoding="utf-8") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise EvidenceError("Non-finite valuation")
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _load_capture(capture: Path) -> tuple[dict, dict, dict]:
    capture = capture.resolve()
    manifest_path = capture / "run_manifest.json"
    if not manifest_path.exists():
        raise EvidenceError("Sealed capture manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for relative, expected in manifest.get("files", {}).items():
        path = capture / relative
        if not path.is_file() or _sha256(path) != expected:
            raise EvidenceError(f"Sealed capture hash mismatch: {relative}")
    configuration = json.loads((capture / "configuration.json").read_text(encoding="utf-8"))
    summary = json.loads((capture / "summary.json").read_text(encoding="utf-8"))
    if summary.get("status") != "CAPTURED_BLOCKED":
        raise EvidenceError("Unexpected raw capture status")
    if digest(summary) != manifest.get("summary_hash"):
        raise EvidenceError("Raw capture summary hash mismatch")
    return configuration, summary, manifest


def _historical_assets(path: Path) -> list[int]:
    path = path.resolve()
    if not path.is_file():
        raise EvidenceError("Historical activity parquet is missing")
    with duckdb.connect() as db:
        rows = db.execute(
            """SELECT DISTINCT asset FROM read_parquet(?)
               WHERE asset IS NOT NULL AND regexp_full_match(asset, '[0-9]+')
               ORDER BY length(asset), asset""",
            [str(path)],
        ).fetchall()
    return [uint(row[0]) for row in rows]


def _historical_token_metadata(path: Path) -> tuple[dict[str, dict], list[str]]:
    path = path.resolve()
    if not path.is_file():
        raise EvidenceError("Historical activity parquet is missing")
    with duckdb.connect() as db:
        rows = db.execute(
            """SELECT DISTINCT asset, condition_id, outcome_index, outcome
               FROM read_parquet(?)
               WHERE asset IS NOT NULL
                 AND regexp_full_match(asset, '[0-9]+')
                 AND condition_id IS NOT NULL
                 AND regexp_full_match(condition_id, '0x[0-9a-fA-F]{64}')
                 AND outcome_index IS NOT NULL""",
            [str(path)],
        ).fetchall()
    result: dict[str, dict] = {}
    conflicts: set[str] = set()
    for token, condition, outcome_index, outcome in rows:
        token = str(token)
        if not token.isdigit() or token in conflicts:
            continue
        record = {"condition_id": hex_bytes(str(condition), 32),
                  "outcome_index": uint(outcome_index), "outcome": str(outcome or "")}
        previous = result.get(token)
        if previous is not None and (
                previous["condition_id"], previous["outcome_index"]) != (
                    record["condition_id"], record["outcome_index"]):
            result.pop(token)
            conflicts.add(token)
            continue
        if previous is None or (not previous.get("outcome") and record.get("outcome")):
            result[token] = record
    return result, sorted(conflicts, key=int)


def _legacy_positions(path: Path, wallet: str) -> tuple[list[dict], dict]:
    uri = "file:" + path.resolve().as_posix() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        row = db.execute(
            """SELECT captured_at,row_count,payload_sha256,payload_json
               FROM snapshots WHERE source='positions'
               ORDER BY captured_at DESC LIMIT 1"""
        ).fetchone()
    if row is None:
        raise EvidenceError("Legacy complete position snapshot is missing")
    positions = json.loads(row[3])
    if not isinstance(positions, list) or len(positions) != row[1]:
        raise EvidenceError("Legacy position snapshot row count mismatch")
    if hashlib.sha256(stable_json(positions).encode("utf-8")).hexdigest() != row[2]:
        raise EvidenceError("Legacy position snapshot payload hash mismatch")
    for item in positions:
        if address(item["proxyWallet"]) != wallet:
            raise EvidenceError("Legacy position snapshot belongs to another wallet")
    return positions, {"captured_at": row[0], "rows": row[1], "payload_sha256": row[2]}


def _token_ids_from_balance_keys(rows: dict) -> set[int]:
    result = set()
    for asset in rows.get("balances", {}):
        token = asset.rsplit(":", 1)[-1]
        if token != "erc20":
            result.add(uint(token))
    return result


def _current_positions(client, wallet: str) -> tuple[list[dict], list[dict]]:
    rows, pages, complete = fetch_all_positions(client, wallet)
    if not complete:
        raise EvidenceError("Current position pagination did not prove completeness")
    return rows, [row.as_dict() for row in pages]


def _redeemable_positions(client, wallet: str) -> tuple[list[dict], list[dict]]:
    rows, pages, complete = fetch_offset_pages(
        client, "/positions",
        {"user": wallet, "sizeThreshold": 0, "redeemable": True,
         "sortBy": "TOKENS", "sortDirection": "DESC"},
        wallet=wallet, limit=500, max_offset=10_000,
    )
    if not complete:
        raise EvidenceError("Redeemable position pagination did not prove completeness")
    return rows, [row.as_dict() for row in pages]


def _position_key(row: dict) -> tuple[str, str, str]:
    return (str(row.get("conditionId") or "").lower(), str(row.get("asset") or ""),
            str(row.get("outcomeIndex") if row.get("outcomeIndex") is not None else ""))


def _merge_position_rows(*groups: list[dict]) -> list[dict]:
    unique: dict[tuple[str, str, str], dict] = {}
    for group in groups:
        for row in group:
            key = _position_key(row)
            previous = unique.get(key)
            if previous is None:
                unique[key] = row
            elif previous != row:
                # Prefer the explicitly redeemable view for a resolved position.
                if bool(row.get("redeemable")) and not bool(previous.get("redeemable")):
                    unique[key] = row
                elif bool(previous.get("redeemable")) == bool(row.get("redeemable")):
                    raise EvidenceError("Position API returned conflicting duplicate rows")
    return [unique[key] for key in sorted(unique)]


def _combo_positions(client, wallet: str) -> tuple[list[dict], list[dict]]:
    """Fetch the complete current Combo inventory with opaque cursor checks."""
    cursor = None
    seen_cursors = set()
    unique: dict[str, dict] = {}
    audits = []
    for page_number in range(100):
        url = build_url(DATA_API, "/v2/positions/combos",
                        {"user": wallet, "limit": 1000, "cursor": cursor})
        payload = client.get_json(url)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise EvidenceError("Invalid Combo position response")
        pagination = payload.get("pagination")
        if not isinstance(pagination, dict):
            raise EvidenceError("Combo position pagination metadata is missing")
        rows = payload["data"]
        for row in rows:
            if not isinstance(row, dict) or address(row.get("proxy_wallet")) != wallet:
                raise EvidenceError("Combo positions contain invalid ownership data")
            position = str(row.get("combo_position_id") or "")
            if not position.isdigit():
                raise EvidenceError("Combo position ID is invalid")
            previous = unique.get(position)
            if previous is not None and previous != row:
                raise EvidenceError("Combo position pagination conflicts")
            unique[position] = row
        next_cursor_raw = pagination.get("next_cursor")
        next_cursor = str(next_cursor_raw) if next_cursor_raw not in (None, "") else None
        has_more = pagination.get("has_more") is True
        audits.append({"page": page_number + 1, "rows": len(rows), "url": url,
                       "has_more": has_more, "next_cursor_present": next_cursor is not None})
        if not has_more:
            if next_cursor is not None:
                raise EvidenceError("Combo pagination ended with an unexpected cursor")
            return [unique[key] for key in sorted(unique, key=int)], audits
        if next_cursor is None or next_cursor == cursor or next_cursor in seen_cursors:
            raise EvidenceError("Combo pagination cursor is missing or repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise EvidenceError("Combo position pagination exceeded its bounded page limit")


def _market_token_metadata(path: Path) -> dict[str, dict]:
    uri = "file:" + path.resolve().as_posix() + "?mode=ro"
    result: dict[str, dict] = {}
    with sqlite3.connect(uri, uri=True) as db:
        rows = db.execute(
            "SELECT condition_id,outcomes_json,token_ids_json FROM market_metadata"
        )
        for condition, outcomes_raw, tokens_raw in rows:
            outcomes, tokens = json.loads(outcomes_raw), json.loads(tokens_raw)
            if not isinstance(outcomes, list) or not isinstance(tokens, list) or len(outcomes) != len(tokens):
                continue
            condition = hex_bytes(str(condition), 32)
            for index, (outcome, token) in enumerate(zip(outcomes, tokens, strict=True)):
                token = str(token)
                if not token.isdigit():
                    continue
                record = {"condition_id": condition, "outcome_index": index, "outcome": str(outcome)}
                previous = result.get(token)
                if previous is not None and previous != record:
                    raise EvidenceError("Market metadata maps one token to conflicting conditions")
                result[token] = record
    return result


def _merge_token_metadata(*sources: dict[str, dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for source in sources:
        for token, row in source.items():
            previous = result.get(token)
            if previous is None:
                result[token] = dict(row)
                continue
            if (previous["condition_id"], previous["outcome_index"]) != (
                    row["condition_id"], row["outcome_index"]):
                raise EvidenceError("Token metadata sources disagree on condition or outcome index")
            if not previous.get("outcome") and row.get("outcome"):
                result[token] = dict(row)
    return result


def _erc20_calls(wallet: str, deployments: dict) -> list[tuple[str, str]]:
    result = []
    selector = "0x" + keccak(b"balanceOf(address)")[:4].hex()
    for contract in deployments["contracts"]:
        if contract.get("token_standard") != "erc20":
            continue
        contract_address = address(contract["address"])
        result.append((f"137:{contract_address}:erc20", selector + encode(["address"], [wallet]).hex()))
    return result


def _call_groups(rpc: ReadOnlyRPC, calls: list[tuple[dict, list[str]]]) -> tuple[list[object], list[str], set[str]]:
    """Run state calls with EIP-1898 and a canonical numbered fallback."""
    values: list[object] = [None] * len(calls)
    errors: list[str] = []
    methods: set[str] = set()
    for offset in range(0, len(calls), 10):
        group = calls[offset:offset + 10]
        try:
            payloads = rpc.batch([("eth_call", [params["call"], params["pinned"]]) for params, _ in group])
            methods.add("eip1898-block-hash")
        except EvidenceError:
            payloads = []
            for params, assets in group:
                try:
                    payloads.append(rpc.call("eth_call", [params["call"], params["number_tag"]]))
                    methods.add("block-number-with-hash-recheck")
                except EvidenceError:
                    payloads.append(None)
                    errors.extend(assets)
        values[offset:offset + len(group)] = payloads
    return values, errors, methods


def _expanded_balances(rpc: ReadOnlyRPC, block: dict, wallet: str, deployments: dict,
                       token_ids: list[int]) -> dict:
    pinned = {"blockHash": hex_bytes(block["hash"], 32), "requireCanonical": True}
    number_tag = hex(uint(block["number"]))
    calls: list[tuple[dict, list[str]]] = []
    decoders: list[tuple[str, list[int]]] = []
    for asset, data in _erc20_calls(wallet, deployments):
        contract = asset.split(":")[1]
        calls.append(({"call": {"to": contract, "data": data}, "pinned": pinned,
                       "number_tag": number_tag}, [asset]))
        decoders.append((contract, []))
    selector = "0x" + keccak(b"balanceOfBatch(address[],uint256[])")[:4].hex()
    for contract in deployments["contracts"]:
        if contract.get("token_standard") != "erc1155":
            continue
        contract_address = address(contract["address"])
        for offset in range(0, len(token_ids), BALANCE_BATCH_SIZE):
            ids = token_ids[offset:offset + BALANCE_BATCH_SIZE]
            assets = [f"137:{contract_address}:{token}" for token in ids]
            data = selector + encode(["address[]", "uint256[]"], [[wallet] * len(ids), ids]).hex()
            calls.append(({"call": {"to": contract_address, "data": data}, "pinned": pinned,
                           "number_tag": number_tag}, assets))
            decoders.append((contract_address, ids))
    payloads, errors, methods = _call_groups(rpc, calls)
    balances: dict[str, int] = {}
    for payload, (_, assets), (contract, ids) in zip(payloads, calls, decoders):
        if payload is None:
            continue
        try:
            raw = bytes.fromhex(hex_bytes(payload)[2:])
            if not ids:
                values = [int.from_bytes(raw, "big")]
            else:
                values = list(decode(["uint256[]"], raw, strict=True)[0])
                if len(values) != len(ids):
                    raise EvidenceError("ERC-1155 balanceOfBatch length mismatch")
            for asset, value in zip(assets, values):
                balances[asset] = uint(value)
        except (EvidenceError, ValueError, OverflowError):
            errors.extend(assets)
    if "block-number-with-hash-recheck" in methods and rpc.block(uint(block["number"]))["hash"] != block["hash"]:
        raise EvidenceError("Reorg during expanded numbered balance reads")
    return {"chain": 137, "wallet": wallet, "block_number": uint(block["number"]),
            "block_hash": block["hash"], "balances": balances, "errors": sorted(set(errors)),
            "pinning_methods": sorted(methods), "complete_for_declared_scope": not errors}


def _uint_state_calls(rpc: ReadOnlyRPC, block: dict, contract: str,
                      requests: list[tuple[str, str]]) -> tuple[dict[str, int], list[str], list[str]]:
    pinned = {"blockHash": hex_bytes(block["hash"], 32), "requireCanonical": True}
    number_tag = hex(uint(block["number"]))
    calls = [({"call": {"to": contract, "data": data}, "pinned": pinned,
               "number_tag": number_tag}, [key]) for key, data in requests]
    payloads, errors, methods = _call_groups(rpc, calls)
    values = {}
    for payload, (key, _) in zip(payloads, requests):
        if payload is None:
            continue
        try:
            values[key] = int(hex_bytes(payload, 32), 16)
        except (EvidenceError, ValueError):
            errors.append(key)
    if "block-number-with-hash-recheck" in methods and rpc.block(uint(block["number"]))["hash"] != block["hash"]:
        raise EvidenceError("Reorg during numbered settlement reads")
    return values, sorted(set(errors)), sorted(methods)


def _ctf_settlement_marks(rpc: ReadOnlyRPC, block: dict, ctf: str, tokens: list[int],
                          metadata: dict[str, dict], progress=None) -> dict:
    denominator_selector = "0x" + keccak(b"payoutDenominator(bytes32)")[:4].hex()
    numerator_selector = "0x" + keccak(b"payoutNumerators(bytes32,uint256)")[:4].hex()
    token_records = {str(token): metadata.get(str(token)) for token in tokens}
    conditions = sorted({row["condition_id"] for row in token_records.values() if row is not None})
    denominator_requests = [
        (condition, denominator_selector + encode(["bytes32"], [bytes.fromhex(condition[2:])]).hex())
        for condition in conditions
    ]
    if progress:
        progress({"status": "CTF_SETTLEMENT", "cut_block": uint(block["number"]),
                  "conditions": len(conditions), "phase": "denominators"})
    denominators, denominator_errors, methods = _uint_state_calls(
        rpc, block, ctf, denominator_requests
    )
    numerator_requests = []
    for token, row in token_records.items():
        if row is None or denominators.get(row["condition_id"], 0) == 0:
            continue
        key = token
        data = numerator_selector + encode(
            ["bytes32", "uint256"],
            [bytes.fromhex(row["condition_id"][2:]), uint(row["outcome_index"])],
        ).hex()
        numerator_requests.append((key, data))
    if progress:
        progress({"status": "CTF_SETTLEMENT", "cut_block": uint(block["number"]),
                  "positions": len(numerator_requests), "phase": "numerators"})
    numerators, numerator_errors, numerator_methods = _uint_state_calls(
        rpc, block, ctf, numerator_requests
    ) if numerator_requests else ({}, [], [])
    marks = {}
    for token, row in token_records.items():
        if row is None:
            marks[token] = {"status": "METADATA_MISSING", "price": None}
            continue
        condition = row["condition_id"]
        if condition in denominator_errors:
            marks[token] = {"status": "DENOMINATOR_READ_ERROR", "price": None, **row}
            continue
        denominator = denominators.get(condition, 0)
        if denominator == 0:
            marks[token] = {"status": "UNRESOLVED", "price": None, **row}
            continue
        if token in numerator_errors or token not in numerators:
            marks[token] = {"status": "NUMERATOR_READ_ERROR", "price": None,
                            "payout_denominator": denominator, **row}
            continue
        numerator = numerators[token]
        price = Decimal(numerator) / Decimal(denominator)
        marks[token] = {"status": "SETTLED", "price": _decimal_text(price),
                        "payout_numerator": numerator, "payout_denominator": denominator, **row}
    return {"block_number": uint(block["number"]), "block_hash": block["hash"], "marks": marks,
            "metadata_missing": sum(row is None for row in token_records.values()),
            "denominator_errors": denominator_errors, "numerator_errors": numerator_errors,
            "pinning_methods": sorted(set(methods) | set(numerator_methods))}


def _history_one(client, token: int, start: int, end: int) -> dict:
    url = build_url(CLOB, "/prices-history", {"market": str(token), "startTs": start,
                                                "endTs": end, "fidelity": 1})
    try:
        payload = client.get_json(url)
        history = payload.get("history") if isinstance(payload, dict) else None
        if not isinstance(history, list):
            raise EvidenceError("CLOB price history response is not a list")
        by_time: dict[int, str] = {}
        future = 0
        for item in history:
            if not isinstance(item, dict):
                raise EvidenceError("Malformed CLOB price point")
            timestamp = uint(item.get("t"))
            price = Decimal(str(item.get("p")))
            if not price.is_finite() or not Decimal(0) <= price <= Decimal(1):
                raise EvidenceError("CLOB historical price outside [0,1]")
            if timestamp < start or timestamp > end:
                future += int(timestamp > end)
                continue
            text = _decimal_text(price)
            if timestamp in by_time and by_time[timestamp] != text:
                raise EvidenceError("Conflicting CLOB prices at one timestamp")
            by_time[timestamp] = text
        return {"token_id": str(token), "query_url": url,
                "history": [{"t": timestamp, "p": by_time[timestamp]} for timestamp in sorted(by_time)],
                "points_outside_requested_end": future, "error": None}
    except Exception as exc:  # Individual market failure must not erase the other marks.
        return {"token_id": str(token), "query_url": url, "history": [],
                "points_outside_requested_end": 0,
                "error": type(exc).__name__ + ": " + str(exc)}


def _price_histories(client, tokens: list[int], start: int, end: int, workers: int,
                     progress=None) -> list[dict]:
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(_history_one, client, token, start, end): token for token in tokens}
        for count, future in enumerate(concurrent.futures.as_completed(futures), 1):
            results.append(future.result())
            if progress and (count == len(tokens) or count % 25 == 0):
                progress({"status": "PRICE_HISTORY", "captured": count, "total": len(tokens)})
    return sorted(results, key=lambda row: int(row["token_id"]))


def _mark_before(history: list[dict], cut: int, max_age: int) -> dict:
    eligible = [row for row in history if uint(row["t"]) <= cut]
    if not eligible:
        return {"status": "MISSING", "price": None, "timestamp": None, "age_seconds": None}
    row = max(eligible, key=lambda item: uint(item["t"]))
    age = cut - uint(row["t"])
    return {"status": "FRESH" if age <= max_age else "STALE", "price": str(row["p"]),
            "timestamp": uint(row["t"]), "age_seconds": age}


def _combined_mark(settlement: dict, history: list[dict], cut: int, max_age: int) -> dict:
    if settlement.get("status") == "SETTLED":
        return {**settlement, "source": "CTF_ONCHAIN_SETTLEMENT", "timestamp": cut,
                "age_seconds": 0}
    mark = _mark_before(history, cut, max_age)
    return {**mark, "source": "CLOB_HISTORY", "settlement_status": settlement.get("status")}


def _combo_resolution_marks(opening: dict, closing: dict, combo: str, positions: list[dict],
                            opening_timestamp: int) -> dict:
    records = {str(row["combo_position_id"]): row for row in positions}
    assets = sorted({asset.rsplit(":", 1)[-1]
                     for balances in (opening, closing)
                     for asset, raw in balances["balances"].items()
                     if raw > 0 and asset.split(":")[1] == combo}, key=int)
    marks, errors = {}, []
    for token in assets:
        asset = f"137:{combo}:{token}"
        open_raw = opening["balances"].get(asset, 0)
        close_raw = closing["balances"].get(asset, 0)
        row = records.get(token)
        reason = None
        resolved_timestamp = None
        if row is None:
            reason = "COMBO_POSITION_API_RECORD_MISSING"
        elif str(row.get("status")) != "RESOLVED_LOSS":
            reason = "COMBO_POSITION_NOT_RESOLVED_LOSS"
        elif not row.get("resolved_at"):
            reason = "COMBO_RESOLUTION_TIME_MISSING"
        else:
            resolved_timestamp = int(datetime.fromisoformat(
                str(row["resolved_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc).timestamp())
            if resolved_timestamp > opening_timestamp:
                reason = "COMBO_RESOLVED_AFTER_OPENING_CUT"
            elif atomic_decimal(str(row.get("current_size") or "0")) != close_raw or open_raw != close_raw:
                reason = "COMBO_API_AND_EXACT_BLOCK_BALANCES_DIFFER"
        if reason:
            errors.append({"asset": asset, "reason": reason})
            marks[token] = {"opening": {"status": "MISSING", "price": None},
                            "closing": {"status": "MISSING", "price": None}, "evidence": row}
        else:
            resolved = {"status": "SETTLED_LOSS", "price": "0",
                        "source": "DATA_API_COMBO_RESOLUTION_WITH_BALANCE_MATCH",
                        "resolved_timestamp": resolved_timestamp}
            marks[token] = {"opening": resolved, "closing": resolved, "evidence": row}
    return {"marks": marks, "errors": errors, "positions_checked": len(assets),
            "resolved_loss_at_both_cuts": len(assets) - len(errors)}


def _cash_delta_atomic(row: dict) -> int | None:
    event_type = str(row.get("type") or "").upper()
    side = str(row.get("side") or "").upper()
    value = atomic_decimal(str(row.get("usdcSize") or "0"))
    if event_type == "TRADE" and side in {"BUY", "SELL"}:
        return -value if side == "BUY" else value
    if event_type == "SPLIT":
        return -value
    if event_type in {"MERGE", "REDEEM", "REWARD", "MAKER_REBATE", "TAKER_REBATE",
                      "REFERRAL_REWARD", "YIELD", "DEPOSIT"}:
        return value
    if event_type == "WITHDRAWAL":
        return -value
    if event_type == "CONVERSION":
        return 0
    return None


def _cash_transfer_audit(capture: Path, wallet: str, deployments: dict, activity: list[dict]) -> dict:
    cash_contracts = {address(row["address"]): row["label"] for row in deployments["contracts"]
                      if row.get("token_standard") == "erc20"}
    actual: dict[str, int] = defaultdict(int)
    actual_by_asset: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    cash_counterparties: dict[str, list[dict]] = defaultdict(list)
    token_transfer_transactions: set[str] = set()
    token_contracts = {address(row["address"]) for row in deployments["contracts"]
                       if row.get("token_standard") == "erc1155"}
    declared_contracts = {address(row["address"]) for row in deployments["contracts"]}
    for path in sorted((capture / "receipts").glob("*.json")):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        transaction = hex_bytes(receipt["transactionHash"], 32)
        for raw in receipt.get("logs", []):
            contract = address(raw["address"])
            topics = raw.get("topics") or []
            if (contract in token_contracts and len(topics) == 4
                    and topics[0].lower() in {TRANSFER_TOPICS["single"], TRANSFER_TOPICS["batch"]}
                    and any(value[-40:].lower() == wallet[2:] for value in topics[2:4])):
                token_transfer_transactions.add(transaction)
            if contract not in cash_contracts or len(topics) != 3 or topics[0].lower() != TRANSFER_TOPICS["erc20"]:
                continue
            sender = "0x" + topics[1][-40:].lower()
            receiver = "0x" + topics[2][-40:].lower()
            amount = int(hex_bytes(raw["data"], 32), 16)
            delta = (amount if receiver == wallet else 0) - (amount if sender == wallet else 0)
            if delta:
                actual[transaction] += delta
                actual_by_asset[transaction][f"137:{contract}:erc20"] += delta
                cash_counterparties[transaction].append({"asset": f"137:{contract}:erc20",
                                                         "direction": "IN" if delta > 0 else "OUT",
                                                         "counterparty": sender if delta > 0 else receiver,
                                                         "amount_atomic": amount})
    expected: dict[str, int] = defaultdict(int)
    unknown_types: dict[str, set[str]] = defaultdict(set)
    external: dict[str, int] = defaultdict(int)
    for row in activity:
        transaction = str(row.get("transactionHash") or "").lower()
        if not transaction:
            continue
        delta = _cash_delta_atomic(row)
        event_type = str(row.get("type") or "").upper()
        if delta is None:
            unknown_types[transaction].add(event_type or "UNKNOWN")
            continue
        expected[transaction] += delta
        if event_type in {"DEPOSIT", "WITHDRAWAL"}:
            external[transaction] += delta
    transactions = sorted(set(actual) | set(expected) | set(unknown_types))
    rows = []
    for transaction in transactions:
        inferred_external = 0
        counterparties = cash_counterparties.get(transaction, [])
        direct_external_outflow = (
            actual.get(transaction, 0) < 0 and transaction not in expected
            and not unknown_types.get(transaction) and transaction not in token_transfer_transactions
            and counterparties and all(row["direction"] == "OUT"
                                       and row["counterparty"] not in declared_contracts
                                       for row in counterparties)
        )
        if direct_external_outflow:
            inferred_external = actual[transaction]
            external[transaction] += inferred_external
        difference = actual.get(transaction, 0) - expected.get(transaction, 0) - inferred_external
        rows.append({"transaction_hash": transaction, "actual_cash_delta_atomic": actual.get(transaction, 0),
                     "actual_by_asset_atomic": dict(sorted(actual_by_asset.get(transaction, {}).items())),
                     "activity_expected_delta_atomic": expected.get(transaction, 0),
                     "inferred_external_delta_atomic": inferred_external,
                     "classification": ("ONCHAIN_DIRECT_EXTERNAL_OUTFLOW" if direct_external_outflow
                                        else "ACTIVITY_RECONCILED" if not difference else "UNRESOLVED"),
                     "cash_counterparties": counterparties,
                     "difference_atomic": difference,
                     "unknown_activity_types": sorted(unknown_types.get(transaction, set()))})
    mismatches = [row for row in rows if row["difference_atomic"] or row["unknown_activity_types"]]
    return {"status": "MATCH" if not mismatches else "BLOCKED", "cash_transfer_transactions": len(actual),
            "activity_cash_transactions": len(expected), "mismatches": mismatches,
            "actual_net_cash_delta_atomic": sum(actual.values()),
            "activity_expected_net_cash_delta_atomic": sum(expected.values()),
            "external_net_funding_atomic": sum(external.values()), "transactions": rows}


def _position_metadata(legacy: list[dict], current: list[dict], activity: list[dict]) -> dict[str, dict]:
    fields = ("conditionId", "title", "slug", "eventSlug", "outcome", "outcomeIndex", "oppositeAsset",
              "oppositeOutcome", "endDate", "negativeRisk", "redeemable", "mergeable", "curPrice")
    result: dict[str, dict] = {}
    for source, rows in (("CAPTURE_ACTIVITY", activity), ("LEGACY_POSITION", legacy),
                         ("CURRENT_POSITION", current)):
        for row in rows:
            token = str(row.get("asset") or "")
            if not token.isdigit():
                continue
            item = result.setdefault(token, {"asset": token, "metadata_source": source})
            for field in fields:
                if row.get(field) is not None:
                    value = row[field]
                    item[field] = repr(value) if isinstance(value, float) else value
            item["metadata_source"] = source
    return result


def _value_cut(balances: dict, marks: dict[str, dict], combo_marks: dict[str, dict],
               ctf: str, combo: str, cash_contracts: set[str], cut: str) -> dict:
    cash = Decimal(0)
    ctf_value = Decimal(0)
    combo_value = Decimal(0)
    unpriced_payout_upper_bound = Decimal(0)
    unpriced = []
    positive_ctf = positive_combo = 0
    for asset, raw in balances["balances"].items():
        if raw <= 0:
            continue
        _, contract, token = asset.split(":")
        if token == "erc20" and contract in cash_contracts:
            cash += Decimal(raw) / SCALE
        elif contract == ctf:
            positive_ctf += 1
            mark = marks.get(token, {}).get(cut, {})
            if mark.get("price") is None:
                unpriced.append(asset)
                unpriced_payout_upper_bound += Decimal(raw) / SCALE
            else:
                ctf_value += Decimal(raw) / SCALE * Decimal(mark["price"])
        elif contract == combo:
            positive_combo += 1
            mark = combo_marks.get(token, {}).get(cut, {})
            if mark.get("price") is None:
                unpriced.append(asset)
                unpriced_payout_upper_bound += Decimal(raw) / SCALE
            else:
                combo_value += Decimal(raw) / SCALE * Decimal(mark["price"])
    subtotal = cash + ctf_value + combo_value
    return {"collateral_usd_nominal": _decimal_text(cash),
            "ctf_mark_value_usd": _decimal_text(ctf_value),
            "combo_mark_value_usd": _decimal_text(combo_value),
            "valued_subtotal_usd": _decimal_text(subtotal),
            "unpriced_payout_upper_bound_usd": _decimal_text(unpriced_payout_upper_bound),
            "total_equity_lower_bound_usd": _decimal_text(subtotal),
            "total_equity_upper_bound_usd": _decimal_text(subtotal + unpriced_payout_upper_bound),
            "total_equity_usd": _decimal_text(subtotal) if not unpriced else None,
            "positive_ctf_positions": positive_ctf, "positive_combo_positions": positive_combo,
            "unpriced_positive_assets": sorted(unpriced), "complete_for_expanded_scope": not unpriced}


def value_wallet_capture(*, capture: Path, output: Path, wallet: str, deployments_path: Path,
                         history_assets_path: Path, legacy_db_path: Path, metadata_db_path: Path,
                         rpc: ReadOnlyRPC | None = None, data_client=None,
                         price_lookback_seconds: int = PRICE_LOOKBACK_SECONDS,
                         max_mark_age_seconds: int = MAX_MARK_AGE_SECONDS,
                         workers: int = 12, progress=None) -> dict:
    capture, output = Path(capture).resolve(), Path(output).resolve()
    wallet = address(wallet)
    if output.exists() or output.with_name(output.name + ".partial").exists():
        raise EvidenceError("Supplemental valuation output already exists")
    partial = output.with_name(output.name + ".partial")
    partial.mkdir(parents=True)
    configuration, raw_summary, raw_manifest = _load_capture(capture)
    if address(configuration["wallet"]) != wallet:
        raise EvidenceError("Raw capture belongs to another wallet")
    deployments = json.loads(Path(deployments_path).read_text(encoding="utf-8"))
    if uint(deployments["chain"]) != 137:
        raise EvidenceError("Valuation deployments must target Polygon")
    rpc = rpc or ReadOnlyRPC(url=configuration["state_rpc_source"])
    data_client = data_client or ExactPublicClient()

    current_positions, current_pages = _current_positions(data_client, wallet)
    redeemable_positions, redeemable_pages = _redeemable_positions(data_client, wallet)
    all_current_positions = _merge_position_rows(current_positions, redeemable_positions)
    combo_positions, combo_pages = _combo_positions(data_client, wallet)
    legacy_positions, legacy_audit = _legacy_positions(Path(legacy_db_path), wallet)
    activity = json.loads((capture / "activity.json").read_text(encoding="utf-8"))
    captured_open = json.loads((capture / "opening_balances.json").read_text(encoding="utf-8"))
    captured_close = json.loads((capture / "closing_balances.json").read_text(encoding="utf-8"))
    history_assets = set(_historical_assets(Path(history_assets_path)))
    historical_metadata, historical_metadata_conflicts = _historical_token_metadata(
        Path(history_assets_path)
    )
    capture_assets = (_token_ids_from_balance_keys(captured_open)
                      | _token_ids_from_balance_keys(captured_close)
                      | {uint(row["asset"]) for row in activity if str(row.get("asset") or "").isdigit()})
    legacy_assets = {uint(row["asset"]) for row in legacy_positions if str(row.get("asset") or "").isdigit()}
    current_assets = {uint(row["asset"]) for row in all_current_positions
                      if str(row.get("asset") or "").isdigit()}
    token_ids = sorted(history_assets | capture_assets | legacy_assets | current_assets)

    opening_number = raw_summary["balances"]["opening_block"]
    closing_number = raw_summary["balances"]["closing_block"]
    opening_header = {"number": hex(opening_number), "hash": captured_open["block_hash"]}
    closing_header = {"number": hex(closing_number), "hash": captured_close["block_hash"]}
    if rpc.block(opening_number)["hash"] != opening_header["hash"] or rpc.block(closing_number)["hash"] != closing_header["hash"]:
        raise EvidenceError("State RPC disagrees with sealed capture anchors")
    if progress:
        progress({"status": "EXPANDED_BALANCES", "tokens": len(token_ids), "cut": "opening"})
    opening = _expanded_balances(rpc, opening_header, wallet, deployments, token_ids)
    if progress:
        progress({"status": "EXPANDED_BALANCES", "tokens": len(token_ids), "cut": "closing"})
    closing = _expanded_balances(rpc, closing_header, wallet, deployments, token_ids)

    ctf_contract = next(address(row["address"]) for row in deployments["contracts"] if row["family"] == "ctf")
    combo_contract = next(address(row["address"]) for row in deployments["contracts"] if row["family"] == "combo_v2")
    cash_contracts = {address(row["address"]) for row in deployments["contracts"]
                      if row.get("token_standard") == "erc20"}
    price_tokens = sorted({uint(asset.rsplit(":", 1)[-1])
                           for balances in (opening, closing)
                           for asset, raw in balances["balances"].items()
                           if raw > 0 and asset.split(":")[1] == ctf_contract})
    database_metadata = _market_token_metadata(Path(metadata_db_path))
    token_metadata = _merge_token_metadata(database_metadata, historical_metadata)
    opening_settlement = _ctf_settlement_marks(
        rpc, opening_header, ctf_contract, price_tokens, token_metadata, progress
    )
    closing_settlement = _ctf_settlement_marks(
        rpc, closing_header, ctf_contract, price_tokens, token_metadata, progress
    )
    history_tokens = sorted(
        token for token in price_tokens
        if (opening_settlement["marks"].get(str(token), {}).get("status") != "SETTLED"
            or closing_settlement["marks"].get(str(token), {}).get("status") != "SETTLED")
    )
    query_start = configuration["start_timestamp"] - uint(price_lookback_seconds)
    query_end = configuration["end_timestamp"]
    histories = _price_histories(data_client, history_tokens, query_start, query_end, workers, progress)
    history_by_token = {row["token_id"]: row for row in histories}
    marks: dict[str, dict] = {}
    for token_value in price_tokens:
        token = str(token_value)
        item = history_by_token.get(token, {"history": [], "error": None})
        marks[token] = {
            "opening": _combined_mark(opening_settlement["marks"].get(token, {}), item["history"],
                                      configuration["start_timestamp"], uint(max_mark_age_seconds)),
            "closing": _combined_mark(closing_settlement["marks"].get(token, {}), item["history"],
                                      configuration["end_timestamp"], uint(max_mark_age_seconds)),
            "history_error": item["error"],
        }
    combo_resolution = _combo_resolution_marks(
        opening, closing, combo_contract, combo_positions, configuration["start_timestamp"]
    )
    combo_marks = combo_resolution["marks"]
    opening_value = _value_cut(opening, marks, combo_marks, ctf_contract, combo_contract,
                               cash_contracts, "opening")
    closing_value = _value_cut(closing, marks, combo_marks, ctf_contract, combo_contract,
                               cash_contracts, "closing")
    cash_audit = _cash_transfer_audit(capture, wallet, deployments, activity)
    metadata = _position_metadata(legacy_positions, all_current_positions, activity)
    position_rows = []
    for token in price_tokens:
        token_text = str(token)
        open_raw = opening["balances"].get(f"137:{ctf_contract}:{token}", 0)
        close_raw = closing["balances"].get(f"137:{ctf_contract}:{token}", 0)
        state = ("OPENED" if not open_raw and close_raw else "CLOSED" if open_raw and not close_raw
                 else "INCREASED" if close_raw > open_raw else "REDUCED" if close_raw < open_raw else "UNCHANGED")
        position_rows.append({**metadata.get(token_text, {"asset": token_text}),
                              "opening_balance_atomic": open_raw, "closing_balance_atomic": close_raw,
                              "balance_change_atomic": close_raw - open_raw, "window_state": state,
                              "opening_mark": marks[token_text]["opening"],
                              "closing_mark": marks[token_text]["closing"]})

    fresh_missing = sorted(token for token, item in marks.items()
                           if item["opening"]["status"] not in {"FRESH", "SETTLED"}
                           or item["closing"]["status"] not in {"FRESH", "SETTLED"})
    calculation_available = (not opening["errors"] and not closing["errors"]
                             and opening_value["complete_for_expanded_scope"]
                             and closing_value["complete_for_expanded_scope"])
    raw_change = adjusted = None
    if calculation_available:
        raw_change_decimal = (Decimal(closing_value["total_equity_usd"])
                              - Decimal(opening_value["total_equity_usd"]))
        raw_change = _decimal_text(raw_change_decimal)
        if cash_audit["status"] == "MATCH":
            adjusted = _decimal_text(raw_change_decimal
                                     - Decimal(cash_audit["external_net_funding_atomic"]) / SCALE)
    interval_available = (not opening["errors"] and not closing["errors"]
                          and cash_audit["status"] == "MATCH")
    adjusted_lower = adjusted_upper = None
    if interval_available:
        funding = Decimal(cash_audit["external_net_funding_atomic"]) / SCALE
        adjusted_lower = _decimal_text(
            Decimal(closing_value["total_equity_lower_bound_usd"])
            - Decimal(opening_value["total_equity_upper_bound_usd"]) - funding
        )
        adjusted_upper = _decimal_text(
            Decimal(closing_value["total_equity_upper_bound_usd"])
            - Decimal(opening_value["total_equity_lower_bound_usd"]) - funding
        )
    rewards_atomic = sum((_cash_delta_atomic(row) or 0) for row in activity
                         if str(row.get("type") or "").upper() in
                         {"REWARD", "MAKER_REBATE", "TAKER_REBATE", "REFERRAL_REWARD", "YIELD"})
    blockers = {"FULL_WALLET_ASSET_UNIVERSE_NOT_PROVEN", "HISTORICAL_MARKS_NOT_EXECUTABLE",
                "REALIZED_UNREALIZED_DECOMPOSITION_REQUIRES_OPENING_BASIS"}
    if opening["errors"] or closing["errors"]:
        blockers.add("EXPANDED_BALANCE_READS_INCOMPLETE")
    if not calculation_available:
        blockers.add("COMPLETE_MTM_CALCULATION_UNAVAILABLE")
    if fresh_missing:
        blockers.add("FRESH_HISTORICAL_MARKS_MISSING")
    if cash_audit["status"] != "MATCH":
        blockers.add("EXTERNAL_FUNDING_CLASSIFICATION_UNRESOLVED")
    if combo_resolution["errors"]:
        blockers.add("COMBO_ECONOMICS_NOT_FULLY_RESOLVED")
    current_mergeable = sum(bool(row.get("mergeable")) for row in current_positions)
    summary = {
        "schema": 1, "version": VERSION, "status": "PROVISIONAL_VALUATION_BLOCKED",
        "wallet": wallet,
        "raw_capture": {"path": str(capture), "manifest_sha256": _sha256(capture / "run_manifest.json"),
                        "summary_hash": raw_manifest["summary_hash"]},
        "window": raw_summary["window"],
        "universe": {"historical_activity_tokens": len(history_assets),
                     "historical_metadata_tokens": len(historical_metadata),
                     "historical_metadata_conflicts_excluded": len(historical_metadata_conflicts),
                     "legacy_position_tokens": len(legacy_assets), "current_position_tokens": len(current_assets),
                     "capture_tokens": len(capture_assets), "union_tokens": len(token_ids),
                     "wallet_universe_complete": False},
        "balances": {"queried_assets": len(opening["balances"]), "opening_errors": len(opening["errors"]),
                     "closing_errors": len(closing["errors"]),
                     "opening_positive": sum(value > 0 for value in opening["balances"].values()),
                     "closing_positive": sum(value > 0 for value in closing["balances"].values())},
        "marks": {"ctf_tokens": len(price_tokens), "ctf_settled_at_opening": sum(
                      row["status"] == "SETTLED" for row in opening_settlement["marks"].values()),
                  "ctf_settled_at_closing": sum(
                      row["status"] == "SETTLED" for row in closing_settlement["marks"].values()),
                  "clob_history_tokens_queried": len(history_tokens),
                  "fresh_or_settled_at_both_cuts": len(price_tokens) - len(fresh_missing),
                  "not_fresh_at_one_or_both_cuts": len(fresh_missing),
                  "method": "exact-block CTF payout when resolved; otherwise last official CLOB historical reference at or before each cut",
                  "max_age_seconds": uint(max_mark_age_seconds), "executable": False},
        "combo": {"api_positions": len(combo_positions),
                  "positive_onchain_positions": combo_resolution["positions_checked"],
                  "resolved_loss_at_both_cuts": combo_resolution["resolved_loss_at_both_cuts"],
                  "resolution_errors": len(combo_resolution["errors"])},
        "valuation": {"opening": opening_value, "closing": closing_value,
                      "raw_mtm_change_usd": raw_change,
                      "external_net_funding_usd": (_decimal_text(Decimal(cash_audit["external_net_funding_atomic"]) / SCALE)
                                                   if cash_audit["status"] == "MATCH" else None),
                      "funding_adjusted_mtm_change_usd": adjusted,
                      "funding_adjusted_mtm_lower_bound_usd": adjusted_lower,
                      "funding_adjusted_mtm_upper_bound_usd": adjusted_upper,
                      "interval_available_for_expanded_scope": interval_available,
                      "explicit_rewards_in_window_usd": _decimal_text(Decimal(rewards_atomic) / SCALE),
                      "calculation_available_for_expanded_scope": calculation_available,
                      "interpretation": "Portfolio mark-to-market change, not independently reconstructed realized PnL."},
        "cash_transfer_audit": {"status": cash_audit["status"],
                                "transactions": cash_audit["cash_transfer_transactions"],
                                "mismatches": len(cash_audit["mismatches"])},
        "current_tracking_snapshot": {"captured_at": now_utc(), "positions": len(current_positions),
                                      "redeemable_position_rows": len(redeemable_positions),
                                      "combined_unique_position_rows": len(all_current_positions),
                                      "redeemable": sum(bool(row.get("redeemable"))
                                                        for row in all_current_positions),
                                      "mergeable": current_mergeable, "combo_positions": len(combo_positions),
                                      "current_value_usd_api": _decimal_text(sum(Decimal(str(row.get("currentValue") or "0"))
                                                                                 for row in all_current_positions)),
                                      "unrealized_cash_pnl_usd_api": _decimal_text(sum(Decimal(str(row.get("cashPnl") or "0"))
                                                                                         for row in all_current_positions)),
                                      "source": "data-api.polymarket.com/positions",
                                      "used_as_historical_cut_price": False},
        "blockers": sorted(blockers), "p0_exit_allowed": False, "execution_allowed": False,
        "safety": SAFETY,
    }
    configuration_out = {"schema": 1, "wallet": wallet, "raw_capture": str(capture),
                         "raw_manifest_sha256": summary["raw_capture"]["manifest_sha256"],
                         "deployments_sha256": _sha256(Path(deployments_path)),
                         "historical_assets_sha256": _sha256(Path(history_assets_path)),
                         "metadata_db_sha256": _sha256(Path(metadata_db_path)),
                         "legacy_db_snapshot": legacy_audit, "state_rpc_source": rpc.url,
                         "price_source": CLOB + "/prices-history", "price_query_start": query_start,
                         "price_query_end": query_end, "price_lookback_seconds": uint(price_lookback_seconds),
                         "max_mark_age_seconds": uint(max_mark_age_seconds), "safety": SAFETY}
    _save(partial / "configuration.json", configuration_out)
    _save(partial / "universe.json", {"token_ids": [str(token) for token in token_ids],
                                      "source_counts": summary["universe"],
                                      "historical_metadata_conflicts_excluded": historical_metadata_conflicts})
    _save(partial / "current_positions.json", current_positions)
    _save(partial / "current_position_pages.json", current_pages)
    _save(partial / "redeemable_positions.json", redeemable_positions)
    _save(partial / "redeemable_position_pages.json", redeemable_pages)
    _save(partial / "combo_positions.json", combo_positions)
    _save(partial / "combo_position_pages.json", combo_pages)
    _save(partial / "expanded_opening_balances.json", opening)
    _save(partial / "expanded_closing_balances.json", closing)
    _save(partial / "price_histories.json", histories)
    _save(partial / "ctf_settlement_opening.json", opening_settlement)
    _save(partial / "ctf_settlement_closing.json", closing_settlement)
    _save(partial / "marks.json", marks)
    _save(partial / "combo_marks.json", combo_resolution)
    _save(partial / "position_tracking.json", position_rows)
    _save(partial / "cash_transfer_audit.json", cash_audit)
    _save(partial / "summary.json", summary)
    project = Path(__file__).resolve().parents[3]
    try:
        git = ["git", "-c", "safe.directory=" + str(project).replace("\\", "/"), "-C", str(project)]
        commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
        clean = not subprocess.check_output(git + ["status", "--porcelain"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit, clean = "unavailable", False
    evidence = sorted(path for path in partial.glob("*.json") if path.name != "run_manifest.json")
    manifest = {"schema": 1, "completed_at": now_utc(), "code_commit": commit,
                "working_tree_clean": clean,
                "source_hashes": {
                    str(Path(__file__).relative_to(project)).replace("\\", "/"): _sha256(Path(__file__)),
                    "src/polymarket_bot/ledger/acquire.py": _sha256(Path(__file__).with_name("acquire.py")),
                },
                "files": {path.name: _sha256(path) for path in evidence},
                "summary_hash": digest(summary), "p0_exit": "BLOCKED", "safety": SAFETY}
    _save(partial / "run_manifest.json", manifest)
    os.rename(partial, output)
    return summary
