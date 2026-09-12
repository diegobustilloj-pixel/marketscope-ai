from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import SAFETY
from .acquire import ReadOnlyRPC, capture_range
from .common import EvidenceError, canonical
from .fixtures import sample_bundle
from .replay import replay_file
from .store import EvidenceStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="PolyLedger P0 — read-only/replay; no signing or execution")
    commands = parser.add_subparsers(dest="command", required=True)
    fixture = commands.add_parser("fixture", help="Write explicitly synthetic replay vectors")
    fixture.add_argument("--catalog", type=Path, default=Path("configs/polyledger/abi_catalog.json"))
    fixture.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("replay", help="Replay a sealed evidence bundle to a NEW output directory")
    run.add_argument("--bundle", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    status = commands.add_parser("status", help="Inspect an existing P0 database read-only")
    status.add_argument("--database", type=Path, required=True)
    capture = commands.add_parser("capture", help="Bounded public RPC capture; no account/key or execution")
    capture.add_argument("--chain", type=int, default=137)
    capture.add_argument("--first-block", type=int, required=True)
    capture.add_argument("--last-block", type=int, required=True)
    capture.add_argument("--contract", action="append", required=True)
    capture.add_argument("--method", choices=("logs", "receipts"), default="logs")
    capture.add_argument("--confirmations", type=int, default=200)
    capture.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "fixture":
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as target:
                target.write(canonical(sample_bundle(args.catalog)) + "\n")
            result = {"status": "SYNTHETIC_FIXTURE", "path": str(args.output.resolve()), "safety": SAFETY}
        elif args.command == "replay":
            report = replay_file(args.bundle, args.output)
            result = {"output": str(args.output.resolve()), "ledger_hash": report["ledger"]["ledger_hash"],
                      "reconciliation": report["reconciliation"]["status"], "p0_exit": report["p0_exit"], "safety": SAFETY}
        elif args.command == "capture":
            result = capture_range(ReadOnlyRPC(), args.output, chain=args.chain, first=args.first_block,
                                   last=args.last_block, contracts=args.contract, method=args.method,
                                   confirmations=args.confirmations,
                                   progress=lambda row: print(json.dumps(row), flush=True))
        else:
            with EvidenceStore(args.database, read_only=True) as store:
                result = {"integrity": store.verify(),
                          "cursors": [dict(r) for r in store.db.execute("SELECT * FROM chain_cursors")],
                          "raw_logs": store.db.execute("SELECT COUNT(*) FROM raw_logs").fetchone()[0],
                          "canonical_logs": store.db.execute("SELECT COUNT(*) FROM canonical_logs").fetchone()[0],
                          "current_runs": store.db.execute("SELECT COUNT(*) FROM current_runs").fetchone()[0],
                          "incidents": [dict(r) for r in store.db.execute("SELECT code,COUNT(*) AS count FROM incidents GROUP BY code")],
                          "safety": SAFETY}
        print(json.dumps(result, indent=2))
        return 0 if args.command != "replay" or result["p0_exit"]["status"] == "PASS" else 2
    except (EvidenceError, ValueError, KeyError, OSError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc), "safety": SAFETY}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
