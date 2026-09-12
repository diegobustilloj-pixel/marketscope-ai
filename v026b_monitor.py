from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from polymarket_bot.config import Settings
from polymarket_bot.system_guard import prevent_system_sleep
from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v026b_audit import audit_v026b
from polymarket_bot.v026b_runner import (
    CHECKPOINT_HOURS,
    MAXIMUM_HOURS,
    VARIANT,
    load_and_verify_prereg,
    run_v026b,
    v026b_status,
)


PREREG = ROOT / "data" / "prereg_v026b_adaptive_checkpoints.json"
DATABASE = ROOT / "data" / "paper_v026b_adaptive_checkpoints.db"
RESULT = ROOT / "data" / "resultado_v026b_adaptive_checkpoints.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--monitor", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--audit", action="store_true")
    group.add_argument("--check", action="store_true")
    parser.add_argument("--prereg", type=Path, default=PREREG)
    parser.add_argument("--database", type=Path, default=DATABASE)
    parser.add_argument("--result", type=Path, default=RESULT)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.status:
        payload = v026b_status(args.database)
    elif args.audit:
        payload = audit_v026b(
            database=args.database,
            prereg_path=args.prereg,
            result_path=args.result,
        )
    elif args.check:
        prereg = load_and_verify_prereg(args.prereg)
        payload = {
            "status": "READY",
            "variant": VARIANT,
            "maximum_hours": MAXIMUM_HOURS,
            "checkpoint_hours": list(CHECKPOINT_HOURS),
            "early_success_allowed": False,
            "early_stop_for_futility_only": True,
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
                run_v026b(
                    settings=Settings.from_env(),
                    prereg_path=args.prereg,
                    output_db=args.database,
                )
            )
        if run_payload.get("completion_reason") is not None:
            payload = audit_v026b(
                database=args.database,
                prereg_path=args.prereg,
                result_path=args.result,
            )
        else:
            payload = run_payload
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("status") not in {"FAILED", "SAFETY_STOP"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
