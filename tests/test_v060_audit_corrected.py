from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.v060_audit_corrected import _corrected_open_ro


class V060CorrectedAuditTests(unittest.TestCase):
    def test_position_ids_are_sorted_as_arbitrary_precision_integers(self) -> None:
        identifiers = [
            "99999999999999999999999999999999999999999999999999999999999999999",
            "10000000000000000000000000000000000000000000000000000000000000000",
            "20000000000000000000000000000000000000000000000000000000000000000",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "map.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE v058_position_map(position_id TEXT,clob_token_id TEXT,condition_id TEXT,outcome_index INTEGER)"
            )
            connection.executemany(
                "INSERT INTO v058_position_map VALUES(?,?,?,?)",
                [(value, str(index), "0x" + "a" * 64, index % 2) for index, value in enumerate(identifiers)],
            )
            connection.commit()
            connection.close()
            reader = _corrected_open_ro(path.resolve())
            rows = reader.execute(
                "SELECT * FROM v058_position_map ORDER BY CAST(position_id AS INTEGER)"
            )
            observed = [row["position_id"] for row in rows]
            reader.close()
        self.assertEqual(observed, sorted(identifiers, key=int))


if __name__ == "__main__":
    unittest.main()
