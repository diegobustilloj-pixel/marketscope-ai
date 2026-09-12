from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v047_contract import build_preregistration
from polymarket_bot.v047_ws_census import census_v047


ROOT = Path(__file__).resolve().parent
PREREG = ROOT / "data" / "prereg_v047_ws_neg_risk_economic_census.json"
RESULT = ROOT / "data" / "resultado_v047_ws_neg_risk_economic_census.json"


def _status() -> dict[str, object]:
    result = json.loads(RESULT.read_text(encoding="utf-8")) if RESULT.is_file() else None
    census = result.get("census", {}) if result else {}
    return {
        "status": "COMPLETED" if result else ("PREREGISTERED" if PREREG.is_file() else "NOT_PREPARED"),
        "verdict": result.get("verdict") if result else None,
        "events_selected": result.get("sample", {}).get("events_selected") if result else None,
        "events_evaluated": census.get("events_evaluated"),
        "required_evaluated_events": census.get("required_evaluated_events"),
        "cost_candidates": census.get("cost_candidates"),
        "scheduled_supervision": False,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="V0.47 WebSocket NegRisk economic census")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--census", action="store_true")
    actions.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        payload = build_preregistration(output_path=PREREG, project_root=ROOT)
    elif args.census:
        payload = census_v047(prereg_path=PREREG, result_path=RESULT, project_root=ROOT)
    else:
        payload = _status()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
