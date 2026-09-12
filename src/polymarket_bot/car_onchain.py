from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


RPC_URL = "https://polygon-bor-rpc.publicnode.com"
DEFAULT_WALLET = "0x7c3db723f1d4d8cb9c550095203b686cb11e5c6b"
V2_START_BLOCK = 84_902_353
# @car's public activity begins in February 2024, before block 57M.  Starting at
# 50M is deliberately conservative and still scans only wallet-indexed topics.
V1_START_BLOCK = 50_000_000
MAX_BLOCK_RANGE = 10_000
RPC_BATCH_SIZE = 10

ORDER_FILLED_V1 = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"
ORDER_FILLED_V2 = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"


@dataclass(frozen=True)
class Exchange:
    key: str
    address: str
    version: str
    order_filled_topic: str
    start_block: int
    end_block: int | None
    negative_risk: bool


EXCHANGES = (
    Exchange(
        "v1_ctf",
        "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
        "v1",
        ORDER_FILLED_V1,
        V1_START_BLOCK,
        V2_START_BLOCK - 1,
        False,
    ),
    Exchange(
        "v1_negrisk",
        "0xc5d563a36ae78145c45a50134d48a1215220f80a",
        "v1",
        ORDER_FILLED_V1,
        V1_START_BLOCK,
        V2_START_BLOCK - 1,
        True,
    ),
    Exchange(
        "v2_ctf",
        "0xe111180000d2663c0091e4f400237545b87b996b",
        "v2",
        ORDER_FILLED_V2,
        V2_START_BLOCK,
        None,
        False,
    ),
    Exchange(
        "v2_negrisk",
        "0xe2222d279d744050d28e00520010520000310f59",
        "v2",
        ORDER_FILLED_V2,
        V2_START_BLOCK,
        None,
        True,
    ),
)


