from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .common import EvidenceError, address, canonical, digest, hex_bytes, now_utc, uint

APPLICATION_ID = 0x504C5030
SCHEMA = 2


class EvidenceStore:
    """One new SQLite file; raw tables append-only, canonicality kept separately.

    BEGIN IMMEDIATE is the writer barrier. Every writer supplies the cursor it
    observed before acquisition, including its epoch, so a reorg fences stale
    in-flight batches. Existing historical databases are refused, never migrated.
    """

    def __init__(self, path: Path, *, read_only: bool = False):
        path = Path(path).resolve()
        if not read_only:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path.as_uri() + ("?mode=ro" if read_only else "?mode=rwc"),
                                  uri=True, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        app_id = self.db.execute("PRAGMA application_id").fetchone()[0]
        tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if (tables and app_id != APPLICATION_ID) or (read_only and app_id != APPLICATION_ID):
            self.db.close()
            raise EvidenceError("Not a PolyLedger P0 database; historical files cannot be reused")
        if app_id == APPLICATION_ID:
            if self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA:
                self.db.close()
                raise EvidenceError("Unsupported evidence schema")
        if read_only:
            return
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(f"""
            PRAGMA application_id={APPLICATION_ID};
            PRAGMA user_version={SCHEMA};
            CREATE TABLE IF NOT EXISTS blocks(
                chain INTEGER NOT NULL, hash TEXT NOT NULL, number INTEGER NOT NULL,
                parent TEXT NOT NULL, event_time INTEGER NOT NULL,
                payload TEXT NOT NULL, payload_hash TEXT NOT NULL, received_at TEXT NOT NULL,
                PRIMARY KEY(chain,hash));
            CREATE TABLE IF NOT EXISTS canonical_blocks(
                chain INTEGER NOT NULL, number INTEGER NOT NULL, hash TEXT NOT NULL,
                PRIMARY KEY(chain,number), FOREIGN KEY(chain,hash) REFERENCES blocks(chain,hash));
            CREATE TABLE IF NOT EXISTS raw_logs(
                id TEXT PRIMARY KEY, chain INTEGER NOT NULL, block_hash TEXT NOT NULL,
                block_number INTEGER NOT NULL, tx TEXT NOT NULL, tx_index INTEGER NOT NULL,
                log_index INTEGER NOT NULL, address TEXT NOT NULL, payload TEXT NOT NULL,
                payload_hash TEXT NOT NULL, received_at TEXT NOT NULL,
                UNIQUE(chain,block_hash,log_index),
                FOREIGN KEY(chain,block_hash) REFERENCES blocks(chain,hash));
            CREATE TABLE IF NOT EXISTS chain_cursors(
                chain INTEGER PRIMARY KEY, first_number INTEGER NOT NULL,
                number INTEGER NOT NULL, hash TEXT NOT NULL, epoch INTEGER NOT NULL,
                scope_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS batches(
                id INTEGER PRIMARY KEY, chain INTEGER NOT NULL, manifest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS source_messages(
                id TEXT PRIMARY KEY, source TEXT NOT NULL, channel TEXT NOT NULL,
                session TEXT NOT NULL, sequence INTEGER NOT NULL, event_time INTEGER NOT NULL,
                received_at TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
                UNIQUE(source,channel,session,sequence));
            CREATE TABLE IF NOT EXISTS source_cursors(
                source TEXT NOT NULL, channel TEXT NOT NULL, session TEXT NOT NULL,
                sequence INTEGER NOT NULL, PRIMARY KEY(source,channel,session));
            CREATE TABLE IF NOT EXISTS incidents(
                id INTEGER PRIMARY KEY, code TEXT NOT NULL, evidence TEXT NOT NULL,
                received_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS contract_versions(
                id TEXT PRIMARY KEY, chain INTEGER NOT NULL, address TEXT NOT NULL,
                first_block INTEGER NOT NULL, last_block INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS contract_observations(
                id TEXT PRIMARY KEY, version_id TEXT NOT NULL, payload TEXT NOT NULL,
                FOREIGN KEY(version_id) REFERENCES contract_versions(id));
            CREATE TABLE IF NOT EXISTS decoded_events(
                id TEXT PRIMARY KEY, raw_id TEXT NOT NULL, decoder TEXT NOT NULL,
                registry_id TEXT NOT NULL, payload TEXT NOT NULL,
                FOREIGN KEY(raw_id) REFERENCES raw_logs(id));
            CREATE TABLE IF NOT EXISTS derived_runs(
                id TEXT PRIMARY KEY, chain INTEGER NOT NULL, epoch INTEGER NOT NULL,
                tip_hash TEXT NOT NULL, input_hash TEXT NOT NULL, payload TEXT NOT NULL,
                evidence_revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence_state(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL);
            INSERT OR IGNORE INTO evidence_state VALUES(1,0);
            CREATE VIEW IF NOT EXISTS canonical_logs AS
                SELECT r.* FROM raw_logs r JOIN canonical_blocks b
                ON r.chain=b.chain AND r.block_number=b.number AND r.block_hash=b.hash;
            CREATE VIEW IF NOT EXISTS canonical_decoded AS
                SELECT d.* FROM decoded_events d JOIN canonical_logs r ON r.id=d.raw_id;
            CREATE VIEW IF NOT EXISTS current_runs AS
                SELECT r.* FROM derived_runs r JOIN chain_cursors c
                ON r.chain=c.chain AND r.epoch=c.epoch AND r.tip_hash=c.hash
                WHERE r.evidence_revision=(SELECT revision FROM evidence_state WHERE id=1);
        """)
        for table in ("contract_versions", "contract_observations", "source_messages"):
            self.db.execute(f"""CREATE TRIGGER IF NOT EXISTS dependency_{table}
                AFTER INSERT ON {table} BEGIN UPDATE evidence_state SET revision=revision+1 WHERE id=1; END""")
        self.db.execute("""CREATE TRIGGER IF NOT EXISTS dependency_incident AFTER INSERT ON incidents
            WHEN NEW.code IN ('SOURCE_GAP','CONTRACT_DRIFT') BEGIN
            UPDATE evidence_state SET revision=revision+1 WHERE id=1; END""")
        for table in ("blocks", "raw_logs", "batches", "source_messages", "incidents",
                      "contract_versions", "contract_observations", "decoded_events", "derived_runs"):
            for operation in ("UPDATE", "DELETE"):
                self.db.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation}
                    BEFORE {operation} ON {table} BEGIN
                    SELECT RAISE(ABORT, 'append-only evidence'); END""")

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def cursor(self, chain: int) -> dict | None:
        row = self.db.execute("SELECT * FROM chain_cursors WHERE chain=?", (chain,)).fetchone()
        return dict(row) if row else None

    def incident(self, code: str, evidence: dict):
        self.db.execute("INSERT INTO incidents(code,evidence,received_at) VALUES(?,?,?)",
                        (code, canonical(evidence), now_utc()))

    def ingest(self, chain: int, blocks: list[dict], logs: list[dict], *,
               expected_cursor: dict | None, scope: dict, received_at: str | None = None,
               fault: Callable[[str], None] = lambda stage: None) -> dict:
        """Ingest complete filtered blocks, including empty ones, in ascending order.

        Replacement begins at a known parent. Blocks beyond the replacement tip
        become noncanonical. An unknown/deep parent requires an explicit backfill;
        it can never silently reset the acquisition anchor.
        """
        chain = uint(chain)
        if not blocks or not scope:
            raise EvidenceError("A nonempty block range and acquisition scope are required")
        received_at = received_at or now_utc()
        scope_hash = digest(scope)
        headers = []
        for b in blocks:
            headers.append((uint(b["number"]), hex_bytes(b["hash"], 32),
                            hex_bytes(b["parentHash"], 32), uint(b["timestamp"]), b))
        for previous, current in zip(headers, headers[1:]):
            if current[0] != previous[0] + 1 or current[2] != previous[1] or current[3] < previous[3]:
                raise EvidenceError("Block gap, parent mismatch or time reversal")
        by_hash = {b[1]: b for b in headers}
        if len(by_hash) != len(headers):
            raise EvidenceError("Repeated block hash")
        with self.transaction():
            old = self.cursor(chain)
            if old != expected_cursor:
                raise EvidenceError("Stale writer: cursor/epoch changed during acquisition")
            first, last = headers[0], headers[-1]
            reorg = False
            epoch = old["epoch"] if old else 0
            if old:
                if scope_hash != old["scope_hash"]:
                    raise EvidenceError("Acquisition scope changed; use a separate database")
                if first[0] < old["first_number"] or first[0] > old["number"] + 1:
                    raise EvidenceError("Gap or attempt to move the acquisition anchor")
                parent = self.db.execute(
                    "SELECT hash FROM canonical_blocks WHERE chain=? AND number=?",
                    (chain, first[0] - 1)).fetchone()
                if first[0] == old["first_number"]:
                    parent = self.db.execute(
                        "SELECT parent FROM blocks WHERE chain=? AND hash=(SELECT hash FROM canonical_blocks WHERE chain=? AND number=?)",
                        (chain, chain, first[0])).fetchone()
                if not parent or parent[0] != first[2]:
                    raise EvidenceError("Unknown common ancestor; backfill required")
                for number, block_hash, *_ in headers:
                    prior = self.db.execute("SELECT hash FROM canonical_blocks WHERE chain=? AND number=?",
                                            (chain, number)).fetchone()
                    if prior and prior[0] != block_hash:
                        reorg = True
                        break
                if reorg:
                    epoch += 1
                    self.db.execute("DELETE FROM canonical_blocks WHERE chain=? AND number>=?", (chain, first[0]))
                    self.incident("REORG", {"chain": chain, "from_block": first[0], "old_tip": old["hash"], "epoch": epoch})
            for number, block_hash, parent, event_time, payload in headers:
                encoded = canonical(payload)
                prior = self.db.execute("SELECT payload_hash FROM blocks WHERE chain=? AND hash=?", (chain, block_hash)).fetchone()
                if prior and prior[0] != digest(payload):
                    raise EvidenceError("Conflicting raw block")
                if prior:
                    supplied = {digest([chain, block_hash, address(l["address"]), hex_bytes(l["transactionHash"], 32), uint(l["logIndex"])])
                                for l in logs if hex_bytes(l["blockHash"], 32) == block_hash}
                    stored = {r[0] for r in self.db.execute("SELECT id FROM raw_logs WHERE chain=? AND block_hash=?", (chain, block_hash))}
                    if supplied != stored:
                        raise EvidenceError("Incomplete replay or changed log set for captured block")
                self.db.execute("INSERT OR IGNORE INTO blocks VALUES(?,?,?,?,?,?,?,?)",
                                (chain, block_hash, number, parent, event_time, encoded, digest(payload), received_at))
                self.db.execute("INSERT OR IGNORE INTO canonical_blocks VALUES(?,?,?)", (chain, number, block_hash))
            for log in logs:
                block_hash = hex_bytes(log["blockHash"], 32)
                if block_hash not in by_hash or uint(log["blockNumber"]) != by_hash[block_hash][0] or log.get("removed", False):
                    raise EvidenceError("Log outside batch or marked removed")
                tx = hex_bytes(log["transactionHash"], 32)
                contract = address(log["address"])
                index = uint(log["logIndex"])
                key = digest([chain, block_hash, contract, tx, index])
                prior = self.db.execute("SELECT payload_hash FROM raw_logs WHERE id=?", (key,)).fetchone()
                if prior and prior[0] != digest(log):
                    raise EvidenceError("Conflicting raw log")
                # A conflicting global log index must raise, not be ignored.
                if not prior:
                    self.db.execute("INSERT INTO raw_logs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                    (key, chain, block_hash, by_hash[block_hash][0], tx,
                                     uint(log["transactionIndex"]), index, contract,
                                     canonical(log), digest(log), received_at))
            fault("after_raw")
            # A provider changing the set of logs for an already committed block is
            # a gap/conflict, even if every individual payload is well formed.
            for _, block_hash, *_ in headers:
                supplied = {digest([chain, block_hash, address(l["address"]), hex_bytes(l["transactionHash"], 32), uint(l["logIndex"])])
                            for l in logs if hex_bytes(l["blockHash"], 32) == block_hash}
                stored = {r[0] for r in self.db.execute("SELECT id FROM raw_logs WHERE chain=? AND block_hash=?", (chain, block_hash))}
                if supplied != stored:
                    raise EvidenceError("Incomplete replay of an already captured block")
            tip_number, tip_hash = last[0], last[1]
            if old and not reorg and old["number"] > tip_number:
                tip_number, tip_hash = old["number"], old["hash"]
            self.db.execute("INSERT INTO chain_cursors VALUES(?,?,?,?,?,?) ON CONFLICT(chain) DO UPDATE SET number=excluded.number,hash=excluded.hash,epoch=excluded.epoch",
                            (chain, old["first_number"] if old else first[0], tip_number, tip_hash, epoch, scope_hash))
            fault("after_cursor")
            self.db.execute("INSERT INTO batches(chain,manifest) VALUES(?,?)", (chain, canonical({
                "scope": scope, "from": first[0], "to": last[0], "logs_hash": digest(sorted(digest(l) for l in logs)),
                "blocks_hash": digest(blocks), "reorg": reorg, "received_at": received_at})))
        return self.cursor(chain)

    def capture_message(self, source: str, channel: str, session: str, sequence: int,
                        event_time: int, payload: dict, *, expected_sequence: int | None,
                        received_at: str | None = None) -> str:
        sequence, event_time = uint(sequence), uint(event_time)
        key = digest([source, channel, session, sequence])
        gap = False
        with self.transaction():
            old = self.db.execute("SELECT sequence FROM source_cursors WHERE source=? AND channel=? AND session=?",
                                  (source, channel, session)).fetchone()
            previous = old[0] if old else None
            prior = self.db.execute("SELECT payload_hash,event_time FROM source_messages WHERE id=?", (key,)).fetchone()
            if prior:
                if tuple(prior) != (digest(payload), event_time):
                    raise EvidenceError("Conflicting source sequence")
                return key
            if previous != expected_sequence:
                raise EvidenceError("Stale source cursor")
            if sequence != (previous + 1 if previous is not None else 0):
                self.incident("SOURCE_GAP", {"source": source, "channel": channel, "session": session,
                                             "expected": previous, "observed": sequence,
                                             "event_time": event_time, "payload": payload})
                gap = True
            else:
                self.db.execute("INSERT INTO source_messages VALUES(?,?,?,?,?,?,?,?,?)",
                                (key, source, channel, session, sequence, event_time, received_at or now_utc(), canonical(payload), digest(payload)))
                self.db.execute("INSERT INTO source_cursors VALUES(?,?,?,?) ON CONFLICT(source,channel,session) DO UPDATE SET sequence=excluded.sequence",
                                (source, channel, session, sequence))
        if gap:
            raise EvidenceError("Source gap: cursor not advanced; backfill or new snapshot/session required")
        return key

    def logs(self, chain: int) -> Iterator[dict]:
        for row in self.db.execute("SELECT * FROM canonical_logs WHERE chain=? ORDER BY block_number,tx_index,log_index", (chain,)):
            yield {**dict(row), "raw": json.loads(row["payload"])}

    def revision(self) -> int:
        return self.db.execute("SELECT revision FROM evidence_state WHERE id=1").fetchone()[0]

    def verify(self) -> dict:
        """Read-only structural and payload-hash audit; no repairs or rewrites."""
        errors = []
        integrity = self.db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            errors.append("SQLITE_INTEGRITY_FAILURE")
        if self.db.execute("PRAGMA foreign_key_check").fetchall():
            errors.append("FOREIGN_KEY_FAILURE")
        checked = 0
        for table in ("blocks", "raw_logs", "source_messages"):
            for row in self.db.execute(f"SELECT payload,payload_hash FROM {table}"):
                checked += 1
                if digest(json.loads(row["payload"])) != row["payload_hash"]:
                    errors.append(table.upper() + "_HASH_MISMATCH")
        for cursor in self.db.execute("SELECT * FROM chain_cursors"):
            rows = self.db.execute("SELECT b.* FROM canonical_blocks c JOIN blocks b ON c.chain=b.chain AND c.hash=b.hash WHERE c.chain=? ORDER BY c.number",
                                   (cursor["chain"],)).fetchall()
            if (not rows or rows[0]["number"] != cursor["first_number"] or rows[-1]["hash"] != cursor["hash"]
                    or rows[-1]["number"] != cursor["number"]):
                errors.append("CURSOR_CANONICAL_MISMATCH")
            for previous, current in zip(rows, rows[1:]):
                if current["number"] != previous["number"] + 1 or current["parent"] != previous["hash"]:
                    errors.append("CANONICAL_CHAIN_GAP")
        return {"status": "OK" if not errors else "BLOCKED", "payloads_checked": checked,
                "errors": sorted(set(errors)), "sqlite_integrity": integrity}

    def save_run(self, chain: int, expected_cursor: dict, input_hash: str, result: dict,
                 *, expected_revision: int | None = None) -> str:
        revision = self.revision() if expected_revision is None else expected_revision
        key = digest([chain, expected_cursor, input_hash, result, revision])
        with self.transaction():
            if self.cursor(chain) != expected_cursor or self.revision() != revision:
                raise EvidenceError("Cannot publish reconstruction: canonical chain changed or new source/contract evidence")
            self.db.execute("INSERT OR IGNORE INTO derived_runs VALUES(?,?,?,?,?,?,?)",
                            (key, chain, expected_cursor["epoch"], expected_cursor["hash"], input_hash, canonical(result), revision))
        return key
