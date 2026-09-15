"""Build a conservative inventory/basis bundle from a closed lifetime capture.

The builder joins the original transaction closures with the independently
discovered receipt gap, derives authoritative wallet deltas from the continuous
crosscheck, and maps only transaction shapes with unambiguous semantics.  Any
unknown event, mixed action, or unsupported collateral pattern is retained in
``unresolved_actions`` instead of being guessed.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from eth_abi import decode
from eth_hash.auto import keccak

from . import SAFETY
from .common import EvidenceError, address, canonical, digest, hex_bytes, now_utc, uint
from .decoders import decode_event, semantics
from .lifetime_inventory import _load_source

BUNDLE_VERSION = "lifetime-basis-bundle/2"
QUOTE_CONTRACT = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
TRANSFER = "0x" + keccak(b"Transfer(address,address,uint256)").hex()
TRANSFER_SINGLE = "0x" + keccak(
    b"TransferSingle(address,address,address,uint256,uint256)"
).hex()
TRANSFER_BATCH = "0x" + keccak(
    b"TransferBatch(address,address,address,uint256[],uint256[])"
).hex()
BALANCE_TOPICS = {TRANSFER, TRANSFER_SINGLE, TRANSFER_BATCH}


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


def _asset(contract: str, token: str) -> str:
    return f"137:{address(contract)}:{token}"


def _raw_id(raw: dict) -> str:
    return "raw:" + digest([
        hex_bytes(raw["blockHash"], 32), hex_bytes(raw["transactionHash"], 32),
        uint(raw["logIndex"]), digest(raw),
    ])


def _address_topic(value: str) -> str:
    return address("0x" + hex_bytes(value, 32)[-40:])


def _fold_transaction_logs(logs: list[dict], wallet: str, contracts: dict) -> dict:
    wallet = address(wallet)
    deltas = defaultdict(int)
    raw_ids = []
    block = tx_index = None
    transaction = None
    for raw in logs:
        contract = address(raw["address"])
        standard = contracts[contract]["token_standard"]
        topics = [hex_bytes(item, 32) for item in raw["topics"]]
        data = bytes.fromhex(hex_bytes(raw["data"])[2:])
        current = (uint(raw["blockNumber"]), uint(raw["transactionIndex"]),
                   hex_bytes(raw["transactionHash"], 32))
        if transaction is None:
            block, tx_index, transaction = current
        elif current != (block, tx_index, transaction):
            raise EvidenceError("Transaction delta received mixed transaction logs")
        if standard == "erc20":
            if len(topics) != 3 or topics[0] != TRANSFER or len(data) != 32:
                raise EvidenceError("Malformed ERC-20 lifetime balance log")
            sender, receiver = _address_topic(topics[1]), _address_topic(topics[2])
            values = [("erc20", int.from_bytes(data, "big"))]
        else:
            if len(topics) != 4 or topics[0] not in {TRANSFER_SINGLE, TRANSFER_BATCH}:
                raise EvidenceError("Malformed ERC-1155 lifetime balance log")
            sender, receiver = _address_topic(topics[2]), _address_topic(topics[3])
            if topics[0] == TRANSFER_SINGLE:
                if len(data) != 64:
                    raise EvidenceError("Malformed lifetime TransferSingle data")
                values = [(str(int.from_bytes(data[:32], "big")),
                           int.from_bytes(data[32:], "big"))]
            else:
                ids, amounts = decode(["uint256[]", "uint256[]"], data, strict=True)
                if len(ids) != len(amounts):
                    raise EvidenceError("Malformed lifetime TransferBatch arrays")
                values = list(zip(map(str, ids), map(int, amounts)))
        sign = int(receiver == wallet) - int(sender == wallet)
        for token, quantity in values:
            deltas[_asset(contract, token)] += sign * quantity
        raw_ids.append(_raw_id(raw))
    return {"block": block, "tx_index": tx_index, "tx": transaction,
            "deltas": {key: value for key, value in deltas.items() if value},
            "raw_ids": sorted(set(raw_ids))}


def _family_map(scope: dict) -> dict[str, str]:
    result = {}
    for row in scope["balance_contracts"]:
        contract = address(row["address"])
        if row["token_standard"] == "erc20":
            result[contract] = "collateral"
        elif row["label"] == "Conditional Tokens":
            result[contract] = "ctf"
        elif row["label"] == "Combo PositionManager":
            result[contract] = "combo_v2"
    for row in scope["settlement_contracts"]:
        contract = address(row["address"])
        family = row["family"]
        if contract in result and result[contract] != family:
            raise EvidenceError("One lifetime contract has conflicting ABI families")
        result[contract] = family
    return result


def _contains_wallet(value, wallet: str) -> bool:
    if isinstance(value, str) and len(value) == 42 and value.startswith("0x"):
        try:
            return address(value) == wallet
        except EvidenceError:
            return False
    if isinstance(value, (list, tuple)):
        return any(_contains_wallet(item, wallet) for item in value)
    if isinstance(value, dict):
        return any(_contains_wallet(item, wallet) for item in value.values())
    return False


def _raw_mentions_wallet(raw: dict, wallet: str) -> bool:
    """Return true only for an ABI-aligned address word naming ``wallet``.

    Full receipts can contain hundreds of logs for users other than the wallet
    under study.  An undecodable log from a registered contract is relevant to
    this wallet only when its indexed topics or ABI data explicitly contain the
    wallet address.  Requiring the canonical 32-byte address representation
    avoids substring matches against hashes and quantities.
    """
    needle = "0" * 24 + address(wallet)[2:]
    for topic in (raw.get("topics") or [])[1:]:
        try:
            if hex_bytes(topic, 32)[2:] == needle:
                return True
        except EvidenceError:
            continue
    try:
        data = hex_bytes(raw.get("data", "0x"))[2:]
    except EvidenceError:
        return False
    return any(data[offset:offset + 64] == needle
               for offset in range(0, len(data) - 63, 64))


def _signature_rows(counter: Counter) -> list[dict]:
    return [
        {"family": family, "contract": contract, "topic0": topic0, "count": count}
        for (family, contract, topic0), count in sorted(
            counter.items(), key=lambda item: (-item[1], item[0])
        )
    ]


def _manifest_closures(directory: Path, manifest: list[dict], folder: str):
    shard_dir = directory / folder
    actual = {path.name for path in shard_dir.glob("*.json")}
    declared = {row["file"] for row in manifest}
    if actual != declared or len(actual) != len(manifest):
        raise EvidenceError("Receipt closure shard set differs from its manifest")
    for row in manifest:
        path = shard_dir / row["file"]
        if _sha256(path) != row["sha256"]:
            raise EvidenceError("Receipt closure shard hash mismatch")
        value = json.loads(path.read_text(encoding="utf-8"))
        if len(value.get("transactions", [])) != len(value.get("closures", [])):
            raise EvidenceError("Receipt closure shard is incomplete")
        for transaction, closure in zip(value["transactions"], value["closures"], strict=True):
            if hex_bytes(closure["transaction_hash"], 32) != hex_bytes(transaction, 32):
                raise EvidenceError("Receipt closure breaks its transaction index")
            yield closure


def _semantic_features(*, source_capture: Path, gap_closure: Path,
                       transactions: set[str], families: dict[str, str],
                       catalog: dict, wallet: str, progress=None) -> tuple[dict, dict]:
    source_manifest = json.loads((source_capture / "run_manifest.json").read_text(encoding="utf-8"))
    source_rows = source_manifest.get("result", {}).get("transaction_log_manifest")
    gap_summary = json.loads((gap_closure / "summary.json").read_text(encoding="utf-8"))
    gap_rows = gap_summary.get("receipt_manifest")
    if not isinstance(source_rows, list) or not isinstance(gap_rows, list):
        raise EvidenceError("Source or gap receipt manifest is missing")
    abis = {}
    for family in set(families.values()):
        try:
            abis[family] = catalog["families"][family]["abi"]
        except KeyError as exc:
            raise EvidenceError(f"ABI catalog omits {family}") from exc
    features = defaultdict(lambda: {"names": [], "action_kinds": [], "fills": [],
                                    "decode_failures": [], "raw_ids": []})
    names, seen, receipt_logs = Counter(), set(), 0
    unknown_relevant, unknown_ignored = Counter(), Counter()
    sources = [(source_capture, source_rows, "transaction_log_shards"),
               (gap_closure, gap_rows, "receipt_shards")]
    closures_done = 0
    for directory, rows, folder in sources:
        for closure in _manifest_closures(directory, rows, folder):
            transaction = hex_bytes(closure["transaction_hash"], 32)
            if transaction not in transactions or transaction in seen:
                raise EvidenceError("Receipt closure union is duplicated or outside the wallet index")
            seen.add(transaction)
            for raw in closure["logs"]:
                receipt_logs += 1
                contract = address(raw["address"])
                family = families.get(contract)
                if family is None:
                    continue
                topics = raw.get("topics") or []
                if topics and hex_bytes(topics[0], 32) in BALANCE_TOPICS:
                    continue
                raw_id = _raw_id(raw)
                try:
                    name, args = decode_event(raw, abis[family])
                    economic = semantics(family, name, args)
                    names[name] += 1
                    relevant = False
                    if economic["kind"] == "fill" and (
                        address(economic["maker"]) == wallet
                        or address(economic["taker"]) == wallet
                    ):
                        relevant = True
                        item = features[transaction]
                        item["fills"].append(economic)
                    elif economic["kind"] == "inventory_action" and _contains_wallet(args, wallet):
                        relevant = True
                        item = features[transaction]
                        item["action_kinds"].append(economic["action"])
                    elif _contains_wallet(args, wallet):
                        relevant = True
                        item = features[transaction]
                    if relevant:
                        item["names"].append(name)
                        item["raw_ids"].append(raw_id)
                except Exception as exc:
                    topics = raw.get("topics") or []
                    topic0 = hex_bytes(topics[0], 32) if topics else "0x"
                    signature = (family, contract, topic0)
                    if _raw_mentions_wallet(raw, wallet):
                        unknown_relevant[signature] += 1
                        item = features[transaction]
                        failures = item.setdefault("_failures", {})
                        key = "|".join(signature) + "|" + str(exc)[:300]
                        failure = failures.setdefault(key, {
                            "family": family, "contract": contract, "topic0": topic0,
                            "error": str(exc)[:300], "count": 0, "raw_ids_sample": [],
                        })
                        failure["count"] += 1
                        if len(failure["raw_ids_sample"]) < 3:
                            failure["raw_ids_sample"].append(raw_id)
                        item["raw_ids"].append(raw_id)
                    else:
                        unknown_ignored[signature] += 1
            closures_done += 1
            if progress and closures_done % 25_000 == 0:
                progress({"stage": "semantic_receipts", "transactions": closures_done,
                          "transactions_total": len(transactions)})
    if seen != transactions:
        raise EvidenceError("Receipt closure union does not cover every wallet transaction")
    for item in features.values():
        failures = item.pop("_failures", {})
        item["decode_failures"] = sorted(
            failures.values(), key=lambda row: (row["family"], row["contract"], row["topic0"])
        )
    return dict(features), {"transactions": len(seen), "receipt_logs": receipt_logs,
                            "decoded_event_names": dict(sorted(names.items())),
                            "unknown_events_wallet_referenced": sum(unknown_relevant.values()),
                            "unknown_events_ignored_not_wallet_referenced": sum(unknown_ignored.values()),
                            "unknown_signatures_wallet_referenced": _signature_rows(unknown_relevant),
                            "unknown_signatures_ignored": _signature_rows(unknown_ignored)}


def _legs(deltas: dict[str, int]) -> tuple[list[dict], list[dict]]:
    inputs = [{"asset": asset, "quantity": -quantity}
              for asset, quantity in sorted(deltas.items()) if quantity < 0]
    outputs = [{"asset": asset, "quantity": quantity}
               for asset, quantity in sorted(deltas.items()) if quantity > 0]
    return inputs, outputs


def classify_transaction(delta: dict, features: dict, wallet: str,
                         quote_asset: str,
                         collateral_assets: set[str] | None = None,
                         ) -> tuple[dict | None, dict | None]:
    """Map an exact wallet delta to one conservative lot action or quarantine it."""
    values = dict(delta["deltas"])
    cash = values.pop(quote_asset, 0)
    inputs, outputs = _legs(values)
    raw_ids = sorted(set(delta["raw_ids"] + features.get("raw_ids", [])))
    base = {"order": [delta["block"], delta["tx_index"], 0], "tx": delta["tx"],
            "wallet": wallet, "inputs": inputs, "outputs": outputs,
            "cash_delta": cash, "raw_ids": raw_ids}
    if not inputs and not outputs and not cash:
        return None, None
    reasons = []
    action_kinds = sorted(set(features.get("action_kinds", [])))
    fills = features.get("fills", [])
    if features.get("decode_failures"):
        reasons.append("RELEVANT_EVENT_DECODE_FAILURE")
    if fills and action_kinds:
        reasons.append("MIXED_FILL_AND_INVENTORY_ACTION")
    kind = None
    if not reasons and fills:
        if not inputs and len(outputs) == 1 and cash < 0:
            kind = "buy"
        elif len(inputs) == 1 and not outputs and cash >= 0:
            kind = "sell"
        elif len(inputs) == 1 and len(outputs) == 1 and cash == 0:
            kind = "convert"
        else:
            reasons.append("MIXED_OR_MULTI_ASSET_FILL")
    elif not reasons and action_kinds:
        if len(action_kinds) != 1:
            reasons.append("MIXED_INVENTORY_ACTION_FAMILIES")
        else:
            candidate = action_kinds[0]
            valid = {
                "split": bool(outputs) and cash <= 0 and (bool(inputs) or cash < 0),
                "merge": bool(inputs) and cash >= 0,
                "redeem": bool(inputs) and cash >= 0,
                "convert": bool(inputs) and bool(outputs),
                "wrap": bool(inputs) and bool(outputs) and cash == 0,
                "unwrap": bool(inputs) and bool(outputs) and cash == 0,
            }.get(candidate, False)
            if valid:
                kind = candidate
            elif candidate in {"wrap", "unwrap"} and len(inputs) + len(outputs) == 1 and cash:
                # One side is the designated quote cash, so represent the exact
                # 1:1 wrapper exchange as a buy/sell in the lot engine.
                kind = "buy" if outputs and cash < 0 else "sell" if inputs and cash > 0 else None
            if kind is None:
                reasons.append("INVENTORY_ACTION_DELTA_SHAPE_UNSUPPORTED")
    elif not reasons:
        collateral_assets = collateral_assets or set()
        if not inputs and not outputs and cash:
            kind = "cash"
        elif (len(inputs) == 1 and not outputs and cash > 0
              and inputs[0]["asset"] in collateral_assets):
            kind = "sell"
        elif (not inputs and len(outputs) == 1 and cash < 0
              and outputs[0]["asset"] in collateral_assets):
            kind = "buy"
        elif (len(inputs) == 1 and len(outputs) == 1 and cash == 0
              and inputs[0]["asset"] in collateral_assets
              and outputs[0]["asset"] in collateral_assets):
            kind = "convert"
        elif outputs and not inputs and cash == 0:
            kind = "receive"
        elif inputs and not outputs and cash == 0:
            kind = "transfer"
        else:
            reasons.append("UNEXPLAINED_BALANCE_EXCHANGE")
    if reasons:
        unresolved = {**base, "codes": sorted(set(reasons)),
                      "semantic_names": sorted(set(features.get("names", []))),
                      "decode_failures": features.get("decode_failures", [])}
        return None, unresolved
    action = {"id": digest([BUNDLE_VERSION, kind, base]), **base, "kind": kind,
              "normalizer": BUNDLE_VERSION}
    return action, None


def build_lifetime_basis_bundle(
    *, crosscheck: Path, source_capture: Path, gap_closure: Path,
    inventory_capture: Path, scope_path: Path, catalog_path: Path,
    wallet: str, output: Path, progress=None,
) -> dict:
    """Create a sealed basis-engine input bundle without guessing mappings."""
    crosscheck, source_capture, gap_closure, inventory_capture, scope_path, catalog_path, output = (
        Path(crosscheck).resolve(), Path(source_capture).resolve(), Path(gap_closure).resolve(),
        Path(inventory_capture).resolve(), Path(scope_path).resolve(), Path(catalog_path).resolve(),
        Path(output).resolve(),
    )
    wallet = address(wallet)
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists():
        raise EvidenceError("Lifetime basis bundle output or partial directory already exists")
    cross_summary, registry, logs = _load_source(crosscheck, scope_path, wallet)
    scope, contracts = registry["scope"], registry["contracts"]
    source_transactions = {
        hex_bytes(item, 32) for item in json.loads(
            (source_capture / "transactions.json").read_text(encoding="utf-8")
        )
    }
    gap_summary_path = gap_closure / "summary.json"
    gap_manifest_path = gap_closure / "run_manifest.json"
    gap_summary = json.loads(gap_summary_path.read_text(encoding="utf-8"))
    gap_manifest = json.loads(gap_manifest_path.read_text(encoding="utf-8"))
    if (_sha256(gap_summary_path) != gap_manifest.get("summary_sha256")
            or gap_summary.get("status") != "RECEIPTS_CLOSED"
            or gap_summary.get("coverage", {}).get("bundle_created") is not False):
        raise EvidenceError("Receipt gap is not sealed and complete")
    gap_transactions = {
        hex_bytes(item, 32) for item in json.loads(
            (gap_closure / "transactions.json").read_text(encoding="utf-8")
        )
    }
    if source_transactions & gap_transactions:
        raise EvidenceError("Source and gap transaction indexes overlap")
    transactions = source_transactions | gap_transactions
    if len(transactions) != uint(gap_summary["progress"]["union_transactions"]):
        raise EvidenceError("Closed receipt union count mismatch")

    grouped = defaultdict(list)
    for raw in logs:
        grouped[hex_bytes(raw["transactionHash"], 32)].append(raw)
    if set(grouped) != transactions:
        raise EvidenceError("Continuous balance logs and receipt transaction union differ")
    deltas = {transaction: _fold_transaction_logs(rows, wallet, contracts)
              for transaction, rows in grouped.items()}
    del logs, grouped
    if progress:
        progress({"stage": "wallet_deltas", "transactions": len(deltas),
                  "transactions_total": len(transactions)})

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    families = _family_map(scope)
    features, receipt_audit = _semantic_features(
        source_capture=source_capture, gap_closure=gap_closure,
        transactions=transactions, families=families, catalog=catalog,
        wallet=wallet, progress=progress,
    )
    quote_asset = _asset(QUOTE_CONTRACT, "erc20")
    collateral_assets = {
        _asset(contract, "erc20") for contract, family in families.items()
        if family == "collateral"
    }
    actions, unresolved, noops = [], [], 0
    action_counts, unresolved_counts = Counter(), Counter()
    for transaction in sorted(transactions, key=lambda tx: (
        deltas[tx]["block"], deltas[tx]["tx_index"], tx
    )):
        action, failure = classify_transaction(
            deltas[transaction], features.get(transaction, {}), wallet, quote_asset,
            collateral_assets,
        )
        if action is not None:
            actions.append(action)
            action_counts[action["kind"]] += 1
        elif failure is not None:
            unresolved.append(failure)
            unresolved_counts.update(failure["codes"])
        else:
            noops += 1
    inventory_summary_path = inventory_capture / "summary.json"
    inventory_state_path = inventory_capture / "state_balances.json"
    inventory_manifest = json.loads(
        (inventory_capture / "run_manifest.json").read_text(encoding="utf-8")
    )
    inventory_summary = json.loads(inventory_summary_path.read_text(encoding="utf-8"))
    state = json.loads(inventory_state_path.read_text(encoding="utf-8"))
    files = inventory_manifest.get("files", {})
    if (inventory_summary.get("status") != "INVENTORY_RECONCILED"
            or inventory_summary.get("reconciliation", {}).get("status") != "MATCH"
            or files.get("summary.json") != _sha256(inventory_summary_path)
            or files.get("state_balances.json") != _sha256(inventory_state_path)
            or state.get("errors")):
        raise EvidenceError("Closing inventory evidence is not reconciled and sealed")
    universe = sorted(set(state["balances"]) | {quote_asset}
                      | {asset for row in deltas.values() for asset in row["deltas"]})
    closing_balances = {asset: uint(state["balances"].get(asset, 0)) for asset in universe}
    bundle = {
        "schema": 1, "chain": 137, "wallet": wallet,
        "quote_asset": quote_asset, "quote_decimals": 6, "token_decimals": 6,
        "range": cross_summary["range"],
        "coverage": {"starts_at_wallet_origin": True, "continuous_blocks": True,
                     "raw_actions_complete": True,
                     "reviewed_mappings_complete": not unresolved,
                     "evidence": [
                         "crosscheck:" + _sha256(crosscheck / "summary.json"),
                         "receipt-gap:" + _sha256(gap_summary_path),
                     ]},
        "asset_universe": {"assets": universe, "complete": True,
                           "evidence": ["closing-state:" + _sha256(inventory_state_path)]},
        "opening": {"block_number": uint(cross_summary["range"]["start_block"]) - 1,
                    "block_hash": cross_summary["origin"]["parent_block_hash"],
                    "evidence": "wallet-origin:" + cross_summary["origin"]["receipt_hash"],
                    "basis_complete": True, "cash": {wallet: 0}, "lots": []},
        "actions": actions, "unresolved_actions": unresolved,
        "opening_marks": {}, "closing_marks": {},
        "marks_evidence": {"opening": [], "closing": []},
        "closing": {"block_number": uint(cross_summary["range"]["end_block"]),
                    "block_hash": state["block_hash"],
                    "evidence": "exact-state:" + _sha256(inventory_state_path),
                    "complete": True, "balances": closing_balances},
    }
    report = {
        "schema": 1, "version": BUNDLE_VERSION,
        "status": "BUNDLE_COMPLETE" if not unresolved else "BUNDLE_REVIEW_REQUIRED",
        "wallet": wallet, "range": cross_summary["range"],
        "transactions": {"total": len(transactions), "mapped": len(actions),
                         "unresolved": len(unresolved), "zero_net": noops},
        "actions_by_kind": dict(sorted(action_counts.items())),
        "unresolved_by_code": dict(sorted(unresolved_counts.items())),
        "asset_universe": len(universe), "receipt_audit": receipt_audit,
        "bundle_hash": digest(bundle),
        "basis_ready": not unresolved, "basis_calculated": False,
        "next_gate": "RUN_INVENTORY_BASIS" if not unresolved else "REVIEW_UNRESOLVED_ACTIONS",
        "ledger_approval": False, "execution_allowed": False, "safety": SAFETY,
    }
    partial.mkdir(parents=True)
    configuration = {
        "schema": 1, "version": BUNDLE_VERSION, "wallet": wallet,
        "crosscheck": str(crosscheck), "source_capture": str(source_capture),
        "gap_closure": str(gap_closure), "inventory_capture": str(inventory_capture),
        "scope": str(scope_path), "catalog": str(catalog_path), "safety": SAFETY,
    }
    _write_new(partial / "configuration.json", configuration)
    _write_new(partial / "bundle.json", bundle)
    _write_new(partial / "summary.json", report)
    project = Path(__file__).resolve().parents[3]
    try:
        git = ["git", "-c", "safe.directory=" + str(project).replace("\\", "/"),
               "-C", str(project)]
        commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
        clean = not subprocess.check_output(git + ["status", "--porcelain"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit, clean = "unavailable", False
    output_files = {path.name: _sha256(path) for path in sorted(partial.glob("*.json"))}
    _write_new(partial / "run_manifest.json", {
        "schema": 1, "version": BUNDLE_VERSION, "completed_at": now_utc(),
        "code_commit": commit, "working_tree_clean": clean, "files": output_files,
        "bundle_hash": report["bundle_hash"], "status": report["status"],
        "safety": SAFETY,
    })
    os.replace(partial, output)
    return report
