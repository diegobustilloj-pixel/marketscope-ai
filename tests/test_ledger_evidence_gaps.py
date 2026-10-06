import json
from pathlib import Path

import pytest

from polymarket_bot.ledger.__main__ import main as ledger_main
from polymarket_bot.ledger.common import EvidenceError, canonical
from polymarket_bot.ledger.evidence_gaps import (
    audit_basis_evidence,
    build_basis_evidence_gap_file,
)
from test_ledger_inventory_basis import NO, QUOTE, YES, action, bundle, leg


def test_gap_audit_lists_only_explicit_missing_evidence():
    value = bundle()
    value["actions"].extend([
        action(6, "receive", outputs=[leg(YES, 4)]),
        action(7, "transfer", inputs=[leg(NO, 5)]),
    ])
    value["closing_marks"] = {YES: "0.6"}
    value["marks_evidence"] = {"opening": [], "closing": ["marks:sealed"]}

    audit = audit_basis_evidence(value)

    assert audit["summary"]["status"] == "EVIDENCE_REQUIRED"
    assert audit["summary"]["actions"]["external_flow_requests_by_kind"] == {
        "receive": 1, "transfer": 1,
    }
    receive, transfer = audit["external_flow_requests"]
    assert receive["required"] == [
        "EXTERNAL_FLOW_EVIDENCE_MISSING", "EXTERNAL_FLOW_VALUE_MISSING",
        "RECEIVED_BASIS_EVIDENCE_MISSING", "RECEIVED_BASIS_MISSING",
    ]
    assert transfer["required"] == [
        "EXTERNAL_FLOW_EVIDENCE_MISSING", "EXTERNAL_FLOW_VALUE_MISSING",
    ]
    assert "raw_ids" not in receive
    assert audit["closing_mark_requests"] == [{
        "asset": NO, "quantity_atomic": 35, "required": ["CLOSING_MARK_MISSING"],
    }]
    assert audit["independent_report_request"]["required"] is True


def test_gap_audit_accepts_declared_complete_evidence_without_fabricating_data():
    value = bundle()
    value["actions"] = [action(
        1, "receive", outputs=[leg(YES, 10)], received_basis=5,
        basis_evidence=["basis:sealed"], external_flow_value=5,
        external_flow_evidence=["flow:sealed"],
    )]
    value["closing"]["balances"] = {QUOTE: 0, YES: 10, NO: 0}
    value["closing_marks"] = {YES: "0.5"}
    value["marks_evidence"] = {"opening": [], "closing": ["marks:sealed"]}
    value["independent_report"] = {}

    audit = audit_basis_evidence(value)

    assert audit["summary"]["status"] == "NO_DECLARED_GAPS"
    assert audit["external_flow_requests"] == []
    assert audit["closing_mark_requests"] == []
    assert audit["independent_report_request"]["required"] is False


def test_gap_audit_file_is_sealed_and_cli_keeps_request_payload_off_stdout(tmp_path: Path, capsys):
    value = bundle()
    value["actions"].append(action(6, "receive", outputs=[leg(YES, 4)]))
    source = tmp_path / "bundle.json"
    source.write_text(canonical(value) + "\n", encoding="utf-8")
    output = tmp_path / "gaps"

    summary = build_basis_evidence_gap_file(source, output)
    configuration = json.loads((output / "configuration.json").read_text(encoding="utf-8"))
    requests = json.loads((output / "external_flow_requests.json").read_text(encoding="utf-8"))
    assert summary["status"] == "EVIDENCE_REQUIRED"
    assert configuration["serialization"] == "sealed-input-reference-v1"
    assert configuration["input_action_count"] == len(value["actions"])
    assert len(requests) == 1
    with pytest.raises(EvidenceError, match="already exists"):
        build_basis_evidence_gap_file(source, output)

    cli_output = tmp_path / "cli-gaps"
    assert ledger_main([
        "audit-basis-evidence", "--bundle", str(source), "--output", str(cli_output),
    ]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["output"] == str(cli_output.resolve())
    assert receipt["actions"]["external_flow_requests_by_kind"] == {"receive": 1}
    assert "external_flow_requests" not in receipt
