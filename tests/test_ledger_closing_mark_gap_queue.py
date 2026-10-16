import hashlib
import json
from pathlib import Path

import pytest

from polymarket_bot.ledger import __main__ as ledger_cli
from polymarket_bot.ledger.closing_mark_compiler import (
    compile_closing_mark_candidates_file,
)
from polymarket_bot.ledger.closing_mark_gap_queue import (
    COMBO_POSITION,
    NO_OBSERVATION,
    PUSD_COLLATERAL,
    STALE,
    _probe_gap_row,
    build_closing_mark_gap_queue_file,
)
from polymarket_bot.ledger.common import EvidenceError
from test_ledger_closing_mark_compiler import _accepted_policy


def _compiled(tmp_path: Path) -> tuple[Path, Path, Path]:
    bundle, gaps, policy_output = _accepted_policy(tmp_path)
    compiled = tmp_path / "compiled"
    compile_closing_mark_candidates_file(
        bundle, gaps, [policy_output], [], compiled, require_clean_git=False,
    )
    return bundle, gaps, compiled


def test_gap_queue_partitions_every_unresolved_asset_without_integration(tmp_path: Path):
    bundle, gaps, compiled = _compiled(tmp_path)
    output = tmp_path / "gap-queue"
    bundle_before = hashlib.sha256(bundle.read_bytes()).hexdigest()

    summary = build_closing_mark_gap_queue_file(
        compiled, gaps, output, require_clean_git=False,
    )

    assert summary["status"] == "GAP_QUEUE_COMPLETE_NOT_RESOLVED"
    assert summary["coverage"] == {
        "closing_mark_requests": 4,
        "accepted_candidates_excluded_from_queue": 1,
        "unresolved_assets_queued": 3,
        "eligible_ctf_outcomes_probed": 2,
        "excluded_non_ctf_assets_queued": 2,
        "probe_chunks": 1,
        "fresh_policy_decisions": 1,
    }
    assert summary["category_counts"] == {
        COMBO_POSITION: 1,
        NO_OBSERVATION: 1,
        PUSD_COLLATERAL: 1,
    }
    assert summary["integration"] == {
        "closing_marks_written_to_bundle": 0,
        "basis_bundle_modified": False,
        "compiled_candidates_modified": False,
        "stale_candidates_promoted": 0,
        "freshness_threshold_modified": False,
        "status": "NOT_INTEGRATED",
    }
    queue = json.loads((output / "gap_queue.json").read_text(encoding="utf-8"))
    assert len(queue) == len({row["asset"] for row in queue}) == 3
    assert [row["queue_sequence"] for row in queue] == [1, 2, 3]
    assert all(row["eligible_as_closing_mark"] is False for row in queue)
    assert not (output / "closing_marks.json").exists()
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == bundle_before


def test_stale_probe_row_preserves_observation_as_diagnostic_only(tmp_path: Path):
    probe = tmp_path / "probe"
    responses = probe / "responses"
    responses.mkdir(parents=True)
    raw = responses / "00001.raw.json"
    raw.write_bytes(b'{"data":[]}')
    raw_sha = hashlib.sha256(raw.read_bytes()).hexdigest()
    row = _probe_gap_row({
        "asset": "137:0x4d97dcd97ec945f40cf65f87097ace5ea0476045:1",
        "quantity_atomic": 25,
        "sequence": 1,
        "status": STALE,
        "cutoff_timestamp": 999,
        "request_sha256": "request-seal",
        "raw_response_file": "responses/00001.raw.json",
        "raw_response_sha256": raw_sha,
        "observation": {
            "timestamp": 800,
            "price": "0.6",
            "age_seconds": 199,
            "resolution_seconds": 60,
        },
    }, maximum_age_seconds=60, probe=probe, manifest_sha="probe-seal")

    assert row["category"] == STALE
    assert row["route"] == "RECOVER_FRESH_HISTORICAL_PRICE"
    assert row["diagnostic_observation_only"]["price"] == "0.6"
    assert row["excess_age_seconds"] == 139
    assert row["eligible_as_closing_mark"] is False


def test_gap_queue_rejects_tampered_compiler_seal(tmp_path: Path):
    _, gaps, compiled = _compiled(tmp_path)
    summary = compiled / "summary.json"
    summary.write_text("{}\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match="manifest hash verification failed"):
        build_closing_mark_gap_queue_file(
            compiled, gaps, tmp_path / "gap-queue", require_clean_git=False,
        )


def test_gap_queue_cli_returns_compact_blocked_receipt(tmp_path: Path, monkeypatch, capsys):
    _, gaps, compiled = _compiled(tmp_path)
    output = tmp_path / "gap-queue"
    original = ledger_cli.build_closing_mark_gap_queue_file

    def build_without_git(*args, **kwargs):
        kwargs["require_clean_git"] = False
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger_cli, "build_closing_mark_gap_queue_file", build_without_git)
    code = ledger_cli.main([
        "build-closing-mark-gap-queue",
        "--compiled-candidates", str(compiled),
        "--evidence-gaps", str(gaps),
        "--output", str(output),
    ])

    assert code == 2
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "GAP_QUEUE_COMPLETE_NOT_RESOLVED"
    assert receipt["coverage"]["unresolved_assets_queued"] == 3
    assert "gap_queue" not in receipt
