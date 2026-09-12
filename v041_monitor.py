from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from polymarket_bot.config import Settings
from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v041_audit import audit_v041
from polymarket_bot.v041_runner import (
    V041RunnerError,
    build_implementation_manifest,
    load_and_verify_implementation,
    run_v041,
    run_v041_chainlink_preflight,
    v041_status,
)


PREREG = ROOT / "data" / "prereg_v041_chainlink_silence_watchdog_4h.json"
IMPLEMENTATION = ROOT / "data" / "implementation_v041_chainlink_silence_watchdog_4h.json"
LAUNCH = ROOT / "data" / "launch_approval_v041_chainlink_silence_watchdog_4h.json"
DATABASE = ROOT / "data" / "capture_v041_chainlink_silence_watchdog_4h.db"
RESULT = ROOT / "data" / "resultado_v041_chainlink_silence_watchdog_4h.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "V0.41 vigila silencios de Chainlink y conserva íntegra la salida V0.40; "
            "sin precios, outcomes, PnL ni órdenes."
        )
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true")
    action.add_argument("--build-implementation", action="store_true")
    action.add_argument("--run", "--monitor", dest="run", action="store_true")
    action.add_argument("--audit", action="store_true")
    action.add_argument("--preflight", action="store_true")
    parser.add_argument("--preflight-seconds", type=float, default=45.0)
    parser.add_argument("--prereg", type=Path, default=PREREG)
    parser.add_argument("--implementation", type=Path, default=IMPLEMENTATION)
    parser.add_argument("--launch", type=Path, default=LAUNCH)
    parser.add_argument("--database", type=Path, default=DATABASE)
    parser.add_argument("--result", type=Path, default=RESULT)
    args = parser.parse_args()
    if args.build_implementation:
        payload = build_implementation_manifest(
            prereg_path=args.prereg, output_path=args.implementation
        )
    elif args.preflight:
        payload = asyncio.run(run_v041_chainlink_preflight(
            settings=Settings.from_env(), seconds=args.preflight_seconds
        ))
    elif args.status:
        payload = v041_status(args.database)
        if args.implementation.is_file():
            try:
                implementation = load_and_verify_implementation(args.implementation)
            except (V041RunnerError, OSError, ValueError) as exc:
                payload["implementation_status"] = "INVALID"
                payload["implementation_error"] = str(exc)
            else:
                payload["implementation_status"] = implementation["status"]
        else:
            payload["implementation_status"] = "NOT_BUILT"
        payload["launch_approval_exists"] = args.launch.is_file()
        payload["final_result_exists"] = args.result.is_file()
        if args.result.is_file():
            payload["final_verdict"] = json.loads(
                args.result.read_text(encoding="utf-8")
            ).get("verdict")
    elif args.audit:
        payload = audit_v041(
            database=args.database, prereg_path=args.prereg,
            implementation_path=args.implementation, result_path=args.result,
        )
    else:
        capture = asyncio.run(
            run_v041(
                settings=Settings.from_env(), prereg_path=args.prereg,
                implementation_path=args.implementation, launch_path=args.launch,
                output_db=args.database,
            )
        )
        payload = {"capture": capture}
        if capture.get("completion_reason") is not None:
            payload["final_audit"] = audit_v041(
                database=args.database, prereg_path=args.prereg,
                implementation_path=args.implementation, result_path=args.result,
            )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
