from __future__ import annotations

import argparse
import json

from polymarket_bot import v015


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--activate", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--monitor", action="store_true")
    args = parser.parse_args()

    if args.activate:
        active = v015.activate_if_ready()
        payload = (
            v015.activation_status()
            if active is None
            else {
                "status": "ACTIVE",
                "selected_stratum": active["selected_stratum"],
                "minimum_market_start_ms": active["minimum_market_start_ms"],
                "labels_read": 0,
                "real_money": "BLOQUEADO",
            }
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    elif args.status:
        print(json.dumps(v015.status(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        v015.monitor()


if __name__ == "__main__":
    main()
