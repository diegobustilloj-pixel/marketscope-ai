from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from polymarket_bot.config import Settings
from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v031_audit import audit_v031
from polymarket_bot.v031_runner import (
    V031RunnerError,
    build_implementation_manifest,
    load_and_verify_implementation,
    run_v031,
    v031_status,
)


PREREG = ROOT / "data" / "prereg_v031_path_execution_capture.json"
IMPLEMENTATION = ROOT / "data" / "implementation_v031_path_execution_capture.json"
LAUNCH = ROOT / "data" / "launch_approval_v031_path_execution_capture.json"
DATABASE = ROOT / "data" / "capture_v031_path_execution_1h.db"
RESULT = ROOT / "data" / "resultado_v031_path_execution_capture.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="V0.31 captura tecnica; sin outcomes, PnL, ordenes ni wallet."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true")
    action.add_argument("--build-implementation", action="store_true")
    action.add_argument("--run", "--monitor", dest="run", action="store_true")
    action.add_argument("--audit", action="store_true")
    parser.add_argument("--prereg", type=Path, default=PREREG)
    parser.add_argument("--implementation", type=Path, default=IMPLEMENTATION)
    parser.add_argument("--launch", type=Path, default=LAUNCH)
    parser.add_argument("--database", type=Path, default=DATABASE)
    parser.add_argument("--result", type=Path, default=RESULT)
    args = parser.parse_args()

    if args.build_implementation:
        payload = build_implementation_manifest(
            prereg_path=args.prereg,
            output_path=args.implementation,
        )
    elif args.status:
        payload = v031_status(args.database)
        if args.implementation.is_file():
            try:
                implementation = load_and_verify_implementation(args.implementation)
            except (V031RunnerError, OSError, ValueError) as exc:
                payload["implementation_status"] = "INVALID"
                payload["implementation_error"] = str(exc)
            else:
                payload["implementation_status"] = implementation["status"]
        else:
            payload["implementation_status"] = "NOT_BUILT"
        payload["launch_approval_exists"] = args.launch.is_file()
        payload["launch_status"] = (
            "AWAITING_EXPLICIT_APPROVAL"
            if not args.launch.is_file()
            else "APPROVAL_FILE_PRESENT_NOT_EVALUATED_BY_STATUS"
        )
    elif args.audit:
        payload = audit_v031(
            database=args.database,
            prereg_path=args.prereg,
            implementation_path=args.implementation,
            result_path=args.result,
        )
    else:
        payload = asyncio.run(
            run_v031(
                settings=Settings.from_env(),
                prereg_path=args.prereg,
                implementation_path=args.implementation,
                launch_path=args.launch,
                output_db=args.database,
            )
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
