from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v024_structural_check import build_structural_check


DESIGN = ROOT / "data" / "diagnostico_v024_parallel_tournament_design.json"
RESULT = ROOT / "data" / "resultado_v024_parallel_tournament_4h.json"
OUTPUT = ROOT / "data" / "v024_structural_cross_window_check.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--design", type=Path, default=DESIGN)
    parser.add_argument("--result", type=Path, default=RESULT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    payload = build_structural_check(
        design_path=args.design,
        v024_result_path=args.result,
        output_path=args.output,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