def _rpc(payload: Any, *, attempts: int = 6, rpc_url: str = RPC_URL, timeout: int = 90) -> Any:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            rpc_url,
            data=encoded,
            headers={"Content-Type": "application/json", "User-Agent": "PolyLedger-CarForensics/0.1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(min(20.0, 1.5 * (2**attempt)))
    raise RuntimeError(f"Polygon RPC failed after {attempts} attempts: {last_error}")


def current_block() -> int:
    response = _rpc({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
    if not isinstance(response, dict) or not response.get("result"):
        raise RuntimeError(f"Invalid eth_blockNumber response: {response}")
    return int(str(response["result"]), 16)


def _word(data: str, index: int) -> int:
    payload = data[2:] if data.startswith("0x") else data
    start = index * 64
    end = start + 64
    if len(payload) < end:
        raise ValueError("short OrderFilled data")
    return int(payload[start:end], 16)


def _topic_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def decode_log(log: dict[str, Any], exchange: Exchange, wallet: str) -> dict[str, Any]:
    topics = log.get("topics") or []
    if len(topics) < 4:
        raise ValueError("OrderFilled log has fewer than four topics")
    maker = _topic_address(str(topics[2]))
    taker = _topic_address(str(topics[3]))
    if maker != wallet.lower():
        raise ValueError("OrderFilled maker does not match requested wallet")

    if exchange.version == "v1":
        maker_asset = _word(str(log["data"]), 0)
        taker_asset = _word(str(log["data"]), 1)
        maker_amount = _word(str(log["data"]), 2)
        taker_amount = _word(str(log["data"]), 3)
        fee_raw = _word(str(log["data"]), 4)
        side = "BUY" if maker_asset == 0 else "SELL"
        token_id = taker_asset if side == "BUY" else maker_asset
        share_raw = taker_amount if side == "BUY" else maker_amount
        cash_raw = maker_amount if side == "BUY" else taker_amount
        builder = None
        metadata = None
        side_raw = 0 if side == "BUY" else 1
    else:
        side_raw = _word(str(log["data"]), 0)
        token_id = _word(str(log["data"]), 1)
        maker_amount = _word(str(log["data"]), 2)
        taker_amount = _word(str(log["data"]), 3)
        fee_raw = _word(str(log["data"]), 4)
        builder = "0x" + (str(log["data"])[2:])[5 * 64 : 6 * 64]
        metadata = "0x" + (str(log["data"])[2:])[6 * 64 : 7 * 64]
        side = "BUY" if side_raw == 0 else "SELL"
        share_raw = taker_amount if side == "BUY" else maker_amount
        cash_raw = maker_amount if side == "BUY" else taker_amount

    shares = share_raw / 1_000_000
    notional = cash_raw / 1_000_000
    price = notional / shares if shares else None
    role = "TAKER" if taker == exchange.address.lower() else "MAKER"
    return {
        "transaction_hash": str(log["transactionHash"]).lower(),
        "log_index": int(str(log["logIndex"]), 16),
        "block_number": int(str(log["blockNumber"]), 16),
        "exchange_key": exchange.key,
        "exchange_address": exchange.address.lower(),
        "exchange_version": exchange.version,
        "negative_risk_exchange": int(exchange.negative_risk),
        "maker": maker,
        "taker": taker,
        "role": role,
        "side": side,
        "side_raw": side_raw,
        "token_id": str(token_id),
        "shares": shares,
        "notional_usd": notional,
        "price": price,
        "fee_raw": str(fee_raw),
        "fee_usd_estimate": fee_raw / 1_000_000,
        "builder": builder,
        "metadata": metadata,
        "raw_json": json.dumps(log, sort_keys=True, separators=(",", ":")),
    }


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS onchain_fills(
            transaction_hash TEXT NOT NULL,
            log_index INTEGER NOT NULL,
            block_number INTEGER NOT NULL,
            exchange_key TEXT NOT NULL,
            exchange_address TEXT NOT NULL,
            exchange_version TEXT NOT NULL,
            negative_risk_exchange INTEGER NOT NULL,
            maker TEXT NOT NULL,
            taker TEXT NOT NULL,
            role TEXT NOT NULL,
            side TEXT NOT NULL,
            side_raw INTEGER NOT NULL,
            token_id TEXT NOT NULL,
            shares REAL NOT NULL,
            notional_usd REAL NOT NULL,
            price REAL,
            fee_raw TEXT NOT NULL,
            fee_usd_estimate REAL NOT NULL,
            builder TEXT,
            metadata TEXT,
            raw_json TEXT NOT NULL,
            PRIMARY KEY(transaction_hash, log_index)
        );
        CREATE INDEX IF NOT EXISTS onchain_token_tx
            ON onchain_fills(token_id, transaction_hash);
        CREATE INDEX IF NOT EXISTS onchain_role
            ON onchain_fills(role, exchange_version);
        CREATE TABLE IF NOT EXISTS scan_progress(
            exchange_key TEXT PRIMARY KEY,
            last_block INTEGER NOT NULL,
            head_block INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """
    )
    return connection


def _insert_rows(connection: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    columns = (
        "transaction_hash", "log_index", "block_number", "exchange_key",
        "exchange_address", "exchange_version", "negative_risk_exchange",
        "maker", "taker", "role", "side", "side_raw", "token_id", "shares",
        "notional_usd", "price", "fee_raw", "fee_usd_estimate", "builder",
        "metadata", "raw_json",
    )
    statement = (
        f"INSERT OR IGNORE INTO onchain_fills({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)})"
    )
    before = connection.total_changes
    connection.executemany(statement, ([row[column] for column in columns] for row in rows))
    return connection.total_changes - before


def scan(wallet: str, database: Path, *, recent_blocks: int | None = None) -> dict[str, Any]:
    if len(wallet) != 42 or not wallet.startswith("0x"):
        raise ValueError("wallet must be a 0x-prefixed 20-byte address")
    wallet = wallet.lower()
    padded_wallet = "0x" + ("0" * 24) + wallet[2:]
    head = current_block()
    connection = _connect(database)
    total_new = 0
    total_ranges = 0

    for exchange in EXCHANGES:
        end = min(head, exchange.end_block) if exchange.end_block is not None else head
        progress = connection.execute(
            "SELECT last_block FROM scan_progress WHERE exchange_key=?", (exchange.key,)
        ).fetchone()
        available_start = head - recent_blocks + 1 if recent_blocks else exchange.start_block
        next_block = max(
            exchange.start_block,
            available_start,
            (int(progress[0]) + 1) if progress else exchange.start_block,
        )
        if next_block > end:
            continue
        ranges = math.ceil((end - next_block + 1) / MAX_BLOCK_RANGE)
        done = 0
        while next_block <= end:
            batch: list[tuple[int, int, int]] = []
            for _ in range(RPC_BATCH_SIZE):
                if next_block > end:
                    break
                range_end = min(end, next_block + MAX_BLOCK_RANGE - 1)
                request_id = len(batch) + 1
                batch.append((request_id, next_block, range_end))
                next_block = range_end + 1
            payload = []
            for request_id, start, stop in batch:
                payload.append(
                    {
                        "jsonrpc": "2.0",
                        "method": "eth_getLogs",
                        "params": [
                            {
                                "fromBlock": hex(start),
                                "toBlock": hex(stop),
                                "address": exchange.address,
                                "topics": [exchange.order_filled_topic, None, padded_wallet],
                            }
                        ],
                        "id": request_id,
                    }
                )
            response = _rpc(payload)
            if not isinstance(response, list):
                raise RuntimeError(f"Invalid batch response for {exchange.key}: {response}")
            by_id = {int(item.get("id")): item for item in response if isinstance(item, dict)}
            decoded: list[dict[str, Any]] = []
            for request_id, start, stop in batch:
                item = by_id.get(request_id)
                if item is None or item.get("error"):
                    raise RuntimeError(f"eth_getLogs failed for {exchange.key} {start}-{stop}: {item}")
                for log in item.get("result") or []:
                    decoded.append(decode_log(log, exchange, wallet))
            with connection:
                total_new += _insert_rows(connection, decoded)
                connection.execute(
                    """INSERT INTO scan_progress(exchange_key,last_block,head_block,updated_at)
                       VALUES(?,?,?,?)
                       ON CONFLICT(exchange_key) DO UPDATE SET
                         last_block=excluded.last_block,
                         head_block=excluded.head_block,
                         updated_at=excluded.updated_at""",
                    (exchange.key, batch[-1][2], head, int(time.time())),
                )
            done += len(batch)
            total_ranges += len(batch)
            print(
                json.dumps(
                    {
                        "status": "ONCHAIN_PROGRESS",
                        "exchange": exchange.key,
                        "ranges": f"{done}/{ranges}",
                        "last_block": batch[-1][2],
                        "fills_total": connection.execute("SELECT COUNT(*) FROM onchain_fills").fetchone()[0],
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )

    result = {
        "status": "COMPLETE",
        "wallet": wallet,
        "head_block": head,
        "coverage": "recent_partial" if recent_blocks else "full_contract_history",
        "requested_recent_blocks": recent_blocks,
        "ranges_scanned_this_run": total_ranges,
        "fills_inserted_this_run": total_new,
        "fills_total": connection.execute("SELECT COUNT(*) FROM onchain_fills").fetchone()[0],
        "maker_fills": connection.execute("SELECT COUNT(*) FROM onchain_fills WHERE role='MAKER'").fetchone()[0],
        "taker_fills": connection.execute("SELECT COUNT(*) FROM onchain_fills WHERE role='TAKER'").fetchone()[0],
    }
    connection.close()
    print(json.dumps(result, indent=2), flush=True)
    return result


def main(argv: list[str] | None = None) -> None:
    global RPC_URL
    parser = argparse.ArgumentParser(description="Indexa fills públicos de @car en Polygon")
    parser.add_argument("--wallet", default=DEFAULT_WALLET)
    parser.add_argument("--database", default="data/car_forensics/car_onchain.db")
    parser.add_argument("--rpc-url", default=RPC_URL)
    parser.add_argument(
        "--recent-blocks",
        type=int,
        default=None,
        help="Limita el escaneo a los últimos N bloques cuando el RPC público no conserva archivo.",
    )
    args = parser.parse_args(argv)
    RPC_URL = args.rpc_url
    scan(args.wallet, Path(args.database), recent_blocks=args.recent_blocks)


if __name__ == "__main__":
    main()
