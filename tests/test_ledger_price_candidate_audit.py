import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from polymarket_bot.ledger import __main__ as ledger_cli
from polymarket_bot.ledger.common import EvidenceError, canonical
from polymarket_bot.ledger.price_candidate_audit import (
    OfficialCandidateMetadataClient,
    audit_price_candidates_file,
)
from polymarket_bot.ledger.price_history_probe import build_price_history_probe_file
from test_ledger_price_history_probe import FakePriceClient, FakeRPC, _source_and_gaps, h


CONDITION = h(123)


class FakeCandidateClient:
    def __init__(self, *, clob_outcome: str = "Yes"):
        self.clob_outcome = clob_outcome
        self.urls = []

    def fetch(self, url: str) -> dict:
        self.urls.append(url)
        parsed = urlsplit(url)
        if parsed.path == "/markets-by-token/1":
            payload = {
                "condition_id": CONDITION,
                "primary_token_id": "1",
                "secondary_token_id": "2",
            }
        elif parsed.path == "/clob-markets/" + CONDITION:
            peer_outcome = "No" if self.clob_outcome == "Yes" else "Yes"
            payload = {"t": [
                {"t": "1", "o": self.clob_outcome},
                {"t": "2", "o": peer_outcome},
            ]}
        elif parsed.path == "/markets":
            assert parse_qs(parsed.query) == {"condition_ids": [CONDITION], "limit": ["10"]}
            payload = [{
                "id": "market-123",
                "conditionId": CONDITION,
                "question": "Will the test condition occur?",
                "slug": "test-condition",
                "startDate": "1970-01-01T00:00:00Z",
                "endDate": "2100-01-01T00:00:00Z",
                "closedTime": None,
                "active": True,
                "closed": False,
                "negRisk": False,
                "resolutionSource": "https://example.invalid/rules",
                "clobTokenIds": '["1","2"]',
                "outcomes": '["Yes","No"]',
            }]
        else:  # pragma: no cover - a changed allowlisted route must fail loudly
            raise AssertionError(url)
        body = canonical(payload).encode("utf-8")
        return {
            "body": body,
            "status": 200,
            "response_headers": {"content-type": "application/json"},
            "attempts": [{"attempt": 1, "status": 200, "latency_ms": 1}],
        }


def _fresh_probe(tmp_path: Path) -> Path:
    source, gaps, value, _, _ = _source_and_gaps(tmp_path)
    probe = tmp_path / "probe"
    build_price_history_probe_file(
        source, gaps, probe,
        rpc=FakeRPC(host="rpc-one.invalid", block_hash=value["closing"]["block_hash"]),
        secondary_rpc=FakeRPC(host="rpc-two.invalid", block_hash=value["closing"]["block_hash"]),
        client=FakePriceClient({
            "1": b'{"data":[{"timestamp":999,"price":0.6,"resolution_seconds":60}]}',
            "2": b'{"data":[]}',
        }),
        sample_size=20,
        max_age_seconds=60,
    )
    return probe


