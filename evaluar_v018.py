from __future__ import annotations

import json

from polymarket_bot.v018_evaluator import run_evaluation


def main() -> int:
    result = run_evaluation()
    summary = {
        "status": result["status"],
        "decisive_failures": result["decisive_failures"],
        "outcomes_read": result["outcomes_read"],
        "pre_outcome_metrics": result["pre_outcome_evaluation"]["metrics"],
        "performance": {
            key: value
            for key, value in result["performance"].items()
            if key != "details"
        },
        "real_money": result["real_money"],
        "active_forward_read": result["active_forward_read"],
        "active_forward_modified": result["active_forward_modified"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
