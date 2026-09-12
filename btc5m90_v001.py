from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from polymarket_bot.btc_5m_90.backtest import REPORT_PATH, RESULT_PATH, run_backtest
from polymarket_bot.btc_5m_90.research import (
    LABELS_PATH,
    PROTOCOL_PATH,
    fetch_and_freeze_labels,
    freeze_protocol,
    load_and_verify_protocol,
    load_universe,
)


def _status() -> dict[str, Any]:
    result: dict[str, Any] = {
        "branch": "BTC5M90_V001",
        "universe_markets": len(load_universe()),
        "protocol": "MISSING",
        "labels": "MISSING",
        "backtest": "MISSING",
        "wallet_required": False,
        "orders_enabled": False,
        "real_money": "BLOQUEADO",
    }
    if PROTOCOL_PATH.is_file():
        load_and_verify_protocol()
        result["protocol"] = "FROZEN_AND_VERIFIED"
    if LABELS_PATH.is_file():
        payload = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
        result["labels"] = f"{len(payload.get('markets', {}))}/1148"
    if RESULT_PATH.is_file():
        payload = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
        result["backtest"] = "COMPLETE"
        result["base_test"] = payload.get("base", {}).get("TEST")
        result["verdict"] = payload.get("verdict")
        result["report"] = str(REPORT_PATH)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rama paper-only BTC Up/Down 5m: primer toque exacto a 90c"
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--freeze", action="store_true", help="Congela protocolo antes de OOS")
    action.add_argument("--fetch-labels", action="store_true", help="Descarga/valida outcomes oficiales")
    action.add_argument("--backtest", action="store_true", help="Ejecuta el backtest congelado una vez")
    action.add_argument("--status", action="store_true", help="Muestra estado de la rama")
    args = parser.parse_args()

    if args.freeze:
        payload = freeze_protocol()
    elif args.fetch_labels:
        payload = fetch_and_freeze_labels()
    elif args.backtest:
        payload = run_backtest()
    else:
        payload = _status()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
