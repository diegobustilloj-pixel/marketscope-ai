from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v027_x_profile_express import build_x_profile_express_diagnostic


RESULT = ROOT / "data" / "resultado_v026b_adaptive_checkpoints.json"
RECONCILIATION = ROOT / "data" / "reconciliacion_v026b_auditoria_terminal.json"
OUTPUT = ROOT / "data" / "diagnostico_v027_x_profiles_express.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, default=RESULT)
    parser.add_argument("--reconciliation", type=Path, default=RECONCILIATION)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--official-twap-window-seconds", type=int, default=60)
    args = parser.parse_args()
    payload = build_x_profile_express_diagnostic(
        result_path=args.result,
        reconciliation_path=args.reconciliation,
        output_path=args.output,
        official_market_twap_window_seconds=args.official_twap_window_seconds,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
