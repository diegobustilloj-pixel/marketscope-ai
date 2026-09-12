from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from polymarket_bot.config import Settings
from polymarket_bot.system_guard import prevent_system_sleep
from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v020_runner import run_smoke, run_v020, v020_status


PREREG = ROOT / "data" / "prereg_v020_mandatory_hedge_paper.json"
DATABASE = ROOT / "data" / "paper_v020_mandatory_hedge.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--monitor", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--smoke-seconds", type=float)
    parser.add_argument("--prereg", type=Path, default=PREREG)
    parser.add_argument("--database", type=Path, default=DATABASE)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.status:
        payload = v020_status(args.database)
    elif args.smoke_seconds is not None:
        payload = asyncio.run(run_smoke(Settings.from_env(), args.smoke_seconds))
    else:
        with prevent_system_sleep(True):
            payload = asyncio.run(run_v020(settings=Settings.from_env(), prereg_path=args.prereg, output_db=args.database))
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("status") not in {"FAILED", "SMOKE_NO_BOOK"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
