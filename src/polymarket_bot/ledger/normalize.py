from __future__ import annotations

from collections import defaultdict

from .common import EvidenceError, address, digest

NORMALIZER_VERSION = "transaction-net-conservative/1"


def asset_id(chain: int, contract: str, token: str) -> str:
    return f"{chain}:{address(contract)}:{token}"


def movements(events: list[dict], wallet: str, quote_asset: str) -> tuple[dict[str, int], int]:
    """Authoritative balance changes from transfers; never also count fills."""
    tokens, cash = defaultdict(int), 0
    for e in events:
        economic = e["economic"]
        kind = economic["kind"]
        if kind == "transfer":
            for movement in economic["movements"]:
                sign = int(movement["to"] == wallet) - int(movement["from"] == wallet)
                tokens[asset_id(e["chain"], e["contract"], movement["token_id"])] += sign * movement["quantity"]
        elif kind == "cash_transfer":
            sign = int(economic["to"] == wallet) - int(economic["from"] == wallet)
            if sign and asset_id(e["chain"], e["contract"], "erc20") != quote_asset:
                raise EvidenceError("Multiple collateral assets require explicit wrap/unwrap mapping")
            cash += sign * economic["quantity"]
    return {k: v for k, v in tokens.items() if v}, cash


def normalize_transactions(events: list[dict], wallet: str, quote_asset: str) -> tuple[list[dict], list[dict]]:
    """Support only unambiguous transaction economics; quarantine mixed operations.

    This initial normalizer is intentionally narrower than the lot engine. A
    transaction containing buys and sells, multiple inventory action families,
    unresolved token mappings or unrelated transfers is not guessed from net
    deltas. It requires a reviewed transaction-specific action bundle.
    """
    wallet = address(wallet)
    transactions = defaultdict(list)
    for event in events:
        transactions[(event["block_number"], event["tx_index"], event["tx"])].append(event)
    actions, failures = [], []
    for (block, index, tx), group in sorted(transactions.items()):
        raw_ids = sorted({e["raw_id"] for e in group})
        try:
            delta, cash = movements(group, wallet, quote_asset)
            fills = [e for e in group if e["economic"]["kind"] == "fill" and e["economic"]["maker"] == wallet]
            annotations = {e["economic"]["action"] for e in group if e["economic"]["kind"] == "inventory_action"
                           and wallet in [e["args"].get("stakeholder"), e["args"].get("redeemer")]}
            if not delta and not cash:
                if fills:
                    raise EvidenceError("Fill without corresponding transfer evidence")
                continue
            inputs = [{"asset": a, "quantity": -q} for a, q in sorted(delta.items()) if q < 0]
            outputs = [{"asset": a, "quantity": q} for a, q in sorted(delta.items()) if q > 0]
            if fills:
                if annotations or len({e["economic"]["side"] for e in fills}) != 1:
                    raise EvidenceError("Mixed fill/inventory economics require explicit action mapping")
                token_ids = {e["economic"]["token_id"] for e in fills}
                if len(token_ids) != 1 or len(delta) != 1:
                    raise EvidenceError("Multiple assets in fill transaction")
                family = {e["family"] for e in fills}
                if len(family) != 1:
                    raise EvidenceError("Mixed exchange eras")
                buy = fills[0]["economic"]["side"] == "BUY"
                quantity = sum(e["economic"]["quantity"] for e in fills)
                quote = sum(e["economic"]["quote"] for e in fills)
                fee = sum(e["economic"]["fee"] for e in fills)
                v1_buy = buy and family == {"clob_v1"}
                expected_quantity = quantity - fee if v1_buy else quantity
                expected_cash = -quote - (0 if v1_buy else fee) if buy else quote - fee
                asset, observed = next(iter(delta.items()))
                if (asset.rsplit(":", 1)[1] not in token_ids or observed != (expected_quantity if buy else -quantity)
                        or cash != expected_cash):
                    raise EvidenceError("Fill/transfer/fee mismatch")
                kind = "buy" if buy else "sell"
            elif annotations:
                if len(annotations) != 1:
                    raise EvidenceError("Mixed inventory actions require explicit action mapping")
                kind = next(iter(annotations))
                if kind in {"wrap", "unwrap"}:
                    raise EvidenceError("Wrap/unwrap needs both collateral asset legs")
            else:
                # Mint/burn without a semantic explanation must never be treated
                # as an external gift or a cost-free redemption.
                relevant = [m for e in group if e["economic"]["kind"] == "transfer" for m in e["economic"]["movements"]
                            if wallet in (m["from"], m["to"])]
                zero = "0x" + "00" * 20
                if any(zero in (m["from"], m["to"]) for m in relevant):
                    raise EvidenceError("Unexplained mint/burn")
                if inputs and outputs or delta and cash:
                    raise EvidenceError("Unexplained exchange/transfer bundle")
                kind = "transfer" if inputs else "receive" if outputs else "cash"
            actions.append({"id": digest([NORMALIZER_VERSION, wallet, tx, raw_ids]),
                            "order": [block, index, 0], "tx": tx, "wallet": wallet, "kind": kind,
                            "inputs": inputs, "outputs": outputs, "cash_delta": cash,
                            "raw_ids": raw_ids, "normalizer": NORMALIZER_VERSION})
        except EvidenceError as exc:
            failures.append({"tx": tx, "raw_ids": raw_ids, "code": "NORMALIZATION_FAILURE", "error": str(exc)})
    return actions, failures
