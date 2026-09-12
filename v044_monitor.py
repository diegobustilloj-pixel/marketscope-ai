from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v044_audit import audit_v044
from polymarket_bot.v044_contract import build_preregistration


ROOT = Path(__file__).resolve().parent
PREREG = ROOT / "data" / "prereg_v044_short_horizon_repricing_rejection.json"
RESULT = ROOT / "data" / "resultado_v044_short_horizon_repricing_rejection.json"


def _status() -> dict[str, object]:
    result = json.loads(RESULT.read_text(encoding="utf-8")) if RESULT.is_file() else None
    return {
        "status": "COMPLETED" if result else ("PREREGISTERED" if PREREG.is_file() else "NOT_PREPARED"),
        "verdict": result.get("verdict") if result else None,
        "variants_tested": result.get("family", {}).get("variants_tested") if result else 0,
        "positive_net_variants": result.get("family", {}).get("positive_net_variants") if result else None,
        "fresh_capture_hours": 0.0,
        "scheduled_supervision": False,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="V0.44 closed repricing rejection audit")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--audit", action="store_true")
    actions.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        payload = build_preregistration(output_path=PREREG, project_root=ROOT)
    elif args.audit:
        payload = audit_v044(prereg_path=PREREG, result_path=RESULT, project_root=ROOT)
    else:
        payload = _status()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
