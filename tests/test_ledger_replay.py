import copy
from pathlib import Path

from polymarket_bot.ledger.fixtures import sample_bundle
from polymarket_bot.ledger.replay import P0_WINDOW_SECONDS, replay

CATALOG = Path(__file__).resolve().parents[1] / "configs/polyledger/abi_catalog.json"


def test_full_replay_two_balance_pipelines_and_exact_pnl(tmp_path):
    bundle = sample_bundle(CATALOG)
    first = replay(bundle, tmp_path / "one.db")
    assert first["failures"] == []
    assert first["reconciliation"]["status"] == "MATCH"
    wallet = bundle["wallet"]
    assert first["ledger"]["known_realized_pnl"][wallet] == 10
    assert first["ledger"]["unrealized_pnl"][wallet] == "7"
    assert first["p0_exit"]["status"] == "BLOCKED"
    second = replay(bundle, tmp_path / "two.db")
    assert first == second
    # Acquisition chunking can change while economic replay stays identical.
    chunked = copy.deepcopy(bundle)
    chunked["batches"] = [{"blocks": [h for b in bundle["batches"] for h in b["blocks"]],
                            "logs": [l for b in bundle["batches"] for l in b["logs"]]}]
    third = replay(chunked, tmp_path / "three.db")
    assert first["ledger"]["ledger_hash"] == third["ledger"]["ledger_hash"]


def test_missing_independent_log_and_contract_drift_block_replay(tmp_path):
    for case in ("gap", "drift"):
        bundle = sample_bundle(CATALOG)
        if case == "gap":
            bundle["independent"]["raw_logs"].pop()
        else:
            bundle["contract_observations"][0]["code"] = "0x6001"
        result = replay(bundle, tmp_path / (case + ".db"))
        assert result["reconciliation"]["status"] == "BLOCKED"
        assert result["p0_exit"]["status"] == "BLOCKED"


def test_opening_lot_provenance_does_not_depend_on_acquisition_chunking(tmp_path):
    bundle = sample_bundle(CATALOG)
    asset = next(a for a in bundle["required_assets"] if a.endswith(":1"))
    bundle["opening"]["lots"] = [{"wallet": bundle["wallet"], "asset": asset, "quantity": 1, "cost": 4}]
    bundle["onchain"]["balances"][asset] += 1
    first = replay(bundle, tmp_path / "first.db")
    bundle["batches"] = [{"blocks": [h for b in bundle["batches"] for h in b["blocks"]],
                          "logs": [l for b in bundle["batches"] for l in b["logs"]]}]
    second = replay(bundle, tmp_path / "second.db")
    assert first["reconciliation"]["status"] == second["reconciliation"]["status"] == "MATCH"
    assert first["ledger"]["ledger_hash"] == second["ledger"]["ledger_hash"]


def test_operator_window_is_now_twenty_four_hours(tmp_path):
    bundle = sample_bundle(CATALOG)
    origin = 1_800_000_000
    for index, batch in enumerate(bundle["batches"]):
        timestamp = origin + (P0_WINDOW_SECONDS * index // (len(bundle["batches"]) - 1))
        batch["blocks"][0]["timestamp"] = hex(timestamp)
    bundle["as_of"] = origin + P0_WINDOW_SECONDS
    for row in bundle["health"]:
        row["received_at"] = bundle["as_of"]
    result = replay(bundle, tmp_path / "window.db")
    assert result["duration_seconds"] == P0_WINDOW_SECONDS
    assert "TWENTY_FOUR_HOUR_REPLAY_MISSING" not in result["p0_exit"]["blockers"]
    assert result["p0_exit"]["status"] == "BLOCKED"
