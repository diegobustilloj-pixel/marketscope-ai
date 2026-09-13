from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import SAFETY
from .acquire import ReadOnlyRPC, capture_range
from .deployments import verify_deployment
from .common import EvidenceError, canonical
from .fixtures import sample_bundle
from .readiness import audit_archived_pilot
from .replay import replay_file
from .store import EvidenceStore
from .wallet_capture import capture_wallet_24h


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
    capture.add_argument("--rpc-url", help="Public HTTPS RPC endpoint; no inline credentials")
    verify = commands.add_parser("verify-contract", help="Check a source-verifier report against exact-block RPC code")
    verify.add_argument("--chain", type=int, default=137)
    verify.add_argument("--contract", required=True)
    verify.add_argument("--block", type=int, required=True)
    verify.add_argument("--family", required=True)
    verify.add_argument("--proxy-kind", choices=("direct", "eip1967"), default="direct")
    verify.add_argument("--implementation")
    verify.add_argument("--source-report", type=Path, required=True)
    verify.add_argument("--source-url", required=True)
    verify.add_argument("--rpc-url")
    verify.add_argument("--output", type=Path, required=True)
    readiness = commands.add_parser("pilot-readiness", help="Audit legacy inputs before a real seven-day pilot")
    readiness.add_argument("--activity", type=Path, required=True)
    readiness.add_argument("--onchain", type=Path, required=True)
    readiness.add_argument("--identity", type=Path, required=True)
    readiness.add_argument("--wallet", required=True)
    readiness.add_argument("--window-days", type=int, default=7)
    readiness.add_argument("--output", type=Path, required=True)
    wallet24 = commands.add_parser("capture-wallet-24h", help="Capture a sealed 24h public wallet evidence pack")
    wallet24.add_argument("--wallet", required=True)
    wallet24.add_argument("--identity", type=Path, required=True)
    wallet24.add_argument("--deployments", type=Path,
                          default=Path("configs/polyledger/polygon_wallet_capture.json"))
    wallet24.add_argument("--catalog", type=Path, default=Path("configs/polyledger/abi_catalog.json"))
    wallet24.add_argument("--combo-abi", type=Path,
                          default=Path("configs/polyledger/combo_position_manager_abi.json"))
    wallet24.add_argument("--validation", type=Path,
                          default=Path("artifacts/polyledger_p0/validation_20260912.json"))
    wallet24.add_argument("--confirmations", type=int, default=200)
    wallet24.add_argument("--rpc-url")
    wallet24.add_argument("--state-rpc-url", help="Independent HTTPS RPC for exact-block balances/code")
    wallet24.add_argument("--output", type=Path, required=True)
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
            rpc = ReadOnlyRPC(**({"url": args.rpc_url} if args.rpc_url else {}))
            result = capture_range(rpc, args.output, chain=args.chain, first=args.first_block,
                                   last=args.last_block, contracts=args.contract, method=args.method,
                                   confirmations=args.confirmations,
                                   progress=lambda row: print(json.dumps(row), flush=True))
        elif args.command == "verify-contract":
            rpc = ReadOnlyRPC(**({"url": args.rpc_url} if args.rpc_url else {}))
            result = verify_deployment(rpc, report_path=args.source_report, output=args.output, chain=args.chain,
                                       contract=args.contract, block_number=args.block, family=args.family,
                                       proxy_kind=args.proxy_kind, expected_implementation=args.implementation,
                                       source_url=args.source_url)
        elif args.command == "pilot-readiness":
            result = audit_archived_pilot(args.activity, args.onchain, args.identity, wallet=args.wallet,
                                          window_days=args.window_days)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as target:
                target.write(canonical(result) + "\n")
        elif args.command == "capture-wallet-24h":
            rpc = ReadOnlyRPC(**({"url": args.rpc_url} if args.rpc_url else {}))
            state_rpc = ReadOnlyRPC(**({"url": args.state_rpc_url} if args.state_rpc_url else {})) \
                if args.state_rpc_url else rpc
            result = capture_wallet_24h(output=args.output, wallet=args.wallet, identity_path=args.identity,
                                        deployments_path=args.deployments, catalog_path=args.catalog,
                                        combo_path=args.combo_abi, validation_path=args.validation,
                                        rpc=rpc, state_rpc=state_rpc, confirmations=args.confirmations,
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
        if args.command == "verify-contract":
            return 0 if result["status"] == "VERIFIED_AT_BLOCK" else 2
        if args.command == "pilot-readiness":
            return 2  # A legacy census can diagnose readiness, never approve P0.
        if args.command == "capture-wallet-24h":
            return 2  # Captured evidence remains blocked until all P0 gates pass.
        return 0 if args.command != "replay" or result["p0_exit"]["status"] == "PASS" else 2
    except (EvidenceError, ValueError, KeyError, OSError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc), "safety": SAFETY}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
