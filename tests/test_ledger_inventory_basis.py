import hashlib
import json
from pathlib import Path

import pytest

from polymarket_bot.ledger.common import EvidenceError, canonical, digest
from polymarket_bot.ledger.inventory_basis import (
    build_inventory_basis,
    build_inventory_basis_file,
)


WALLET = "0x" + "11" * 20
CASH = "0x" + "22" * 20
CTF = "0x" + "33" * 20
QUOTE = f"137:{CASH}:erc20"
YES = f"137:{CTF}:1"
NO = f"137:{CTF}:2"


def h(number):
    return "0x" + f"{number:064x}"


def leg(asset, quantity):
    return {"asset": asset, "quantity": quantity}


def action(number, kind, *, inputs=(), outputs=(), cash=0, **extra):
    return {"id": f"action:{number}", "order": [99 + number, 0, 0],
            "wallet": WALLET, "kind": kind, "inputs": list(inputs),
            "outputs": list(outputs), "cash_delta": cash,
            "raw_ids": [f"raw:{number}"], **extra}


def bundle():
    return {
        "schema": 1,
        "chain": 137,
        "wallet": WALLET,
        "quote_asset": QUOTE,
        "quote_decimals": 6,
        "token_decimals": 6,
        "range": {"start_block": 100, "end_block": 105},
        "coverage": {
            "starts_at_wallet_origin": True,
            "continuous_blocks": True,
            "raw_actions_complete": True,
            "reviewed_mappings_complete": True,
            "evidence": ["coverage:synthetic-independent"],
        },
        "asset_universe": {
            "assets": [QUOTE, YES, NO],
            "complete": True,
            "evidence": ["universe:synthetic-independent"],
        },
        "opening": {
            "block_number": 99,
            "block_hash": h(99),
            "evidence": "opening:synthetic-independent",
            "basis_complete": True,
            "cash": {WALLET: 0},
            "lots": [],
        },
        "actions": [
            action(1, "cash", cash=100),
            action(2, "split", outputs=[leg(YES, 40), leg(NO, 40)], cash=-40),
            action(3, "sell", inputs=[leg(YES, 10)], cash=7),
            action(4, "merge", inputs=[leg(YES, 5), leg(NO, 5)], cash=5),
            action(5, "reward", cash=1),
        ],
        "unresolved_actions": [],
        "opening_marks": {},
        "closing_marks": {YES: "0.6", NO: "0.4"},
        "marks_evidence": {"opening": [], "closing": ["marks:synthetic-independent"]},
        "closing": {
            "block_number": 105,
            "block_hash": h(105),
            "evidence": "closing:synthetic-independent",
            "complete": True,
            "balances": {QUOTE: 73, YES: 25, NO: 35},
        },
    }


def independent_from(report):
    return {**report["independent_expected_contract"],
            "method": "synthetic-second-implementation/1",
            "code_commit": "independent-commit",
            "evidence": ["independent:synthetic-vector"]}


def test_complete_inventory_basis_and_internal_pnl_identity():
    result = build_inventory_basis(bundle())
    assert result["status"] == "COMPLETE"
    assert result["basis_gate"]["status"] == "BLOCKED"
    assert result["pnl"]["period_pnl_quote_atomic"] == "2"
    assert result["pnl"]["equity_method_period_pnl_quote_atomic"] == "2"
    assert result["pnl"]["basis_method_period_pnl_quote_atomic"] == "2"
    assert result["pnl"]["internal_identity"] == "MATCH"
    assert result["pnl"]["known_realized_quote_atomic"] == "4"
    assert result["pnl"]["closing_unrealized_quote_atomic"] == "-2"
    inventory = {row["asset"]: row for row in result["closing"]["inventory"]}
    assert inventory[YES]["quantity_atomic"] == 25
    assert inventory[YES]["basis_atomic"] == 13
    assert inventory[NO]["quantity_atomic"] == 35
    assert inventory[NO]["basis_atomic"] == 18
    assert result["reconciliation"]["status"] == "MATCH"
    commitment = inventory[YES]["provenance_commitment"]
    assert commitment["algorithm"] == "sha256-ordered-direct-provenance-v1"
    assert commitment["count"] == 1


def test_independent_report_must_match_exact_contract():
    value = bundle()
    first = build_inventory_basis(value)
    value["independent_report"] = independent_from(first)
    matched = build_inventory_basis(value)
    assert matched["basis_gate"]["status"] == "PASS"
    value["independent_report"]["period_pnl_quote_atomic"] = "2.1"
    mismatch = build_inventory_basis(value)
    assert mismatch["basis_gate"]["status"] == "BLOCKED"
    assert "INDEPENDENT_ACCOUNTING_MISMATCH" in mismatch["independent_comparison"]["reasons"]


