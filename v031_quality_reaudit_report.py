from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v031_quality_reaudit import (
    build_quality_implementation_manifest,
    load_and_verify_quality_implementation,
    load_and_verify_quality_prereg,
    reaudit_v031_quality,
)


PREREG = ROOT / "data" / "prereg_v031_quality_semantics_reaudit_v2.json"
IMPLEMENTATION = ROOT / "data" / "implementation_v031_quality_semantics_reaudit_v2.json"
RESULT = ROOT / "data" / "resultado_v031_quality_semantics_reaudit_v2.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "V0.31R reauditoria cerrada de calidad y liquidez unilateral; "
            "sin captura, outcomes, PnL, señales, órdenes ni wallet."
        )
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true")
    action.add_argument("--build-implementation", action="store_true")
    action.add_argument("--audit", action="store_true")
    parser.add_argument("--prereg", type=Path, default=PREREG)
    parser.add_argument("--implementation", type=Path, default=IMPLEMENTATION)
    parser.add_argument("--result", type=Path, default=RESULT)
    args = parser.parse_args()

    if args.build_implementation:
        payload = build_quality_implementation_manifest(
            prereg_path=args.prereg,
            output_path=args.implementation,
        )
    elif args.audit:
        payload = reaudit_v031_quality(
            prereg_path=args.prereg,
            implementation_path=args.implementation,
            result_path=args.result,
        )
    else:
        prereg = load_and_verify_quality_prereg(args.prereg)
        payload = {
            "variant": prereg["variant"],
            "preregistration_status": prereg["status"],
            "implementation_status": "NOT_BUILT",
            "result_status": "NOT_AUDITED",
            "new_capture_hours": 0,
            "outcomes_read": 0,
            "pnl_calculated": False,
            "orders_created": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
        if args.implementation.is_file():
            implementation = load_and_verify_quality_implementation(args.implementation)
            payload["implementation_status"] = implementation["status"]
        if args.result.is_file():
            result = json.loads(args.result.read_text(encoding="utf-8"))
            payload["result_status"] = "AUDITED"
            payload["verdict"] = result.get("verdict")
            payload["data_complete_v2_coverage"] = result.get(
                "quality_summary", {}
            ).get("data_complete_v2_coverage")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
