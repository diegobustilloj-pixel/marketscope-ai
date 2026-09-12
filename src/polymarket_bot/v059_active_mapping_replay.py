from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v056_rfq_observer import credential_preflight
from polymarket_bot.v057_joined_replay import collect_joined_replay, fetch_public_books
from polymarket_bot.v058_mapped_replay import (
    DDL,
    V058Store,
    V058StorageError,
    fetch_position_map,
    load_position_seed,
)
from polymarket_bot.v059_contract import VARIANT, load_and_verify_preregistration


class V059MappingError(RuntimeError):
    pass


class V059ReplayError(RuntimeError):
    pass


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class V059Store(V058Store):
    def open_new(self, preregistration_sha256: str, duration_seconds: int) -> None:
        if self.path.exists():
            raise V058StorageError("La base V0.59 ya existe; no se reanuda ni sobrescribe")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=60)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(f"PRAGMA wal_autocheckpoint={int(self.storage['wal_autocheckpoint_pages'])}")
        self.connection.executescript(DDL)
        values = {
            "schema_version": "1",
            "variant": VARIANT,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "preregistration_sha256": preregistration_sha256,
            "duration_seconds": int(duration_seconds),
            "completion_reason": None,
            "mapping_summary": self.mapping_summary,
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "quote_submission_enabled": False,
            "signatures_enabled": False,
            "transactions_enabled": False,
            "credential_values_stored": False,
            "addresses_stored": False,
            "raw_payloads_stored": False,
            "realized_pnl_measured": False,
            "real_money": "BLOQUEADO",
        }
        self.connection.executemany(
            "INSERT INTO v058_meta(key,value) VALUES(?,?)",
            [(key, _compact(value)) for key, value in values.items()],
        )
        self.connection.executemany(
            "INSERT INTO v058_position_map VALUES(?,?,?,?)",
            [
                (record["position_id"], record["clob_token_id"], record["condition_id"], record["outcome_index"])
                for record in self.position_map.values()
            ],
        )
        self.connection.commit()


async def run_v059_async(
    *,
    prereg_path: str | Path,
    database_path: str | Path,
    project_root: str | Path = ROOT,
    environ: Mapping[str, str] | None = None,
    connect_factory: Callable[..., Any] = connect,
    book_fetcher: Callable[[str, list[str], float], Any] = fetch_public_books,
    mapping_fetcher: Callable[..., tuple[dict[str, dict[str, Any]], dict[str, Any]]] = fetch_position_map,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    prereg = load_and_verify_preregistration(prereg_file, project_root=root)
    credentials, preflight = credential_preflight(prereg["contract"], environ=environ)
    if credentials is None:
        return {
            "status": "BLOCKED_CREDENTIAL_PREFLIGHT",
            "preflight": preflight,
            "database_created": False,
            "network_connection_attempted": False,
            "orders_created": 0,
            "paper_orders": 0,
            "transactions_created": 0,
            "real_money": "BLOQUEADO",
        }
    mapping_contract = prereg["contract"]["mapping"]
    try:
        position_seed, seed_summary = load_position_seed(root / str(mapping_contract["position_seed_path"]))
        if len(position_seed) < int(mapping_contract["minimum_seed_positions"]):
            raise V059MappingError("V059_POSITION_SEED_TOO_SMALL")
        position_map, mapping_summary = await asyncio.to_thread(
            mapping_fetcher,
            str(mapping_contract["endpoint"]),
            dict(mapping_contract["query"]),
            position_seed,
            str(mapping_contract["position_ids_parameter"]),
            int(mapping_contract["position_ids_per_batch"]),
            int(mapping_contract["maximum_batches"]),
            float(mapping_contract["request_timeout_seconds"]),
            int(mapping_contract["maximum_attempts_per_batch"]),
            list(mapping_contract["retry_backoff_seconds"]),
        )
        mapping_summary = {**mapping_summary, "seed": seed_summary, "closed_markets_included": False}
        if int(mapping_summary["markets"]) < int(mapping_contract["minimum_markets"]):
            raise V059MappingError("GAMMA_MAPPING_TOO_SMALL")
        if float(mapping_summary["seed_resolution_rate"]) < float(mapping_contract["minimum_seed_resolution_rate"]):
            raise V059MappingError("GAMMA_ACTIVE_SEED_RESOLUTION_RATE_TOO_LOW")
    except Exception as exc:
        return {
            "status": "BLOCKED_ACTIVE_POSITION_MAPPING_PREFLIGHT",
            "mapping_error_class": type(exc).__name__,
            "mapping_error_code": str(exc)[:128],
            "database_created": False,
            "network_connection_attempted": True,
            "credentials_sent_to_mapping": False,
            "orders_created": 0,
            "paper_orders": 0,
            "transactions_created": 0,
            "real_money": "BLOQUEADO",
        }
    database = Path(database_path).resolve()
    store = V059Store(database, prereg["contract"]["storage"], position_map, mapping_summary)
    store.open_new(sha256_file(prereg_file), int(prereg["contract"]["transport"]["duration_seconds"]))
    run_id = store.start_run()
    try:
        result = await collect_joined_replay(
            endpoint=str(prereg["contract"]["transport"]["rfq_endpoint"]),
            credentials=credentials,
            store=store,
            run_id=run_id,
            contract=prereg["contract"],
            connect_factory=connect_factory,
            book_fetcher=book_fetcher,
            duration_seconds=duration_seconds,
        )
        error = V059ReplayError(str(result["terminal_error_code"])) if result.get("terminal_error_class") else None
        store.finish(run_id, str(result["status"]), error, int(result["reconnects"]))
        return {
            **result,
            "mapping": mapping_summary,
            "preflight": {**preflight, "database_created": True},
            "database_created": True,
            "network_connection_attempted": True,
            "database_path": str(database),
            "real_money": "BLOQUEADO",
        }
    except BaseException as exc:
        store.finish(run_id, "INTERRUPTED", exc, 0)
        raise
    finally:
        store.close()


def run_v059(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_v059_async(**kwargs))


__all__ = ["V059MappingError", "V059ReplayError", "V059Store", "run_v059", "run_v059_async"]
