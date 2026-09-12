from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v034_guard_design import run_guard_design


DEFAULT_OUTPUT = ROOT / "data" / "diagnostico_v034_relative_depth_guard.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnostico tecnico V0.34 sin precios, outcomes, PnL ni ordenes."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_guard_design(output_path=args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
