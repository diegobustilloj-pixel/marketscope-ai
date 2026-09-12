from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v024_postmortem import build_v024_postmortem


DATABASE = ROOT / "data" / "paper_v024_parallel_tournament_4h.db"
RESULT = ROOT / "data" / "resultado_v024_parallel_tournament_4h.json"
DESIGN = ROOT / "data" / "diagnostico_v024_parallel_tournament_design.json"
OUTPUT = ROOT / "data" / "postmortem_v024_loss_attribution.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=DATABASE)
    parser.add_argument("--result", type=Path, default=RESULT)
    parser.add_argument("--design", type=Path, default=DESIGN)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    payload = build_v024_postmortem(
        database=args.database,
        result_path=args.result,
        design_path=args.design,
        output_path=args.output,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
