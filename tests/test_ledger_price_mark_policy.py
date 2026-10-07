import hashlib
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from polymarket_bot.ledger import SAFETY
from polymarket_bot.ledger import __main__ as ledger_cli
from polymarket_bot.ledger.common import EvidenceError, canonical, digest
from polymarket_bot.ledger.evidence_gaps import build_basis_evidence_gap_file
from polymarket_bot.ledger.price_candidate_audit import (
    _files_sha256,
    audit_price_candidates_file,
)
from polymarket_bot.ledger.price_history_probe import build_price_history_probe_file
from polymarket_bot.ledger.price_history_probe import CTF_CONTRACT
from polymarket_bot.ledger.price_mark_policy import (
    POLICY_STATUS,
    POLICY_VERSION,
    _decision,
    _validate_rpc_endpoint,
    evaluate_price_policy_file,
)
from test_ledger_price_candidate_audit import CONDITION, FakeCandidateClient
from test_ledger_price_history_probe import FakePriceClient, FakeRPC, _source_and_gaps


class PolicyCandidateClient(FakeCandidateClient):
    def fetch(self, url: str) -> dict:
        response = super().fetch(url)
        if urlsplit(url).path == "/clob-markets/" + CONDITION:
            payload = json.loads(response["body"])
            payload["c"] = CONDITION
            response["body"] = canonical(payload).encode("utf-8")
        return response


class FakeSettlementRPC:
    def __init__(self, *, host: str, block_number: int, block_hash: str,
                 timestamp: int = 1_000, denominator: int = 0, numerator: int = 0):
        self.url = "https://" + host
        self.block_number = block_number
        self.block_hash = block_hash
        self.timestamp = timestamp
        self.denominator = denominator
        self.numerator = numerator
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            assert params == []
            return "0x89"
        assert method == "eth_call"
        assert params[1] == {"blockHash": self.block_hash, "requireCanonical": True}
        assert params[0]["to"] == CTF_CONTRACT
        assert params[0]["data"][10:74] == CONDITION[2:]
        selector = params[0]["data"][:10]
        if selector == "0xdd34de67":
            assert len(params[0]["data"]) == 74
            value = self.denominator
        elif selector == "0x0504c814":
            assert len(params[0]["data"]) == 138
            outcome_index = int(params[0]["data"][-64:], 16)
            assert outcome_index in {0, 1}
            value = self.numerator if outcome_index == 0 else self.denominator - self.numerator
        else:  # pragma: no cover - production allowlist regression
            raise AssertionError(selector)
        return "0x" + f"{value:064x}"

    def block(self, number):
        assert number == self.block_number
        self.calls.append(("eth_getBlockByNumber", [hex(number), False]))
        return {"number": hex(number), "hash": self.block_hash,
                "timestamp": hex(self.timestamp)}


def _metadata_db(path: Path, *, include: bool = True, fetched_at: int = 999) -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE event_metadata (event_slug TEXT, fetched_at INTEGER, error TEXT);"
            "CREATE TABLE market_metadata (condition_id TEXT, event_slug TEXT, "
            "outcomes_json TEXT, token_ids_json TEXT);"
        )
        if include:
            connection.execute(
                "INSERT INTO event_metadata VALUES (?,?,NULL)", ("test-condition", fetched_at)
            )
            connection.execute(
                "INSERT INTO market_metadata VALUES (?,?,?,?)",
                (CONDITION, "test-condition", '["Yes","No"]', '["1","2"]'),
            )
    return path


