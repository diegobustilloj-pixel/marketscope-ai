import hashlib
import json
import shutil
from pathlib import Path

import pytest

from polymarket_bot.ledger import SAFETY
from polymarket_bot.ledger.__main__ import main as ledger_main
from polymarket_bot.ledger.common import EvidenceError, canonical, digest
from polymarket_bot.ledger.price_batch_paginator import (
    price_pagination_status,
    run_price_batch_paginator,
)
from polymarket_bot.ledger.price_candidate_audit import (
    CandidateAuditRequestError,
    _files_sha256,
)
from test_ledger_price_candidate_audit import CONDITION
from test_ledger_price_history_probe import FakePriceClient, _source_and_gaps
from test_ledger_price_mark_policy import (
    FakeSettlementRPC,
    PolicyCandidateClient,
    _metadata_db,
)


POLICY_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "configs/polyledger/closing_mark_paginator_template_v1.json"
)


def _sealed_inputs(tmp_path: Path):
    source, gaps, value, _, _ = _source_and_gaps(tmp_path)
    bundle_root = tmp_path / "sealed-bundle"
    bundle_root.mkdir()
    bundle = bundle_root / "bundle.json"
    source.replace(bundle)
    (bundle_root / "configuration.json").write_text(
        canonical({"schema": 1, "engine": "synthetic-lifetime-bundle"}) + "\n",
        encoding="utf-8",
    )
    (bundle_root / "summary.json").write_text(
        canonical({"schema": 1, "status": "BUNDLE_COMPLETE"}) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema": 1,
        "version": "lifetime-basis-bundle/3",
        "status": "BUNDLE_COMPLETE",
        "bundle_hash": digest(value),
        "files": _files_sha256(bundle_root),
        "safety": SAFETY,
    }
    (bundle_root / "run_manifest.json").write_text(
        canonical(manifest) + "\n", encoding="utf-8",
    )
    metadata = _metadata_db(tmp_path / "metadata.db")
    return bundle, gaps, metadata, value


def _rpcs(value: dict):
    arguments = {
        "block_number": value["closing"]["block_number"],
        "block_hash": value["closing"]["block_hash"],
    }
    return (
        FakeSettlementRPC(host="state-one.invalid", **arguments),
        FakeSettlementRPC(host="state-two.invalid", **arguments),
    )


def _price_client():
    return FakePriceClient({
        "1": (
            b'{"data":[{"timestamp":939,"price":0.6,"resolution_seconds":60}],'
            b'"pagination":{"limit":10000,"offset":0,"has_more":false,'
            b'"next_cursor":null}}'
        ),
        "2": (
            b'{"data":[],"pagination":{"limit":10000,"offset":0,'
            b'"has_more":false,"next_cursor":null}}'
        ),
    })


def test_paginator_pauses_resumes_and_seals_contiguous_batches(tmp_path: Path):
    bundle, gaps, metadata, value = _sealed_inputs(tmp_path)
    primary, secondary = _rpcs(value)
    output = tmp_path / "pagination"
    before = hashlib.sha256(bundle.read_bytes()).hexdigest()

    paused = run_price_batch_paginator(
        bundle=bundle, evidence_gaps=gaps, metadata_db=metadata,
        policy_template=POLICY_TEMPLATE, output=output,
        primary_rpc=primary, secondary_rpc=secondary,
        price_client=_price_client(), metadata_client=PolicyCandidateClient(),
        start_index=0, batch_size=1, max_age_seconds=900,
        max_attempts=2, retry_delay_seconds=0, batch_delay_seconds=0,
        max_batches=1, require_clean_git=False,
    )

    assert paused["status"] == "PAUSED_BATCH_LIMIT"
    assert paused["next_start_index"] == 1
    assert paused["aggregate"]["accepted_for_new_bundle_compilation"] == 1

    complete = run_price_batch_paginator(
        bundle=bundle, evidence_gaps=gaps, metadata_db=metadata,
        policy_template=POLICY_TEMPLATE, output=output,
        primary_rpc=primary, secondary_rpc=secondary,
        price_client=_price_client(), metadata_client=PolicyCandidateClient(),
        start_index=0, batch_size=1, max_age_seconds=900,
        max_attempts=2, retry_delay_seconds=0, batch_delay_seconds=0,
        resume=True, require_clean_git=False,
    )

    assert complete["status"] == "COMPLETE"
    assert complete["next_start_index"] == 2
    assert complete["batches_completed"] == 2
    assert complete["aggregate"] == {
        "outcomes_examined": 2,
        "fresh_candidates": 1,
        "stale_candidates": 0,
        "no_observation": 1,
        "candidates_audited": 1,
        "accepted_for_new_bundle_compilation": 1,
        "deferred_missing_historical_identity": 0,
        "deferred_missing_current_official_identity": 0,
        "rejected_by_policy": 0,
        "batches_without_fresh_candidates": 1,
    }
    assert (output / "summary.json").is_file()
    assert (output / "run_manifest.json").is_file()
    assert price_pagination_status(output) == complete
    assert len(list(output.rglob("batch_result.json"))) == 2
    assert not list(output.rglob("closing_marks.json"))
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == before


