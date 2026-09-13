import json
from pathlib import Path

from polymarket_bot.ledger.valuation import _load_capture


def test_raw_capture_opening_balance_can_precede_first_activity_header(tmp_path: Path):
    """The sealed opening cut is first_block - 1 and need not be in block_headers."""
    capture = tmp_path / "capture"
    capture.mkdir()
    summary = {"status": "CAPTURED_BLOCKED", "balances": {"opening_block": 9, "closing_block": 10}}
    configuration = {"wallet": "0x" + "11" * 20}
    (capture / "summary.json").write_text(json.dumps(summary, separators=(",", ":")), encoding="utf-8")
    (capture / "configuration.json").write_text(json.dumps(configuration, separators=(",", ":")), encoding="utf-8")

    from polymarket_bot.ledger.common import digest
    from polymarket_bot.ledger.valuation import _sha256

    files = {name: _sha256(capture / name) for name in ("summary.json", "configuration.json")}
    manifest = {"files": files, "summary_hash": digest(summary)}
    (capture / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    loaded, loaded_summary, _ = _load_capture(capture)
    assert loaded["wallet"] == configuration["wallet"]
    assert loaded_summary["balances"]["opening_block"] == 9
