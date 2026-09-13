from __future__ import annotations

from eth_abi import decode, encode
from eth_hash.auto import keccak

from .common import EvidenceError, canonical, digest, hex_bytes
from .combo import position_fields, require_supported_layout
from .registry import ContractRegistry

DECODER_VERSION = "abi-events/2"
INVENTORY = {"PositionSplit": "split", "PositionsMerge": "merge",
             "PositionsConverted": "convert", "PayoutRedemption": "redeem",
             "Wrapped": "wrap", "Unwrapped": "unwrap"}
FAMILY_EVENTS = {
    "clob_v1": {"OrderFilled", "OrdersMatched", "FeeCharged", "OrderCancelled"},
    "clob_v2_ctf": {"OrderFilled", "OrdersMatched", "FeeCharged"},
    "ctf": {"TransferSingle", "TransferBatch", "PositionSplit", "PositionsMerge", "PayoutRedemption",
            "ConditionPreparation", "ConditionResolution", "ApprovalForAll"},
    "negrisk": {"PositionSplit", "PositionsMerge", "PositionsConverted", "PayoutRedemption",
                "MarketPrepared", "QuestionPrepared", "OutcomeReported"},
    "collateral": {"Transfer", "Approval", "Wrapped", "Unwrapped", "Paused", "Unpaused"},
    "combo_v2": {"TransferSingle", "TransferBatch", "ApprovalForAll", "URI", "Upgraded", "Initialized",
                 "ModuleAdded", "ModuleRemoved", "CrossModuleAuthSet", "OwnershipHandoverCanceled",
                 "OwnershipHandoverRequested", "OwnershipTransferred", "RolesUpdated"},
}


def event_topic(event: dict) -> str:
    signature = event["name"] + "(" + ",".join(i["type"] for i in event["inputs"]) + ")"
    return "0x" + keccak(signature.encode()).hex()


def json_value(value):
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    return value


def decode_event(raw: dict, abi: list[dict]) -> tuple[str, dict]:
    topics = [hex_bytes(x, 32) for x in raw["topics"]]
    matches = [e for e in abi if e.get("type") == "event" and not e.get("anonymous")
               and topics and event_topic(e) == topics[0]]
    if len(matches) != 1:
        raise EvidenceError("Unknown or ambiguous ABI event")
    event = matches[0]
    indexed = [i for i in event["inputs"] if i["indexed"]]
    plain = [i for i in event["inputs"] if not i["indexed"]]
    if len(topics) != 1 + len(indexed):
        raise EvidenceError("Incorrect topic count")
    values = {}
    try:
        for item, topic in zip(indexed, topics[1:]):
            kind = item["type"]
            if kind not in {"address", "bytes32", "bool"} and not kind.startswith("uint"):
                raise EvidenceError("Indexed dynamic/complex value requires explicit semantics")
            values[item["name"]] = json_value(decode([kind], bytes.fromhex(topic[2:]), strict=True)[0])
        data = bytes.fromhex(hex_bytes(raw["data"])[2:])
        types = [i["type"] for i in plain]
        decoded = decode(types, data, strict=True)
        if encode(types, decoded) != data:
            raise EvidenceError("Noncanonical ABI data or trailing bytes")
        values.update({i["name"]: json_value(v) for i, v in zip(plain, decoded)})
    except (ValueError, OverflowError) as exc:
        raise EvidenceError("Malformed ABI payload") from exc
    # eth-abi decoding errors have a separate hierarchy; caller quarantines them.
    return event["name"], values


def semantics(family: str, name: str, args: dict) -> dict:
    if name not in FAMILY_EVENTS[family]:
        raise EvidenceError(f"Unsupported {family} event: {name}")
    if name == "OrderFilled":
        if family == "clob_v1":
            maker_asset, taker_asset = args["makerAssetId"], args["takerAssetId"]
            if (maker_asset == 0) == (taker_asset == 0):
                raise EvidenceError("V1 fill must exchange exactly one collateral asset")
            buy = maker_asset == 0
            token = taker_asset if buy else maker_asset
        else:
            if args["side"] not in (0, 1) or args["tokenId"] == 0:
                raise EvidenceError("Invalid V2 side/token")
            buy, token = args["side"] == 0, args["tokenId"]
        return {"kind": "fill", "side": "BUY" if buy else "SELL", "token_id": str(token),
                "quantity": args["takerAmountFilled"] if buy else args["makerAmountFilled"],
                "quote": args["makerAmountFilled"] if buy else args["takerAmountFilled"],
                "fee": args["fee"], "maker": args["maker"], "taker": args["taker"],
                "order_hash": args["orderHash"], "fee_asset": "outcome" if family == "clob_v1" and buy else "collateral"}
    if name in ("TransferSingle", "TransferBatch"):
        ids = args["ids"] if name == "TransferBatch" else [args["id"]]
        plural, singular = ("amounts", "amount") if family == "combo_v2" else ("values", "value")
        quantities = args[plural] if name == "TransferBatch" else [args[singular]]
        if len(ids) != len(quantities):
            raise EvidenceError("TransferBatch array lengths differ")
        return {"kind": "transfer", "movements": [
            {"item_index": i, "token_id": str(token), "quantity": quantity,
             "from": args["from"], "to": args["to"],
             **({"position": position_fields(token)} if family == "combo_v2" else {})}
            for i, (token, quantity) in enumerate(zip(ids, quantities))]}
    if name == "Transfer":
        return {"kind": "cash_transfer", "from": args["from"], "to": args["to"], "quantity": args.get("value", args.get("amount"))}
    if name in INVENTORY:
        return {"kind": "inventory_action", "action": INVENTORY[name]}
    return {"kind": "annotation"}  # OrdersMatched is never a second fill.


def decode_canonical(registry: ContractRegistry, chain: int) -> tuple[list[dict], list[dict]]:
    events, failures = [], []
    for row in registry.store.logs(chain):
        try:
            version = registry.resolve(chain, row["address"], row["block_number"])
            registry.require_attestation(version, row["block_hash"])
            if version["family"] == "combo_v2":
                require_supported_layout(version)
            name, args = decode_event(row["raw"], version["abi"])
            result = {"raw_id": row["id"], "registry_id": version["id"],
                      "decoder": DECODER_VERSION + "/" + version["family"], "family": version["family"],
                      "chain": chain, "contract": row["address"], "block_number": row["block_number"],
                      "block_hash": row["block_hash"], "tx": row["tx"], "tx_index": row["tx_index"],
                      "log_index": row["log_index"], "name": name, "args": args,
                      "economic": semantics(version["family"], name, args)}
            key = digest(result)
            registry.store.db.execute("INSERT OR IGNORE INTO decoded_events VALUES(?,?,?,?,?)",
                                      (key, row["id"], result["decoder"], version["id"], canonical(result)))
            events.append(result)
        except Exception as exc:
            # Preserve the raw log and error; no guessed zero-valued accounting row.
            failure = {"raw_id": row["id"], "code": "DECODE_FAILURE", "error": str(exc), "decoder": DECODER_VERSION}
            registry.store.incident("DECODE_FAILURE", failure)
            failures.append(failure)
    return events, failures
