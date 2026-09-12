from __future__ import annotations

import argparse
import json

from polymarket_bot.btc_5m_90.v002_backtest import (
    PROTOCOL_PATH,
    REPORT_PATH,
    RESULT_PATH,
    freeze_protocol,
    run_development_backtest,
    verify_protocol,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="BTC5M90 V002 late-window edge gate")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--freeze", action="store_true")
    actions.add_argument("--backtest", action="store_true")
    actions.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.freeze:
        result = freeze_protocol()
    elif args.backtest:
        result = run_development_backtest()
    else:
        result = {
            "branch": "BTC5M90_V002",
            "protocol": "FROZEN_AND_VERIFIED" if PROTOCOL_PATH.exists() and verify_protocol() else "MISSING",
            "development_backtest": "COMPLETE" if RESULT_PATH.exists() else "MISSING",
            "report": str(REPORT_PATH),
            "wallet_required": False,
            "orders_enabled": False,
            "real_money": "BLOQUEADO",
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
