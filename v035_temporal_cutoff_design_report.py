from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v035_temporal_cutoff_design import run_temporal_cutoff_design


DEFAULT_OUTPUT = ROOT / "data" / "diagnostico_v035_temporal_exposure_cutoff.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnostico temporal V0.35 sin precios, outcomes, PnL ni ordenes."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_temporal_cutoff_design(output_path=args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