def test_candidate_audit_seals_official_identity_without_integrating(tmp_path: Path):
    probe = _fresh_probe(tmp_path)
    output = tmp_path / "audit"
    client = FakeCandidateClient()
    probe_hash = hashlib.sha256((probe / "run_manifest.json").read_bytes()).hexdigest()

    summary = audit_price_candidates_file(probe, output, client=client)

    assert summary["status"] == "AUDIT_COMPLETE_EVIDENCE_PENDING_REVIEW"
    assert summary["requests"] == {
        "attempted": 3,
        "raw_saved": 3,
        "transport_errors": 0,
        "parse_errors": 0,
    }
    assert summary["reviews"] == {
        "current_official_identity_consistent": 1,
        "eligible_for_mark_policy_review": 1,
        "not_eligible": 0,
        "auxiliary_local_mapping_before_cutoff": 0,
    }
    assert summary["integration"] == {
        "closing_marks_written": 0,
        "basis_bundle_modified": False,
        "status": "NOT_INTEGRATED",
    }
    assert summary["review_gate"]["status"] == "BLOCKED"
    assert len(client.urls) == 3
    reviews = json.loads((output / "candidate_reviews.json").read_text(encoding="utf-8"))
    assert reviews[0]["current_identity"]["status"] == "CURRENT_OFFICIAL_IDENTITY_CONSISTENT"
    assert reviews[0]["current_identity"]["outcome"] == "YES"
    assert reviews[0]["integration"]["status"] == "PENDING_MARK_POLICY_REVIEW"
    assert reviews[0]["integration"]["closing_mark_written"] is False
    assert not (output / "closing_marks.json").exists()
    assert hashlib.sha256((probe / "run_manifest.json").read_bytes()).hexdigest() == probe_hash
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    assert sum(name.startswith("responses/") for name in manifest["files"]) == 3
    assert all((output / name).is_file() for name in manifest["files"])

    with pytest.raises(EvidenceError, match="already exists"):
        audit_price_candidates_file(probe, output, client=client)


def test_candidate_audit_blocks_conflicting_clob_outcome(tmp_path: Path):
    probe = _fresh_probe(tmp_path)
    output = tmp_path / "audit"

    summary = audit_price_candidates_file(
        probe, output, client=FakeCandidateClient(clob_outcome="No")
    )

    assert summary["reviews"]["current_official_identity_consistent"] == 0
    assert summary["reviews"]["eligible_for_mark_policy_review"] == 0
    assert summary["reviews"]["not_eligible"] == 1
    requests = json.loads((output / "request_results.json").read_text(encoding="utf-8"))
    clob = next(row for row in requests if row["kind"] == "clob")
    assert "conflicts" in clob["parse_error"]
    assert summary["requests"]["transport_errors"] == 0
    assert summary["requests"]["parse_errors"] == 1
    review = json.loads((output / "candidate_reviews.json").read_text(encoding="utf-8"))[0]
    assert review["integration"]["status"] == "NOT_ELIGIBLE_FOR_POLICY_REVIEW"
    assert "CLOB_OUTCOME_LABEL_UNVERIFIED" in review["integration"]["reasons"]


def test_candidate_audit_rejects_tampered_probe_before_network(tmp_path: Path):
    probe = _fresh_probe(tmp_path)
    rows = json.loads((probe / "request_results.json").read_text(encoding="utf-8"))
    fresh = next(row for row in rows if row["status"] == "FRESH_CANDIDATE")
    (probe / fresh["raw_response_file"]).write_bytes(b"{}")
    client = FakeCandidateClient()

    with pytest.raises(EvidenceError, match="manifest hash verification failed"):
        audit_price_candidates_file(probe, tmp_path / "audit", client=client)

    assert client.urls == []


def test_candidate_audit_cli_is_compact_and_remains_blocked(tmp_path: Path, monkeypatch, capsys):
    probe = _fresh_probe(tmp_path)
    output = tmp_path / "audit"
    monkeypatch.setattr(ledger_cli, "OfficialCandidateMetadataClient", FakeCandidateClient)

    code = ledger_cli.main([
        "audit-price-candidates", "--probe", str(probe), "--output", str(output),
        "--metadata-db", str(tmp_path / "missing.db"),
    ])

    receipt = json.loads(capsys.readouterr().out)
    assert code == 2
    assert receipt["status"] == "AUDIT_COMPLETE_EVIDENCE_PENDING_REVIEW"
    assert receipt["output"] == str(output.resolve())
    assert receipt["review_gate"]["status"] == "BLOCKED"
    assert "candidate_reviews" not in receipt


def test_candidate_audit_transport_rejects_nonstandard_https_port():
    with pytest.raises(EvidenceError, match="credential-free HTTPS"):
        OfficialCandidateMetadataClient._validate_url(
            "https://clob.polymarket.com:444/markets-by-token/1"
        )
