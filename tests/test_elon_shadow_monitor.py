from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from polymarket_bot.elon_post_count_research import Bucket
from polymarket_bot.elon_shadow_monitor import ShadowStore, evaluate_side


def test_positive_edge_is_shadow_only() -> None:
    row = evaluate_side(
        bucket="40-64",
        side="YES",
        probability=0.70,
        ask=0.50,
        ask_size=100,
        threshold=0.05,
        fee_rate=0.05,
        fees_enabled=True,
        count_consistent=True,
        remaining_hours=8,
    )
    assert row["decision"] == "SHADOW_SIGNAL"
    assert row["net_edge"] > 0.05


def test_inconsistent_count_is_fail_closed() -> None:
    row = evaluate_side(
        bucket="40-64",
        side="YES",
        probability=0.90,
        ask=0.20,
        ask_size=100,
        threshold=0.05,
        fee_rate=0.05,
        fees_enabled=True,
        count_consistent=False,
        remaining_hours=8,
    )
    assert row["decision"] == "NO_TRADE"
    assert row["reason"] == "COUNT_NOT_SYNCHRONIZED"


def test_insufficient_size_blocks_signal() -> None:
    row = evaluate_side(
        bucket="65-89",
        side="NO",
        probability=0.80,
        ask=0.30,
        ask_size=4.99,
        threshold=0.05,
        fee_rate=0.05,
        fees_enabled=True,
        count_consistent=True,
        remaining_hours=2,
    )
    assert row["decision"] == "NO_TRADE"
    assert row["reason"] == "INSUFFICIENT_VISIBLE_SIZE"


def test_store_declares_no_wallet_or_orders(tmp_path: Path) -> None:
    store = ShadowStore(tmp_path / "shadow.db", tmp_path / "research")
    try:
        meta = {row[0]: json.loads(row[1]) for row in store.db.execute("SELECT key,value FROM shadow_meta")}
        assert meta["wallet_required"] is False
        assert meta["orders_enabled"] is False
        assert meta["real_money_enabled"] is False
        tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "shadow_signals" in tables
        assert "orders" not in tables
    finally:
        store.close()


def test_signal_resolution_uses_visible_fill_and_fee(tmp_path: Path) -> None:
    store = ShadowStore(tmp_path / "shadow.db", tmp_path / "research")
    now = datetime.now(timezone.utc)
    bucket = Bucket("40-64", 40, 64, "yes", "no", "m1", "c1", True, 0.05)
    try:
        run_id = store.start_run(1, 30)
        cycle_id = store.start_cycle(run_id)
        store.upsert_market(
            {"id": "t1", "title": "test", "startDate": now - timedelta(hours=1), "endDate": now},
            {"id": "e1", "slug": "test-market", "description": "test"},
            [bucket],
            now,
        )
        snapshot_id = store.insert_snapshot(
            cycle_id,
            "test-market",
            now,
            "test-model",
            {"40-64": 0.70},
            np.array([0.0, 1.0]),
            {"current_count": 50, "time_elapsed_hours": 1, "time_remaining_hours": 1},
            50,
            True,
            "NORMAL",
        )
        row = evaluate_side(
            bucket="40-64",
            side="YES",
            probability=0.70,
            ask=0.50,
            ask_size=100,
            threshold=0.05,
            fee_rate=0.05,
            fees_enabled=True,
            count_consistent=True,
            remaining_hours=1,
        )
        assert store.insert_signal(snapshot_id, "test-market", now, 50, row, now)
        assert store.resolve_market_signals("test-market", "40-64", 50, now) == 1
        resolved = store.db.execute("SELECT * FROM shadow_signals").fetchone()
        assert resolved["status"] == "RESOLVED_SHADOW"
        assert resolved["payout"] == 1.0
        assert abs(resolved["pnl_usdc"] - 48.75) < 1e-9
    finally:
        store.close()
