from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v050_contract import build_preregistration
from polymarket_bot.v050_partial_conversion_census import census_v050


ROOT = Path(__file__).resolve().parent
PREREG = ROOT / "data" / "prereg_v050_neg_risk_partial_conversion_cross_market_census.json"
RESULT = ROOT / "data" / "resultado_v050_neg_risk_partial_conversion_cross_market_census.json"


def _status() -> dict[str, object]:
    result = json.loads(RESULT.read_text(encoding="utf-8")) if RESULT.is_file() else None
    census = result.get("census", {}) if result else {}
    return {
        "status": "COMPLETED" if result else ("PREREGISTERED" if PREREG.is_file() else "NOT_PREPARED"),
        "verdict": result.get("verdict") if result else None,
        "events_selected": result.get("sample", {}).get("events_selected") if result else None,
        "events_synchronized": census.get("events_synchronized"),
        "required_synchronized_events": census.get("required_synchronized_events"),
        "subsets_screened": census.get("subsets_screened"),
        "subsets_economically_evaluated": census.get("subsets_economically_evaluated"),
        "subsets_non_executable": census.get("subsets_non_executable"),
        "cost_candidates_before_gas": census.get("cost_candidates_before_gas"),
        "scheduled_supervision": False,
        "orders_created": 0,
        "paper_orders": 0,
        "transactions_created": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="V0.50 NegRisk partial conversion census")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--census", action="store_true")
    actions.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        payload = build_preregistration(output_path=PREREG, project_root=ROOT)
    elif args.census:
        payload = census_v050(prereg_path=PREREG, result_path=RESULT, project_root=ROOT)
    else:
        payload = _status()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