class FailingCandidateClient:
    def fetch(self, _url: str):
        raise CandidateAuditRequestError(
            "simulated transport failure",
            [{"attempt": 1, "status": None, "latency_ms": 1, "error": "OSError"}],
        )


def test_paginator_never_skips_a_batch_after_bounded_retries(tmp_path: Path):
    bundle, gaps, metadata, value = _sealed_inputs(tmp_path)
    primary, secondary = _rpcs(value)
    output = tmp_path / "pagination"

    result = run_price_batch_paginator(
        bundle=bundle, evidence_gaps=gaps, metadata_db=metadata,
        policy_template=POLICY_TEMPLATE, output=output,
        primary_rpc=primary, secondary_rpc=secondary,
        price_client=_price_client(), metadata_client=FailingCandidateClient(),
        start_index=0, stop_index=1, batch_size=1, max_age_seconds=900,
        max_attempts=2, retry_delay_seconds=0, batch_delay_seconds=0,
        require_clean_git=False,
    )

    assert result["status"] == "BLOCKED"
    assert result["next_start_index"] == 0
    assert result["batches_completed"] == 0
    assert result["aggregate"]["outcomes_examined"] == 0
    assert result["last_error"]["type"] == "EvidenceError"
    assert len(list(output.rglob("orchestrator_failure.json"))) == 2
    assert not list(output.rglob("batch_result.json"))
    assert not list(output.rglob("policy.json"))
    assert not list(output.rglob("closing_marks.json"))

    with pytest.raises(EvidenceError, match="use --retry-blocked"):
        run_price_batch_paginator(
            bundle=bundle, evidence_gaps=gaps, metadata_db=metadata,
            policy_template=POLICY_TEMPLATE, output=output,
            primary_rpc=primary, secondary_rpc=secondary,
            price_client=_price_client(), metadata_client=FailingCandidateClient(),
            start_index=0, stop_index=1, batch_size=1, max_age_seconds=900,
            max_attempts=2, retry_delay_seconds=0, batch_delay_seconds=0,
            resume=True, require_clean_git=False,
        )

    assert ledger_main([
        "price-pagination-status", "--output", str(output),
    ]) == 2

    recovered = run_price_batch_paginator(
        bundle=bundle, evidence_gaps=gaps, metadata_db=metadata,
        policy_template=POLICY_TEMPLATE, output=output,
        primary_rpc=primary, secondary_rpc=secondary,
        price_client=_price_client(), metadata_client=PolicyCandidateClient(),
        start_index=0, stop_index=1, batch_size=1, max_age_seconds=900,
        max_attempts=2, retry_delay_seconds=0, batch_delay_seconds=0,
        resume=True, retry_blocked=True, require_clean_git=False,
    )

    assert recovered["status"] == "COMPLETE"
    assert recovered["next_start_index"] == 1
    assert (output / "batches/batch_00001_00001/attempt_003/batch_result.json").is_file()


