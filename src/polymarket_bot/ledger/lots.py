from __future__ import annotations

import copy
from decimal import Decimal, localcontext

from .common import EvidenceError, address, canonical, digest, uint

POLICY = "fifo-atomic-proportional-remainder-v1"
KINDS = {"buy", "sell", "split", "merge", "convert", "redeem", "wrap", "unwrap",
         "transfer", "receive", "cash", "reward"}


def allocate(total: int | None, weights: list[int]) -> list[int | None]:
    """Allocate atomic basis without loss; final output receives the remainder."""
    if total is None:
        return [None] * len(weights)
    if not weights:
        return []
    denominator = sum(weights)
    if denominator <= 0:
        raise EvidenceError("Positive allocation quantities required")
    parts = [total * weight // denominator for weight in weights[:-1]]
    return parts + [total - sum(parts)]


class LotLedger:
    """Exact FIFO lots, explicit basis uncertainty, atomic batches in memory.

    Values are quote-asset atomic units, never USD floats. Initial lots require
    supplied evidence; a missing basis stays NULL. Assets must be namespaced by
    chain and token contract. Output cost allocation is an accounting policy,
    not a market-value estimate or tax treatment.
    """

    def __init__(self, opening: dict):
        self.opening = copy.deepcopy(opening)
        canonical(opening)
        if not opening.get("evidence") or not opening.get("quote_asset"):
            raise EvidenceError("Opening snapshot must identify quote asset and evidence")
        self.lots = []
        self.cash = {address(w): uint(v) for w, v in opening.get("cash", {}).items()}
        self.realized = {}
        self.journal = []
        self.seen = {}
        self.last_order = None
        self.unknown_realizations = []
        for i, lot in enumerate(opening.get("lots", [])):
            self._open("opening:" + str(i), address(lot["wallet"]), lot["asset"], uint(lot["quantity"]),
                       None if lot["cost"] is None else uint(lot["cost"]), [opening["evidence"]])

    def _open(self, lot_id, wallet, asset, quantity, cost, provenance):
        if not isinstance(asset, str) or asset.count(":") < 2 or not quantity:
            raise EvidenceError("Positive quantity and chain:contract:token asset required")
        self.lots.append({"id": lot_id, "wallet": wallet, "asset": asset, "quantity": quantity,
                          "remaining": quantity, "cost": cost, "remaining_cost": cost,
                          "provenance": list(provenance)})

    def _consume(self, wallet, asset, quantity):
        remaining, cost, provenance, parts = quantity, 0, [], []
        for lot in self.lots:
            if (lot["wallet"], lot["asset"]) != (wallet, asset) or lot["remaining"] == 0:
                continue
            take = min(remaining, lot["remaining"])
            basis = lot["remaining_cost"]
            consumed = None if basis is None else basis * take // lot["remaining"]
            if consumed is None:
                cost = None
            elif cost is not None:
                cost += consumed
            lot["remaining"] -= take
            if basis is not None:
                lot["remaining_cost"] -= consumed
            provenance.extend(lot["provenance"])
            parts.append({"lot_id": lot["id"], "quantity": take, "cost": consumed})
            remaining -= take
            if not remaining:
                break
        if remaining:
            raise EvidenceError(f"Insufficient observed inventory for {asset}; opening snapshot required")
        return cost, provenance, parts

    def apply_batch(self, actions: list[dict]):
        candidate = copy.deepcopy(self)
        for action in actions:
            candidate._apply(action)
        self.__dict__ = candidate.__dict__

    def _apply(self, action):
        canonical(action)
        key = action["id"]
        if key in self.seen:
            if self.seen[key] != digest(action):
                raise EvidenceError("Conflicting economic action identity")
            return
        order = tuple(uint(x) for x in action["order"])
        if len(order) != 3 or (self.last_order is not None and order <= self.last_order):
            raise EvidenceError("Actions must have a unique increasing event order")
        kind, wallet = action["kind"], address(action["wallet"])
        cash = action["cash_delta"]
        if kind not in KINDS or type(cash) is not int or not action.get("raw_ids"):
            raise EvidenceError("Known action, atomic signed cash and raw provenance required")
        inputs, outputs = action.get("inputs", []), action.get("outputs", [])
        for legs in (inputs, outputs):
            if len({x["asset"] for x in legs}) != len(legs) or any(uint(x["quantity"]) == 0 for x in legs):
                raise EvidenceError("Duplicate assets or zero quantities in action")
        if kind == "buy" and (inputs or not outputs or cash >= 0):
            raise EvidenceError("Invalid buy legs")
        if kind in {"sell", "merge", "redeem"} and (not inputs or outputs or cash < 0):
            raise EvidenceError("Invalid disposal legs")
        if kind in {"split", "wrap", "unwrap", "convert"} and not outputs:
            raise EvidenceError("Conversion needs explicit outputs")
        if kind == "split" and cash > 0:
            raise EvidenceError("Split cannot create cash")
        if kind in {"wrap", "unwrap"} and (not inputs or cash != 0):
            raise EvidenceError("Wrap/unwrap must carry basis between explicit asset legs")
        if kind == "receive" and (inputs or not outputs or cash):
            raise EvidenceError("External receipt must have unknown basis")
        if kind == "transfer" and (not inputs or outputs or cash):
            raise EvidenceError("Transfer must have explicit source lots and no proceeds")
        if kind in {"cash", "reward"} and (inputs or outputs):
            raise EvidenceError("Cash/reward cannot create outcome inventory")
        if kind == "reward" and cash < 0:
            raise EvidenceError("Negative reward")
        cost, provenance, consumed = 0, list(action["raw_ids"]), []
        transfer_parts = []
        for leg in inputs:
            basis, origins, parts = self._consume(wallet, leg["asset"], uint(leg["quantity"]))
            cost = None if cost is None or basis is None else cost + basis
            provenance.extend(origins)
            consumed.extend(parts)
            transfer_parts.extend((leg["asset"], p) for p in parts)
        self.cash[wallet] = self.cash.get(wallet, 0) + cash
        if self.cash[wallet] < 0:
            raise EvidenceError("Negative observed collateral; missing opening cash or transfer")
        pnl = 0
        if kind in {"sell", "merge", "redeem"}:
            pnl = None if cost is None else cash - cost
        elif kind == "reward":
            pnl = cash
        elif kind == "transfer":
            if action.get("counterparty"):
                target = address(action["counterparty"])
                if target == wallet:
                    raise EvidenceError("Self transfer should be a zero net movement")
                for i, (asset, part) in enumerate(transfer_parts):
                    self._open(f"{key}:{i}", target, asset, part["quantity"], part["cost"], provenance)
        if outputs:
            if kind == "receive":
                output_cost = None
            else:
                output_cost = None if cost is None else cost - cash
                if output_cost is not None and output_cost < 0:
                    # Cash returned beyond all known basis is separately labeled;
                    # remaining outcomes retain zero basis, never negative basis.
                    pnl, output_cost = -output_cost, 0
            weights = [uint(x["quantity"]) for x in outputs]
            for i, (leg, basis) in enumerate(zip(outputs, allocate(output_cost, weights))):
                self._open(f"{key}:{i}", wallet, leg["asset"], uint(leg["quantity"]), basis, provenance)
        if pnl is None:
            self.unknown_realizations.append(key)
        else:
            self.realized[wallet] = self.realized.get(wallet, 0) + pnl
        self.journal.append({**copy.deepcopy(action), "consumed_lots": consumed,
                             "realized_pnl": pnl, "basis_policy": POLICY})
        self.last_order = order
        self.seen[key] = digest(action)

    def snapshot(self, marks: dict[str, str] | None = None) -> dict:
        balances = {}
        unrealized = {}
        for lot in self.lots:
            if not lot["remaining"]:
                continue
            wallet, asset = lot["wallet"], lot["asset"]
            balances.setdefault(wallet, {})[asset] = balances.get(wallet, {}).get(asset, 0) + lot["remaining"]
            pnl = None
            if marks is not None and asset in marks and lot["remaining_cost"] is not None:
                if not isinstance(marks[asset], str):
                    raise EvidenceError("Marks must be exact decimal strings in quote atoms per token atom")
                with localcontext() as context:
                    context.prec = 160
                    mark = Decimal(marks[asset])
                    if not mark.is_finite() or mark < 0:
                        raise EvidenceError("Invalid mark")
                    pnl = Decimal(lot["remaining"]) * mark - lot["remaining_cost"]
                    old = unrealized.get(wallet, Decimal(0))
                    unrealized[wallet] = None if old is None else old + pnl
            if pnl is None:
                unrealized[wallet] = None
        result = {"policy": POLICY, "quote_asset": self.opening["quote_asset"],
                  "balances": balances, "cash": self.cash, "lots": self.lots, "journal": self.journal,
                  "known_realized_pnl": self.realized, "unknown_realizations": self.unknown_realizations,
                  "unrealized_pnl": {w: None if p is None else format(p, "f") for w, p in unrealized.items()},
                  "complete_basis": not self.unknown_realizations and all(l["cost"] is not None for l in self.lots)}
        return {**copy.deepcopy(result), "ledger_hash": digest(result)}
