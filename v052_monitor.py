from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v052_contract import (
    build_discovery_preregistration,
    build_economic_lock,
)
from polymarket_bot.v052_linked_threshold_census import (
    census_v052,
    discover_semantic_relationships,
)


ROOT = Path(__file__).resolve().parent
DISCOVERY_PREREG = ROOT / "data" / "prereg_v052_linked_threshold_semantic_discovery.json"
RELATIONSHIPS = ROOT / "data" / "relaciones_v052_linked_threshold_semantic.json"
ECONOMIC_LOCK = ROOT / "data" / "prereg_v052_linked_threshold_economic_lock.json"
RESULT = ROOT / "data" / "resultado_v052_linked_threshold_taker_floor_census.json"


def _status() -> dict[str, object]:
    semantic = (
        json.loads(RELATIONSHIPS.read_text(encoding="utf-8"))
        if RELATIONSHIPS.is_file()
        else None
    )
    result = json.loads(RESULT.read_text(encoding="utf-8")) if RESULT.is_file() else None
    census = result.get("census", {}) if result else {}
    if result:
        status = "COMPLETED"
    elif ECONOMIC_LOCK.is_file():
        status = "ECONOMICS_LOCKED"
    elif semantic:
        status = "SEMANTICS_FROZEN"
    elif DISCOVERY_PREREG.is_file():
        status = "DISCOVERY_PREREGISTERED"
    else:
        status = "NOT_PREPARED"
    return {
        "status": status,
        "semantic_relationships": (
            len(semantic.get("relationships", [])) if semantic else None
        ),
        "verdict": result.get("verdict") if result else None,
        "markets_clob_validated": (
            result.get("sample", {}).get("markets_clob_validated") if result else None
        ),
        "relationships_evaluated": census.get("relationships_evaluated"),
        "relationships_non_executable": census.get("relationships_non_executable"),
        "cost_candidates_before_gas": census.get("cost_candidates_before_gas"),
        "new_capture_hours": 0.0,
        "scheduled_supervision": False,
        "orders_created": 0,
        "paper_orders": 0,
        "transactions_created": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="V0.52 linked-threshold two-stage census")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--discover", action="store_true")
    actions.add_argument("--lock", action="store_true")
    actions.add_argument("--census", action="store_true")
    actions.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        payload = build_discovery_preregistration(
            output_path=DISCOVERY_PREREG, project_root=ROOT
        )
    elif args.discover:
        payload = discover_semantic_relationships(
            prereg_path=DISCOVERY_PREREG,
            output_path=RELATIONSHIPS,
            project_root=ROOT,
        )
    elif args.lock:
        payload = build_economic_lock(
            discovery_prereg_path=DISCOVERY_PREREG,
            relationships_path=RELATIONSHIPS,
            output_path=ECONOMIC_LOCK,
            project_root=ROOT,
        )
    elif args.census:
        payload = census_v052(
            economic_lock_path=ECONOMIC_LOCK,
            result_path=RESULT,
            project_root=ROOT,
        )
    else:
        payload = _status()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
