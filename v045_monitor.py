from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v045_census import census_v045
from polymarket_bot.v045_contract import build_preregistration


ROOT = Path(__file__).resolve().parent
PREREG = ROOT / "data" / "prereg_v045_neg_risk_structural_census.json"
RESULT = ROOT / "data" / "resultado_v045_neg_risk_structural_census.json"


def _status() -> dict[str, object]:
    result = json.loads(RESULT.read_text(encoding="utf-8")) if RESULT.is_file() else None
    return {
        "status": "COMPLETED" if result else ("PREREGISTERED" if PREREG.is_file() else "NOT_PREPARED"),
        "verdict": result.get("verdict") if result else None,
        "events_received": result.get("census", {}).get("events_received") if result else None,
        "events_evaluated": result.get("census", {}).get("events_evaluated_with_full_books") if result else None,
        "cost_candidates": result.get("census", {}).get("cost_candidates") if result else None,
        "scheduled_supervision": False,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="V0.45 NegRisk structural feasibility census")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--census", action="store_true")
    actions.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        payload = build_preregistration(output_path=PREREG, project_root=ROOT)
    elif args.census:
        payload = census_v045(prereg_path=PREREG, result_path=RESULT, project_root=ROOT)
    else:
        payload = _status()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