def _fresh_audit(tmp_path: Path, *, include_local: bool = True) -> tuple[Path, Path]:
    source, _, value, _, _ = _source_and_gaps(tmp_path)
    bundle_root = tmp_path / "sealed-bundle"
    bundle_root.mkdir()
    sealed_source = bundle_root / "bundle.json"
    source.replace(sealed_source)
    source = sealed_source
    (bundle_root / "configuration.json").write_text(
        canonical({"schema": 1, "engine": "synthetic-lifetime-bundle"}) + "\n",
        encoding="utf-8",
    )
    (bundle_root / "summary.json").write_text(
        canonical({"schema": 1, "status": "BUNDLE_COMPLETE"}) + "\n",
        encoding="utf-8",
    )
    bundle_manifest = {
        "schema": 1, "version": "lifetime-basis-bundle/3", "status": "BUNDLE_COMPLETE",
        "bundle_hash": digest(value), "files": _files_sha256(bundle_root), "safety": SAFETY,
    }
    (bundle_root / "run_manifest.json").write_text(
        canonical(bundle_manifest) + "\n", encoding="utf-8"
    )
    gaps = tmp_path / "sealed-gaps"
    build_basis_evidence_gap_file(source, gaps)
    probe = tmp_path / "probe"
    build_price_history_probe_file(
        source, gaps, probe,
        rpc=FakeRPC(host="probe-one.invalid", block_hash=value["closing"]["block_hash"]),
        secondary_rpc=FakeRPC(host="probe-two.invalid", block_hash=value["closing"]["block_hash"]),
        client=FakePriceClient({
            "1": (b'{"data":[{"timestamp":939,"price":0.6,"resolution_seconds":60}],'
                  b'"pagination":{"limit":10000,"offset":0,"has_more":false,'
                  b'"next_cursor":null}}'),
            "2": (b'{"data":[],"pagination":{"limit":10000,"offset":0,'
                  b'"has_more":false,"next_cursor":null}}'),
        }),
        sample_size=20, max_age_seconds=900,
    )
    metadata = _metadata_db(tmp_path / "metadata.db", include=include_local)
    audit = tmp_path / "audit"
    audit_price_candidates_file(
        probe, audit, client=PolicyCandidateClient(), metadata_db=metadata
    )
    return audit, source


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_policy(audit: Path, path: Path) -> dict:
    audit_manifest = json.loads((audit / "run_manifest.json").read_text(encoding="utf-8"))
    audit_config = json.loads((audit / "configuration.json").read_text(encoding="utf-8"))
    probe = Path(audit_config["probe_path"])
    probe_config = json.loads((probe / "configuration.json").read_text(encoding="utf-8"))
    policy = {
        "schema": 1,
        "policy_id": "synthetic-test-policy-v1",
        "status": POLICY_STATUS,
        "chain": 137,
        "conditional_tokens_contract": "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
        "root_of_trust": {
            "audit_engine": "polyledger-price-candidate-audit/2",
            "audit_code_commit": audit_manifest["code_commit"],
            "audit_manifest_sha256": _sha(audit / "run_manifest.json"),
            "audit_summary_sha256": _sha(audit / "summary.json"),
            "probe_manifest_sha256": _sha(probe / "run_manifest.json"),
            "evidence_gap_manifest_sha256": probe_config["evidence_gap_queue"]["manifest_sha256"],
            "bundle_manifest_sha256": _sha(Path(probe_config["input_path"]).parent
                                             / "run_manifest.json"),
            "bundle_sha256": probe_config["input_sha256"],
            "bundle_digest": probe_config["input_digest"],
            "closing_block_number": probe_config["closing_block"]["number"],
            "closing_block_hash": probe_config["closing_block"]["hash"],
            "cutoff_timestamp": 999,
        },
        "thresholds": {
            "maximum_age_seconds": 900,
            "minimum_resolution_seconds": 1,
            "maximum_resolution_seconds": 300,
            "require_closed_bucket": True,
            "price_interval": "STRICT_INTERIOR_0_1",
        },
        "identity": {
            "require_current_official_identity": True,
            "require_clob_compact_condition": True,
            "require_gamma_binary_pair": True,
            "require_local_mapping_before_cutoff": True,
            "allow_negrisk": False,
            "require_cutoff_within_declared_window": True,
            "current_closed_allowed_only_if_closed_after_cutoff": True,
        },
        "settlement": {
            "require_two_independent_rpcs": True,
            "require_exact_block_hash": True,
            "unresolved_denominator": 0,
            "settled_precedence": "CTF_PAYOUT_STATE_OVERRIDES_HISTORY",
            "state_anchor": "CLOSING_BLOCK_POST_STATE_MATCHING_INVENTORY",
            "approved_rpc_hosts": ["state-one.invalid", "state-two.invalid"],
        },
        "integration": {
            "write_closing_marks": False,
            "modify_basis_bundle": False,
            "automatic_approval": False,
        },
        "safety": SAFETY,
    }
    path.write_text(canonical(policy) + "\n", encoding="utf-8")
    return policy


def _rpcs(policy: dict, *, denominator_one: int = 0, denominator_two: int | None = None):
    root = policy["root_of_trust"]
    denominator_two = denominator_one if denominator_two is None else denominator_two
    return (
        FakeSettlementRPC(host="state-one.invalid", block_number=root["closing_block_number"],
                          block_hash=root["closing_block_hash"], denominator=denominator_one,
                          numerator=denominator_one),
        FakeSettlementRPC(host="state-two.invalid", block_number=root["closing_block_number"],
                          block_hash=root["closing_block_hash"], denominator=denominator_two,
                          numerator=denominator_two),
    )


