from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

import polymarket_bot.v060_audit as frozen_audit
from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v060_contract import VARIANT


ADDENDUM_SCHEMA = "prereg_v060_audit_ordering_addendum_1"


class V060CorrectedAuditError(RuntimeError):
    pass


class _ConnectionProxy:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, *args: Any) -> Any:
        cursor = self._connection.execute(sql, *args)
        normalized = " ".join(sql.lower().split())
        if normalized == "select * from v058_position_map order by cast(position_id as integer)":
            rows = list(cursor)
            rows.sort(key=lambda row: int(str(row["position_id"])))
            return rows
        return cursor

    def close(self) -> None:
        self._connection.close()


def _corrected_open_ro(path: Path) -> _ConnectionProxy:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return _ConnectionProxy(connection)


@contextmanager
def _corrected_reader() -> Iterator[None]:
    original = frozen_audit._open_ro
    frozen_audit._open_ro = _corrected_open_ro
    try:
        yield
    finally:
        frozen_audit._open_ro = original


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def verify_addendum(
    *, addendum_path: str | Path, prereg_path: str | Path, database_path: str | Path
) -> dict[str, Any]:
    addendum_file = Path(addendum_path).resolve()
    prereg_file = Path(prereg_path).resolve()
    database = Path(database_path).resolve()
    payload = json.loads(addendum_file.read_text(encoding="utf-8"))
    expected = {
        "schema": ADDENDUM_SCHEMA,
        "status": "FROZEN_DURING_CAPTURE_BEFORE_TERMINAL_ECONOMIC_OUTCOME",
        "variant": VARIANT,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V060CorrectedAuditError(f"Addendum V0.60 incompatible: {key}")
    identity = payload.get("capture_identity", {})
    if identity.get("preregistration_sha256") != sha256_file(prereg_file):
        raise V060CorrectedAuditError("Addendum V0.60 no corresponde al preregistro")
    correction = payload.get("permitted_audit_correction", {})
    required_true = (
        "fetch_position_map_rows_without_sqlite_integer_cast",
        "sort_in_python_with_int_position_id",
        "all_economic_formulas_unchanged",
        "all_terminal_gates_unchanged",
        "all_trade_and_join_selection_queries_unchanged",
    )
    if any(correction.get(key) is not True for key in required_true):
        raise V060CorrectedAuditError("Addendum V0.60 no autoriza exactamente la correccion")
    if correction.get("capture_database_mutation_allowed") is not False:
        raise V060CorrectedAuditError("Addendum V0.60 permitiria mutar la base")
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        run = connection.execute(
            "SELECT run_id,started_at_ms,finished_at_ms,status FROM v058_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if run is None or int(run[0]) != int(identity.get("run_id", -1)) or run[3] != "COMPLETED":
        raise V060CorrectedAuditError("Captura V0.60 incompatible o no terminal")
    created_ms = int(_parse_utc(str(payload.get("created_at"))).timestamp() * 1000)
    if int(run[1]) >= created_ms or run[2] is None or created_ms >= int(run[2]):
        raise V060CorrectedAuditError("El addendum no fue congelado durante la captura")
    return payload


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def audit_v060_corrected(
    *,
    prereg_path: str | Path,
    addendum_path: str | Path,
    database_path: str | Path,
    result_path: str | Path,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    result_file = Path(result_path).resolve()
    if result_file.exists():
        existing = json.loads(result_file.read_text(encoding="utf-8"))
        if existing.get("audit_correction", {}).get("schema") == ADDENDUM_SCHEMA:
            return existing
        raise V060CorrectedAuditError("Existe un resultado V0.60 sin la correccion preinscrita")
    addendum = verify_addendum(
        addendum_path=addendum_path, prereg_path=prereg_path, database_path=database_path
    )
    with _corrected_reader():
        payload = frozen_audit.audit_v060(
            prereg_path=prereg_path,
            database_path=database_path,
            result_path=result_file,
            project_root=project_root,
        )
    payload = dict(payload)
    evidence = dict(payload["evidence"])
    evidence.update(
        {
            "audit_ordering_addendum_sha256": sha256_file(Path(addendum_path).resolve()),
            "corrected_audit_source_sha256": sha256_file(Path(__file__).resolve()),
            "frozen_v060_sources_modified": False,
            "mapping_rows_sorted_with_python_arbitrary_precision_int": True,
        }
    )
    payload["evidence"] = evidence
    payload["audit_correction"] = {
        "schema": ADDENDUM_SCHEMA,
        "created_before_terminal_outcome": True,
        "reason": addendum["proven_reader_defect"]["cause"],
        "economic_formulas_or_gates_changed": False,
        "database_mutated": False,
    }
    _write_atomic(result_file, payload)
    return payload


__all__ = [
    "ADDENDUM_SCHEMA", "V060CorrectedAuditError", "audit_v060_corrected",
    "verify_addendum",
]
