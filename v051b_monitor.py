from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v051b_contract import build_preregistration
from polymarket_bot.v051b_prefunded_maker_census import census_v051b


ROOT = Path(__file__).resolve().parent
PREREG = ROOT / "data" / "prereg_v051b_prefunded_maker_transport_correction_census.json"
RESULT = ROOT / "data" / "resultado_v051b_prefunded_maker_transport_correction_census.json"


def _status() -> dict[str, object]:
    result = json.loads(RESULT.read_text(encoding="utf-8")) if RESULT.is_file() else None
    census = result.get("census", {}) if result else {}
    return {
        "status": "COMPLETED" if result else ("PREREGISTERED" if PREREG.is_file() else "NOT_PREPARED"),
        "verdict": result.get("verdict") if result else None,
        "markets_selected": result.get("sample", {}).get("markets_selected") if result else None,
        "markets_synchronized": census.get("markets_synchronized"),
        "required_synchronized_markets": census.get("required_synchronized_markets"),
        "directions_screened": census.get("directions_screened"),
        "directions_economically_evaluated": census.get("directions_economically_evaluated"),
        "directions_non_executable": census.get("directions_non_executable"),
        "cost_candidates_before_gas": census.get("cost_candidates_before_gas"),
        "scheduled_supervision": False,
        "orders_created": 0,
        "paper_orders": 0,
        "transactions_created": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="V0.51b corrected prefunded maker census")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--census", action="store_true")
    actions.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        payload = build_preregistration(output_path=PREREG, project_root=ROOT)
    elif args.census:
        payload = census_v051b(prereg_path=PREREG, result_path=RESULT, project_root=ROOT)
    else:
        payload = _status()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
