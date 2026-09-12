from __future__ import annotations

import json
import os
import hashlib
import subprocess
from importlib.metadata import version
from pathlib import Path

from . import SAFETY, VERSION
from .common import EvidenceError, address, canonical, digest, uint
from .decoders import decode_canonical
from .lots import LotLedger
from .normalize import asset_id, normalize_transactions
from .reconcile import independent_transfer_balances, reconcile, source_gate
from .registry import ContractRegistry
from .store import EvidenceStore


def replay(bundle: dict, database: Path) -> dict:
    """Replay a sealed input bundle without networking or historical DB writes.

    Same economic input gives the same ledger/reconciliation hashes regardless
    of batch size or local receipt time. The manifest hashes *all* supplied
    evidence separately. Failures block acceptance and preserve partial evidence.
    """
    chain, wallet = uint(bundle["chain"]), address(bundle["wallet"])
    input_hash = digest(bundle)
    with EvidenceStore(database) as store:
        registry = ContractRegistry(store)
        for spec in bundle["contracts"]:
            registry.register(spec)
        for batch in bundle["batches"]:
            store.ingest(chain, batch["blocks"], batch["logs"], expected_cursor=store.cursor(chain),
                         scope=bundle["scope"], received_at=batch.get("received_at"))
        cursor = store.cursor(chain)
        if cursor is None:
            raise EvidenceError("Replay has no canonical blocks")
        headers = [dict(r) for r in store.db.execute(
            "SELECT b.* FROM blocks b JOIN canonical_blocks c ON b.chain=c.chain AND b.hash=c.hash WHERE b.chain=? ORDER BY b.number", (chain,))]
        first, last = headers[0], headers[-1]
        opening = bundle["opening"]
        if opening["block_number"] != first["number"] - 1 or opening["block_hash"] != first["parent"]:
            raise EvidenceError("Opening inventory is not anchored to the first block's parent")
        for evidence in bundle.get("contract_observations", []):
            version = registry.resolve(chain, evidence["address"], uint(evidence["block_number"]))
            try:
                registry.attest(version, evidence)
            except EvidenceError:
                pass  # Recorded mismatch must surface through decode failures.
        messages = {}
        for source in ("opening", "clob", "onchain", "independent", "health"):
            payload = bundle.get(source, {})
            source_time = first["event_time"] if source == "opening" else last["event_time"]
            if isinstance(payload, dict):
                source_time = uint(payload.get("event_time", payload.get("as_of", source_time)))
            messages[source] = store.capture_message(source, "replay", digest([payload, source_time]), 0, source_time,
                                                      payload, expected_sequence=None)
        revision = store.revision()
        events, failures = decode_canonical(registry, chain)
        actions, normalization_failures = normalize_transactions(events, wallet, opening["quote_asset"])
        failures.extend(normalization_failures)
        ledger = LotLedger({**opening, "evidence": messages["opening"]})
        try:
            ledger.apply_batch(actions)
        except EvidenceError as exc:
            failures.append({"code": "LOT_RECONSTRUCTION_FAILURE", "error": str(exc)})
        snapshot = ledger.snapshot(bundle.get("marks"))
        independent = bundle.get("independent", {})
        opening_balances = {opening["quote_asset"]: uint(opening.get("cash", {}).get(wallet, 0))}
        for lot in opening.get("lots", []):
            if address(lot["wallet"]) == wallet:
                opening_balances[lot["asset"]] = opening_balances.get(lot["asset"], 0) + uint(lot["quantity"])
        for asset in bundle["required_assets"]:
            opening_balances.setdefault(asset, 0)
        if (independent.get("complete") is not True or independent.get("method") != "receipts"
                or independent.get("first_block") != first["number"] or independent.get("last_block") != last["number"]
                or independent.get("tip_hash") != cursor["hash"] or not independent.get("source")):
            failures.append({"code": "INDEPENDENT_ACQUISITION_INCOMPLETE"})
        canonical_logs = [r["raw"] for r in store.logs(chain)]
        # Compare complete acquisition payload sets before independent balance
        # folding. Chain position fields prevent cross-fork equality by accident.
        if sorted(digest(r) for r in canonical_logs) != sorted(digest(r) for r in independent.get("raw_logs", [])):
            failures.append({"code": "INDEPENDENT_RAW_MISMATCH"})
        token_contracts = {s["address"].lower() for s in bundle["contracts"] if s["family"] == "ctf"}
        cash_contracts = {s["address"].lower() for s in bundle["contracts"] if s["family"] == "collateral"}
        try:
            folded = independent_transfer_balances(independent.get("raw_logs", []), wallet, opening_balances,
                                                    chain, token_contracts, cash_contracts)
        except (EvidenceError, ValueError) as exc:
            failures.append({"code": "INDEPENDENT_BALANCE_FAILURE", "error": str(exc)})
            folded = {}
        chain_fills = [{**e["economic"], "exchange": e["contract"], "tx": e["tx"], "log_index": e["log_index"]}
                       for e in events if e["economic"]["kind"] == "fill" and e["economic"]["maker"] == wallet]
        clob = {**bundle.get("clob", {}), "raw_ids": [messages["clob"]]}
        onchain = {**bundle.get("onchain", {}), "raw_ids": [messages["onchain"]]}
        reconciliation = reconcile(cursor=cursor, wallet=wallet, ledger=snapshot, chain_fills=chain_fills,
                                   clob=clob, onchain=onchain, independent_balances=folded,
                                   failures=failures, required_assets=bundle["required_assets"])
        gate = source_gate(bundle.get("health", []), now=uint(bundle["as_of"]), max_age=uint(bundle.get("max_source_age", 60)),
                           required={"rpc", "rest", "ws"})
        blockers = list(reconciliation["reasons"]) + gate["reasons"]
        duration = last["event_time"] - first["event_time"]
        # A successful synthetic fixture or a short capture never qualifies as
        # the real known-wallet, seven-day P0 exit experiment.
        if bundle.get("evidence_kind") != "public_capture":
            blockers.append("SYNTHETIC_EVIDENCE_ONLY")
        if duration < 7 * 86400:
            blockers.append("SEVEN_DAY_REPLAY_MISSING")
        # Independent balance folding is implemented. Full independent PnL for
        # complex inventory still needs an external pipeline evidence pack.
        external = independent.get("accounting_report", {})
        if (external.get("input_hash") != digest(independent.get("raw_logs", []))
                or external.get("quote_asset") != opening["quote_asset"]
                or external.get("wallet") != wallet or not external.get("code_commit")
                or external.get("known_realized_pnl") != snapshot["known_realized_pnl"]
                or external.get("unrealized_pnl") != snapshot["unrealized_pnl"]):
            blockers.append("INDEPENDENT_PNL_EVIDENCE_MISSING_OR_MISMATCH")
        # This release has no implemented Combo semantic mapping. No input flag
        # may authorize an unsupported decoder or turn the P0 exit gate green.
        blockers.append("COMBO_ABI_AND_TOKEN_VECTORS_MISSING")
        report = {"version": VERSION, "input_hash": input_hash, "safety": SAFETY,
                  "evidence_kind": bundle.get("evidence_kind", "unknown"),
                  "ledger": snapshot, "reconciliation": reconciliation, "source_gate": gate,
                  "failures": failures, "duration_seconds": duration,
                  "p0_exit": {"status": "BLOCKED" if blockers else "PASS", "blockers": sorted(set(blockers))},
                  "execution_allowed": False}
        store.save_run(chain, cursor, input_hash, report, expected_revision=revision)
        return report


