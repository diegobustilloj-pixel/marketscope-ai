from __future__ import annotations

from collections import defaultdict

from eth_abi import decode
from eth_hash.auto import keccak

from . import SAFETY
from .common import EvidenceError, address, digest, hex_bytes, uint
from .normalize import asset_id


def independent_transfer_balances(raw_logs: list[dict], wallet: str, opening: dict[str, int],
                                  chain: int, token_contracts: set[str], cash_contracts: set[str]) -> dict[str, int]:
    """Independent balance fold: reads wire topics/data, not normalized events/lots.

    It shares only standard ABI primitive decoding. A receipt-based acquisition
    can supply raw_logs independently of eth_getLogs. Duplicate identities must
    have identical payloads. Unknown contracts and removed logs fail closed.
    """
    wallet = address(wallet)
    balances = defaultdict(int, {k: uint(v) for k, v in opening.items()})
    signatures = ["Transfer(address,address,uint256)", "TransferSingle(address,address,address,uint256,uint256)",
                  "TransferBatch(address,address,address,uint256[],uint256[])"]
    topics = ["0x" + keccak(s.encode()).hex() for s in signatures]
    seen = {}
    for raw in raw_logs:
        contract = address(raw["address"])
        if raw.get("removed"):
            raise EvidenceError("Removed log in independent canonical evidence")
        key = (hex_bytes(raw["blockHash"], 32), hex_bytes(raw["transactionHash"], 32), uint(raw["logIndex"]))
        if key in seen:
            if seen[key] != digest(raw):
                raise EvidenceError("Conflicting independent raw log")
            continue
        seen[key] = digest(raw)
        ts = [hex_bytes(x, 32) for x in raw["topics"]]
        if not ts or ts[0] not in topics:
            continue
        data = bytes.fromhex(hex_bytes(raw["data"])[2:])
        if ts[0] == topics[0]:
            if contract not in cash_contracts or len(ts) != 3 or len(data) != 32:
                raise EvidenceError("Unregistered/malformed ERC20 balance evidence")
            sender, receiver = [decode(["address"], bytes.fromhex(t[2:]))[0] for t in ts[1:]]
            amounts = [("erc20", int.from_bytes(data, "big"))]
        else:
            if contract not in token_contracts or len(ts) != 4:
                raise EvidenceError("Unregistered/malformed ERC1155 balance evidence")
            sender, receiver = [decode(["address"], bytes.fromhex(t[2:]))[0] for t in ts[2:]]
            if ts[0] == topics[1]:
                if len(data) != 64:
                    raise EvidenceError("Invalid TransferSingle size")
                amounts = [(str(int.from_bytes(data[:32], "big")), int.from_bytes(data[32:], "big"))]
            else:
                ids, quantities = decode(["uint256[]", "uint256[]"], data, strict=True)
                if len(ids) != len(quantities):
                    raise EvidenceError("Invalid TransferBatch size")
                amounts = list(zip(map(str, ids), quantities))
        sign = int(receiver == wallet) - int(sender == wallet)
        for token, quantity in amounts:
            balances[asset_id(chain, contract, token)] += sign * quantity
    if any(v < 0 for v in balances.values()):
        raise EvidenceError("Independent balance fold needs opening inventory")
    return dict(balances)


def fill_key(fill: dict) -> tuple:
    return (address(fill["exchange"]), hex_bytes(fill["tx"], 32), uint(fill["log_index"]),
            hex_bytes(fill["order_hash"], 32))


