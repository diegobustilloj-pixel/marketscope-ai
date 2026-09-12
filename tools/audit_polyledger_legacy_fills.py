"""Read-only comparison against the existing, independent car fill decoder."""
import argparse
import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from polymarket_bot.car_onchain import EXCHANGES, decode_log
from polymarket_bot.ledger import SAFETY
from polymarket_bot.ledger.common import digest
from polymarket_bot.ledger.decoders import decode_event, semantics


def audit(database: Path, catalog: Path):
    database = database.resolve()
    catalog_data = json.loads(catalog.read_text(encoding="utf-8"))["families"]
    by_address = {e.address: e for e in EXCHANGES}
    checked, failures, raw_hashes = 0, [], []
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        for wallet, payload in db.execute("SELECT maker,raw_json FROM onchain_fills ORDER BY block_number,log_index"):
            raw = json.loads(payload)
            exchange = by_address[raw["address"].lower()]
            old = decode_log(raw, exchange, wallet)
            family = "clob_v1" if exchange.version == "v1" else "clob_v2_ctf"
            name, values = decode_event(raw, catalog_data[family]["abi"])
            new = semantics(family, name, values)
            comparisons = {"side": new["side"] == old["side"], "token": new["token_id"] == old["token_id"],
                           "maker": new["maker"] == old["maker"], "taker": new["taker"] == old["taker"],
                           "fee": new["fee"] == int(old["fee_raw"]),
                           "quantity": new["quantity"] == Decimal(str(old["shares"])) * 1_000_000,
                           "quote": new["quote"] == Decimal(str(old["notional_usd"])) * 1_000_000}
            if not all(comparisons.values()):
                failures.append({"tx": old["transaction_hash"], "log_index": old["log_index"], "checks": comparisons})
            raw_hashes.append(digest(raw))
            checked += 1
    return {"status": "MATCH" if checked and not failures else "BLOCKED", "checked_fills": checked,
            "failures": failures, "input_raw_hash": digest(raw_hashes),
            "database_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
            "scope": "real archived fill decoding only; not inventory, deployed bytecode or full PnL verification",
            "safety": SAFETY}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path("data/car_forensics/car_onchain.db"))
    parser.add_argument("--catalog", type=Path, default=Path("configs/polyledger/abi_catalog.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.database, args.catalog)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as target:
        json.dump(result, target, indent=2)
        target.write("\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "MATCH" else 2)