def replay_file(bundle_path: Path, output: Path) -> dict:
    """Exclusive output with .partial until computation finishes; never overwrite."""
    bundle_path, output = Path(bundle_path).resolve(), Path(output).resolve()
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    partial = output.with_name(output.name + ".partial")
    if output.exists():
        raise EvidenceError("Output already exists")
    partial.mkdir(parents=True, exist_ok=False)
    report = replay(bundle, partial / "evidence.db")
    project = Path(__file__).resolve().parents[3]
    source_root = Path(__file__).resolve().parent
    try:
        commit = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unavailable"
    source_hash = digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source_root.glob("*.py"))})
    (partial / "configuration.json").write_text(canonical(bundle) + "\n", encoding="utf-8")
    (partial / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (partial / "run_manifest.json").write_text(canonical({
        "version": VERSION, "code_commit": commit, "source_hash": source_hash,
        "dependencies": {name: version(name) for name in ("eth-abi", "eth-hash")},
        "input_sha256": digest(bundle), "input_path": str(bundle_path),
        "ledger_hash": report["ledger"]["ledger_hash"],
        "reconciliation_hash": report["reconciliation"]["reconciliation_hash"],
        "p0_exit": report["p0_exit"], "safety": SAFETY}) + "\n", encoding="utf-8")
    # A completed blocked diagnostic is explicitly labeled; never masquerades
    # as approved. A runtime failure keeps its evidence in .partial.
    os.rename(partial, output)
    return report
