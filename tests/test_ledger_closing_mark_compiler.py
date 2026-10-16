import hashlib
import json
from pathlib import Path

import pytest

from polymarket_bot.ledger import __main__ as ledger_cli
from polymarket_bot.ledger.closing_mark_compiler import (
    compile_closing_mark_candidates_file,
)
from polymarket_bot.ledger.common import EvidenceError
from polymarket_bot.ledger.price_mark_policy import evaluate_price_policy_file
from test_ledger_price_mark_policy import _fresh_audit, _rpcs, _sha, _write_policy


def _accepted_policy(tmp_path: Path):
    audit, bundle = _fresh_audit(tmp_path)
    policy_path = tmp_path / "policy.json"
    policy = _write_policy(audit, policy_path)
    primary, secondary = _rpcs(policy)
    policy_output = tmp_path / "policy-output"
    evaluate_price_policy_file(
        audit, policy_path, policy_output,
        primary_rpc=primary, secondary_rpc=secondary,
    )
    audit_config = json.loads((audit / "configuration.json").read_text(encoding="utf-8"))
    probe_config = json.loads(
        (Path(audit_config["probe_path"]) / "configuration.json").read_text(encoding="utf-8")
    )
    gaps = Path(probe_config["evidence_gap_queue"]["path"])
    return bundle, gaps, policy_output


def test_compiler_seals_accepted_marks_without_modifying_bundle(tmp_path: Path):
    bundle, gaps, policy_output = _accepted_policy(tmp_path)
    output = tmp_path / "compiled"
    before = _sha(bundle)

    summary = compile_closing_mark_candidates_file(
        bundle, gaps, [policy_output], [], output, require_clean_git=False,
    )

    assert summary["status"] == "COMPILATION_COMPLETE_NOT_INTEGRATED"
    assert summary["coverage"] == {
        "start_index": 0,
        "stop_index_exclusive": 2,
        "eligible_ctf_outcomes": 2,
        "positive_non_quote_assets": 4,
        "chunks": 1,
        "candidate_decisions": 1,
    }
    assert summary["decisions"] == {
        "accepted_for_new_bundle_compilation": 1,
        "deferred_missing_historical_identity": 0,
        "deferred_missing_current_official_identity": 0,
        "rejected_by_policy": 0,
    }
    assert summary["marks"] == {
        "requested": 4,
        "proposed": 1,
        "remaining_without_accepted_mark": 3,
    }
    marks = json.loads((output / "closing_marks.json").read_text(encoding="utf-8"))
    assert len(marks) == 1
    assert next(iter(marks.values())) == "0.6"
    evidence = json.loads((output / "marks_evidence.json").read_text(encoding="utf-8"))
    assert set(evidence) == set(marks)
    assert not (output / "bundle.json").exists()
    assert _sha(bundle) == before
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["bundle_sha256"] == before
    assert manifest["closing_marks_digest"]


def test_compiler_rejects_overlapping_sources(tmp_path: Path):
    bundle, gaps, policy_output = _accepted_policy(tmp_path)

    with pytest.raises(EvidenceError, match="gap or overlap"):
        compile_closing_mark_candidates_file(
            bundle, gaps, [policy_output, policy_output], [], tmp_path / "compiled",
            require_clean_git=False,
        )


def test_compiler_cli_returns_blocked_receipt_without_payload(tmp_path: Path, monkeypatch, capsys):
    bundle, gaps, policy_output = _accepted_policy(tmp_path)
    output = tmp_path / "compiled"
    original = ledger_cli.compile_closing_mark_candidates_file

    def compile_without_git(*args, **kwargs):
        kwargs["require_clean_git"] = False
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger_cli, "compile_closing_mark_candidates_file", compile_without_git)
    code = ledger_cli.main([
        "compile-closing-mark-candidates",
        "--bundle", str(bundle),
        "--evidence-gaps", str(gaps),
        "--policy-output", str(policy_output),
        "--output", str(output),
    ])

    assert code == 2
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "COMPILATION_COMPLETE_NOT_INTEGRATED"
    assert receipt["marks"]["proposed"] == 1
    assert "closing_marks" not in receipt


def test_compiler_rejects_tampered_policy_seal(tmp_path: Path):
    bundle, gaps, policy_output = _accepted_policy(tmp_path)
    decisions = policy_output / "decisions.json"
    before = hashlib.sha256(decisions.read_bytes()).hexdigest()
    decisions.write_text("[]\n", encoding="utf-8")
    assert hashlib.sha256(decisions.read_bytes()).hexdigest() != before

    with pytest.raises(EvidenceError, match="manifest hash verification failed"):
        compile_closing_mark_candidates_file(
            bundle, gaps, [policy_output], [], tmp_path / "compiled",
            require_clean_git=False,
        )
