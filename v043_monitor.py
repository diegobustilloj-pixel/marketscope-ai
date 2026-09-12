from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v043_audit import audit_v043
from polymarket_bot.v043_runner import (
    V043RunnerError,
    build_implementation_manifest,
    load_and_verify_implementation,
)


PREREG = ROOT / "data" / "prereg_v043_chainlink_outage_fail_closed_audit.json"
IMPLEMENTATION = (
    ROOT / "data" / "implementation_v043_chainlink_outage_fail_closed_audit.json"
)
RESULT = ROOT / "data" / "resultado_v043_chainlink_outage_fail_closed_audit.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "V0.43 audita el fallo cerrado durante la interrupcion V0.42; "
            "no ejecuta otra captura ni lee precios, outcomes o PnL."
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
        payload = build_implementation_manifest(
            prereg_path=args.prereg,
            output_path=args.implementation,
        )
    elif args.audit:
        payload = audit_v043(
            prereg_path=args.prereg,
            implementation_path=args.implementation,
            result_path=args.result,
        )
    else:
        payload = {
            "variant": "V0.43_CHAINLINK_OUTAGE_FAIL_CLOSED_AUDIT",
            "status": "COMPLETED" if args.result.is_file() else "NOT_AUDITED",
            "fresh_capture_hours": 0.0,
            "scheduled_supervision": False,
            "orders_created": 0,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
        if args.implementation.is_file():
            try:
                implementation = load_and_verify_implementation(
                    args.implementation
                )
            except (V043RunnerError, OSError, ValueError) as exc:
                payload["implementation_status"] = "INVALID"
                payload["implementation_error"] = str(exc)
            else:
                payload["implementation_status"] = implementation["status"]
        else:
            payload["implementation_status"] = "NOT_BUILT"
        payload["final_result_exists"] = args.result.is_file()
        if args.result.is_file():
            final = json.loads(args.result.read_text(encoding="utf-8"))
            payload["final_verdict"] = final.get("verdict")
            payload["technical_passed"] = final.get("technical_passed")
            payload["provider_liveness_claimed"] = final.get(
                "v042_official_result", {}
            ).get("provider_liveness_claimed_by_v043")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
