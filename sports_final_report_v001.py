from __future__ import annotations

import json
from pathlib import Path

from polymarket_bot.sports_final_report import build_final_report


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "sports_wallet_research_v001"


def main() -> int:
    result = build_final_report(OUTPUT)
    summary = {
        "schema": result["schema"],
        "generated_at_utc": result["generated_at_utc"],
        "data_coverage": result["data_coverage"],
        "final_verdict": result["final_verdict"],
        "outputs": str(OUTPUT / "final"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
