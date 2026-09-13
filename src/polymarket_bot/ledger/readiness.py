"""Fail-closed census of archived inputs proposed for a real P0 pilot.

The census never promotes legacy floating-point analytics to ledger evidence.
It only records what is present and names the missing proofs needed to build a
sealed public-capture bundle.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timezone, timedelta
from pathlib import Path

import duckdb

from . import SAFETY, VERSION
from .common import EvidenceError, address, now_utc, uint


def _sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _utc(value) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_columns(actual: set[str], required: set[str], source: str) -> None:
    missing = sorted(required - actual)
    if missing:
        raise EvidenceError(f"{source} missing required columns: {','.join(missing)}")


def audit_archived_pilot(activity: Path, onchain: Path, identity: Path, *, wallet: str,
                         window_days: int = 7) -> dict:
    """Describe legacy coverage without claiming completeness, basis or PnL."""
    activity, onchain, identity = Path(activity).resolve(), Path(onchain).resolve(), Path(identity).resolve()
    wallet, window_days = address(wallet), uint(window_days)
    if not 1 <= window_days <= 31:
        raise EvidenceError("Pilot census window must be 1..31 days")
    if not activity.is_file() or not onchain.is_file() or not identity.is_file():
        raise EvidenceError("Pilot identity, activity parquet and onchain SQLite files are required")
    identity_payload = json.loads(identity.read_text(encoding="utf-8"))
    if (identity_payload.get("verification_status") != "VERIFIED"
            or address(identity_payload.get("verified_proxy_wallet", "")) != wallet):
        raise EvidenceError("Identity artifact does not verify the requested wallet")
    before = {"activity": _sha256(activity), "onchain": _sha256(onchain), "identity": _sha256(identity)}

    analytical = duckdb.connect(":memory:")
    try:
        schema = {row[0] for row in analytical.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(activity)]).fetchall()}
        _require_columns(schema, {"timestamp_utc", "transaction_hash", "block", "event_type",
                                  "size", "price", "usdc_size", "lifecycle_action"}, "activity parquet")
        first, last, rows, transactions = analytical.execute(
            "SELECT min(timestamp_utc),max(timestamp_utc),count(*),"
            "count(DISTINCT transaction_hash) FROM read_parquet(?)", [str(activity)]).fetchone()
        if not rows or first is None or last is None:
            raise EvidenceError("Pilot activity parquet is empty")
        window_start = last - timedelta(days=window_days)
        recent = analytical.execute(
            "SELECT count(*),count(DISTINCT transaction_hash),"
            "count(*) FILTER (WHERE block IS NOT NULL),count(DISTINCT transaction_hash) "
            "FILTER (WHERE block IS NOT NULL),count(DISTINCT event_type),"
            "count(*) FILTER (WHERE lifecycle_action IS NOT NULL) "
            "FROM read_parquet(?) WHERE timestamp_utc>=? AND timestamp_utc<=?",
            [str(activity), window_start, last]).fetchone()
        recent_transactions = {row[0].lower() for row in analytical.execute(
            "SELECT DISTINCT transaction_hash FROM read_parquet(?) WHERE timestamp_utc>=? "
            "AND timestamp_utc<=? AND transaction_hash IS NOT NULL",
            [str(activity), window_start, last]).fetchall() if row[0]}
    finally:
        analytical.close()

    uri = onchain.as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as database:
        tables = {row[0] for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        _require_columns(tables, {"onchain_fills", "scan_progress"}, "onchain SQLite")
        fill_columns = {row[1] for row in database.execute("PRAGMA table_info(onchain_fills)")}
        _require_columns(fill_columns, {"transaction_hash", "log_index", "block_number", "raw_json",
                                                "maker", "taker"},
                         "onchain_fills")
        fills = database.execute(
            "SELECT count(*),count(DISTINCT transaction_hash),min(block_number),max(block_number) "
            "FROM onchain_fills").fetchone()
        onchain_transactions = {row[0].lower() for row in database.execute(
            "SELECT DISTINCT transaction_hash FROM onchain_fills WHERE transaction_hash IS NOT NULL")}
        wallet_fills = database.execute(
            "SELECT count(*) FROM onchain_fills WHERE lower(maker)=? OR lower(taker)=?", (wallet, wallet)).fetchone()[0]
        progress = [{"exchange": row[0], "last_block": row[1], "head_block": row[2], "updated_at": row[3]}
                    for row in database.execute(
                        "SELECT exchange_key,last_block,head_block,updated_at FROM scan_progress ORDER BY exchange_key")]

    sidecars = {}
    for suffix in ("-wal", "-shm"):
        path = Path(str(onchain) + suffix)
        if path.exists():
            sidecars[suffix[1:]] = {"bytes": path.stat().st_size, "sha256": _sha256(path)}
    after = {"activity": _sha256(activity), "onchain": _sha256(onchain), "identity": _sha256(identity)}
    stable = before == after and not sidecars.get("wal", {}).get("bytes", 0)
    linked = len(recent_transactions & onchain_transactions)
    recent_tx_count = int(recent[1])
    blockers = [
        "ARCHIVE_HAS_NO_ACQUISITION_COMPLETENESS_MANIFEST",
        "OPENING_INVENTORY_AND_ATOMIC_BASIS_MISSING",
        "FULL_ONCHAIN_LIFECYCLE_CAPTURE_MISSING",
        "CLOB_ORDERS_AND_FILL_COMPLETENESS_MISSING",
        "SAME_CUT_CLOSING_BALANCES_MISSING",
        "INDEPENDENT_PNL_PIPELINE_MISSING",
        "LEGACY_FLOAT_AMOUNTS_NOT_LEDGER_EVIDENCE",
    ]
    if not stable:
        blockers.append("INPUT_FILES_OR_SQLITE_WAL_NOT_STABLE")
    if not wallet_fills:
        blockers.append("ONCHAIN_ARCHIVE_HAS_NO_REQUESTED_WALLET")
    return {
        "schema": 1,
        "version": VERSION,
        "generated_at": now_utc(),
        "status": "BLOCKED",
        "wallet": wallet,
        "identity": {"path": str(identity), "sha256": after["identity"],
                     "verification_status": identity_payload["verification_status"],
                     "verified_proxy_wallet": wallet,
                     "profile_url": identity_payload.get("verified_profile_url")},
        "requested_window_days": window_days,
        "window": {"start": _utc(window_start), "end": _utc(last), "seconds": window_days * 86400},
        "activity_archive": {
            "path": str(activity), "sha256": after["activity"], "first": _utc(first), "last": _utc(last),
            "rows": int(rows), "transactions": int(transactions), "legacy_numeric_type": "DOUBLE",
            "window_rows": int(recent[0]), "window_transactions": recent_tx_count,
            "window_rows_with_onchain_block": int(recent[2]),
            "window_transactions_with_onchain_block": int(recent[3]),
            "window_event_types": int(recent[4]), "window_rows_with_lifecycle_label": int(recent[5]),
        },
        "onchain_fill_archive": {
            "path": str(onchain), "sha256": after["onchain"], "sidecars": sidecars,
            "stable_read": stable, "fills": int(fills[0]), "transactions": int(fills[1]),
            "wallet_fills": int(wallet_fills),
            "first_block": fills[2], "last_block": fills[3], "scan_progress": progress,
            "recent_activity_transactions_linked": linked,
            "recent_activity_transaction_link_ratio": (
                f"{linked / recent_tx_count:.9f}" if recent_tx_count else "0.000000000"),
            "scope": "OrderFilled only; not complete transfers, splits, merges, conversions or redemptions",
        },
        "gates": {
            "timestamp_span_present": first <= window_start <= last,
            "archive_complete": False,
            "opening_basis_complete": False,
            "onchain_lifecycle_complete": False,
            "clob_complete": False,
            "closing_balances_complete": False,
            "independent_pnl_complete": False,
            "atomic_accounting_inputs": False,
        },
        "blockers": sorted(blockers),
        "decision": "Use archive for discovery only; acquire a new sealed public-capture bundle before P0 replay.",
        "p0_exit_allowed": False,
        "execution_allowed": False,
        "safety": SAFETY,
    }
