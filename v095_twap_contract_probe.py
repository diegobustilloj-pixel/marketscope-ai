from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from polymarket_bot.config import Settings
from polymarket_bot.twap_contract_v095 import (
    finalize_probe_status,
    probe_status,
    run_twap_contract_probe,
)
from polymarket_bot.v018_runner import ROOT


DEFAULT_DB = ROOT / "data" / "captura_tecnica_twap_contract_v095.db"
DEFAULT_JSON = ROOT / "data" / "resultado_captura_tecnica_twap_contract_v095.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-seconds", type=float, default=45.0)
    parser.add_argument("--output-db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--finalize-status", action="store_true")
    args = parser.parse_args()
    if args.status and args.finalize_status:
        parser.error("Use sólo --status o --finalize-status")
    if args.finalize_status:
        payload = finalize_probe_status(
            database=args.output_db,
            output_json=args.output_json,
        )
    elif args.status:
        payload = probe_status(args.output_db)
    else:
        payload = asyncio.run(
            run_twap_contract_probe(
                settings=Settings.from_env(),
                output_db=args.output_db,
                output_json=args.output_json,
                duration_seconds=args.duration_seconds,
            )
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