def test_policy_accepts_only_for_future_bundle_and_seals_dual_rpc_evidence(tmp_path: Path):
    audit, bundle = _fresh_audit(tmp_path)
    policy_path = tmp_path / "policy.json"
    policy = _write_policy(audit, policy_path)
    primary, secondary = _rpcs(policy)
    output = tmp_path / "policy-output"
    before = _sha(bundle)

    summary = evaluate_price_policy_file(
        audit, policy_path, output, primary_rpc=primary, secondary_rpc=secondary
    )

    assert summary["status"] == "POLICY_EVALUATION_COMPLETE_NOT_INTEGRATED"
    assert summary["decisions"] == {
        "accepted_for_new_bundle_compilation": 1,
        "deferred_missing_historical_identity": 0,
        "rejected_by_policy": 0,
    }
    assert summary["integration"] == {
        "closing_marks_written": 0, "basis_bundle_modified": False,
        "status": "NOT_INTEGRATED",
    }
    decision = json.loads((output / "decisions.json").read_text(encoding="utf-8"))[0]
    assert decision["status"] == "ACCEPTED_FOR_NEW_BUNDLE_COMPILATION"
    assert decision["reasons"] == []
    assert decision["checks"]["bucket_closed_by_cutoff"] is True
    assert (decision["settlement_at_closing_block"]["status"]
            == "UNRESOLVED_AT_CLOSING_BLOCK")
    assert not (output / "closing_marks.json").exists()
    assert _sha(bundle) == before
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["engine"] == POLICY_VERSION
    assert manifest["files"] == _files_sha256(output)
    assert any(call[0] == "eth_call" for call in primary.calls)
    assert any(call[0] == "eth_call" for call in secondary.calls)


def test_policy_defers_candidate_without_pre_cut_identity(tmp_path: Path):
    audit, _ = _fresh_audit(tmp_path, include_local=False)
    policy_path = tmp_path / "policy.json"
    policy = _write_policy(audit, policy_path)
    primary, secondary = _rpcs(policy)
    output = tmp_path / "policy-output"

    summary = evaluate_price_policy_file(
        audit, policy_path, output, primary_rpc=primary, secondary_rpc=secondary
    )

    assert summary["decisions"]["deferred_missing_historical_identity"] == 1
    decision = json.loads((output / "decisions.json").read_text(encoding="utf-8"))[0]
    assert decision["status"] == "DEFERRED_MISSING_HISTORICAL_IDENTITY"
    assert decision["reasons"] == ["HISTORICAL_IDENTITY_BEFORE_CUTOFF_MISSING"]


def test_policy_ctf_settlement_supersedes_history(tmp_path: Path):
    audit, _ = _fresh_audit(tmp_path)
    policy_path = tmp_path / "policy.json"
    policy = _write_policy(audit, policy_path)
    primary, secondary = _rpcs(policy, denominator_one=1)
    output = tmp_path / "policy-output"

    summary = evaluate_price_policy_file(
        audit, policy_path, output, primary_rpc=primary, secondary_rpc=secondary
    )

    assert summary["settlement"]["settled_at_closing_block"] == 1
    assert summary["decisions"]["rejected_by_policy"] == 1
    decision = json.loads((output / "decisions.json").read_text(encoding="utf-8"))[0]
    assert "SETTLED_AT_CLOSING_BLOCK_HISTORY_SUPERSEDED_BY_CTF" in decision["reasons"]
    assert decision["settlement_at_closing_block"]["positions"][0]["settlement_ratio"] == "1/1"


def test_policy_rejects_independent_rpc_disagreement_without_output(tmp_path: Path):
    audit, _ = _fresh_audit(tmp_path)
    policy_path = tmp_path / "policy.json"
    policy = _write_policy(audit, policy_path)
    primary, secondary = _rpcs(policy, denominator_one=0, denominator_two=1)
    output = tmp_path / "policy-output"

    with pytest.raises(EvidenceError, match="RPCs disagree"):
        evaluate_price_policy_file(
            audit, policy_path, output, primary_rpc=primary, secondary_rpc=secondary
        )

    assert not output.exists()
    assert not output.with_name(output.name + ".partial").exists()