def reconcile(*, cursor: dict, wallet: str, ledger: dict, chain_fills: list[dict],
              clob: dict, onchain: dict, independent_balances: dict[str, int],
              failures: list[dict], required_assets: list[str]) -> dict:
    """Triple reconciliation at an explicit cut; missing != zero.

    CLOB has no authoritative outcome balance endpoint. Compare settled fills
    and order evidence there, then compare ledger inventory/collateral with
    pinned eth_call balances and the independently acquired transfer fold.
    """
    wallet = address(wallet)
    reasons = [f["code"] for f in failures]
    for label, evidence in (("CLOB", clob), ("ONCHAIN", onchain)):
        if (evidence.get("chain") != cursor["chain"] or evidence.get("block_hash") != cursor["hash"]
                or evidence.get("block_number") != cursor["number"] or evidence.get("wallet") != wallet):
            reasons.append(label + "_CUT_MISMATCH")
        if evidence.get("complete") is not True or not evidence.get("raw_ids"):
            reasons.append(label + "_INCOMPLETE_EVIDENCE")
    if not ledger["complete_basis"]:
        reasons.append("UNKNOWN_COST_BASIS")
    if not isinstance(clob.get("fills"), list) or not isinstance(clob.get("orders"), list):
        reasons.append("CLOB_SNAPSHOT_FIELDS_MISSING")
    balances = dict(ledger["balances"].get(wallet, {}))
    balances[ledger["quote_asset"]] = ledger["cash"].get(wallet, 0)
    rpc_balances = onchain.get("balances", {})
    assets = set(required_assets) | set(balances) | set(rpc_balances) | set(independent_balances)
    if not required_assets or ledger["quote_asset"] not in required_assets:
        reasons.append("ASSET_UNIVERSE_INCOMPLETE")
    rows = []
    for asset in sorted(assets):
        expected = balances.get(asset, 0)
        observed = uint(rpc_balances[asset]) if asset in rpc_balances else None
        independent = uint(independent_balances[asset]) if asset in independent_balances else None
        ok = observed is not None and independent is not None and expected == observed == independent
        if not ok:
            reasons.append("BALANCE_MISMATCH_OR_MISSING")
        rows.append({"asset": asset, "ledger": expected, "onchain": observed,
                     "independent": independent, "delta": None if observed is None else expected - observed,
                     "status": "MATCH" if ok else "BLOCKED"})
    def indexed(fills):
        result = {}
        for fill in fills:
            key = fill_key(fill)
            item = {k: fill[k] for k in ("side", "token_id", "quantity", "quote", "fee", "maker")}
            for field in ("quantity", "quote", "fee"):
                item[field] = uint(item[field])
            item["token_id"] = str(uint(item["token_id"]))
            item["maker"] = address(item["maker"])
            if item["side"] not in {"BUY", "SELL"}:
                raise EvidenceError("Unknown CLOB fill side")
            if key in result:
                raise EvidenceError("Duplicate fill identity in reconciliation snapshot")
            result[key] = item
        return result
    observed = indexed(chain_fills)
    reported = indexed(clob.get("fills", []))
    if observed != reported:
        reasons.append("CLOB_FILL_MISMATCH")
    if clob.get("orders_complete") is not True:
        reasons.append("CLOB_ORDER_SNAPSHOT_INCOMPLETE")
    for order in clob.get("orders", []):
        if order.get("state") not in {"OPEN", "PARTIAL", "FILLED", "CANCELED", "EXPIRED"}:
            reasons.append("UNKNOWN_ORDER_STATE")
        if order.get("state") in {"OPEN", "PARTIAL"} and uint(order["expires_at"]) <= uint(clob["as_of"]):
            reasons.append("ORDER_TTL_EXPIRED")
    reasons = sorted(set(reasons))
    result = {"status": "MATCH" if not reasons else "BLOCKED", "reasons": reasons,
              "balances": rows, "chain_fill_count": len(observed), "clob_fill_count": len(reported),
              "cut": cursor, "safety": SAFETY, "execution_allowed": False}
    return {**result, "reconciliation_hash": digest(result)}


def source_gate(health: list[dict], *, now: int, max_age: int, required: set[str]) -> dict:
    """TTL/gap/reconnect circuit breaker; requests no real cancellation or order."""
    reasons = []
    sources = {h["source"] for h in health}
    if sources != required or len(sources) != len(health):
        reasons.append("MISSING_OR_DUPLICATE_SOURCE")
    for h in health:
        if h.get("gap") or h.get("resynced") is not True:
            reasons.append(h["source"] + ":GAP_OR_UNRESOLVED_RECONNECT")
        age = uint(now) - uint(h["received_at"])
        if age < 0 or age > uint(max_age):
            reasons.append(h["source"] + ":STALE_OR_FUTURE")
        if h.get("contract_verified") is not True:
            reasons.append(h["source"] + ":CONTRACT_UNVERIFIED")
    return {"shadow_allowed": not reasons, "execution_allowed": False,
            "action": "ABSTAIN" if reasons else "READ_ONLY", "reasons": sorted(reasons)}
