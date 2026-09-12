from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from polymarket_bot.config import Settings
from polymarket_bot.system_guard import prevent_system_sleep
from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v023_audit import audit_v023
from polymarket_bot.v023_runner import load_and_verify_prereg, run_v023, v023_status


PREREG = ROOT / "data" / "prereg_v023_filtered_forward_4h.json"
DATABASE = ROOT / "data" / "paper_v023_filtered_forward_4h.db"
PHASE4 = ROOT / "data" / "fase4_modelos.db"
RESULT = ROOT / "data" / "resultado_v023_filtered_forward_4h.json"


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
        payload = v023_status(args.database)
    elif args.audit:
        payload = audit_v023(
            database=args.database,
            prereg_path=args.prereg,
            phase4_db=args.phase4_db,
            result_path=args.result,
        )
    elif args.check:
        prereg = load_and_verify_prereg(args.prereg)
        payload = {
            "status": "READY",
            "variant": "V0.23_FILTERED_FORWARD_4H",
            "target_hours": prereg["target_hours"],
            "expected_markets": 48,
            "preregistration_sha256": sha256_file(args.prereg),
            "orders_created": False,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
    else:
        with prevent_system_sleep(True):
            payload = asyncio.run(
                run_v023(
                    settings=Settings.from_env(),
                    prereg_path=args.prereg,
                    output_db=args.database,
                )
            )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("status") not in {"FAILED", "SAFETY_STOP"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