def test_policy_reparses_clob_compact_condition_instead_of_trusting_review(tmp_path: Path):
    audit, _ = _fresh_audit(tmp_path)
    requests_path = audit / "request_results.json"
    requests = json.loads(requests_path.read_text(encoding="utf-8"))
    clob = next(row for row in requests if row["kind"] == "clob")
    raw_path = audit / clob["raw_response_file"]
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    payload["c"] = "0x" + "ff" * 32
    raw_path.write_text(canonical(payload) + "\n", encoding="utf-8")
    clob["raw_response_sha256"] = _sha(raw_path)
    requests_path.write_text(canonical(requests) + "\n", encoding="utf-8")
    manifest_path = audit / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = _files_sha256(audit)
    manifest_path.write_text(canonical(manifest) + "\n", encoding="utf-8")
    policy_path = tmp_path / "policy.json"
    policy = _write_policy(audit, policy_path)
    primary, secondary = _rpcs(policy)

    with pytest.raises(EvidenceError, match="compact condition"):
        evaluate_price_policy_file(
            audit, policy_path, tmp_path / "policy-output",
            primary_rpc=primary, secondary_rpc=secondary,
        )

    assert primary.calls == []
    assert secondary.calls == []


@pytest.mark.parametrize(
    ("age", "resolution", "timestamp", "expected_reason"),
    [
        (900, 300, 100, None),
        (300, 300, 700, None),
        (299, 300, 701, "HISTORICAL_BUCKET_NOT_CLOSED_AT_CUTOFF"),
        (901, 300, 99, "HISTORICAL_OBSERVATION_TOO_OLD"),
        (900, 301, 100, "HISTORICAL_RESOLUTION_OUTSIDE_POLICY"),
        (0, 0, 1_000, "HISTORICAL_RESOLUTION_OUTSIDE_POLICY"),
        (1, 2, 999, "HISTORICAL_BUCKET_NOT_CLOSED_AT_CUTOFF"),
    ],
)
def test_decision_boundary_failures_are_explicit(age, resolution, timestamp, expected_reason):
    cutoff = 1_000
    entry = {
        "candidate": {"sequence": 1, "asset": "asset", "token_id": "1",
                      "observation": {"timestamp": timestamp, "age_seconds": age,
                                      "resolution_seconds": resolution, "price": "0.5"}},
        "review": {
            "current_identity": {"status": "CURRENT_OFFICIAL_IDENTITY_CONSISTENT"},
            "auxiliary_local_metadata": {"status": "AUXILIARY_MAPPING_BEFORE_CUTOFF"},
            "integration": {"status": "PENDING_MARK_POLICY_REVIEW",
                            "closing_mark_written": False, "basis_bundle_modified": False},
        },
        "condition_id": CONDITION, "compact_condition": CONDITION,
        "outcome": "YES", "outcome_index": 0,
        "market": {"question": "Q", "negRisk": False, "closed": False,
                   "startDate": "1970-01-01T00:00:00Z", "endDate": "2100-01-01T00:00:00Z",
                   "closedTime": None},
    }
    policy = {
        "thresholds": {"maximum_age_seconds": 900, "minimum_resolution_seconds": 1,
                       "maximum_resolution_seconds": 300}
    }
    settlement = {"primary": {"conditions": {
        CONDITION: {"status": "UNRESOLVED_AT_CLOSING_BLOCK", "payout_denominator": 0,
                    "positions": []}
    }}}

    decision = _decision(entry, cutoff=cutoff, policy=policy, settlement=settlement)

    if expected_reason is None:
        assert decision["status"] == "ACCEPTED_FOR_NEW_BUNDLE_COMPILATION"
        assert decision["reasons"] == []
    else:
        assert expected_reason in decision["reasons"]


