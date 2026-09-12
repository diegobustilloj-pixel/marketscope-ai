from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v054b_combo_public_census import census_v054b
from polymarket_bot.v054b_contract import build_preregistration


ROOT = Path(__file__).resolve().parent
PREREG = ROOT / "data" / "prereg_v054b_public_combo_binary_labels_census.json"
RESULT = ROOT / "data" / "resultado_v054b_public_combo_binary_labels_census.json"


def _status() -> dict[str, object]:
    result = json.loads(RESULT.read_text(encoding="utf-8")) if RESULT.is_file() else None
    catalog = result.get("catalog", {}) if result else {}
    observability = result.get("observability", {}) if result else {}
    return {
        "status": "COMPLETED" if result else ("PREREGISTERED" if PREREG.is_file() else "NOT_PREPARED"),
        "markets_received": catalog.get("markets_received"),
        "valid_markets": catalog.get("valid_markets"),
        "outcome_label_classes": catalog.get("outcome_label_classes"),
        "executable_combo_quote_observed": observability.get("executable_combo_bid_or_ask_observed"),
        "profitability_measured": observability.get("profitability_measured"),
        "verdict": result.get("verdict") if result else None,
        "new_capture_hours": 0.0,
        "scheduled_supervision": False,
        "orders_created": 0,
        "paper_orders": 0,
        "transactions_created": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="V0.54b corrected public combo census")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--census", action="store_true")
    actions.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        payload = build_preregistration(output_path=PREREG, project_root=ROOT)
    elif args.census:
        payload = census_v054b(prereg_path=PREREG, result_path=RESULT, project_root=ROOT)
    else:
        payload = _status()
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
