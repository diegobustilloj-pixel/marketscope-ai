from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.sports_wallet_research import (
    PublicApiClient,
    SportsResearchError,
    capture,
    prepare,
)
from polymarket_bot.sports_wallet_analysis import analyze
from polymarket_bot.sports_wallet_copy import analyze_copy, build_signals, capture_tapes


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "sports_wallet_research_v001"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Investigación reproducible de cuatro wallets deportivas de Polymarket"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--capture", action="store_true")
    group.add_argument("--analyze", action="store_true")
    group.add_argument("--prepare-copy", action="store_true")
    group.add_argument("--capture-copy", action="store_true")
    group.add_argument("--analyze-copy", action="store_true")
    group.add_argument("--status", action="store_true")
    args = parser.parse_args()
    try:
        if args.prepare:
            result = prepare(OUTPUT)
        elif args.capture:
            result = capture(OUTPUT, PublicApiClient())
        elif args.analyze:
            result = analyze(OUTPUT)
        elif args.prepare_copy:
            result = build_signals(OUTPUT)
        elif args.capture_copy:
            result = capture_tapes(OUTPUT)
        elif args.analyze_copy:
            result = analyze_copy(OUTPUT)
        else:
            manifest = OUTPUT / "audit" / "coverage_manifest.json"
            result = (
                json.loads(manifest.read_text(encoding="utf-8"))
                if manifest.exists()
                else {
                    "status": "NOT_CAPTURED",
                    "protocol_exists": (OUTPUT / "protocol.json").exists(),
                    "output": str(OUTPUT),
                }
            )
    except SportsResearchError as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
