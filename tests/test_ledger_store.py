import sqlite3

import pytest

from polymarket_bot.ledger.common import EvidenceError, digest
from polymarket_bot.ledger.store import EvidenceStore


def h(number):
    return "0x" + f"{number:064x}"


def block(number, *, fork=0, parent=None):
    return {"number": hex(number), "hash": h(number + fork),
            "parentHash": parent or h(number - 1 + fork), "timestamp": hex(number * 2)}


def raw_log(number, *, fork=0):
    return {"blockNumber": hex(number), "blockHash": h(number + fork),
            "transactionHash": h(900), "transactionIndex": "0x0", "logIndex": "0x0",
            "address": "0x" + "11" * 20, "topics": [h(1)], "data": "0x"}


SCOPE = {"addresses": ["0x" + "11" * 20], "topics": None}


def test_crash_raw_cursor_and_reopen(tmp_path):
    path = tmp_path / "evidence.db"
    for stage in ("after_raw", "after_cursor"):
        with EvidenceStore(path) as store:
            def crash(point):
                if point == stage:
                    raise RuntimeError("injected crash")
            with pytest.raises(RuntimeError):
                store.ingest(137, [block(10)], [raw_log(10)], expected_cursor=None, scope=SCOPE, fault=crash)
        with EvidenceStore(path) as store:
            assert store.cursor(137) is None
            assert list(store.logs(137)) == []
    with EvidenceStore(path) as store:
        cursor = store.ingest(137, [block(10)], [raw_log(10)], expected_cursor=None, scope=SCOPE)
        store.ingest(137, [block(10)], [raw_log(10)], expected_cursor=cursor, scope=SCOPE)
        assert len(list(store.logs(137))) == 1


def test_reorg_preserves_orphans_invalidates_runs_and_fences_writer(tmp_path):
    with EvidenceStore(tmp_path / "e.db") as store:
        cursor = store.ingest(137, [block(10), block(11)], [raw_log(11)], expected_cursor=None, scope=SCOPE)
        store.save_run(137, cursor, digest([]), {"balances": {}})
        replacement = block(11, fork=100, parent=h(10))
        new = store.ingest(137, [replacement], [raw_log(11, fork=100)], expected_cursor=cursor, scope=SCOPE)
        assert new["epoch"] == 1
        assert store.db.execute("SELECT COUNT(*) FROM raw_logs").fetchone()[0] == 2
        assert len(list(store.logs(137))) == 1
        assert store.db.execute("SELECT COUNT(*) FROM current_runs").fetchone()[0] == 0
        with pytest.raises(EvidenceError, match="Stale writer"):
            store.ingest(137, [block(12)], [], expected_cursor=cursor, scope=SCOPE)
        with pytest.raises(EvidenceError, match="canonical chain changed"):
            store.save_run(137, cursor, digest([]), {})


def test_immutable_and_conflicting_payloads(tmp_path):
    with EvidenceStore(tmp_path / "e.db") as store:
        cursor = store.ingest(137, [block(10)], [raw_log(10)], expected_cursor=None, scope=SCOPE)
        for statement in ("DELETE FROM raw_logs", "UPDATE blocks SET event_time=1"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                store.db.execute(statement)
        with pytest.raises(EvidenceError, match="Conflicting raw log"):
            store.ingest(137, [block(10)], [{**raw_log(10), "data": "0x12"}], expected_cursor=cursor, scope=SCOPE)
        with pytest.raises(EvidenceError, match="Incomplete replay"):
            store.ingest(137, [block(10)], [], expected_cursor=cursor, scope=SCOPE)


def test_gap_scope_and_deep_reorg_fail_closed(tmp_path):
    with EvidenceStore(tmp_path / "e.db") as store:
        cursor = store.ingest(137, [block(10)], [], expected_cursor=None, scope=SCOPE)
        for blocks, scope in (([block(12)], SCOPE), ([block(11)], {"topics": []}),
                              ([block(11, fork=100)], SCOPE)):
            with pytest.raises(EvidenceError):
                store.ingest(137, blocks, [], expected_cursor=cursor, scope=scope)
            assert store.cursor(137) == cursor


def test_source_cursor_gap_crash_conflict(tmp_path):
    with EvidenceStore(tmp_path / "e.db") as store:
        store.capture_message("clob", "ws", "session", 0, 10, {"state": "open"}, expected_sequence=None)
        with pytest.raises(EvidenceError, match="Source gap"):
            store.capture_message("clob", "ws", "session", 2, 11, {}, expected_sequence=0)
        assert store.db.execute("SELECT sequence FROM source_cursors").fetchone()[0] == 0
        assert store.db.execute("SELECT code FROM incidents").fetchone()[0] == "SOURCE_GAP"
        with pytest.raises(EvidenceError, match="Conflicting"):
            store.capture_message("clob", "ws", "session", 0, 10, {}, expected_sequence=0)


def test_historical_database_is_refused(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE activity(id TEXT)")
    db.close()
    with pytest.raises(EvidenceError, match="historical"):
        EvidenceStore(path)
