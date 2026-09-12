from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v038_recovery_budget_design import run_v038_design


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnostico cerrado V0.38")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "diagnostico_v038_single_layer_recovery_budget.json",
    )
    args = parser.parse_args()
    print(json.dumps(run_v038_design(output_path=args.output), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
