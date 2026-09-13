from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from eth_abi import encode
from eth_hash.auto import keccak

from polymarket_bot.car_onchain import RPC_URL, _rpc
from polymarket_bot.polyledger import PublicDataClient

from .common import EvidenceError, address, canonical, digest, hex_bytes, now_utc, uint
from .registry import BEACON_SLOT, IMPLEMENTATION_SLOT

READ_METHODS = {"eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_getBlockByHash",
                "eth_getLogs", "eth_getTransactionReceipt", "eth_getCode", "eth_getStorageAt", "eth_call"}
# Only read selectors used by this module; eth_call cannot be repurposed as an
# order/withdrawal simulator through this public client.
READ_SELECTORS = {
    "0x70a08231",  # ERC-20 balanceOf(address)
    "0x00fdd58e",  # ERC-1155 balanceOf(address,uint256)
    "0x4e1273f4",  # ERC-1155 balanceOfBatch(address[],uint256[])
    "0x5c60da1b",  # EIP-1967 beacon implementation()
    "0xdd34de67",  # ConditionalTokens payoutDenominator(bytes32)
    "0x0504c814",  # ConditionalTokens payoutNumerators(bytes32,uint256)
}


class ExactPublicClient(PublicDataClient):
    """Existing HTTP retries/GET transport, preserving decimal wire literals."""
    def get_json(self, url):
        return json.loads(self.get_bytes(url).decode("utf-8"), parse_float=str,
                          parse_constant=lambda _: (_ for _ in ()).throw(EvidenceError("Non-finite JSON number")))


def atomic_decimal(value: str, decimals: int = 6) -> int:
    if not isinstance(value, str) or not 0 <= decimals <= 36:
        raise EvidenceError("Decimal wire string and supported scale required")
    # Scale through Decimal's tuple instead of the ambient precision context.
    number = Decimal(value)
    if not number.is_finite() or number < 0:
        raise EvidenceError("Invalid monetary value")
    sign, digits, exponent = number.as_tuple()
    coefficient = int("".join(map(str, digits)))
    shift = exponent + decimals
    if abs(shift) > 256:
        raise EvidenceError("Unreasonable monetary exponent")
    if shift < 0:
        divisor = 10 ** -shift
        if coefficient % divisor:
            raise EvidenceError("Monetary value is not an integral atomic amount")
        coefficient //= divisor
    else:
        coefficient *= 10**shift
    return uint(coefficient)