def test_balance_gap_and_unknown_opening_basis_block_pnl():
    value = bundle()
    value["closing"]["balances"].pop(NO)
    result = build_inventory_basis(value)
    assert result["status"] == "BLOCKED"
    assert result["pnl"]["period_pnl_quote_atomic"] is None
    assert "CLOSING_BALANCE_MISMATCH_OR_MISSING" in result["blockers"]
    value = bundle()
    value["coverage"]["starts_at_wallet_origin"] = False
    value["opening"]["basis_complete"] = False
    assert "OPENING_BASIS_NOT_PROVEN" in build_inventory_basis(value)["blockers"]


def test_quote_collateral_cannot_be_double_counted_as_inventory():
    value = bundle()
    value["actions"][1]["outputs"][0]["asset"] = QUOTE
    with pytest.raises(EvidenceError, match="cash_delta"):
        build_inventory_basis(value)


def test_mark_outside_universe_and_missing_mark_provenance_fail_closed():
    value = bundle()
    value["closing_marks"][f"137:{CTF}:999"] = "0.5"
    with pytest.raises(EvidenceError, match="declared non-cash"):
        build_inventory_basis(value)
    value = bundle()
    value["marks_evidence"]["closing"] = []
    result = build_inventory_basis(value)
    assert "CLOSING_MARK_EVIDENCE_MISSING" in result["blockers"]


def test_evidenced_token_receipt_uses_boundary_value_as_basis():
    value = bundle()
    value["range"]["end_block"] = 100
    value["actions"] = [action(
        1, "receive", outputs=[leg(YES, 10)], received_basis=6,
        basis_evidence=["receipt-basis:synthetic"], external_flow_value=6,
        external_flow_evidence=["receipt-mark:synthetic"],
    )]
    value["closing_marks"] = {YES: "0.8"}
    value["closing"] = {"block_number": 100, "block_hash": h(100),
                        "evidence": "closing:synthetic-independent", "complete": True,
                        "balances": {QUOTE: 0, YES: 10, NO: 0}}
    result = build_inventory_basis(value)
    assert result["status"] == "COMPLETE"
    assert result["pnl"]["external_token_flow_quote_atomic"] == "6"
    assert result["pnl"]["period_pnl_quote_atomic"] == "2"


def test_outbound_token_flow_preserves_accrued_performance_identity():
    value = bundle()
    value["range"]["end_block"] = 102
    value["actions"] = [
        action(1, "cash", cash=100),
        action(2, "buy", outputs=[leg(YES, 10)], cash=-40),
        action(3, "transfer", inputs=[leg(YES, 10)], cash=0,
               external_flow_value=-60,
               external_flow_evidence=["transfer-mark:synthetic"]),
    ]
    value["closing_marks"] = {}
    value["closing"] = {"block_number": 102, "block_hash": h(102),
                        "evidence": "closing:synthetic-independent", "complete": True,
                        "balances": {QUOTE: 60, YES: 0, NO: 0}}
    result = build_inventory_basis(value)
    assert result["status"] == "COMPLETE"
    assert result["pnl"]["external_net_flow_quote_atomic"] == "40"
    assert result["pnl"]["outbound_transfer_accrual_quote_atomic"] == "20"
    assert result["pnl"]["equity_method_period_pnl_quote_atomic"] == "20"
    assert result["pnl"]["basis_method_period_pnl_quote_atomic"] == "20"
    assert result["pnl"]["internal_identity"] == "MATCH"


def test_inventory_basis_file_is_sealed_and_never_overwritten(tmp_path: Path):
    value = bundle()
    first = build_inventory_basis(value)
    value["independent_report"] = independent_from(first)
    source = tmp_path / "bundle.json"
    source.write_text(canonical(value) + "\n", encoding="utf-8")
    output = tmp_path / "result"
    result = build_inventory_basis_file(source, output)
    assert result["basis_gate"]["status"] == "PASS"
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    configuration = json.loads((output / "configuration.json").read_text(encoding="utf-8"))
    assert manifest["summary_hash"]
    assert manifest["files"]["summary.json"]
    assert configuration["serialization"] == "sealed-input-reference-v1"
    assert configuration["input_action_count"] == len(value["actions"])
    assert "actions" not in configuration
    with pytest.raises(EvidenceError, match="already exists"):
        build_inventory_basis_file(source, output)


def test_streaming_digest_matches_canonical_sha256():
    value = {"z": [1, {"alpha": "ñ"}], "a": ("x", 2)}
    assert digest(value) == hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