def test_paginator_advances_when_gamma_identity_is_explicitly_unavailable(tmp_path: Path):
    bundle, gaps, metadata, value = _sealed_inputs(tmp_path)
    primary, secondary = _rpcs(value)
    output = tmp_path / "pagination"
    before = hashlib.sha256(bundle.read_bytes()).hexdigest()

    result = run_price_batch_paginator(
        bundle=bundle, evidence_gaps=gaps, metadata_db=metadata,
        policy_template=POLICY_TEMPLATE, output=output,
        primary_rpc=primary, secondary_rpc=secondary,
        price_client=_price_client(),
        metadata_client=PolicyCandidateClient(gamma_missing=True),
        start_index=0, stop_index=1, batch_size=1, max_age_seconds=900,
        max_attempts=1, retry_delay_seconds=0, batch_delay_seconds=0,
        require_clean_git=False,
    )

    assert result["status"] == "COMPLETE"
    assert result["next_start_index"] == 1
    assert result["aggregate"]["deferred_missing_current_official_identity"] == 1
    assert result["aggregate"]["accepted_for_new_bundle_compilation"] == 0
    assert result["aggregate"]["rejected_by_policy"] == 0
    decision_path = next(output.rglob("decisions.json"))
    decision = json.loads(decision_path.read_text(encoding="utf-8"))[0]
    assert decision["status"] == "DEFERRED_MISSING_CURRENT_OFFICIAL_IDENTITY"
    assert not any(call[0] == "eth_call" for call in primary.calls)
    assert not any(call[0] == "eth_call" for call in secondary.calls)
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == before
    assert not list(output.rglob("closing_marks.json"))


def test_paginator_requires_explicit_resume_for_existing_output(tmp_path: Path):
    bundle, gaps, metadata, value = _sealed_inputs(tmp_path)
    primary, secondary = _rpcs(value)
    output = tmp_path / "pagination"
    output.mkdir()

    with pytest.raises(EvidenceError, match="use --resume explicitly"):
        run_price_batch_paginator(
            bundle=bundle, evidence_gaps=gaps, metadata_db=metadata,
            policy_template=POLICY_TEMPLATE, output=output,
            primary_rpc=primary, secondary_rpc=secondary,
            price_client=_price_client(), metadata_client=PolicyCandidateClient(),
            start_index=0, stop_index=1, batch_size=1, max_age_seconds=900,
            max_attempts=1, retry_delay_seconds=0, batch_delay_seconds=0,
            require_clean_git=False,
        )


def test_paginator_rejects_evidence_paths_outside_the_completed_attempt(tmp_path: Path):
    bundle, gaps, metadata, value = _sealed_inputs(tmp_path)
    primary, secondary = _rpcs(value)
    output = tmp_path / "pagination"
    run_price_batch_paginator(
        bundle=bundle, evidence_gaps=gaps, metadata_db=metadata,
        policy_template=POLICY_TEMPLATE, output=output,
        primary_rpc=primary, secondary_rpc=secondary,
        price_client=_price_client(), metadata_client=PolicyCandidateClient(),
        start_index=0, batch_size=1, max_age_seconds=900,
        max_attempts=1, retry_delay_seconds=0, batch_delay_seconds=0,
        max_batches=1, require_clean_git=False,
    )
    result_path = next(output.rglob("batch_result.json"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    external_probe = tmp_path / "external-probe"
    shutil.copytree(Path(result["probe"]["path"]), external_probe)
    result["probe"]["path"] = str(external_probe)
    result_path.write_text(canonical(result) + "\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match="probe path escapes"):
        run_price_batch_paginator(
            bundle=bundle, evidence_gaps=gaps, metadata_db=metadata,
            policy_template=POLICY_TEMPLATE, output=output,
            primary_rpc=primary, secondary_rpc=secondary,
            price_client=_price_client(), metadata_client=PolicyCandidateClient(),
            start_index=0, batch_size=1, max_age_seconds=900,
            max_attempts=1, retry_delay_seconds=0, batch_delay_seconds=0,
            resume=True, require_clean_git=False,
        )


def test_paginator_closes_a_final_partial_batch(tmp_path: Path):
    bundle, gaps, metadata, value = _sealed_inputs(tmp_path)
    primary, secondary = _rpcs(value)
    output = tmp_path / "pagination"

    result = run_price_batch_paginator(
        bundle=bundle, evidence_gaps=gaps, metadata_db=metadata,
        policy_template=POLICY_TEMPLATE, output=output,
        primary_rpc=primary, secondary_rpc=secondary,
        price_client=_price_client(), metadata_client=PolicyCandidateClient(),
        start_index=0, batch_size=20, max_age_seconds=900,
        max_attempts=1, retry_delay_seconds=0, batch_delay_seconds=0,
        require_clean_git=False,
    )

    assert result["status"] == "COMPLETE"
    assert result["next_start_index"] == 2
    assert result["batches_completed"] == 1
    assert (output / "batches/batch_00001_00020/attempt_001/batch_result.json").is_file()
