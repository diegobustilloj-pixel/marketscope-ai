"""Deterministic inventory and cost-basis gate for PolyLedger P0.

The engine consumes a sealed, normalized action bundle. It never fetches data,
signs messages, connects a wallet, or executes a transaction. Missing coverage,
basis, marks, external-flow values, or closing balances remains an explicit
blocker; it is never substituted with zero.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections import Counter, defaultdict
from decimal import Decimal, localcontext
from pathlib import Path

from . import SAFETY, VERSION
from .common import EvidenceError, address, canonical, digest, hex_bytes, now_utc, uint
from .lots import LotLedger, POLICY

ENGINE_SCHEMA = 1
ENGINE_VERSION = "inventory-basis-period-pnl/1"


def _decimal(value: str) -> Decimal:
    if not isinstance(value, str):
        raise EvidenceError("Accounting decimals must be JSON strings")
    number = Decimal(value)
    if not number.is_finite():
        raise EvidenceError("Non-finite accounting decimal")
    return number


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise EvidenceError("Non-finite accounting result")
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _signed_atomic(value) -> int:
    if type(value) is not int:
        raise EvidenceError("Signed accounting amounts must be atomic integers")
    if not -(2**256) < value < 2**256:
        raise EvidenceError("Signed accounting amount outside supported range")
    return value


def _asset(value: str, chain: int) -> str:
    if not isinstance(value, str):
        raise EvidenceError("Asset ID must be a string")
    parts = value.split(":")
    if len(parts) != 3 or uint(parts[0]) != chain:
        raise EvidenceError("Asset must use chain:contract:token namespace")
    contract = address(parts[1])
    token = parts[2]
    if token != "erc20":
        token = str(uint(token))
    normalized = f"{chain}:{contract}:{token}"
    if normalized != value:
        raise EvidenceError("Asset ID must be normalized")
    return normalized


def _marks(raw: dict, chain: int) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise EvidenceError("Marks must be an object")
    result = {}
    for asset, value in raw.items():
        asset = _asset(asset, chain)
        mark = _decimal(value)
        if mark < 0:
            raise EvidenceError("Negative inventory mark")
        result[asset] = _decimal_text(mark)
    return result


def _inventory(snapshot: dict, wallet: str, marks: dict[str, str]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for lot in snapshot["lots"]:
        if address(lot["wallet"]) != wallet or not lot["remaining"]:
            continue
        asset = lot["asset"]
        row = grouped.setdefault(asset, {"asset": asset, "quantity_atomic": 0,
                                         "basis_atomic": 0, "basis_complete": True,
                                         "lot_count": 0, "provenance": set()})
        row["quantity_atomic"] += uint(lot["remaining"])
        row["lot_count"] += 1
        row["provenance"].update(lot["provenance"])
        if lot["remaining_cost"] is None:
            row["basis_complete"] = False
            row["basis_atomic"] = None
        elif row["basis_atomic"] is not None:
            row["basis_atomic"] += uint(lot["remaining_cost"])
    result = []
    with localcontext() as context:
        context.prec = 160
        for asset in sorted(grouped):
            row = grouped[asset]
            quantity = Decimal(row["quantity_atomic"])
            basis = None if row["basis_atomic"] is None else Decimal(row["basis_atomic"])
            mark = _decimal(marks[asset]) if asset in marks else None
            value = None if mark is None else quantity * mark
            pnl = None if value is None or basis is None else value - basis
            result.append({**row, "provenance": sorted(row["provenance"]),
                           "average_basis_quote_atoms_per_token_atom":
                               None if basis is None else _decimal_text(basis / quantity),
                           "mark_quote_atoms_per_token_atom": None if mark is None else _decimal_text(mark),
                           "marked_value_quote_atomic": None if value is None else _decimal_text(value),
                           "unrealized_pnl_quote_atomic": None if pnl is None else _decimal_text(pnl)})
    return result


def _equity(snapshot: dict, wallet: str, inventory: list[dict]) -> tuple[Decimal | None, list[str]]:
    missing = sorted(row["asset"] for row in inventory if row["marked_value_quote_atomic"] is None)
    if missing:
        return None, missing
    value = Decimal(snapshot["cash"].get(wallet, 0))
    value += sum((_decimal(row["marked_value_quote_atomic"]) for row in inventory), Decimal(0))
    return value, []


def _unrealized(snapshot: dict, wallet: str) -> Decimal | None:
    value = snapshot["unrealized_pnl"].get(wallet, "0")
    return None if value is None else _decimal(value)


def _closing_reconciliation(snapshot: dict, wallet: str, quote_asset: str,
                            universe: set[str], closing: dict, chain: int) -> dict:
    raw_balances = closing.get("balances")
    if not isinstance(raw_balances, dict):
        raise EvidenceError("Closing balances must be an object")
    observed = {_asset(asset, chain): uint(value) for asset, value in raw_balances.items()}
    expected = dict(snapshot["balances"].get(wallet, {}))
    expected[quote_asset] = uint(snapshot["cash"].get(wallet, 0))
    rows, reasons = [], []
    if set(observed) - universe:
        reasons.append("CLOSING_ASSET_OUTSIDE_DECLARED_UNIVERSE")
    for asset in sorted(universe | set(observed) | set(expected)):
        ledger_value = expected.get(asset, 0)
        observed_value = observed.get(asset)
        status = "MATCH" if observed_value is not None and observed_value == ledger_value else "BLOCKED"
        if status == "BLOCKED":
            reasons.append("CLOSING_BALANCE_MISMATCH_OR_MISSING")
        rows.append({"asset": asset, "ledger_atomic": ledger_value,
                     "observed_atomic": observed_value,
                     "delta_atomic": None if observed_value is None else ledger_value - observed_value,
                     "status": status})
    return {"status": "MATCH" if not reasons else "BLOCKED", "rows": rows,
            "reasons": sorted(set(reasons)), "balances_hash": digest(observed)}


def _independent_comparison(report: dict | None, expected: dict) -> dict:
    if report is None:
        return {"status": "MISSING", "reasons": ["INDEPENDENT_ACCOUNTING_REPORT_MISSING"]}
    if not isinstance(report, dict):
        raise EvidenceError("Independent accounting report must be an object")
    reasons = []
    if (not isinstance(report.get("method"), str) or not report["method"]
            or not isinstance(report.get("code_commit"), str) or not report["code_commit"]
            or not isinstance(report.get("evidence"), list) or not report["evidence"]
            or any(not isinstance(item, str) or not item for item in report["evidence"])):
        reasons.append("INDEPENDENT_PROVENANCE_INCOMPLETE")
    if report.get("method") == ENGINE_VERSION:
        reasons.append("INDEPENDENT_PIPELINE_NOT_INDEPENDENT")
    for key, value in expected.items():
        observed = report.get(key)
        if key.endswith("_quote_atomic") and observed is not None:
            try:
                observed = _decimal_text(_decimal(observed))
            except EvidenceError:
                reasons.append("INDEPENDENT_ACCOUNTING_VALUE_INVALID")
                continue
        if observed != value:
            reasons.append("INDEPENDENT_ACCOUNTING_MISMATCH")
    return {"status": "MATCH" if not reasons else "BLOCKED",
            "reasons": sorted(set(reasons)), "method": report.get("method"),
            "code_commit": report.get("code_commit"), "evidence": report.get("evidence")}


def build_inventory_basis(bundle: dict) -> dict:
    """Reconstruct exact FIFO basis and period PnL from one sealed action bundle."""
    canonical(bundle)
    if uint(bundle.get("schema")) != ENGINE_SCHEMA:
        raise EvidenceError("Unsupported inventory/basis bundle schema")
    chain, wallet = uint(bundle["chain"]), address(bundle["wallet"])
    quote_asset = _asset(bundle["quote_asset"], chain)
    if not quote_asset.endswith(":erc20"):
        raise EvidenceError("Quote asset must be an ERC-20 namespace")
    quote_decimals = uint(bundle["quote_decimals"])
    token_decimals = uint(bundle["token_decimals"])
    if quote_decimals > 36 or token_decimals > 36:
        raise EvidenceError("Unsupported asset decimal scale")

    range_ = bundle["range"]
    start_block, end_block = uint(range_["start_block"]), uint(range_["end_block"])
    if start_block > end_block:
        raise EvidenceError("Invalid accounting block range")
    opening, closing = bundle["opening"], bundle["closing"]
    if uint(opening["block_number"]) + 1 != start_block or uint(closing["block_number"]) != end_block:
        raise EvidenceError("Opening/closing inventory is not anchored to the accounting range")
    opening_hash = hex_bytes(opening["block_hash"], 32)
    closing_hash = hex_bytes(closing["block_hash"], 32)
    if not opening.get("evidence") or not closing.get("evidence"):
        raise EvidenceError("Opening and closing snapshots require evidence")

    universe_spec = bundle["asset_universe"]
    if not isinstance(universe_spec.get("assets"), list) or not universe_spec.get("evidence"):
        raise EvidenceError("Asset universe requires a list and evidence")
    universe_list = [_asset(item, chain) for item in universe_spec["assets"]]
    if len(set(universe_list)) != len(universe_list) or quote_asset not in universe_list:
        raise EvidenceError("Asset universe is duplicated or omits quote collateral")
    universe = set(universe_list)

    coverage = bundle["coverage"]
    if not coverage.get("evidence"):
        raise EvidenceError("Coverage declaration requires evidence")
    blockers = []
    for field, code in (
        ("continuous_blocks", "BLOCK_RANGE_NOT_CONTINUOUS"),
        ("raw_actions_complete", "RAW_ACTION_COVERAGE_INCOMPLETE"),
        ("reviewed_mappings_complete", "ACTION_MAPPING_INCOMPLETE"),
    ):
        if coverage.get(field) is not True:
            blockers.append(code)
    if universe_spec.get("complete") is not True:
        blockers.append("ASSET_UNIVERSE_INCOMPLETE")
    if closing.get("complete") is not True:
        blockers.append("CLOSING_BALANCE_EVIDENCE_INCOMPLETE")
    unresolved = bundle.get("unresolved_actions", [])
    if not isinstance(unresolved, list):
        raise EvidenceError("Unresolved actions must be a list")
    if unresolved:
        blockers.append("UNRESOLVED_ACTIONS_PRESENT")

    opening_cash = opening.get("cash", {})
    opening_lots = opening.get("lots", [])
    if not isinstance(opening_cash, dict) or not isinstance(opening_lots, list):
        raise EvidenceError("Opening inventory has invalid cash/lots")
    for owner, amount in opening_cash.items():
        if address(owner) != wallet:
            raise EvidenceError("Opening bundle may only seed cash for the audited wallet")
        uint(amount)
    for lot in opening_lots:
        if address(lot["wallet"]) != wallet:
            raise EvidenceError("Opening bundle may only seed the audited wallet")
        lot_asset = _asset(lot["asset"], chain)
        if lot_asset == quote_asset:
            raise EvidenceError("Quote collateral must be opening cash, not an inventory lot")
        if lot_asset not in universe:
            raise EvidenceError("Opening lot lies outside the asset universe")
        uint(lot["quantity"])
        if lot.get("cost") is not None:
            uint(lot["cost"])

    starts_at_origin = coverage.get("starts_at_wallet_origin") is True
    if starts_at_origin:
        if opening_lots or uint(opening_cash.get(wallet, 0)) != 0:
            blockers.append("WALLET_ORIGIN_OPENING_NOT_ZERO")
    elif opening.get("basis_complete") is not True:
        blockers.append("OPENING_BASIS_NOT_PROVEN")
    if any(lot.get("cost") is None for lot in opening_lots):
        blockers.append("OPENING_LOT_BASIS_UNKNOWN")

    opening_marks = _marks(bundle.get("opening_marks", {}), chain)
    closing_marks = _marks(bundle.get("closing_marks", {}), chain)
    if ((set(opening_marks) | set(closing_marks)) - universe
            or quote_asset in opening_marks or quote_asset in closing_marks):
        raise EvidenceError("Inventory marks must reference declared non-cash assets")
    marks_evidence = bundle.get("marks_evidence", {})
    if not isinstance(marks_evidence, dict):
        raise EvidenceError("Mark evidence must be an object")
    actions = bundle.get("actions")
    if not isinstance(actions, list):
        raise EvidenceError("Actions must be a list")
    external_flow_complete = True
    external_cash, external_token = 0, 0
    for action in actions:
        raw_ids = action.get("raw_ids")
        if (address(action["wallet"]) != wallet or not isinstance(raw_ids, list)
                or not raw_ids or any(not isinstance(item, str) or not item for item in raw_ids)):
            raise EvidenceError("Action wallet/provenance is invalid")
        order = action.get("order", [])
        if len(order) != 3 or not start_block <= uint(order[0]) <= end_block:
            raise EvidenceError("Action lies outside the accounting range")
        for leg in action.get("inputs", []) + action.get("outputs", []):
            leg_asset = _asset(leg["asset"], chain)
            if leg_asset == quote_asset:
                raise EvidenceError("Quote collateral must use cash_delta, not an inventory leg")
            if leg_asset not in universe:
                raise EvidenceError("Action references an asset outside the universe")
            uint(leg["quantity"])
        cash_delta = _signed_atomic(action["cash_delta"])
        kind = action["kind"]
        if kind == "cash":
            external_cash += cash_delta
        if kind in {"receive", "transfer"}:
            value = action.get("external_flow_value")
            flow_evidence = action.get("external_flow_evidence")
            if (type(value) is not int or not isinstance(flow_evidence, list)
                    or not flow_evidence
                    or any(not isinstance(item, str) or not item for item in flow_evidence)):
                external_flow_complete = False
            else:
                value = _signed_atomic(value)
                if (kind == "receive" and value < 0) or (kind == "transfer" and value > 0):
                    raise EvidenceError("External token flow has the wrong sign")
                if kind == "receive" and action.get("received_basis") != value:
                    raise EvidenceError("Received basis must equal evidenced boundary value")
                external_token += value
    if not external_flow_complete:
        blockers.append("EXTERNAL_TOKEN_FLOW_VALUE_MISSING")

    ledger = LotLedger({"evidence": opening["evidence"], "quote_asset": quote_asset,
                        "cash": opening_cash, "lots": opening_lots})
    opening_snapshot = ledger.snapshot(opening_marks)
    action_error = None
    try:
        ledger.apply_batch(actions)
    except EvidenceError as exc:
        action_error = str(exc)
        blockers.append("LOT_RECONSTRUCTION_FAILURE")
    closing_snapshot = ledger.snapshot(closing_marks)
    opening_inventory = _inventory(opening_snapshot, wallet, opening_marks)
    closing_inventory = _inventory(closing_snapshot, wallet, closing_marks)
    opening_equity, opening_missing_marks = _equity(
        opening_snapshot, wallet, opening_inventory
    )
    closing_equity, closing_missing_marks = _equity(
        closing_snapshot, wallet, closing_inventory
    )
    if opening_missing_marks:
        blockers.append("OPENING_MARKS_INCOMPLETE")
    if closing_missing_marks:
        blockers.append("CLOSING_MARKS_INCOMPLETE")
    if opening_inventory and not marks_evidence.get("opening"):
        blockers.append("OPENING_MARK_EVIDENCE_MISSING")
    if closing_inventory and not marks_evidence.get("closing"):
        blockers.append("CLOSING_MARK_EVIDENCE_MISSING")
    if not opening_snapshot["complete_basis"] or not closing_snapshot["complete_basis"]:
        blockers.append("UNKNOWN_COST_BASIS")

    reconciliation = _closing_reconciliation(
        closing_snapshot, wallet, quote_asset, universe, closing, chain
    )
    blockers.extend(reconciliation["reasons"])

    outbound_accrual = Decimal(0)
    outbound_accrual_complete = True
    for row in closing_snapshot["journal"]:
        if row["kind"] != "transfer":
            continue
        value = row.get("external_flow_value")
        consumed = row.get("consumed_lots", [])
        if type(value) is not int or any(part.get("cost") is None for part in consumed):
            outbound_accrual_complete = False
            continue
        cost = sum(uint(part["cost"]) for part in consumed)
        outbound_accrual += Decimal(-value - cost)
    if not outbound_accrual_complete:
        blockers.append("EXTERNAL_TRANSFER_ACCRUAL_UNKNOWN")

    external_net = Decimal(external_cash + external_token) if external_flow_complete else None
    equity_pnl = None
    if opening_equity is not None and closing_equity is not None and external_net is not None:
        equity_pnl = closing_equity - opening_equity - external_net
    opening_unrealized = _unrealized(opening_snapshot, wallet)
    closing_unrealized = _unrealized(closing_snapshot, wallet)
    realized = Decimal(closing_snapshot["known_realized_pnl"].get(wallet, 0))
    basis_pnl = None
    if (opening_unrealized is not None and closing_unrealized is not None
            and outbound_accrual_complete):
        basis_pnl = realized + closing_unrealized - opening_unrealized + outbound_accrual
    identity_status = "UNAVAILABLE"
    if equity_pnl is not None and basis_pnl is not None:
        identity_status = "MATCH" if equity_pnl == basis_pnl else "BLOCKED"
        if identity_status == "BLOCKED":
            blockers.append("INTERNAL_PNL_IDENTITY_MISMATCH")

    action_counts = Counter(row.get("kind") for row in actions)
    realized_by_kind = defaultdict(int)
    unknown_by_kind = Counter()
    for row in closing_snapshot["journal"]:
        if row["realized_pnl"] is None:
            unknown_by_kind[row["kind"]] += 1
        else:
            realized_by_kind[row["kind"]] += row["realized_pnl"]

    accounting_input = {
        "engine_schema": ENGINE_SCHEMA, "chain": chain, "wallet": wallet,
        "quote_asset": quote_asset, "range": range_, "opening": opening,
        "asset_universe": universe_spec, "coverage": coverage, "actions": actions,
        "unresolved_actions": unresolved, "opening_marks": opening_marks,
        "closing_marks": closing_marks, "marks_evidence": marks_evidence,
        "closing": closing,
    }
    accounting_input_hash = digest(accounting_input)
    blockers = sorted(set(blockers))
    engine_complete = not blockers
    period_pnl = basis_pnl if engine_complete else None
    independent_expected = {
        "accounting_input_hash": accounting_input_hash,
        "wallet": wallet,
        "quote_asset": quote_asset,
        "opening_equity_quote_atomic": None if opening_equity is None else _decimal_text(opening_equity),
        "closing_equity_quote_atomic": None if closing_equity is None else _decimal_text(closing_equity),
        "external_net_flow_quote_atomic": None if external_net is None else _decimal_text(external_net),
        "period_pnl_quote_atomic": None if period_pnl is None else _decimal_text(period_pnl),
        "closing_balances_hash": reconciliation["balances_hash"],
    }
    independent = _independent_comparison(bundle.get("independent_report"), independent_expected)
    accounting_gate_pass = engine_complete and independent["status"] == "MATCH"
    gate_blockers = sorted(set(blockers + ([] if independent["status"] == "MATCH"
                                           else independent["reasons"])))
    journal = closing_snapshot["journal"]
    return {
        "schema": ENGINE_SCHEMA, "version": VERSION, "engine": ENGINE_VERSION,
        "status": "COMPLETE" if engine_complete else "BLOCKED",
        "wallet": wallet, "chain": chain, "quote_asset": quote_asset,
        "quote_decimals": quote_decimals, "token_decimals": token_decimals,
        "range": {"start_block": start_block, "end_block": end_block,
                  "opening_block": start_block - 1, "opening_block_hash": opening_hash,
                  "closing_block_hash": closing_hash},
        "coverage": {"starts_at_wallet_origin": starts_at_origin,
                     "continuous_blocks": coverage.get("continuous_blocks") is True,
                     "raw_actions_complete": coverage.get("raw_actions_complete") is True,
                     "reviewed_mappings_complete": coverage.get("reviewed_mappings_complete") is True,
                     "asset_universe_complete": universe_spec.get("complete") is True,
                     "unresolved_actions": len(unresolved)},
        "actions": {"count": len(actions), "by_kind": dict(sorted(action_counts.items())),
                    "reconstruction_error": action_error,
                    "journal_hash": digest(journal)},
        "journal": journal,
        "opening": {"cash_atomic": opening_snapshot["cash"].get(wallet, 0),
                    "inventory": opening_inventory,
                    "equity_quote_atomic": None if opening_equity is None else _decimal_text(opening_equity),
                    "missing_marks": opening_missing_marks,
                    "ledger_hash": opening_snapshot["ledger_hash"]},
        "closing": {"cash_atomic": closing_snapshot["cash"].get(wallet, 0),
                    "inventory": closing_inventory,
                    "equity_quote_atomic": None if closing_equity is None else _decimal_text(closing_equity),
                    "missing_marks": closing_missing_marks,
                    "ledger_hash": closing_snapshot["ledger_hash"]},
        "pnl": {"available": period_pnl is not None,
                "known_realized_quote_atomic": _decimal_text(realized),
                "realized_by_kind_quote_atomic": dict(sorted(realized_by_kind.items())),
                "unknown_realizations_by_kind": dict(sorted(unknown_by_kind.items())),
                "opening_unrealized_quote_atomic": None if opening_unrealized is None else _decimal_text(opening_unrealized),
                "closing_unrealized_quote_atomic": None if closing_unrealized is None else _decimal_text(closing_unrealized),
                "outbound_transfer_accrual_quote_atomic":
                    _decimal_text(outbound_accrual) if outbound_accrual_complete else None,
                "external_cash_flow_quote_atomic": _decimal_text(Decimal(external_cash)),
                "external_token_flow_quote_atomic":
                    _decimal_text(Decimal(external_token)) if external_flow_complete else None,
                "external_net_flow_quote_atomic": None if external_net is None else _decimal_text(external_net),
                "equity_method_period_pnl_quote_atomic":
                    None if equity_pnl is None else _decimal_text(equity_pnl),
                "basis_method_period_pnl_quote_atomic":
                    None if basis_pnl is None else _decimal_text(basis_pnl),
                "period_pnl_quote_atomic": None if period_pnl is None else _decimal_text(period_pnl),
                "internal_identity": identity_status,
                "policy": POLICY,
                "interpretation": "Period PnL in quote-asset atomic units; not tax accounting."},
        "reconciliation": reconciliation,
        "accounting_input_hash": accounting_input_hash,
        "independent_expected_contract": independent_expected,
        "independent_comparison": independent,
        "basis_gate": {"status": "PASS" if accounting_gate_pass else "BLOCKED",
                       "blockers": [] if accounting_gate_pass else gate_blockers},
        "blockers": blockers,
        "p0_exit_allowed": False, "execution_allowed": False, "safety": SAFETY,
    }


def _sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _write_new(path: Path, value) -> None:
    content = canonical(value) + "\n"
    with path.open("x", encoding="utf-8") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())


def build_inventory_basis_file(bundle_path: Path, output: Path) -> dict:
    """Build a sealed report in a new directory; runtime failure keeps .partial."""
    bundle_path, output = Path(bundle_path).resolve(), Path(output).resolve()
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists():
        raise EvidenceError("Inventory/basis output or partial directory already exists")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    partial.mkdir(parents=True)
    report = build_inventory_basis(bundle)
    _write_new(partial / "configuration.json", bundle)
    _write_new(partial / "summary.json", report)
    project = Path(__file__).resolve().parents[3]
    try:
        git = ["git", "-c", "safe.directory=" + str(project).replace("\\", "/"), "-C", str(project)]
        commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
        clean = not subprocess.check_output(git + ["status", "--porcelain"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit, clean = "unavailable", False
    files = {path.name: _sha256(path) for path in sorted(partial.glob("*.json"))}
    manifest = {"schema": ENGINE_SCHEMA, "engine": ENGINE_VERSION,
                "completed_at": now_utc(), "code_commit": commit,
                "working_tree_clean": clean, "input_path": str(bundle_path),
                "input_sha256": _sha256(bundle_path), "files": files,
                "summary_hash": digest(report), "basis_gate": report["basis_gate"],
                "safety": SAFETY}
    _write_new(partial / "run_manifest.json", manifest)
    os.replace(partial, output)
    return report
