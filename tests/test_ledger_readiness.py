import json
import sqlite3

import duckdb
import pytest

from polymarket_bot.ledger.common import EvidenceError
from polymarket_bot.ledger.readiness import audit_archived_pilot


WALLET = "0x" + "11" * 20


def archives(tmp_path):
    parquet = tmp_path / "activity.parquet"
    analytical = duckdb.connect(":memory:")
    analytical.execute(
        "CREATE TABLE activity(timestamp_utc TIMESTAMP,transaction_hash VARCHAR,block JSON,event_type VARCHAR,"
        "size DOUBLE,price DOUBLE,usdc_size DOUBLE,lifecycle_action VARCHAR)")
    analytical.execute(
        "INSERT INTO activity VALUES "
        "('2026-09-01 00:00:00','0x01',NULL,'TRADE',1,0.5,0.5,NULL),"
        "('2026-09-08 00:00:00','0x02','1','TRADE',1,0.5,0.5,NULL)")
    analytical.execute("COPY activity TO ? (FORMAT PARQUET)", [str(parquet)])
    analytical.close()
    database = tmp_path / "onchain.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE onchain_fills(transaction_hash TEXT,log_index INTEGER,"
                           "block_number INTEGER,raw_json TEXT,maker TEXT,taker TEXT)")
        connection.execute("INSERT INTO onchain_fills VALUES('0x02',0,10,'{}',?,?)", (WALLET, WALLET))
        connection.execute("CREATE TABLE scan_progress(exchange_key TEXT,last_block INTEGER,"
                           "head_block INTEGER,updated_at INTEGER)")
        connection.execute("INSERT INTO scan_progress VALUES('v2_ctf',10,10,1)")
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"verification_status": "VERIFIED", "verified_proxy_wallet": WALLET,
                                    "verified_profile_url": "https://example.test/@pilot"}))
    return parquet, database, identity


def test_legacy_census_is_useful_but_never_approves_p0(tmp_path):
    activity, onchain, identity = archives(tmp_path)
    result = audit_archived_pilot(activity, onchain, identity, wallet=WALLET)
    assert result["status"] == "BLOCKED"
    assert result["activity_archive"]["window_rows"] == 2
    assert result["onchain_fill_archive"]["recent_activity_transactions_linked"] == 1
    assert result["gates"]["timestamp_span_present"] is True
    assert result["gates"]["atomic_accounting_inputs"] is False
    assert "OPENING_INVENTORY_AND_ATOMIC_BASIS_MISSING" in result["blockers"]
    assert result["execution_allowed"] is False
    json.dumps(result)


def test_census_rejects_missing_schema_and_bad_window(tmp_path):
    activity, onchain, identity = archives(tmp_path)
    with pytest.raises(EvidenceError, match="1..31"):
        audit_archived_pilot(activity, onchain, identity, wallet=WALLET, window_days=0)
    broken = tmp_path / "broken.parquet"
    connection = duckdb.connect(":memory:")
    connection.execute("COPY (SELECT TIMESTAMP '2026-09-01' AS timestamp_utc) TO ? (FORMAT PARQUET)",
                       [str(broken)])
    connection.close()
    with pytest.raises(EvidenceError, match="missing required columns"):
        audit_archived_pilot(broken, onchain, identity, wallet=WALLET)


def test_census_binds_identity_and_onchain_rows_to_wallet(tmp_path):
    activity, onchain, identity = archives(tmp_path)
    payload = json.loads(identity.read_text())
    payload["verified_proxy_wallet"] = "0x" + "22" * 20
    identity.write_text(json.dumps(payload))
    with pytest.raises(EvidenceError, match="does not verify"):
        audit_archived_pilot(activity, onchain, identity, wallet=WALLET)
