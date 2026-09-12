from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v056_rfq_observer import credential_preflight
from polymarket_bot.v057_audit import audit_v057, inspect_database
from polymarket_bot.v057_contract import build_preregistration, load_and_verify_preregistration
from polymarket_bot.v057_joined_replay import run_v057


ROOT = Path(__file__).resolve().parent
PREREG = ROOT / "data" / "prereg_v057_rfq_clob_joined_replay.json"
DATABASE = ROOT / "data" / "v057_rfq_clob_joined_replay.db"
RESULT = ROOT / "data" / "resultado_v057_rfq_clob_joined_replay.json"


def _preflight() -> dict[str, object]:
    prereg = load_and_verify_preregistration(PREREG, project_root=ROOT)
    _, report = credential_preflight(prereg["contract"])
    return report


def _status() -> dict[str, object]:
    if not PREREG.is_file():
        state: dict[str, object] = {"status": "NOT_PREPARED"}
    elif not DATABASE.is_file():
        preflight = _preflight()
        state = {
            "status": "READY_NOT_STARTED" if preflight["status"] == "PASS" else "BLOCKED_CREDENTIAL_PREFLIGHT",
            "preflight": preflight,
        }
    else:
        state = inspect_database(prereg_path=PREREG, database_path=DATABASE, project_root=ROOT)
    return {
        **state, "result_exists": RESULT.is_file(), "target_hours": 1.0,
        "scheduled_supervision": False, "orders_created": 0, "paper_orders": 0,
        "quotes_submitted": 0, "confirmations_sent": 0, "transactions_created": 0,
        "private_key_required": False, "real_money": "BLOQUEADO",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="V0.57 RFQ+CLOB joined paper replay")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--preflight", action="store_true")
    actions.add_argument("--run", action="store_true")
    actions.add_argument("--status", action="store_true")
    actions.add_argument("--audit", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        payload = build_preregistration(output_path=PREREG, project_root=ROOT)
    elif args.preflight:
        payload = _preflight()
    elif args.run:
        payload = run_v057(prereg_path=PREREG, database_path=DATABASE, project_root=ROOT)
    elif args.audit:
        payload = audit_v057(
            prereg_path=PREREG, database_path=DATABASE, result_path=RESULT, project_root=ROOT
        )
    else:
        payload = _status()
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
