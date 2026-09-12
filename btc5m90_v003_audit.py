from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.btc_5m_90.v003_audit import RESULT_PATH, audit_v003
from polymarket_bot.btc_5m_90.v003_shadow import DATABASE_PATH, PREREG_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="Auditoria final BTC5M90 V003")
    parser.add_argument("--prereg", type=Path, default=PREREG_PATH)
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument("--result", type=Path, default=RESULT_PATH)
    args = parser.parse_args()
    payload = audit_v003(
        prereg_path=args.prereg,
        database_path=args.database,
        result_path=args.result,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
