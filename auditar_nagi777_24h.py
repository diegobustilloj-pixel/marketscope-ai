from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.wallet_window import (
    ROOT,
    WalletWindowError,
    analyze_snapshot,
    capture_snapshot,
    sha256_file,
    validate_preregistration,
    write_json_exclusive_or_verify,
)


PREREG = ROOT / "data" / "prereg_nagi777_wallet24h_20260814.json"
SNAPSHOT = ROOT / "data" / "nagi777_activity_24h_20260814.json"
RESULT = ROOT / "data" / "resultado_nagi777_wallet24h_20260814.json"


def _load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise WalletWindowError(f"JSON incompatible: {path}")
    return payload


def _status() -> dict:
    payload = {
        "status": "READY_TO_CAPTURE" if not SNAPSHOT.exists() else "CAPTURED",
        "prereg_exists": PREREG.exists(),
        "snapshot_exists": SNAPSHOT.exists(),
        "result_exists": RESULT.exists(),
        "active_forward_read": False,
        "active_forward_modified": False,
        "market_outcomes_read": False,
        "orders_enabled": False,
        "real_money": "BLOQUEADO",
    }
    if SNAPSHOT.exists():
        snapshot = _load(SNAPSHOT)
        payload["activity_count"] = snapshot.get("activity_count")
        payload["window"] = snapshot.get("window")
        payload["snapshot_sha256"] = sha256_file(SNAPSHOT)
    if RESULT.exists():
        result = _load(RESULT)
        payload["verdict"] = result.get("verdict")
        payload["metrics"] = result.get("metrics")
    return payload


def _capture() -> dict:
    prereg = _load(PREREG)
    validate_preregistration(prereg)
    if SNAPSHOT.exists():
        snapshot = _load(SNAPSHOT)
        snapshot_status = "VERIFIED_EXISTING"
    else:
        snapshot = capture_snapshot(prereg)
        snapshot_status = write_json_exclusive_or_verify(SNAPSHOT, snapshot)

    result = analyze_snapshot(snapshot)
    result["snapshot_sha256"] = sha256_file(SNAPSHOT)
    result["prereg_sha256"] = sha256_file(PREREG)
    result_status = write_json_exclusive_or_verify(RESULT, result)
    return {
        "status": "OK",
        "snapshot_status": snapshot_status,
        "result_status": result_status,
        "snapshot": str(SNAPSHOT),
        "result": str(RESULT),
        "activity_count": snapshot["activity_count"],
        "verdict": result["verdict"],
        "metrics": result["metrics"],
        "active_forward_read": False,
        "active_forward_modified": False,
        "market_outcomes_read": False,
        "orders_enabled": False,
        "real_money": "BLOQUEADO",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--capture", action="store_true")
    group.add_argument("--status", action="store_true")
    args = parser.parse_args()
    try:
        payload = _status() if args.status else _capture()
    except (OSError, json.JSONDecodeError, WalletWindowError) as exc:
        parser.exit(2, f"ERROR WALLET WINDOW: {exc}\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
