from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v046_contract import build_preregistration
from polymarket_bot.v046_transport_probe import probe_v046


ROOT = Path(__file__).resolve().parent
PREREG = ROOT / "data" / "prereg_v046_rest_websocket_transport_probe.json"
RESULT = ROOT / "data" / "resultado_v046_rest_websocket_transport_probe.json"


def _status() -> dict[str, object]:
    result = json.loads(RESULT.read_text(encoding="utf-8")) if RESULT.is_file() else None
    transport = result.get("transport", {}) if result else {}
    return {
        "status": "COMPLETED" if result else ("PREREGISTERED" if PREREG.is_file() else "NOT_PREPARED"),
        "verdict": result.get("verdict") if result else None,
        "eligible_events": transport.get("eligible_events"),
        "complete_events": transport.get("complete_events"),
        "required_complete_events": transport.get("required_complete_events"),
        "source_timestamp_semantics": transport.get("source_timestamp_semantics"),
        "scheduled_supervision": False,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="V0.46 REST/WebSocket public transport probe")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--probe", action="store_true")
    actions.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        payload = build_preregistration(output_path=PREREG, project_root=ROOT)
    elif args.probe:
        payload = probe_v046(prereg_path=PREREG, result_path=RESULT, project_root=ROOT)
    else:
        payload = _status()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
