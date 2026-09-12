"""Small adversarial replay vectors. All code, blocks and wallets are SYNTHETIC."""
from __future__ import annotations

import json
from pathlib import Path

from eth_abi import encode

from .common import ZERO, digest
from .decoders import event_topic
from .registry import code_hash


def sample_bundle(catalog_path: Path) -> dict:
    catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))["families"]
    wallet = "0x" + "11" * 20
    peer = "0x" + "22" * 20
    contracts = {"ctf": "0x" + "33" * 20, "collateral": "0x" + "44" * 20,
                 "clob_v2_ctf": "0x" + "55" * 20, "negrisk": "0x" + "66" * 20}
    chain = 137
    def h(n):
        return "0x" + f"{n:064x}"
    def asset(token):
        return f"{chain}:{contracts['ctf']}:{token}"
    quote_asset = f"{chain}:{contracts['collateral']}:erc20"
    batches, observations, clob_fills = [], [], []
    for number in range(100, 108):
        header = {"number": hex(number), "hash": h(number), "parentHash": h(number - 1), "timestamp": hex(1800000000 + (number - 100) * 2)}
        logs = []
        def emit(family, name, args):
            event = next(e for e in catalog[family]["abi"] if e["name"] == name)
            topics, types, values = [event_topic(event)], [], []
            for item in event["inputs"]:
                value = args[item["name"]]
                if item["type"] == "bytes32":
                    value = bytes.fromhex(value[2:])
                if item["indexed"]:
                    topics.append("0x" + encode([item["type"]], [value]).hex())
                else:
                    types.append(item["type"])
                    values.append(value)
            raw = {"address": contracts[family], "blockNumber": hex(number), "blockHash": h(number),
                   "transactionHash": h(1000 + number), "transactionIndex": "0x0", "logIndex": hex(len(logs)),
                   "topics": topics, "data": "0x" + encode(types, values).hex(), "removed": False}
            logs.append(raw)
            return raw
        def transfer(token, quantity, sender=peer, receiver=wallet):
            emit("ctf", "TransferSingle", {"operator": peer, "from": sender, "to": receiver, "id": token, "value": quantity})
        def cash(quantity, incoming=True):
            emit("collateral", "Transfer", {"from": peer if incoming else wallet, "to": wallet if incoming else peer, "amount": quantity})
        if number in (100, 101):
            buy = number == 100
            quantity, quote = (10, 40) if buy else (4, 20)
            transfer(1, quantity, peer if buy else wallet, wallet if buy else peer)
            cash(quote + 1 if buy else quote - 1, not buy)
            raw = emit("clob_v2_ctf", "OrderFilled", {"orderHash": h(number + 500), "maker": wallet, "taker": peer,
                       "side": 0 if buy else 1, "tokenId": 1, "makerAmountFilled": quote if buy else quantity,
                       "takerAmountFilled": quantity if buy else quote, "fee": 1, "builder": h(0), "metadata": h(0)})
            clob_fills.append({"exchange": contracts["clob_v2_ctf"], "tx": raw["transactionHash"],
                               "log_index": int(raw["logIndex"], 16), "order_hash": h(number + 500),
                               "side": "BUY" if buy else "SELL", "token_id": "1", "quantity": quantity,
                               "quote": quote, "fee": 1, "maker": wallet})
        elif number == 102:
            emit("ctf", "TransferBatch", {"operator": peer, "from": ZERO, "to": wallet, "ids": [2, 3], "values": [10, 10]})
            cash(10, False)
            emit("ctf", "PositionSplit", {"stakeholder": wallet, "collateralToken": contracts["collateral"],
                 "parentCollectionId": h(0), "conditionId": h(2), "partition": [1, 2], "amount": 10})
        elif number == 103:
            transfer(2, 10, wallet, ZERO)
            transfer(4, 10, ZERO, wallet)
            cash(2)
            emit("negrisk", "PositionsConverted", {"stakeholder": wallet, "marketId": h(3), "indexSet": 1, "amount": 10})
        elif number == 104:
            transfer(1, 2, wallet, peer)
        elif number in (105, 106):
            token, payout = (3, 10) if number == 105 else (4, 5)
            transfer(token, 10, wallet, ZERO)
            cash(payout)
            emit("ctf", "PayoutRedemption", {"redeemer": wallet, "collateralToken": contracts["collateral"],
                 "parentCollectionId": h(0), "conditionId": h(token), "indexSets": [1], "payout": payout})
        for contract in sorted({l["address"] for l in logs}):
            observations.append({"chain": chain, "address": contract, "block_number": number, "block_hash": h(number),
                                 "code": "0x6000", "implementation_slot": h(0), "beacon_slot": h(0),
                                 "source": "synthetic-fixture-not-a-deployment"})
        batches.append({"blocks": [header], "logs": logs, "received_at": "2026-09-12T00:00:00Z"})
    assets = [quote_asset] + [asset(i) for i in range(1, 5)]
    balances = {quote_asset: 985, asset(1): 4, **{asset(i): 0 for i in range(2, 5)}}
    cut = {"chain": chain, "wallet": wallet, "block_number": 107, "block_hash": h(107), "complete": True}
    return {"schema": 1, "evidence_kind": "synthetic", "chain": chain, "wallet": wallet,
            "scope": {"addresses": sorted(contracts.values()), "topics": None},
            "contracts": [{"chain": chain, "address": c, "family": f, "valid_from_block": 100, "valid_to_block": 107,
                           "abi": catalog[f]["abi"], "code_hash": code_hash("0x6000"), "source": "synthetic-fixture-only"}
                          for f, c in contracts.items()], "contract_observations": observations, "batches": batches,
            "opening": {"block_number": 99, "block_hash": h(99), "evidence": "synthetic-opening",
                        "quote_asset": quote_asset, "cash": {wallet: 1000}, "lots": []},
            "clob": {**cut, "fills": clob_fills, "orders": [], "orders_complete": True},
            "onchain": {**cut, "balances": balances}, "marks": {asset(1): "6"}, "required_assets": assets,
            "independent": {"complete": True, "method": "receipts", "source": "synthetic-receipt-vectors",
                            "first_block": 100, "last_block": 107, "tip_hash": h(107),
                            "raw_logs": [l for b in batches for l in b["logs"]]},
            "as_of": 1800000014, "health": [{"source": s, "received_at": 1800000014, "gap": False,
                  "resynced": True, "contract_verified": True} for s in ("rpc", "rest", "ws")]}
