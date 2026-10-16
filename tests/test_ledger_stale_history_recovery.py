import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from polymarket_bot.ledger import SAFETY
from polymarket_bot.ledger import __main__ as ledger_cli
from polymarket_bot.ledger.closing_mark_gap_queue import GAP_QUEUE_VERSION, STALE
from polymarket_bot.ledger.common import EvidenceError, digest
from polymarket_bot.ledger.price_candidate_audit import _files_sha256, _sha256
from polymarket_bot.ledger.price_history_probe import CTF_CONTRACT, _write_new
from polymarket_bot.ledger.stale_history_recovery import (
    NO_WINDOW_OBSERVATION,
    RECOVERED,
    parse_clob_window,
    recover_stale_clob_history,
    stale_history_recovery_status,
)


class FakeClobHistoryClient:
    def __init__(self):
        self.urls = []

    def fetch(self, url: str) -> dict:
        self.urls.append(url)
        token = parse_qs(urlsplit(url).query)["market"][0]
        if token == "1":
            body = b'{"history":[{"t":950,"p":0.4}]}'
        else:
            body = b'{"history":[]}'
        return {
            "body": body,
            "status": 200,
            "response_headers": {"content-type": "application/json"},
            "attempts": [{"attempt": 1, "status": 200, "latency_ms": 1}],
        }


def _sealed_stale_queue(tmp_path: Path) -> Path:
    root = tmp_path / "gap-queue"
    root.mkdir()
    rows = []
    for sequence, token in enumerate(("1", "2"), start=1):
        rows.append({
            "asset": f"137:{CTF_CONTRACT}:{token}",
            "quantity_atomic": 20 + sequence,
            "sequence": sequence,
            "source_status": STALE,
            "cutoff_timestamp": 1_000,
            "category": STALE,
            "priority": 1,
            "route": "RECOVER_FRESH_HISTORICAL_PRICE",
            "reason_codes": ["OBSERVATION_EXCEEDS_MAXIMUM_AGE"],
            "diagnostic_observation_only": {
                "timestamp": 900,
                "price": "0.5",
                "age_seconds": 100,
                "resolution_seconds": 60,
            },
            "maximum_age_seconds": 60,
            "excess_age_seconds": 40,
            "eligible_as_closing_mark": False,
            "queue_sequence": sequence,
        })
    configuration = {
        "schema": 1,
        "engine": GAP_QUEUE_VERSION,
        "integration": "NONE",
        "safety": SAFETY,
    }
    summary = {
        "schema": 1,
        "engine": GAP_QUEUE_VERSION,
        "status": "GAP_QUEUE_COMPLETE_NOT_RESOLVED",
        "category_counts": {STALE: 2},
        "safety": SAFETY,
    }
    _write_new(root / "configuration.json", configuration)
    _write_new(root / "stale_candidates.json", rows)
    _write_new(root / "summary.json", summary)
    _write_new(root / "run_manifest.json", {
        "schema": 1,
        "engine": GAP_QUEUE_VERSION,
        "files": _files_sha256(root),
        "summary_sha256": _sha256(root / "summary.json"),
        "gap_queue_digest": digest(rows),
        "safety": SAFETY,
    })
    return root


def test_recovery_resumes_and_seals_candidates_without_promoting_them(tmp_path: Path):
    queue = _sealed_stale_queue(tmp_path)
    output = tmp_path / "recovery"
    client = FakeClobHistoryClient()

    paused = recover_stale_clob_history(
        queue, output, batch_size=1, workers=1, max_batches=1,
        batch_delay_seconds=0, client=client, require_clean_git=False,
    )
    assert paused["status"] == "PAUSED_BATCH_LIMIT"
    assert paused["progress"] == {
        "completed": 1, "total": 2, "remaining": 1, "batches_completed": 1,
    }

    summary = recover_stale_clob_history(
        queue, output, batch_size=1, workers=1, batch_delay_seconds=0,
        resume=True, client=client, require_clean_git=False,
    )
    assert summary["status"] == "RECOVERY_COMPLETE_EVIDENCE_NOT_INTEGRATED"
    assert summary["coverage"] == {
        "stale_candidates": 2,
        "attempted": 2,
        "batches": 2,
        "remaining_unattempted": 0,
    }
    assert summary["status_counts"] == {
        RECOVERED: 1,
        NO_WINDOW_OBSERVATION: 1,
    }
    assert summary["recovered_candidates"] == 1
    assert summary["integration"]["recovered_candidates_promoted"] == 0
    recovered = json.loads((output / "recovered_candidates.json").read_text())
    assert recovered[0]["observation"] == {
        "timestamp": 950,
        "price": "0.4",
        "age_seconds": 50,
        "source_fidelity_seconds": 60,
    }
    assert recovered[0]["eligible_as_closing_mark"] is False
    assert not (output / "closing_marks.json").exists()
    assert stale_history_recovery_status(output)["status"] == "COMPLETE"
    assert all("startTs=940" in url and "endTs=1000" in url for url in client.urls)


def test_clob_window_rejects_future_and_boundary_points():
    result = parse_clob_window(
        b'{"history":[{"t":941,"p":0},{"t":1001,"p":0.7}]}',
        start_timestamp=940, cutoff_timestamp=1_000, fidelity_minutes=1,
    )
    assert result["status"] == "BOUNDARY_PRICE_ONLY"
    assert result["diagnostics"] == {
        "points_before_window": 0,
        "points_after_cutoff": 1,
        "boundary_price_points": 1,
        "eligible_interior_points": 0,
    }


def test_complete_recovery_rejects_tampered_raw_response(tmp_path: Path):
    queue = _sealed_stale_queue(tmp_path)
    output = tmp_path / "recovery"
    recover_stale_clob_history(
        queue, output, batch_size=2, workers=2, batch_delay_seconds=0,
        client=FakeClobHistoryClient(), require_clean_git=False,
    )
    raw = next((output / "batches").rglob("*.raw.json"))
    before = hashlib.sha256(raw.read_bytes()).hexdigest()
    raw.write_bytes(b"{}")
    assert hashlib.sha256(raw.read_bytes()).hexdigest() != before

    with pytest.raises(EvidenceError, match="manifest hash verification failed"):
        stale_history_recovery_status(output)


def test_recovery_cli_returns_compact_evidence_receipt(tmp_path: Path, monkeypatch, capsys):
    queue = _sealed_stale_queue(tmp_path)
    output = tmp_path / "recovery"
    original = ledger_cli.recover_stale_clob_history

    def run_fake(*args, **kwargs):
        kwargs["client"] = FakeClobHistoryClient()
        kwargs["require_clean_git"] = False
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger_cli, "recover_stale_clob_history", run_fake)
    code = ledger_cli.main([
        "recover-stale-clob-history",
        "--gap-queue", str(queue),
        "--output", str(output),
        "--batch-size", "2",
        "--workers", "2",
        "--batch-delay-seconds", "0",
    ])

    assert code == 2
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "RECOVERY_COMPLETE_EVIDENCE_NOT_INTEGRATED"
    assert receipt["recovered_candidates"] == 1
    assert "recovered_candidates.json" not in receipt
