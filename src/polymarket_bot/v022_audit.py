from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v022_runner import load_and_verify_prereg


RESULT_SCHEMA = "result_v022_synced_persistent_observer_1"


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


def audit_v022(
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
        raise RuntimeError("Existe un resultado V0.22 para otra evidencia")

    before = sha256_file(db_path)
    connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        meta = {
            str(key): json.loads(str(value))
            for key, value in connection.execute("SELECT key,value FROM v022_meta")
        }
        if not meta.get("experiment_completed_at"):
            raise RuntimeError("V0.22 aun no completo sus 12 horas")
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        aggregate = connection.execute(
            """
            SELECT COUNT(*) AS markets_seen,
             COALESCE(SUM(status='COMPLETE'),0) AS markets_complete,
             COALESCE(SUM(status='INTERRUPTED_EXCLUDED'),0) AS markets_interrupted,
             COALESCE(SUM(message_count),0) AS clob_messages,
             COALESCE(SUM(valid_observations),0) AS valid_observations,
             COALESCE(SUM(synchronized_observations),0) AS synchronized_observations,
             COALESCE(SUM(unsynchronized_observations),0) AS unsynchronized_observations,
             COALESCE(SUM(raw_eligible_observations),0) AS raw_eligible_observations,
             COALESCE(SUM(raw_opportunity_episodes),0) AS raw_opportunity_episodes,
             COALESCE(SUM(confirmed_observations),0) AS confirmed_observations,
             COALESCE(SUM(confirmed_opportunity_episodes),0) AS confirmed_opportunity_episodes,
             MIN(minimum_complete_set_cost) AS minimum_complete_set_cost,
             MIN(minimum_synchronized_complete_set_cost) AS minimum_synchronized_complete_set_cost,
             MIN(minimum_raw_eligible_cost) AS minimum_raw_eligible_cost,
             MIN(minimum_confirmed_cost) AS minimum_confirmed_cost,
             MAX(maximum_candidate_persistence_ms) AS maximum_candidate_persistence_ms,
             COALESCE(SUM(stale_ask_updates_rejected),0) AS stale_ask_updates_rejected,
             COALESCE(SUM(nonmonotonic_receive_timestamps),0) AS nonmonotonic_receive_timestamps,
             COALESCE(SUM(unilateral_positions),0) AS unilateral_positions,
             COALESCE(SUM(paper_orders),0) AS paper_orders,
             COALESCE(SUM(outcomes_read),0) AS outcomes_read,
             COALESCE(SUM(orders_sent),0) AS orders_sent,
             COALESCE(SUM(real_money),0) AS real_money_rows
            FROM v022_markets
            """
        ).fetchone()
        samples = connection.execute(
            """
            SELECT COUNT(*) AS buckets,
             COALESCE(SUM(raw_eligible_observations>0),0) AS raw_buckets,
             COALESCE(SUM(confirmed_observations>0),0) AS confirmed_buckets
            FROM v022_samples
            """
        ).fetchone()
        signals = connection.execute(
            """
            SELECT COALESCE(SUM(event_type='RAW_OPEN'),0) AS raw_open,
             COALESCE(SUM(event_type='CONFIRMED'),0) AS confirmed
            FROM v022_signals
            """
        ).fetchone()
    finally:
        connection.close()
    after = sha256_file(db_path)
    if before != after:
        raise RuntimeError("La auditoria V0.22 modifico la base fuente")
    assert aggregate is not None and samples is not None and signals is not None

    gates = prereg["gates"]
    expected = int(round(float(meta["target_hours"]) * 12))
    seen = int(aggregate["markets_seen"] or 0)
    complete = int(aggregate["markets_complete"] or 0)
    interrupted = int(aggregate["markets_interrupted"] or 0)
    safety_failures: list[str] = []
    if int(aggregate["unilateral_positions"] or 0) != 0:
        safety_failures.append("unilateral_positions_nonzero")
    if int(aggregate["paper_orders"] or 0) != 0:
        safety_failures.append("paper_orders_nonzero")
    if int(aggregate["outcomes_read"] or 0) != 0 or int(meta.get("outcomes_read", 0)) != 0:
        safety_failures.append("outcomes_read_nonzero")
    if int(aggregate["orders_sent"] or 0) != 0:
        safety_failures.append("orders_sent_nonzero")
    if int(aggregate["real_money_rows"] or 0) != 0:
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
    if int(aggregate["valid_observations"] or 0) < int(gates["minimum_valid_observations"]):
        technical_failures.append("minimum_valid_observations")
    if int(aggregate["synchronized_observations"] or 0) < int(
        gates["minimum_synchronized_observations"]
    ):
        technical_failures.append("minimum_synchronized_observations")
    if int(samples["buckets"] or 0) < int(gates["minimum_sample_buckets"]):
        technical_failures.append("minimum_sample_buckets")
    if int(aggregate["nonmonotonic_receive_timestamps"] or 0) > int(
        gates["maximum_nonmonotonic_receive_timestamps"]
    ):
        technical_failures.append("maximum_nonmonotonic_receive_timestamps")
    if int(signals["raw_open"] or 0) != int(aggregate["raw_opportunity_episodes"] or 0):
        technical_failures.append("raw_signal_episode_mismatch")
    if int(signals["confirmed"] or 0) != int(
        aggregate["confirmed_opportunity_episodes"] or 0
    ):
        technical_failures.append("confirmed_signal_episode_mismatch")

    frequency_failures: list[str] = []
    if int(aggregate["raw_opportunity_episodes"] or 0) < int(
        gates["minimum_raw_opportunity_episodes"]
    ):
        frequency_failures.append("minimum_raw_opportunity_episodes")
    if int(samples["raw_buckets"] or 0) < int(gates["minimum_raw_eligible_sample_buckets"]):
        frequency_failures.append("minimum_raw_eligible_sample_buckets")

    confirmation_failures: list[str] = []
    if int(aggregate["confirmed_opportunity_episodes"] or 0) < int(
        gates["minimum_confirmed_opportunity_episodes"]
    ):
        confirmation_failures.append("minimum_confirmed_opportunity_episodes")
    if int(samples["confirmed_buckets"] or 0) < int(
        gates["minimum_confirmed_sample_buckets"]
    ):
        confirmation_failures.append("minimum_confirmed_sample_buckets")

    if safety_failures:
        verdict = "FAIL_SAFETY"
    elif technical_failures:
        verdict = "FAIL_TECHNICAL_QUALITY"
    elif frequency_failures:
        verdict = "FAIL_INSUFFICIENT_FREQUENCY"
    elif confirmation_failures:
        verdict = "FAIL_UNCONFIRMED_TRANSIENTS"
    else:
        verdict = "PASS_SYNCHRONIZED_OBSERVER_ONLY"

    result = {
        "schema": RESULT_SCHEMA,
        "created_at": _utc_now(),
        "verdict": verdict,
        "meaning": (
            "PASS_SYNCHRONIZED_OBSERVER_ONLY demuestra asks sincronizados y persistentes bajo "
            "el contrato congelado; no demuestra atomicidad, fills, PnL ni habilita dinero real."
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
            "clob_messages": int(aggregate["clob_messages"] or 0),
            "valid_observations": int(aggregate["valid_observations"] or 0),
            "synchronized_observations": int(aggregate["synchronized_observations"] or 0),
            "unsynchronized_observations": int(aggregate["unsynchronized_observations"] or 0),
            "sample_buckets_250ms": int(samples["buckets"] or 0),
        },
        "opportunities": {
            "raw_eligible_observations": int(aggregate["raw_eligible_observations"] or 0),
            "raw_opportunity_episodes": int(aggregate["raw_opportunity_episodes"] or 0),
            "raw_eligible_sample_buckets": int(samples["raw_buckets"] or 0),
            "confirmed_observations": int(aggregate["confirmed_observations"] or 0),
            "confirmed_opportunity_episodes": int(
                aggregate["confirmed_opportunity_episodes"] or 0
            ),
            "confirmed_sample_buckets": int(samples["confirmed_buckets"] or 0),
            "minimum_complete_set_cost": aggregate["minimum_complete_set_cost"],
            "minimum_synchronized_complete_set_cost": aggregate[
                "minimum_synchronized_complete_set_cost"
            ],
            "minimum_raw_eligible_cost": aggregate["minimum_raw_eligible_cost"],
            "minimum_confirmed_cost": aggregate["minimum_confirmed_cost"],
            "maximum_candidate_persistence_ms": int(
                aggregate["maximum_candidate_persistence_ms"] or 0
            ),
            "maximum_allowed_complete_set_cost": prereg["strategy"][
                "maximum_complete_set_cost"
            ],
        },
        "data_quality": {
            "stale_ask_updates_rejected": int(aggregate["stale_ask_updates_rejected"] or 0),
            "nonmonotonic_receive_timestamps": int(
                aggregate["nonmonotonic_receive_timestamps"] or 0
            ),
        },
        "failures": {
            "safety": safety_failures,
            "technical": technical_failures,
            "frequency": frequency_failures,
            "confirmation": confirmation_failures,
        },
        "sqlite_quick_check": quick,
        "paper_orders": 0,
        "orders_sent": 0,
        "outcomes_read": 0,
        "real_money": "BLOQUEADO",
        "execution_study_candidate": verdict == "PASS_SYNCHRONIZED_OBSERVER_ONLY",
        "forward_candidate": False,
    }
    if output is not None:
        _write_atomic(output, result)
    return result


__all__ = ["RESULT_SCHEMA", "audit_v022"]
