import json
from pathlib import Path

import pytest

from polymarket_bot.ledger.common import EvidenceError, canonical
from polymarket_bot.ledger.independent_report_gate import verify_independent_report_file
from polymarket_bot.ledger.inventory_basis import build_inventory_basis, build_inventory_basis_file
from test_ledger_inventory_basis import bundle, independent_from


def _sealed_primary(tmp_path: Path):
    value = bundle()
    first = build_inventory_basis(value)
    value["independent_report"] = independent_from(first)
    source = tmp_path / "bundle.json"
    source.write_text(canonical(value) + "\n", encoding="utf-8")
    result = tmp_path / "primary"
    build_inventory_basis_file(source, result)
    expected = json.loads((result / "summary.json").read_text(encoding="utf-8"))[
        "independent_expected_contract"
    ]
    report = tmp_path / "independent_report.json"
    report.write_text(canonical(independent_from(first)) + "\n", encoding="utf-8")
    return result, report, expected


def test_independent_report_gate_accepts_exact_external_contract(tmp_path: Path):
    primary, report, _ = _sealed_primary(tmp_path)
    output = tmp_path / "gate"

    summary = verify_independent_report_file(primary, report, output)

    assert summary["status"] == "INDEPENDENT_REPORT_ACCEPTED"
    assert summary["contract"]["status"] == "MATCH"
    assert summary["gate"] == {"status": "PASS", "reasons": []}
    assert summary["integration"] == {"report_accepted": True, "p0_state_changed": False}
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]["summary.json"]
    assert not (output / "independent_report.json").exists()


def test_missing_independent_report_is_sealed_blocker(tmp_path: Path):
    primary, _, _ = _sealed_primary(tmp_path)
    report = tmp_path / "missing-report.json"
    output = tmp_path / "gate"

    summary = verify_independent_report_file(primary, report, output)

    assert summary["status"] == "INDEPENDENT_REPORT_GATE_BLOCKED"
    assert summary["contract"]["reasons"] == ["INDEPENDENT_REPORT_MISSING"]
    assert summary["gate"]["status"] == "BLOCKED"
    assert "PRIMARY_BASIS_GATE_NOT_PASS" not in summary["gate"]["reasons"]


def test_independent_report_mismatch_and_same_engine_are_rejected(tmp_path: Path):
    primary, report, expected = _sealed_primary(tmp_path)
    value = {**expected, "method": "inventory-basis-period-pnl/1",
             "code_commit": "independent-commit", "evidence": ["independent:fixture"]}
    report.write_text(canonical(value) + "\n", encoding="utf-8")
    output = tmp_path / "gate"

    summary = verify_independent_report_file(primary, report, output)

    assert summary["status"] == "INDEPENDENT_REPORT_GATE_BLOCKED"
    assert "INDEPENDENT_PROVENANCE_METHOD_INVALID" in summary["gate"]["reasons"]


def test_independent_report_gate_does_not_overwrite_output(tmp_path: Path):
    primary, report, _ = _sealed_primary(tmp_path)
    output = tmp_path / "gate"
    verify_independent_report_file(primary, report, output)

    with pytest.raises(EvidenceError, match="already exists"):
        verify_independent_report_file(primary, report, output)
