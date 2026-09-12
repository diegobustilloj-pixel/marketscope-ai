from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from polymarket_bot.config import Settings
from polymarket_bot.system_guard import prevent_system_sleep
from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v025_audit import audit_v025
from polymarket_bot.v025_runner import (
    MAXIMUM_HOURS,
    TARGET_PRIMARY_TRADES,
    VARIANT,
    load_and_verify_prereg,
    run_v025,
    v025_status,
)


PREREG = ROOT / "data" / "prereg_v025_down_asymmetry.json"
DATABASE = ROOT / "data" / "paper_v025_down_asymmetry.db"
PHASE4 = ROOT / "data" / "fase4_modelos.db"
RESULT = ROOT / "data" / "resultado_v025_down_asymmetry.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--monitor", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--audit", action="store_true")
    group.add_argument("--check", action="store_true")
    parser.add_argument("--prereg", type=Path, default=PREREG)
    parser.add_argument("--database", type=Path, default=DATABASE)
    parser.add_argument("--phase4-db", type=Path, default=PHASE4)
    parser.add_argument("--result", type=Path, default=RESULT)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.status:
        payload = v025_status(args.database)
    elif args.audit:
        payload = audit_v025(
            database=args.database,
            prereg_path=args.prereg,
            phase4_db=args.phase4_db,
            result_path=args.result,
        )
    elif args.check:
        prereg = load_and_verify_prereg(args.prereg)
        payload = {
            "status": "READY",
            "variant": VARIANT,
            "maximum_hours": MAXIMUM_HOURS,
            "target_resolved_primary_trades": TARGET_PRIMARY_TRADES,
            "arms": [item["id"] for item in prereg["arms"]],
            "preregistration_sha256": sha256_file(args.prereg),
            "final_result_only": True,
            "orders_created": False,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
    else:
        with prevent_system_sleep(True):
            run_payload = asyncio.run(
                run_v025(
                    settings=Settings.from_env(),
                    prereg_path=args.prereg,
                    output_db=args.database,
                )
            )
        if run_payload.get("status") == "COMPLETED":
            payload = audit_v025(
                database=args.database,
                prereg_path=args.prereg,
                phase4_db=args.phase4_db,
                result_path=args.result,
            )
        else:
            payload = run_payload
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("status") not in {"FAILED", "SAFETY_STOP"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
