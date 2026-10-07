from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import SAFETY
from .acquire import ReadOnlyRPC, capture_range
from .blockscout_backfill import BlockscoutClient, backfill_wallet_blockscout
from .deployments import verify_deployment
from .common import EvidenceError, canonical
from .evidence_gaps import build_basis_evidence_gap_file
from .fixtures import sample_bundle
from .history_backfill import backfill_wallet_history
from .inventory_basis import build_inventory_basis_file
from .lifetime_basis_bundle import build_lifetime_basis_bundle
from .lifetime_inventory import build_lifetime_inventory_file
from .lifetime_receipt_closure import close_lifetime_receipt_gap
from .lifetime_crosscheck import crosscheck_lifetime_capture
from .price_history_probe import OfficialPriceHistoryClient, build_price_history_probe_file
from .price_candidate_audit import OfficialCandidateMetadataClient, audit_price_candidates_file
from .price_mark_policy import evaluate_price_policy_file
from .readiness import audit_archived_pilot
from .replay import replay_file
from .store import EvidenceStore
from .wallet_capture import capture_wallet_24h
from .valuation import value_wallet_capture


def _terminal_result(args, result: dict) -> dict:
    """Keep a large sealed basis report out of stdout.

    The full report is already atomically written to the requested output
    directory. Emitting its journal again can exceed a terminal's practical
    capacity and has no audit benefit, so the CLI returns a compact receipt.
    """
    if args.command == "audit-basis-evidence":
        return {
            "status": result["status"],
            "output": str(args.output.resolve()),
            "actions": result["actions"],
            "closing_marks": result["closing_marks"],
            "independent_accounting": result["independent_accounting"],
            "request_hashes": result["request_hashes"],
            "safety": result["safety"],
        }
    if args.command == "probe-price-history":
        return {
            "status": result["status"],
            "output": str(args.output.resolve()),
            "closing_block": result["closing_block"],
            "sample": result["sample"],
            "responses": result["responses"],
            "integration": result["integration"],
            "review_gate": result["review_gate"],
            "safety": result["safety"],
        }
    if args.command == "audit-price-candidates":
        return {
            "status": result["status"],
            "output": str(args.output.resolve()),
            "probe": result["probe"],
            "requests": result["requests"],
            "reviews": result["reviews"],
            "integration": result["integration"],
            "review_gate": result["review_gate"],
            "safety": result["safety"],
        }
    if args.command == "evaluate-price-policy":
        return {
            "status": result["status"],
            "output": str(args.output.resolve()),
            "policy_id": result["policy_id"],
            "settlement": result["settlement"],
            "decisions": result["decisions"],
            "integration": result["integration"],
            "review_gate": result["review_gate"],
            "safety": result["safety"],
        }
    if args.command != "inventory-basis":
        return result
    return {
        "status": result["status"],
        "output": str(args.output.resolve()),
        "actions": {
            "count": result["actions"]["count"],
            "reconstruction_error": result["actions"]["reconstruction_error"],
            "journal_hash": result["actions"]["journal_hash"],
        },
        "reconciliation": {
            "status": result["reconciliation"]["status"],
            "reasons": result["reconciliation"]["reasons"],
        },
        "basis_gate": result["basis_gate"],
        "safety": result["safety"],
    }


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
    readiness = commands.add_parser("pilot-readiness", help="Audit legacy inputs before a real wallet pilot")
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
    valuation = commands.add_parser(
        "value-wallet-24h",
        help="Expand exact-block balances and mark a sealed 24h capture without trading",
    )
    valuation.add_argument("--capture", type=Path, required=True)
    valuation.add_argument("--wallet", required=True)
    valuation.add_argument("--deployments", type=Path,
                           default=Path("configs/polyledger/polygon_wallet_capture.json"))
    valuation.add_argument("--history-assets", type=Path,
                           default=Path("data/car_forensics/car_raw_activity.parquet"))
    valuation.add_argument("--legacy-db", type=Path, default=Path("data/polyledger/car.db"))
    valuation.add_argument("--metadata-db", type=Path,
                           default=Path("data/car_forensics/car_metadata.db"))
    valuation.add_argument("--state-rpc-url")
    valuation.add_argument("--price-lookback-seconds", type=int, default=3600)
    valuation.add_argument("--max-mark-age-seconds", type=int, default=900)
    valuation.add_argument("--workers", type=int, default=12)
    valuation.add_argument("--output", type=Path, required=True)
    basis = commands.add_parser(
        "inventory-basis",
        help="Reconstruct sealed FIFO inventory, basis and period PnL without execution",
    )
    basis.add_argument("--bundle", type=Path, required=True)
    basis.add_argument("--output", type=Path, required=True)
    gap_audit = commands.add_parser(
        "audit-basis-evidence",
        help="Seal offline requests for missing basis values, marks and independent accounting",
    )
    gap_audit.add_argument("--bundle", type=Path, required=True)
    gap_audit.add_argument("--output", type=Path, required=True)
    price_probe = commands.add_parser(
        "probe-price-history",
        help="Seal a <=20 CTF historical-price probe; no mark, PnL, or execution",
    )
    price_probe.add_argument("--bundle", type=Path, required=True)
    price_probe.add_argument("--evidence-gaps", type=Path, required=True)
    price_probe.add_argument("--output", type=Path, required=True)
    price_probe.add_argument("--rpc-url", help="Primary public HTTPS Polygon RPC; no inline credentials")
    price_probe.add_argument(
        "--secondary-rpc-url", default="https://polygon.drpc.org",
        help="Independent public HTTPS Polygon RPC used only to cross-check the closing block",
    )
    price_probe.add_argument("--sample-size", type=int, default=20)
    price_probe.add_argument(
        "--max-age-seconds", type=int, required=True,
        help="Explicit freshness bound used only to classify candidate observations",
    )
    price_audit = commands.add_parser(
        "audit-price-candidates",
        help="Audit fresh probe candidates against official market identity; never integrate marks",
    )
    price_audit.add_argument("--probe", type=Path, required=True)
    price_audit.add_argument("--output", type=Path, required=True)
    price_audit.add_argument(
        "--metadata-db", type=Path, default=Path("data/car_forensics/car_metadata.db"),
        help="Optional read-only local metadata cache used only as auxiliary corroboration",
    )
    price_policy = commands.add_parser(
        "evaluate-price-policy",
        help="Apply a sealed closing-mark acceptance policy; never integrate marks",
    )
    price_policy.add_argument("--audit", type=Path, required=True)
    price_policy.add_argument(
        "--policy", type=Path,
        default=Path("configs/polyledger/closing_mark_policy_v1.json"),
    )
    price_policy.add_argument("--output", type=Path, required=True)
    price_policy.add_argument(
        "--rpc-url", default="https://polygon.drpc.org",
        help="Primary approved archive HTTPS Polygon RPC; no credentials",
    )
    price_policy.add_argument(
        "--secondary-rpc-url", default="https://tenderly.rpc.polygon.community",
        help="Independent approved archive HTTPS Polygon RPC for exact-block settlement agreement",
    )
    history = commands.add_parser(
        "backfill-wallet-history",
        help="Resume a lifetime public Polygon wallet capture; no basis or execution",
    )
    history.add_argument("--wallet", required=True)
    history.add_argument("--identity", type=Path, required=True)
    history.add_argument("--scope", type=Path,
                         default=Path("configs/polyledger/polygon_lifetime_backfill.json"))
    history.add_argument("--first-block", type=int, default=1)
    history.add_argument("--last-block", type=int)
    history.add_argument("--confirmations", type=int, default=200)
    history.add_argument("--segment-blocks", type=int, default=100000)
    history.add_argument("--min-query-blocks", type=int, default=1000)
    history.add_argument("--receipt-shard-size", type=int, default=100)
    history.add_argument("--max-segments", type=int)
    history.add_argument("--max-receipt-shards", type=int)
    history.add_argument("--rpc-url")
    history.add_argument("--output", type=Path, required=True)
    explorer = commands.add_parser(
        "backfill-wallet-blockscout",
        help="Resume a no-key Polygon Blockscout lifetime capture; no execution",
    )
    explorer.add_argument("--wallet", required=True)
    explorer.add_argument("--identity", type=Path, required=True)
    explorer.add_argument("--scope", type=Path,
                          default=Path("configs/polyledger/polygon_lifetime_backfill.json"))
    explorer.add_argument("--first-block", type=int, default=1)
    explorer.add_argument("--last-block", type=int)
    explorer.add_argument("--confirmations", type=int, default=200)
    explorer.add_argument("--log-shard-size", type=int, default=25)
    explorer.add_argument("--log-workers", type=int, default=8)
    explorer.add_argument("--max-transfer-pages", type=int)
    explorer.add_argument("--max-log-shards", type=int)
    explorer.add_argument("--rpc-url")
    explorer.add_argument(
        "--receipt-rpc-url",
        help="Independent public HTTPS RPC for missing logs or primary receipt batching",
    )
    explorer.add_argument(
        "--receipt-batch-size", type=int, default=0,
        help="Use the receipt RPC as the primary log source in read-only batches of 1..10",
    )
    explorer.add_argument("--receipt-batch-workers", type=int, default=4)
    explorer.add_argument("--output", type=Path, required=True)
    crosscheck = commands.add_parser(
        "crosscheck-lifetime-wallet",
        help="Continuously crosscheck a sealed lifetime capture through read-only Polygon logs",
    )
    crosscheck.add_argument("--capture", type=Path, required=True)
    crosscheck.add_argument("--wallet", required=True)
    crosscheck.add_argument("--identity", type=Path, required=True)
    crosscheck.add_argument("--scope", type=Path,
                            default=Path("configs/polyledger/polygon_lifetime_backfill.json"))
    crosscheck.add_argument("--origin-block", type=int, required=True)
    crosscheck.add_argument("--origin-transaction", required=True)
    crosscheck.add_argument("--rpc-url", required=True)
    crosscheck.add_argument("--range-blocks", type=int, default=100)
    crosscheck.add_argument("--shard-ranges", type=int, default=100)
    crosscheck.add_argument("--batch-size", type=int, default=10)
    crosscheck.add_argument("--workers", type=int, default=8)
    crosscheck.add_argument("--retries", type=int, default=5)
    crosscheck.add_argument("--max-shards", type=int)
    crosscheck.add_argument("--output", type=Path, required=True)
    lifetime_inventory = commands.add_parser(
        "recalculate-lifetime-inventory",
        help="Fold a zero-origin lifetime wallet scan and reconcile exact-block balances",
    )
    lifetime_inventory.add_argument("--crosscheck", type=Path, required=True)
    lifetime_inventory.add_argument("--wallet", required=True)
    lifetime_inventory.add_argument("--scope", type=Path,
                                    default=Path("configs/polyledger/polygon_lifetime_backfill.json"))
    lifetime_inventory.add_argument("--state-rpc-url", required=True)
    lifetime_inventory.add_argument("--output", type=Path, required=True)
    gap_receipts = commands.add_parser(
        "close-lifetime-receipt-gap",
        help="Resume and seal only full receipts omitted by the source indexer",
    )
    gap_receipts.add_argument("--crosscheck", type=Path, required=True)
    gap_receipts.add_argument("--source-capture", type=Path, required=True)
    gap_receipts.add_argument("--wallet", required=True)
    gap_receipts.add_argument("--scope", type=Path,
                              default=Path("configs/polyledger/polygon_lifetime_backfill.json"))
    gap_receipts.add_argument("--rpc-url", required=True)
    gap_receipts.add_argument("--shard-size", type=int, default=100)
    gap_receipts.add_argument("--batch-size", type=int, default=10)
    gap_receipts.add_argument("--workers", type=int, default=8)
    gap_receipts.add_argument("--retries", type=int, default=5)
    gap_receipts.add_argument("--max-shards", type=int)
    gap_receipts.add_argument("--output", type=Path, required=True)
    lifetime_bundle = commands.add_parser(
        "build-lifetime-basis-bundle",
        help="Join closed lifetime receipts and conservatively map a basis bundle",
    )
    lifetime_bundle.add_argument("--crosscheck", type=Path, required=True)
    lifetime_bundle.add_argument("--source-capture", type=Path, required=True)
    lifetime_bundle.add_argument("--gap-closure", type=Path, required=True)
    lifetime_bundle.add_argument("--inventory-capture", type=Path, required=True)
    lifetime_bundle.add_argument("--wallet", required=True)
    lifetime_bundle.add_argument("--scope", type=Path,
                                 default=Path("configs/polyledger/polygon_lifetime_backfill.json"))
    lifetime_bundle.add_argument("--catalog", type=Path,
                                 default=Path("configs/polyledger/abi_catalog.json"))
    lifetime_bundle.add_argument("--output", type=Path, required=True)
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
        elif args.command == "value-wallet-24h":
            rpc = ReadOnlyRPC(url=args.state_rpc_url) if args.state_rpc_url else None
            result = value_wallet_capture(
                capture=args.capture, output=args.output, wallet=args.wallet,
                deployments_path=args.deployments, history_assets_path=args.history_assets,
                legacy_db_path=args.legacy_db, metadata_db_path=args.metadata_db, rpc=rpc,
                price_lookback_seconds=args.price_lookback_seconds,
                max_mark_age_seconds=args.max_mark_age_seconds, workers=args.workers,
                progress=lambda row: print(json.dumps(row), flush=True),
            )
        elif args.command == "inventory-basis":
            result = build_inventory_basis_file(args.bundle, args.output)
        elif args.command == "audit-basis-evidence":
            result = build_basis_evidence_gap_file(args.bundle, args.output)
        elif args.command == "probe-price-history":
            primary_rpc = ReadOnlyRPC(**({"url": args.rpc_url} if args.rpc_url else {}))
            secondary_rpc = ReadOnlyRPC(url=args.secondary_rpc_url)
            result = build_price_history_probe_file(
                args.bundle, args.evidence_gaps, args.output,
                rpc=primary_rpc, secondary_rpc=secondary_rpc,
                client=OfficialPriceHistoryClient(), sample_size=args.sample_size,
                max_age_seconds=args.max_age_seconds,
            )
        elif args.command == "audit-price-candidates":
            result = audit_price_candidates_file(
                args.probe, args.output,
                client=OfficialCandidateMetadataClient(), metadata_db=args.metadata_db,
            )
        elif args.command == "evaluate-price-policy":
            primary_rpc = ReadOnlyRPC(**({"url": args.rpc_url} if args.rpc_url else {}))
            secondary_rpc = ReadOnlyRPC(url=args.secondary_rpc_url)
            result = evaluate_price_policy_file(
                args.audit, args.policy, args.output,
                primary_rpc=primary_rpc, secondary_rpc=secondary_rpc,
            )
        elif args.command == "backfill-wallet-history":
            rpc = ReadOnlyRPC(**({"url": args.rpc_url} if args.rpc_url else {}))
            result = backfill_wallet_history(
                output=args.output, wallet=args.wallet, identity_path=args.identity,
                scope_path=args.scope, rpc=rpc, first_block=args.first_block,
                last_block=args.last_block, confirmations=args.confirmations,
                segment_blocks=args.segment_blocks,
                min_query_blocks=args.min_query_blocks,
                receipt_shard_size=args.receipt_shard_size,
                max_segments=args.max_segments,
                max_receipt_shards=args.max_receipt_shards,
                progress=lambda row: print(json.dumps(row), flush=True),
            )
        elif args.command == "backfill-wallet-blockscout":
            rpc = ReadOnlyRPC(**({"url": args.rpc_url} if args.rpc_url else {}))
            result = backfill_wallet_blockscout(
                output=args.output, wallet=args.wallet, identity_path=args.identity,
                scope_path=args.scope, rpc=rpc, client=BlockscoutClient(),
                first_block=args.first_block, last_block=args.last_block,
                confirmations=args.confirmations, log_shard_size=args.log_shard_size,
                max_transfer_pages=args.max_transfer_pages,
                max_log_shards=args.max_log_shards,
                log_workers=args.log_workers,
                receipt_rpc_url=args.receipt_rpc_url,
                receipt_batch_size=args.receipt_batch_size,
                receipt_batch_workers=args.receipt_batch_workers,
                progress=lambda row: print(json.dumps(row), flush=True),
            )
        elif args.command == "crosscheck-lifetime-wallet":
            result = crosscheck_lifetime_capture(
                capture=args.capture, output=args.output, wallet=args.wallet,
                identity_path=args.identity, scope_path=args.scope,
                origin_block=args.origin_block,
                origin_transaction=args.origin_transaction,
                rpc_url=args.rpc_url, range_blocks=args.range_blocks,
                shard_ranges=args.shard_ranges, batch_size=args.batch_size,
                workers=args.workers, retries=args.retries,
                max_shards=args.max_shards,
                progress=lambda row: print(json.dumps(row), flush=True),
            )
        elif args.command == "recalculate-lifetime-inventory":
            result = build_lifetime_inventory_file(
                crosscheck=args.crosscheck, scope_path=args.scope, wallet=args.wallet,
                state_rpc=ReadOnlyRPC(url=args.state_rpc_url), output=args.output,
            )
        elif args.command == "close-lifetime-receipt-gap":
            result = close_lifetime_receipt_gap(
                crosscheck=args.crosscheck, source_capture=args.source_capture,
                scope_path=args.scope, wallet=args.wallet, rpc_url=args.rpc_url,
                output=args.output, shard_size=args.shard_size,
                batch_size=args.batch_size, workers=args.workers,
                retries=args.retries, max_shards=args.max_shards,
                progress=lambda row: print(json.dumps(row), flush=True),
            )
        elif args.command == "build-lifetime-basis-bundle":
            result = build_lifetime_basis_bundle(
                crosscheck=args.crosscheck, source_capture=args.source_capture,
                gap_closure=args.gap_closure, inventory_capture=args.inventory_capture,
                scope_path=args.scope, catalog_path=args.catalog, wallet=args.wallet,
                output=args.output,
                progress=lambda row: print(json.dumps(row), flush=True),
            )
        else:
            with EvidenceStore(args.database, read_only=True) as store:
                result = {"integrity": store.verify(),
                          "cursors": [dict(r) for r in store.db.execute("SELECT * FROM chain_cursors")],
                          "raw_logs": store.db.execute("SELECT COUNT(*) FROM raw_logs").fetchone()[0],
                          "canonical_logs": store.db.execute("SELECT COUNT(*) FROM canonical_logs").fetchone()[0],
                          "current_runs": store.db.execute("SELECT COUNT(*) FROM current_runs").fetchone()[0],
                          "incidents": [dict(r) for r in store.db.execute("SELECT code,COUNT(*) AS count FROM incidents GROUP BY code")],
                          "safety": SAFETY}
        print(json.dumps(_terminal_result(args, result), indent=2))
        if args.command == "verify-contract":
            return 0 if result["status"] == "VERIFIED_AT_BLOCK" else 2
        if args.command == "pilot-readiness":
            return 2  # A legacy census can diagnose readiness, never approve P0.
        if args.command in {"capture-wallet-24h", "value-wallet-24h", "backfill-wallet-history",
                            "backfill-wallet-blockscout", "crosscheck-lifetime-wallet",
                            "recalculate-lifetime-inventory", "close-lifetime-receipt-gap",
                            "build-lifetime-basis-bundle", "probe-price-history",
                            "audit-price-candidates", "evaluate-price-policy"}:
            return 2  # Evidence remains blocked until all P0 gates pass.
        if args.command == "inventory-basis":
            return 0 if result["basis_gate"]["status"] == "PASS" else 2
        return 0 if args.command != "replay" or result["p0_exit"]["status"] == "PASS" else 2
    except (EvidenceError, ValueError, KeyError, OSError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc), "safety": SAFETY}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
