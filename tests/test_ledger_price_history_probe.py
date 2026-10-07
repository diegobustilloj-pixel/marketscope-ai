import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from polymarket_bot.ledger.common import EvidenceError, canonical
from polymarket_bot.ledger.evidence_gaps import build_basis_evidence_gap_file
from polymarket_bot.ledger.price_history_probe import (
    COMBO_CONTRACT,
    CTF_CONTRACT,
    PUSD_CONTRACT,
    build_price_history_probe_file,
    parse_history_observation,
    select_ctf_probe_assets,
)
from test_ledger_inventory_basis import QUOTE, bundle


def h(number: int) -> str:
    return "0x" + f"{number:064x}"


class FakeRPC:
    def __init__(self, *, host: str, block_hash: str, timestamp: int = 1_000):
        self.url = "https://" + host
        self.block_hash = block_hash
        self.timestamp = timestamp

    def call(self, method, params):
        assert method == "eth_chainId"
        assert params == []
        return "0x89"

    def block(self, number):
        return {"number": hex(number), "hash": self.block_hash, "timestamp": hex(self.timestamp)}


class FakePriceClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.urls = []

    def fetch(self, url):
        self.urls.append(url)
        token = parse_qs(urlsplit(url).query)["token_id"][0]
        body = self.payloads[token]
        return {
            "body": body,
            "status": 200,
            "response_headers": {"content-type": "application/json"},
            "attempts": [{"attempt": 1, "status": 200, "latency_ms": 1}],
        }


def _source_and_gaps(tmp_path: Path):
    value = bundle()
    yes = f"137:{CTF_CONTRACT}:1"
    no = f"137:{CTF_CONTRACT}:2"
    combo = f"137:{COMBO_CONTRACT}:3"
    pusd = f"137:{PUSD_CONTRACT}:erc20"
    value["closing_marks"] = {}
    value["marks_evidence"] = {"opening": [], "closing": []}
    value["closing"]["balances"] = {QUOTE: 73, yes: 25, no: 35, combo: 7, pusd: 11}
    source = tmp_path / "bundle.json"
    source.write_text(canonical(value) + "\n", encoding="utf-8")
    gaps = tmp_path / "gaps"
    build_basis_evidence_gap_file(source, gaps)
    return source, gaps, value, yes, no


def test_price_probe_seals_raw_ctf_only_and_never_integrates_marks(tmp_path: Path):
    source, gaps, value, yes, no = _source_and_gaps(tmp_path)
    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    client = FakePriceClient({
        "1": b'{"data":[{"timestamp":999,"price":0.6,"resolution_seconds":60}]}',
        "2": b'{"data":[]}',
    })
    output = tmp_path / "probe"

    summary = build_price_history_probe_file(
        source, gaps, output,
        rpc=FakeRPC(host="rpc-one.invalid", block_hash=value["closing"]["block_hash"]),
        secondary_rpc=FakeRPC(host="rpc-two.invalid", block_hash=value["closing"]["block_hash"]),
        client=client, sample_size=20, max_age_seconds=60,
    )

    assert summary["status"] == "PROBE_COMPLETE_EVIDENCE_PENDING_REVIEW"
    assert summary["closing_block"]["timestamp"] == 1_000
    assert summary["closing_block"]["as_of_cutoff_timestamp"] == 999
    assert summary["sample"]["eligible_ctf_outcomes"] == 2
    assert summary["sample"]["selected_ctf_outcomes"] == 2
    assert summary["sample"]["excluded_assets_by_reason"] == {
        "COMBO_POSITION_NOT_CLOB_OUTCOME": 1,
        "PUSD_COLLATERAL_NOT_CLOB_OUTCOME": 1,
    }
    assert summary["responses"]["status_counts"] == {
        "FRESH_CANDIDATE": 1,
        "NO_OBSERVATION": 1,
    }
    assert summary["integration"] == {
        "closing_marks_written": 0,
        "basis_bundle_modified": False,
        "status": "NOT_INTEGRATED",
    }
    assert len(client.urls) == 2
    assert all("token_id=" in url and "as_of=999" in url for url in client.urls)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before_hash

    rows = json.loads((output / "request_results.json").read_text(encoding="utf-8"))
    assert [row["asset"] for row in rows] == [yes, no]
    assert rows[0]["observation"] == {
        "timestamp": 999, "price": "0.6", "age_seconds": 0, "resolution_seconds": 60,
    }
    assert (output / rows[0]["raw_response_file"]).read_bytes() == client.payloads["1"]
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    assert rows[0]["raw_response_file"] in manifest["files"]
    assert not (output / "closing_marks.json").exists()


def test_price_probe_header_mismatch_preserves_partial_without_requests(tmp_path: Path):
    source, gaps, value, _, _ = _source_and_gaps(tmp_path)
    output = tmp_path / "probe"
    client = FakePriceClient({"1": b'{"data":[]}', "2": b'{"data":[]}'})

    with pytest.raises(EvidenceError, match="hash differs"):
        build_price_history_probe_file(
            source, gaps, output,
            rpc=FakeRPC(host="rpc-one.invalid", block_hash=value["closing"]["block_hash"]),
            secondary_rpc=FakeRPC(host="rpc-two.invalid", block_hash=h(1)),
            client=client, sample_size=2, max_age_seconds=0,
        )

    partial = output.with_name(output.name + ".partial")
    assert not output.exists()
    assert (partial / "configuration.json").is_file()
    assert (partial / "failure.json").is_file()
    assert not client.urls


def test_price_probe_selection_cursor_advances_sorted_queue(tmp_path: Path):
    source, gaps, value, yes, no = _source_and_gaps(tmp_path)
    selected, coverage = select_ctf_probe_assets(
        value,
        closing_mark_requests={yes: 25, no: 35},
        sample_size=1,
        start_index=1,
    )

    assert selected == [{
        "sequence": 2,
        "asset": no,
        "token_id": "2",
        "quantity_atomic": 35,
        "source_eligibility": "CTF_OUTCOME_TOKEN",
    }]
    assert coverage["selection_start_index"] == 1
    assert coverage["selection_end_index_exclusive"] == 2
    assert coverage["next_start_index"] is None


def test_price_history_parser_rejects_future_point_even_if_other_point_exists():
    observed = parse_history_observation(
        b'{"data":[{"timestamp":98,"price":"0.5"},{"timestamp":100,"price":"0.4"}]}',
        cutoff_timestamp=99,
    )
    assert observed == {
        "status": "FUTURE_OBSERVATION_REJECTED",
        "future_points": 1,
        "eligible_points": 1,
    }
