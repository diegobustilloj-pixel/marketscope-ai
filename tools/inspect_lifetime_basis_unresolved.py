"""Inspect only quarantined lifetime-basis receipts without rescanning all shards."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.ledger.common import address, hex_bytes
from polymarket_bot.ledger.decoders import decode_event, semantics
from polymarket_bot.ledger.lifetime_basis_bundle import (
    BALANCE_TOPICS,
    _contains_wallet,
    _family_map,
)


def _closure(root: Path, folder: str, manifest: list[dict], index: int, tx: str) -> dict:
    for row in manifest:
        start, end = (int(value) for value in row["file"].removesuffix(".json").split("-"))
        if start <= index <= end:
            shard = json.loads((root / folder / row["file"]).read_text(encoding="utf-8"))
            offset = shard["transactions"].index(tx)
            return shard["closures"][offset]
    raise RuntimeError(f"No receipt shard covers transaction index {index}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--source-capture", type=Path, required=True)
    parser.add_argument("--gap-closure", type=Path, required=True)
    parser.add_argument("--scope", type=Path,
                        default=Path("configs/polyledger/polygon_lifetime_backfill.json"))
    parser.add_argument("--catalog", type=Path,
                        default=Path("configs/polyledger/abi_catalog.json"))
    parser.add_argument("--wallet", required=True)
    args = parser.parse_args()
    wallet = address(args.wallet)
    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    unresolved = {row["tx"]: row for row in bundle["unresolved_actions"]}
    scope = json.loads(args.scope.read_text(encoding="utf-8"))
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    families = _family_map(scope)
    abis = {family: catalog["families"][family]["abi"]
            for family in set(families.values())}
    source_manifest = json.loads(
        (args.source_capture / "run_manifest.json").read_text(encoding="utf-8")
    )["result"]["transaction_log_manifest"]
    gap_manifest = json.loads(
        (args.gap_closure / "summary.json").read_text(encoding="utf-8")
    )["receipt_manifest"]
    sources = [
        (args.source_capture, "transaction_log_shards", source_manifest),
        (args.gap_closure, "receipt_shards", gap_manifest),
    ]
    located = {}
    for root, folder, manifest in sources:
        transactions = json.loads((root / "transactions.json").read_text(encoding="utf-8"))
        index = {tx: position for position, tx in enumerate(transactions) if tx in unresolved}
        for tx, position in index.items():
            located[tx] = _closure(root, folder, manifest, position, tx)
    if set(located) != set(unresolved):
        raise RuntimeError("Not every unresolved transaction has a receipt")
    result = []
    for tx, failure in sorted(unresolved.items(), key=lambda row: row[1]["order"]):
        events = []
        for raw in located[tx]["logs"]:
            contract = address(raw["address"])
            family = families.get(contract)
            topics = raw.get("topics") or []
            if family is None or topics and hex_bytes(topics[0], 32) in BALANCE_TOPICS:
                continue
            name, values = decode_event(raw, abis[family])
            economic = semantics(family, name, values)
            relevant = _contains_wallet(values, wallet)
            if economic["kind"] == "fill":
                relevant = relevant or address(economic["maker"]) == wallet \
                    or address(economic["taker"]) == wallet
            if relevant:
                events.append({"contract": contract, "family": family, "name": name,
                               "args": values, "economic": economic,
                               "log_index": int(raw["logIndex"], 16)})
        result.append({"tx": tx, "order": failure["order"], "codes": failure["codes"],
                       "inputs": failure["inputs"], "outputs": failure["outputs"],
                       "cash_delta": failure["cash_delta"], "events": events})
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
