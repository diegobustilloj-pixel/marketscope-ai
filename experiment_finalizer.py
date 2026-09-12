from __future__ import annotations

import argparse
import json

from polymarket_bot.finalizer import ExperimentFinalizer


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true")
    group.add_argument("--status", action="store_true")
    args = parser.parse_args()
    result = ExperimentFinalizer().reconcile(mutate=args.once)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
