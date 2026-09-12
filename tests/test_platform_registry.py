from __future__ import annotations

import json
from pathlib import Path

import pytest

from polymarket_bot.platform.registry import (
    ManifestError,
    create_draft_bot,
    discover_manifests,
    validate_manifest,
    validate_registry,
)


def _project(tmp_path: Path) -> Path:
    (tmp_path / "configs" / "bots").mkdir(parents=True)
    return tmp_path


def test_create_draft_bot_is_fail_closed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    manifest = create_draft_bot(root, "sports-nba-v1", "NBA Research", "sports")
    assert manifest["lifecycle_stage"] == "draft"
    assert manifest["enabled"] is False
    assert manifest["safety"] == {
        "real_money": False,
        "automatic_orders": False,
        "withdrawals": False,
    }
    assert (root / "apps" / "sports-nba-v1" / "main.py").exists()
    assert validate_registry(root)["status"] == "OK"


def test_create_refuses_to_overwrite(tmp_path: Path) -> None:
    root = _project(tmp_path)
    create_draft_bot(root, "climate-la-v1", "Climate LA", "climate")
    with pytest.raises(ManifestError):
        create_draft_bot(root, "climate-la-v1", "Climate LA", "climate")


def test_validation_blocks_money_and_live_mode(tmp_path: Path) -> None:
    root = _project(tmp_path)
    manifest = create_draft_bot(root, "copy-wallet-v1", "Copy Wallet", "wallet-copy")
    manifest["lifecycle_stage"] = "live"
    manifest["execution"]["mode"] = "live"
    manifest["safety"]["real_money"] = True
    path = root / "configs" / "bots" / "copy-wallet-v1.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    errors, _ = validate_manifest(manifest, path, root)
    assert any("real_money" in error for error in errors)
    assert any("live está bloqueado" in error for error in errors)


def test_discovery_ignores_registry_metadata(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "configs" / "bots" / "registry.json").write_text("{}", encoding="utf-8")
    create_draft_bot(root, "btc-five-v1", "BTC 5m", "crypto")
    records = discover_manifests(root)
    assert [manifest["bot_id"] for _, manifest in records] == ["btc-five-v1"]