def test_policy_root_tamper_is_rejected_before_rpc(tmp_path: Path):
    audit, _ = _fresh_audit(tmp_path)
    policy_path = tmp_path / "policy.json"
    policy = _write_policy(audit, policy_path)
    primary, secondary = _rpcs(policy)
    (audit / "summary.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match="manifest hash verification failed"):
        evaluate_price_policy_file(
            audit, policy_path, tmp_path / "policy-output",
            primary_rpc=primary, secondary_rpc=secondary,
        )

    assert primary.calls == []
    assert secondary.calls == []


def test_policy_cli_is_compact_and_always_remains_blocked(tmp_path: Path, monkeypatch, capsys):
    audit, _ = _fresh_audit(tmp_path)
    policy_path = tmp_path / "policy.json"
    policy = _write_policy(audit, policy_path)
    root = policy["root_of_trust"]

    def rpc_factory(*, url):
        return FakeSettlementRPC(
            host=urlsplit(url).hostname,
            block_number=root["closing_block_number"],
            block_hash=root["closing_block_hash"],
        )

    monkeypatch.setattr(ledger_cli, "ReadOnlyRPC", rpc_factory)
    output = tmp_path / "policy-output"
    code = ledger_cli.main([
        "evaluate-price-policy", "--audit", str(audit), "--policy", str(policy_path),
        "--output", str(output), "--rpc-url", "https://state-one.invalid",
        "--secondary-rpc-url", "https://state-two.invalid",
    ])

    receipt = json.loads(capsys.readouterr().out)
    assert code == 2
    assert receipt["status"] == "POLICY_EVALUATION_COMPLETE_NOT_INTEGRATED"
    assert receipt["output"] == str(output.resolve())
    assert receipt["decisions"]["accepted_for_new_bundle_compilation"] == 1
    assert receipt["review_gate"]["status"] == "BLOCKED"
    assert "candidate_reviews" not in receipt


@pytest.mark.parametrize("url", [
    "http://rpc.example", "https://user:pass@rpc.example", "https://rpc.example?key=x",
    "https://rpc.example/#secret", "https://rpc.example:444", "https://rpc.example/key",
    "https://localhost", "https://127.0.0.1",
])
def test_policy_rejects_non_public_or_secret_bearing_rpc_urls(url):
    rpc = type("RPC", (), {"url": url})()
    with pytest.raises(EvidenceError, match="credential-free HTTPS|public endpoint"):
        _validate_rpc_endpoint(rpc)


def test_policy_rejects_output_inside_sealed_gap_before_rpc(tmp_path: Path):
    audit, _ = _fresh_audit(tmp_path)
    policy_path = tmp_path / "policy.json"
    policy = _write_policy(audit, policy_path)
    primary, secondary = _rpcs(policy)
    probe = Path(json.loads((audit / "configuration.json").read_text())["probe_path"])
    probe_config = json.loads((probe / "configuration.json").read_text())
    output = Path(probe_config["evidence_gap_queue"]["path"]) / "forbidden-output"

    with pytest.raises(EvidenceError, match="inside the sealed evidence-gap"):
        evaluate_price_policy_file(
            audit, policy_path, output, primary_rpc=primary, secondary_rpc=secondary
        )

    assert primary.calls == []
    assert secondary.calls == []
    assert not output.exists()


def test_policy_rejects_output_inside_sealed_bundle_before_rpc(tmp_path: Path):
    audit, bundle = _fresh_audit(tmp_path)
    policy_path = tmp_path / "policy.json"
    policy = _write_policy(audit, policy_path)
    primary, secondary = _rpcs(policy)
    output = bundle.parent / "forbidden-output"

    with pytest.raises(EvidenceError, match="inside the sealed basis-bundle"):
        evaluate_price_policy_file(
            audit, policy_path, output, primary_rpc=primary, secondary_rpc=secondary
        )

    assert primary.calls == []
    assert secondary.calls == []
    assert not output.exists()


def test_policy_rejects_unapproved_rpc_host_before_calls(tmp_path: Path):
    audit, _ = _fresh_audit(tmp_path)
    policy_path = tmp_path / "policy.json"
    policy = _write_policy(audit, policy_path)
    primary, secondary = _rpcs(policy)
    primary.url = "https://different-provider.invalid"

    with pytest.raises(EvidenceError, match="approved policy allowlist"):
        evaluate_price_policy_file(
            audit, policy_path, tmp_path / "policy-output",
            primary_rpc=primary, secondary_rpc=secondary,
        )

    assert primary.calls == []
    assert secondary.calls == []


def test_policy_detects_source_mutation_during_rpc_window(tmp_path: Path):
    audit, _ = _fresh_audit(tmp_path)
    policy_path = tmp_path / "policy.json"
    policy = _write_policy(audit, policy_path)
    root = policy["root_of_trust"]
    requests = json.loads((audit / "request_results.json").read_text())
    raw_path = audit / requests[0]["raw_response_file"]

    class MutatingRPC(FakeSettlementRPC):
        mutated = False

        def call(self, method, params):
            if not self.mutated:
                self.mutated = True
                raw_path.write_bytes(raw_path.read_bytes() + b" ")
            return super().call(method, params)

    primary = MutatingRPC(host="state-one.invalid", block_number=root["closing_block_number"],
                          block_hash=root["closing_block_hash"])
    secondary = FakeSettlementRPC(host="state-two.invalid",
                                  block_number=root["closing_block_number"],
                                  block_hash=root["closing_block_hash"])
    output = tmp_path / "policy-output"

    with pytest.raises(EvidenceError, match="sealed source changed"):
        evaluate_price_policy_file(
            audit, policy_path, output, primary_rpc=primary, secondary_rpc=secondary
        )

    assert not output.exists()
