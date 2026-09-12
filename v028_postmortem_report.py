from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v028_final_postmortem import build_v028_final_postmortem


RESULT = ROOT / "data" / "resultado_v028_twap_lt5_replication.json"
OUTPUT = ROOT / "data" / "postmortem_v028_twap_lt5_final.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, default=RESULT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    payload = build_v028_final_postmortem(
        result_path=args.result,
        output_path=args.output,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