class ReadOnlyRPC:
    def __init__(self, *, url: str = RPC_URL, transport=None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise EvidenceError("HTTPS RPC without inline credentials/query required")
        self.url = url
        self.transport = transport or (lambda payload: _rpc(payload, attempts=2, rpc_url=url, timeout=20))
        self.sequence = 0

    def call(self, method: str, params: list):
        self._validate(method, params)
        self.sequence += 1
        response = self.transport({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})
        if (not isinstance(response, dict) or response.get("id") != self.sequence or response.get("error")
                or "result" not in response or response["result"] is None):
            raise EvidenceError(f"Invalid read-only RPC response: {method}")
        return response["result"]

    @staticmethod
    def _validate(method: str, params: list):
        if method not in READ_METHODS:
            raise EvidenceError("RPC method prohibited in read-only P0")
        if method == "eth_call":
            if (len(params) != 2 or set(params[0]) != {"to", "data"}
                    or params[0]["data"][:10] not in READ_SELECTORS):
                raise EvidenceError("Only allowlisted balance/implementation eth_call permitted")
    def batch(self, calls: list[tuple[str, list]]) -> list:
        if not 1 <= len(calls) <= 10:
            raise EvidenceError("RPC batch size must be 1..10")
        if len(calls) == 1:
            return [self.call(*calls[0])]
        payload = []
        for method, params in calls:
            self._validate(method, params)
            self.sequence += 1
            payload.append({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})
        response = self.transport(payload)
        if not isinstance(response, list) or len(response) != len(payload):
            raise EvidenceError("Incomplete RPC batch")
        if any(not isinstance(r, dict) or r.get("error") or r.get("result") is None for r in response):
            raise EvidenceError("Failed RPC batch entry")
        by_id = {r.get("id"): r["result"] for r in response}
        if set(by_id) != {p["id"] for p in payload}:
            raise EvidenceError("RPC batch identity mismatch")
        return [by_id[p["id"]] for p in payload]

    def block(self, number: int) -> dict:
        value = self.call("eth_getBlockByNumber", [hex(uint(number)), False])
        if uint(value["number"]) != number:
            raise EvidenceError("Wrong block returned")
        return value

    def observe_contract(self, version: dict, block: dict) -> dict:
        """EIP-1898 pins every state read to the same canonical block hash."""
        pinned = {"blockHash": hex_bytes(block["hash"], 32), "requireCanonical": True}
        contract = version["address"]
        result = {"chain": uint(self.call("eth_chainId", [])), "address": contract,
                  "block_number": uint(block["number"]), "block_hash": block["hash"],
                  "source": "rpc:" + urlsplit(self.url).hostname,
                  "code": self.call("eth_getCode", [contract, pinned]),
                  "implementation_slot": self.call("eth_getStorageAt", [contract, IMPLEMENTATION_SLOT, pinned]),
                  "beacon_slot": self.call("eth_getStorageAt", [contract, BEACON_SLOT, pinned])}
        if version["proxy_kind"] != "direct":
            if version["proxy_kind"] == "beacon":
                beacon = "0x" + hex_bytes(result["beacon_slot"], 32)[-40:]
                implementation = self.call("eth_call", [{"to": beacon, "data": "0x5c60da1b"}, pinned])
                implementation = "0x" + hex_bytes(implementation, 32)[-40:]
                result["beacon_implementation"] = implementation
            else:
                implementation = "0x" + hex_bytes(result["implementation_slot"], 32)[-40:]
            result["implementation_code"] = self.call("eth_getCode", [implementation, pinned])
        return result

    def acquire_block(self, chain: int, number: int, contracts: list[str], *, method="logs") -> tuple[dict, list[dict]]:
        if uint(self.call("eth_chainId", [])) != chain:
            raise EvidenceError("RPC chain mismatch")
        full = self.block(number)
        allowed = {address(c) for c in contracts}
        if not allowed:
            raise EvidenceError("Explicit acquisition contract set required")
        if method == "logs":
            logs = self.call("eth_getLogs", [{"blockHash": full["hash"], "address": sorted(allowed)}])
        elif method == "receipts":
            logs = []
            transactions = full["transactions"]
            for offset in range(0, len(transactions), 10):
                group = transactions[offset:offset + 10]
                receipts = self.batch([("eth_getTransactionReceipt", [tx]) for tx in group])
                for tx, receipt in zip(group, receipts):
                    if receipt["blockHash"] != full["hash"] or receipt["transactionHash"] != tx:
                        raise EvidenceError("Receipt moved during independent acquisition")
                    logs.extend(l for l in receipt["logs"] if address(l["address"]) in allowed)
        else:
            raise EvidenceError("Unknown acquisition method")
        for log in logs:
            if address(log["address"]) not in allowed or log["blockHash"] != full["hash"]:
                raise EvidenceError("RPC returned a log outside the pinned scope")
        if self.block(number)["hash"] != full["hash"]:
            raise EvidenceError("Reorg during acquisition; discard uncommitted batch and retry")
        # Both pipelines retain the exact same compact header schema.
        header = {k: full[k] for k in ("number", "hash", "parentHash", "timestamp")}
        return header, sorted(logs, key=lambda l: uint(l["logIndex"]))

    def balances(self, chain: int, block: dict, wallet: str, assets: list[str]) -> dict:
        if uint(self.call("eth_chainId", [])) != chain:
            raise EvidenceError("RPC chain mismatch")
        wallet = address(wallet)
        pinned = {"blockHash": hex_bytes(block["hash"], 32), "requireCanonical": True}
        result = {}
        for asset in assets:
            asset_chain, contract, token = asset.split(":")
            if int(asset_chain) != chain:
                raise EvidenceError("Asset belongs to a different chain")
            if token == "erc20":
                signature, types, args = "balanceOf(address)", ["address"], [wallet]
            else:
                signature, types, args = "balanceOf(address,uint256)", ["address", "uint256"], [wallet, uint(token)]
            data = "0x" + keccak(signature.encode())[:4].hex() + encode(types, args).hex()
            value = self.call("eth_call", [{"to": address(contract), "data": data}, pinned])
            result[asset] = int(hex_bytes(value, 32), 16)
        return {"chain": chain, "block_number": uint(block["number"]), "block_hash": block["hash"],
                "wallet": wallet, "balances": result, "complete": True,
                "source": "rpc:" + urlsplit(self.url).hostname}


def capture_range(rpc: ReadOnlyRPC, output: Path, *, chain: int, first: int, last: int,
                  contracts: list[str], method: str = "logs", confirmations: int = 200,
                  max_blocks: int = 1000, progress=None) -> dict:
    """Bounded read-only capture with safe resume and full empty-block coverage.

    A failed run stays .partial. Resume validates all retained canonical header
    hashes against the provider, then starts at the first divergence (known
    parent). A reorg deeper than the anchor fails closed. With one RPC request
    per block this is a correctness baseline; use the wallet-indexed 24h capture
    for the operator pilot.
    """
    from . import SAFETY, VERSION
    from .store import EvidenceStore

    chain, first, last = uint(chain), uint(first), uint(last)
    confirmations = uint(confirmations)
    if last < first or last - first + 1 > max_blocks or max_blocks > 1000:
        raise EvidenceError("Capture range must be 1..1000 blocks; use explicit bounded segments")
    if method not in {"logs", "receipts"}:
        raise EvidenceError("Unknown capture method")
    head = uint(rpc.call("eth_blockNumber", []))
    if last > head - confirmations:
        raise EvidenceError("Requested block is inside the confirmation buffer")
    output = Path(output).resolve()
    if output.exists():
        raise EvidenceError("Capture output already exists")
    partial = output.with_name(output.name + ".partial")
    partial.mkdir(parents=True, exist_ok=True)
    configuration = {"chain": chain, "first": first, "last": last,
                     "contracts": sorted({address(c) for c in contracts}), "method": method,
                     "confirmations": confirmations, "source": "rpc:" + urlsplit(rpc.url).hostname}
    config_file = partial / "configuration.json"
    if config_file.exists():
        if json.loads(config_file.read_text()) != configuration:
            raise EvidenceError("Resume configuration differs from retained acquisition")
    else:
        with config_file.open("x", encoding="utf-8") as target:
            target.write(canonical(configuration) + "\n")
    scope = {"addresses": configuration["contracts"], "topics": None}
    with EvidenceStore(partial / "evidence.db") as store:
        cursor = store.cursor(chain)
        start = first
        if cursor:
            start = cursor["number"] + 1
            for row in store.db.execute("SELECT number,hash FROM canonical_blocks WHERE chain=? ORDER BY number", (chain,)):
                if rpc.block(row["number"])["hash"] != row["hash"]:
                    start = row["number"]
                    break
        for number in range(start, last + 1):
            expected_cursor = store.cursor(chain)
            header, logs = rpc.acquire_block(chain, number, configuration["contracts"], method=method)
            store.ingest(chain, [header], logs, expected_cursor=expected_cursor, scope=scope)
            if progress:
                progress({"status": "READ_ONLY_CAPTURE", "block": number, "last": last, "logs": len(logs)})
        rows = [dict(r) for r in store.db.execute(
            "SELECT b.* FROM blocks b JOIN canonical_blocks c ON b.chain=c.chain AND b.hash=c.hash WHERE b.chain=? ORDER BY b.number", (chain,))]
        if rows[-1]["event_time"] - rows[0]["event_time"] > 86400:
            raise EvidenceError("Acquisition segment exceeds the project's 24h limit")
        batches = [{"blocks": [json.loads(row["payload"])], "logs": [r["raw"] for r in store.logs(chain) if r["block_number"] == row["number"]],
                    "received_at": row["received_at"]} for row in rows]
        result = {"version": VERSION, "status": "RAW_CAPTURE_ONLY", "configuration": configuration,
                  "batches": batches, "capture_hash": digest(batches), "safety": SAFETY,
                  "contract_verification": "NOT_PERFORMED", "ledger_approval": False}
        (partial / "capture.json").write_text(canonical(result) + "\n", encoding="utf-8")
        (partial / "run_manifest.json").write_text(canonical({
            "version": VERSION, "configuration": configuration, "capture_hash": result["capture_hash"],
            "completed_at": now_utc(), "safety": SAFETY}) + "\n", encoding="utf-8")
    os.rename(partial, output)
    return {k: v for k, v in result.items() if k != "batches"}
