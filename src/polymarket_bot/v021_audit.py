from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v021_runner import load_and_verify_prereg


RESULT_SCHEMA = "result_v021_safe_pair_observer_1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def audit_v021(
    *, database: str | Path, prereg_path: str | Path, result_path: str | Path | None = None
) -> dict[str, Any]:
    db_path = Path(database).resolve()
    prereg_file = Path(prereg_path).resolve()
    output = Path(result_path).resolve() if result_path is not None else None
    prereg = load_and_verify_prereg(prereg_file)
    if output is not None and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == RESULT_SCHEMA
            and existing.get("database_sha256") == sha256_file(db_path)
            and existing.get("preregistration_sha256") == sha256_file(prereg_file)
        ):
            return existing
        raise RuntimeError("Existe un resultado V0.21 para otra evidencia")
    before = sha256_file(db_path)
    connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        meta = {str(k): json.loads(str(v)) for k, v in connection.execute("SELECT key,value FROM v021_meta")}
        if not meta.get("experiment_completed_at"):
            raise RuntimeError("V0.21 aun no completo sus 24 horas")
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        aggregate = connection.execute(
            """
            SELECT COUNT(*),SUM(status='COMPLETE'),SUM(status='INTERRUPTED_EXCLUDED'),
             COALESCE(SUM(message_count),0),COALESCE(SUM(valid_observations),0),
             COALESCE(SUM(eligible_observations),0),COALESCE(SUM(opportunity_episodes),0),
             MIN(minimum_complete_set_cost),MAX(maximum_opportunity_persistence_ms),
             COALESCE(SUM(unilateral_positions),0),COALESCE(SUM(paper_orders),0),
             COALESCE(SUM(outcomes_read),0),COALESCE(SUM(orders_sent),0),
             COALESCE(SUM(real_money),0)
            FROM v021_markets
            """
        ).fetchone()
        sample = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(eligible),0),MIN(complete_set_cost) FROM v021_samples"
        ).fetchone()
    finally:
        connection.close()
    after = sha256_file(db_path)
    if before != after:
        raise RuntimeError("La auditoria V0.21 modifico la base fuente")

    gates = prereg["gates"]
    expected = int(round(float(meta["target_hours"]) * 12))
    seen = int(aggregate[0] or 0)
    complete = int(aggregate[1] or 0)
    interrupted = int(aggregate[2] or 0)
    safety_failures: list[str] = []
    if int(aggregate[9] or 0) != 0:
        safety_failures.append("unilateral_positions_nonzero")
    if int(aggregate[10] or 0) != 0:
        safety_failures.append("paper_orders_nonzero")
    if int(aggregate[11] or 0) != 0 or int(meta.get("outcomes_read", 0)) != 0:
        safety_failures.append("outcomes_read_nonzero")
    if int(aggregate[12] or 0) != 0:
        safety_failures.append("orders_sent_nonzero")
    if int(aggregate[13] or 0) != 0:
        safety_failures.append("real_money_nonzero")

    technical_failures: list[str] = []
    if quick != "ok":
        technical_failures.append("sqlite_quick_check")
    if complete < int(gates["minimum_complete_markets"]):
        technical_failures.append("minimum_complete_markets")
    if expected and complete / expected < float(gates["minimum_complete_market_fraction"]):
        technical_failures.append("minimum_complete_market_fraction")
    if seen and interrupted / seen > float(gates["maximum_interrupted_market_fraction"]):
        technical_failures.append("maximum_interrupted_market_fraction")
    if int(aggregate[4] or 0) < int(gates["minimum_valid_observations"]):
        technical_failures.append("minimum_valid_observations")
    if int(sample[0] or 0) < int(gates["minimum_sample_buckets"]):
        technical_failures.append("minimum_sample_buckets")

    opportunity_episodes = int(aggregate[6] or 0)
    eligible_buckets = int(sample[1] or 0)
    frequency_failures: list[str] = []
    if opportunity_episodes < int(gates["minimum_opportunity_episodes"]):
        frequency_failures.append("minimum_opportunity_episodes")
    if eligible_buckets < int(gates["minimum_eligible_sample_buckets"]):
        frequency_failures.append("minimum_eligible_sample_buckets")
    if safety_failures:
        verdict = "FAIL_SAFETY"
    elif technical_failures:
        verdict = "FAIL_TECHNICAL_QUALITY"
    elif frequency_failures:
        verdict = "FAIL_INSUFFICIENT_FREQUENCY"
    else:
        verdict = "PASS_OBSERVER_ONLY"

    result = {
        "schema": RESULT_SCHEMA,
        "created_at": _utc_now(),
        "verdict": verdict,
        "meaning": (
            "PASS_OBSERVER_ONLY solo prueba que hubo snapshots de par completo; no prueba "
            "atomicidad, fills, PnL ni habilita dinero real."
        ),
        "database": str(db_path),
        "database_sha256": before,
        "database_read_only_verified": before == after,
        "preregistration": str(prereg_file),
        "preregistration_sha256": sha256_file(prereg_file),
        "window": {
            "started_at": meta["experiment_started_at"],
            "completed_at": meta["experiment_completed_at"],
            "target_end_at": meta["target_end_at"],
            "target_hours": meta["target_hours"],
        },
        "coverage": {
            "expected_markets": expected,
            "markets_seen": seen,
            "markets_complete": complete,
            "complete_market_fraction": complete / expected if expected else None,
            "markets_interrupted_excluded": interrupted,
            "clob_messages": int(aggregate[3] or 0),
            "valid_observations": int(aggregate[4] or 0),
            "sample_buckets_250ms": int(sample[0] or 0),
        },
        "opportunities": {
            "eligible_observations": int(aggregate[5] or 0),
            "opportunity_episodes": opportunity_episodes,
            "eligible_sample_buckets": eligible_buckets,
            "minimum_complete_set_cost_market_summary": float(aggregate[7]) if aggregate[7] is not None else None,
            "minimum_complete_set_cost_samples": float(sample[2]) if sample[2] is not None else None,
            "maximum_opportunity_persistence_ms": int(aggregate[8] or 0),
            "maximum_allowed_complete_set_cost": prereg["strategy"]["maximum_complete_set_cost"],
        },
        "failures": {
            "safety": safety_failures,
            "technical": technical_failures,
            "frequency": frequency_failures,
        },
        "sqlite_quick_check": quick,
        "paper_orders": 0,
        "orders_sent": 0,
        "outcomes_read": 0,
        "real_money": "BLOQUEADO",
        "forward_candidate": False,
    }
    if output is not None:
        _write_atomic(output, result)
    return result


__all__ = ["RESULT_SCHEMA", "audit_v021"]
