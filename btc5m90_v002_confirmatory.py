from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from polymarket_bot.btc_5m_90.v002_confirmatory import (
    DATABASE_PATH,
    PREREG_PATH,
    freeze_prereg,
    run_confirmatory,
    run_smoke,
    status,
)
from polymarket_bot.config import Settings
from polymarket_bot.system_guard import prevent_system_sleep


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cohorte confirmatoria BTC5M90 V002, 24h máximo, paper-only"
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--smoke-seconds", type=float)
    action.add_argument("--monitor", action="store_true")
    action.add_argument("--status", action="store_true")
    parser.add_argument("--prereg", type=Path, default=PREREG_PATH)
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.prepare:
        payload = freeze_prereg(args.prereg)
    elif args.smoke_seconds is not None:
        payload = asyncio.run(run_smoke(Settings.from_env(), args.smoke_seconds))
    elif args.monitor:
        with prevent_system_sleep(True):
            payload = asyncio.run(
                run_confirmatory(Settings.from_env(), args.prereg, args.database)
            )
    else:
        payload = status(args.database)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("status") not in {"FAILED", "SMOKE_NO_BOOK"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
