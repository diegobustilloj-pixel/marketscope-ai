from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v032_exit_safety import (
    build_v032_implementation_manifest,
    load_and_verify_v032_implementation,
    load_and_verify_v032_prereg,
    run_v032_exit_safety_screen,
)


PREREG = ROOT / "data" / "prereg_v032_exit_safety_capacity.json"
IMPLEMENTATION = ROOT / "data" / "implementation_v032_exit_safety_capacity.json"
RESULT = ROOT / "data" / "resultado_v032_exit_safety_capacity.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "V0.32 pantalla cerrada de salida obligatoria; sin señal, outcomes, "
            "PnL, operaciones, órdenes ni wallet."
        )
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true")
    action.add_argument("--build-implementation", action="store_true")
    action.add_argument("--screen", action="store_true")
    parser.add_argument("--prereg", type=Path, default=PREREG)
    parser.add_argument("--implementation", type=Path, default=IMPLEMENTATION)
    parser.add_argument("--result", type=Path, default=RESULT)
    args = parser.parse_args()

    if args.build_implementation:
        payload = build_v032_implementation_manifest(
            prereg_path=args.prereg,
            output_path=args.implementation,
        )
    elif args.screen:
        payload = run_v032_exit_safety_screen(
            prereg_path=args.prereg,
            implementation_path=args.implementation,
            result_path=args.result,
        )
    else:
        prereg = load_and_verify_v032_prereg(args.prereg)
        payload = {
            "variant": prereg["variant"],
            "preregistration_status": prereg["status"],
            "implementation_status": "NOT_BUILT",
            "result_status": "NOT_SCREENED",
            "new_capture_hours": 0,
            "outcomes_read": 0,
            "pnl_calculated": False,
            "orders_created": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
        if args.implementation.is_file():
            implementation = load_and_verify_v032_implementation(args.implementation)
            payload["implementation_status"] = implementation["status"]
        if args.result.is_file():
            result = json.loads(args.result.read_text(encoding="utf-8"))
            payload["result_status"] = "SCREENED"
            payload["verdict"] = result.get("verdict")
            payload["entry_eligible_probes"] = result.get("overall", {}).get(
                "entry_eligible_probes"
            )
            payload["trapped_positions"] = result.get("overall", {}).get(
                "trapped_positions"
            )
            payload["exit_success_within_grace_rate"] = result.get(
                "overall", {}
            ).get("exit_success_within_grace_rate")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
