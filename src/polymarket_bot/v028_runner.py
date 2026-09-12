from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.config import Settings
from polymarket_bot.runtime_policy import enforce_forward_duration
from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v022_runner import V022ProcessLock
from polymarket_bot.v024_tournament import market_record
from polymarket_bot.v025_audit import sequence_metrics
from polymarket_bot.v026b_runner import (
    mean_pnl_upper_bound,
    poisson_mean_upper_bound,
)
from polymarket_bot.v028_forward import (
    V028_MAXIMUM_HOURS,
    V028_REQUIRED_GLOBAL_FEEDS,
    V028_REQUIRED_TWAP_WINDOW_SECONDS,
    V028Store,
    open_read_only,
    parse_utc,
    read_v028_meta,
    run_v028_forward,
)
from polymarket_bot.v028_prereg import load_and_verify_frozen_prereg
from polymarket_bot.v028_strategy import (
    PRIMARY_ID,
    eligible_arm_ids,
    frozen_arm_config,
)


VARIANT = "V0.28_UP_LOW_VOL_TWAP_LT5_REPLICATION_24H"
CHECKPOINT_HOURS = (4.0, 8.0, 12.0, 16.0, 20.0)
TECHNICAL_RETRY_MINUTES = 10.0
MINIMUM_MARKET_COVERAGE = 0.90
MINIMUM_FEATURE_COVERAGE = 0.90
MINIMUM_CONTRACT_COVERAGE = 0.90
MINIMUM_MATURE_RESOLUTION_COVERAGE = 0.90
MAXIMUM_HEALTH_AGE_SECONDS = 180.0
MINIMUM_PROFITABILITY_TRADES = 8
CANDIDATE_IDS = (PRIMARY_ID,)
IMPLEMENTATION_SCHEMA = "implementation_v028_twap_lt5_replication_1"
LAUNCH_SCHEMA = "launch_approval_v028_twap_lt5_replication_1"
IMPLEMENTATION_FILES = {
    "forward": "src/polymarket_bot/v028_forward.py",
    "runner": "src/polymarket_bot/v028_runner.py",
    "auditor": "src/polymarket_bot/v028_audit.py",
    "entrypoint": "v028_monitor.py",
    "resolution_contract": "src/polymarket_bot/resolution_contract.py",
}


class V028RunnerError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _candidate_states() -> dict[str, dict[str, Any]]:
    return {
        candidate_id: {
            "status": "ACTIVE",
            "frozen_at": None,
            "freeze_reason": None,
            "evaluation_cutoff_market_start_ms": None,
        }
        for candidate_id in CANDIDATE_IDS
    }


def build_implementation_manifest(
    *,
    prereg_path: str | Path,
    output_path: str | Path,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    output = Path(output_path).resolve()
    prereg = load_and_verify_frozen_prereg(prereg_file, project_root=root)
    code_hashes: dict[str, str] = {}
    for key, relative in IMPLEMENTATION_FILES.items():
        path = root / relative
        if not path.is_file():
            raise V028RunnerError(f"Implementación V0.28 incompleta: {relative}")
        code_hashes[key] = sha256_file(path)
    payload = {
        "schema": IMPLEMENTATION_SCHEMA,
        "status": "BUILT_TESTED_AWAITING_LAUNCH_APPROVAL",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "preregistration": str(prereg_file),
        "preregistration_sha256": sha256_file(prereg_file),
        "frozen_prereg_implementation_state": prereg["implementation"],
        "code_hashes": code_hashes,
        "runner_built": True,
        "auditor_built": True,
        "entrypoint_built": True,
        "launch_approved": False,
        "launch_status": "NOT_LAUNCHED",
        "maximum_hours": V028_MAXIMUM_HOURS,
        "new_backtest_hours": 0,
        "twap_contract": {
            "required_window": 60,
            "selection": "resolution_source_fail_closed_exact_60s",
            "fallback_allowed": False,
            "open_and_decision_max_age_ms": 5000,
            "transferred_30s_model_enabled": False,
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
    }
    if output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == IMPLEMENTATION_SCHEMA
            and existing.get("preregistration_sha256")
            == payload["preregistration_sha256"]
            and existing.get("code_hashes") == code_hashes
        ):
            return existing
        raise V028RunnerError("Existe otro manifiesto de implementación V0.28")
    _write_atomic(output, payload)
    return payload


def load_and_verify_implementation(
    path: str | Path,
    *,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    implementation_file = Path(path).resolve()
    if not implementation_file.is_file():
        raise V028RunnerError("La implementación V0.28 todavía no fue congelada")
    payload = json.loads(implementation_file.read_text(encoding="utf-8"))
    if payload.get("schema") != IMPLEMENTATION_SCHEMA:
        raise V028RunnerError("Manifiesto de implementación V0.28 incompatible")
    if payload.get("status") != "BUILT_TESTED_AWAITING_LAUNCH_APPROVAL":
        raise V028RunnerError("Implementación V0.28 no está en estado construido")
    if payload.get("launch_approved") is not False:
        raise V028RunnerError("El manifiesto de implementación no concede lanzamiento")
    expected = {
        "variant": VARIANT,
        "runner_built": True,
        "auditor_built": True,
        "entrypoint_built": True,
        "launch_approved": False,
        "launch_status": "NOT_LAUNCHED",
        "maximum_hours": V028_MAXIMUM_HOURS,
        "new_backtest_hours": 0,
        "twap_contract": {
            "required_window": 60,
            "selection": "resolution_source_fail_closed_exact_60s",
            "fallback_allowed": False,
            "open_and_decision_max_age_ms": 5000,
            "transferred_30s_model_enabled": False,
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V028RunnerError(f"Manifiesto de implementación inválido: {key}")
    preregistration_hash = payload.get("preregistration_sha256")
    if (
        not isinstance(preregistration_hash, str)
        or len(preregistration_hash) != 64
        or any(character not in "0123456789abcdef" for character in preregistration_hash)
    ):
        raise V028RunnerError(
            "Manifiesto de implementación inválido: preregistration_sha256"
        )
    hashes = payload.get("code_hashes")
    if not isinstance(hashes, Mapping) or set(hashes) != set(IMPLEMENTATION_FILES):
        raise V028RunnerError("Inventario de implementación V0.28 incompatible")
    for key, relative in IMPLEMENTATION_FILES.items():
        if sha256_file(root / relative) != hashes.get(key):
            raise V028RunnerError(f"Hash de implementación V0.28 no coincide: {key}")
    return dict(payload)


def load_and_verify_launch_approval(
    path: str | Path,
    *,
    prereg_path: str | Path,
    implementation_path: str | Path,
) -> dict[str, Any]:
    launch_file = Path(path).resolve()
    prereg_file = Path(prereg_path).resolve()
    implementation_file = Path(implementation_path).resolve()
    if not launch_file.is_file():
        raise V028RunnerError(
            "V0.28 no tiene aprobación explícita de lanzamiento; permanece NOT_LAUNCHED"
        )
    payload = json.loads(launch_file.read_text(encoding="utf-8"))
    if payload.get("schema") != LAUNCH_SCHEMA:
        raise V028RunnerError("Aprobación de lanzamiento V0.28 incompatible")
    expected = {
        "status": "APPROVED_FOR_ONE_PAPER_FORWARD",
        "variant": VARIANT,
        "maximum_hours": V028_MAXIMUM_HOURS,
        "preregistration_sha256": sha256_file(prereg_file),
        "implementation_sha256": sha256_file(implementation_file),
        "orders_enabled": False,
        "paper_orders_enabled": False,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise V028RunnerError(f"Aprobación de lanzamiento inválida: {key}")
    return dict(payload)


def bind_v028_database(
    *,
    database: str | Path,
    prereg_path: str | Path,
    launch_path: str | Path,
) -> None:
    store = V028Store(database)
    store.open(
        preregistration_sha256=sha256_file(Path(prereg_path).resolve()),
        launch_manifest_sha256=sha256_file(Path(launch_path).resolve()),
    )
    try:
        states = store.meta().get("v028_candidate_states")
        if not states:
            store.set_meta("v028_candidate_states", _candidate_states())
    finally:
        store.close()


def v028_arm_records(database: str | Path) -> dict[str, list[dict[str, Any]]]:
    arms = frozen_arm_config()
    output = {str(arm["id"]): [] for arm in arms}
    connection = open_read_only(database)
    try:
        for raw in connection.execute(
            """
            SELECT m.condition_id,m.market_start_ms,m.label,m.label_verified,
             f.feature_json
            FROM v028_markets AS m
            JOIN v028_features AS f USING(condition_id)
            ORDER BY m.market_start_ms
            """
        ):
            feature = json.loads(str(raw["feature_json"]))
            record = market_record(
                feature_json=feature,
                model_probability_up=None,
                label=(str(raw["label"]) if raw["label"] is not None else None),
                market_start_ms=int(raw["market_start_ms"]),
                condition_id=str(raw["condition_id"]),
            )
            if record is None:
                continue
            record["label_verified"] = int(raw["label_verified"])
            volatility = feature.get("volatility_regime_ratio")
            record["volatility_regime_ratio"] = (
                float(volatility) if volatility is not None else None
            )
            record["resolution_twap_window_s"] = feature.get(
                "resolution_twap_window_s"
            )
            distance = feature.get("twap_distance_to_open_bps")
            record["twap_distance_to_open_bps"] = (
                float(distance) if distance is not None else None
            )
            for arm_id in eligible_arm_ids(record, arms):
                output[arm_id].append(dict(record))
    finally:
        connection.close()
    return output


def _technical_snapshot(
    *,
    database: Path,
    checkpoint_at: datetime,
) -> dict[str, Any]:
    connection = open_read_only(database)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        counts = connection.execute(
            """
            SELECT COUNT(*) AS markets,
             SUM(CASE WHEN feature_status='SAVED' THEN 1 ELSE 0 END) AS features,
             SUM(CASE WHEN resolution_contract_status='VERIFIED' THEN 1 ELSE 0 END)
              AS verified_contracts,
             MIN(market_start_ms) AS first_start
            FROM v028_markets
            """
        ).fetchone()
        mature = connection.execute(
            """
            SELECT COUNT(*) AS markets,
             SUM(CASE WHEN label_verified=1 THEN 1 ELSE 0 END) AS resolved
            FROM v028_markets WHERE market_end_ms<=?
            """,
            (int(checkpoint_at.timestamp() * 1000) - 600_000,),
        ).fetchone()
        health = connection.execute(
            """
            SELECT recorded_at,connections_json FROM v028_health
            ORDER BY recorded_at DESC LIMIT 1
            """
        ).fetchone()
        meta = {
            str(row[0]): json.loads(str(row[1]))
            for row in connection.execute("SELECT key,value FROM v028_meta")
        }
        selected_windows = {
            int(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT m.resolution_twap_window_s
                FROM v028_markets AS m
                JOIN v028_features AS f USING(condition_id)
                WHERE m.resolution_contract_status='VERIFIED'
                 AND m.resolution_twap_window_s IS NOT NULL
                """
            )
        }
        alignment_rows = connection.execute(
            """
            SELECT m.resolution_twap_window_s,f.feature_json
            FROM v028_markets AS m JOIN v028_features AS f USING(condition_id)
            """
        ).fetchall()
        tick_windows = {
            int(row[0]): int(row[1])
            for row in connection.execute(
                """
                SELECT window_s,COUNT(*) FROM v028_twap_ticks
                GROUP BY window_s
                """
            )
        }
    finally:
        connection.close()
    markets = int(counts["markets"] or 0)
    features = int(counts["features"] or 0)
    verified_contracts = int(counts["verified_contracts"] or 0)
    first_start = counts["first_start"]
    checkpoint_ms = int(checkpoint_at.timestamp() * 1000)
    expected_markets = (
        int((checkpoint_ms - int(first_start)) // 300_000) + 1
        if first_start is not None
        else 0
    )
    market_coverage = min(1.0, markets / expected_markets) if expected_markets else 0.0
    feature_coverage = features / markets if markets else 0.0
    contract_coverage = verified_contracts / markets if markets else 0.0
    mature_markets = int(mature["markets"] or 0)
    mature_resolved = int(mature["resolved"] or 0)
    resolution_coverage = (
        mature_resolved / mature_markets if mature_markets else 1.0
    )
    connections = json.loads(str(health["connections_json"])) if health else {}
    health_age = (
        max(
            0.0,
            (checkpoint_at - parse_utc(str(health["recorded_at"]))).total_seconds(),
        )
        if health
        else math.inf
    )
    selected_feed_gates = {
        f"twap_{window_s}s_connected_passed": (
            connections.get(f"v028-rtds-twap-{window_s}s") == "CONNECTED"
        )
        for window_s in selected_windows
    }
    alignment_passed = all(
        int(json.loads(str(row["feature_json"])).get("resolution_twap_window_s"))
        == int(row["resolution_twap_window_s"])
        for row in alignment_rows
    )
    selected_ticks_passed = all(tick_windows.get(window_s, 0) > 0 for window_s in selected_windows)
    gates = {
        "sqlite_quick_check_passed": quick_check == "ok",
        "market_coverage_passed": market_coverage >= MINIMUM_MARKET_COVERAGE,
        "feature_coverage_passed": feature_coverage >= MINIMUM_FEATURE_COVERAGE,
        "resolution_contract_coverage_passed": (
            contract_coverage >= MINIMUM_CONTRACT_COVERAGE
        ),
        "feature_contract_alignment_passed": alignment_passed,
        "selected_twap_ticks_passed": selected_ticks_passed,
        "required_twap_window_passed": selected_windows.issubset(
            {V028_REQUIRED_TWAP_WINDOW_SECONDS}
        ),
        "mature_resolution_coverage_passed": (
            resolution_coverage >= MINIMUM_MATURE_RESOLUTION_COVERAGE
        ),
        "health_fresh_passed": health_age <= MAXIMUM_HEALTH_AGE_SECONDS,
        **{
            f"{feed}_connected_passed": connections.get(feed) == "CONNECTED"
            for feed in V028_REQUIRED_GLOBAL_FEEDS
        },
        **selected_feed_gates,
        "transferred_model_disabled_passed": (
            meta.get("twap_transfer_model_enabled") is False
        ),
    }
    safety_gates = {
        "orders_disabled_passed": meta.get("orders_enabled") is False,
        "paper_orders_disabled_passed": meta.get("paper_orders_enabled") is False,
        "wallet_not_required_passed": meta.get("wallet_required") is False,
        "money_real_disabled_passed": meta.get("money_real_enabled") is False,
        "v028_real_money_blocked_passed": meta.get("v028_real_money") == "BLOQUEADO",
    }
    return {
        "passed": all(gates.values()),
        "gates": gates,
        "sqlite_quick_check": quick_check,
        "expected_markets": expected_markets,
        "markets": markets,
        "market_coverage": round(market_coverage, 8),
        "features": features,
        "feature_coverage": round(feature_coverage, 8),
        "verified_resolution_contracts": verified_contracts,
        "resolution_contract_coverage": round(contract_coverage, 8),
        "selected_twap_windows": sorted(selected_windows),
        "twap_updates_by_window": {str(key): value for key, value in tick_windows.items()},
        "mature_markets": mature_markets,
        "mature_resolved": mature_resolved,
        "mature_resolution_coverage": round(resolution_coverage, 8),
        "latest_connections": connections,
        "health_age_seconds": round(health_age, 3) if math.isfinite(health_age) else None,
        "safety_passed": all(safety_gates.values()),
        "safety_gates": safety_gates,
    }


def _candidate_futility(
    *,
    candidate_id: str,
    checkpoint_hour: float,
    captured_rows: Sequence[Mapping[str, Any]],
    resolved_rows: Sequence[Mapping[str, Any]],
    first_half_count: int,
    prereg: Mapping[str, Any],
) -> dict[str, Any]:
    frequency = prereg["candidate_frequency"]
    minimum_final = int(frequency["minimum_final_trades"])
    minimum_first_half = int(frequency["minimum_first_half_trades"])
    upper_count = poisson_mean_upper_bound(len(captured_rows))
    projected = upper_count * V028_MAXIMUM_HOURS / checkpoint_hour
    frequency_futile = projected < minimum_final
    first_half_futile = (
        checkpoint_hour >= 12.0 and first_half_count < minimum_first_half
    )
    pnl_ucb = mean_pnl_upper_bound(resolved_rows)
    profitability_futile = (
        len(resolved_rows) >= MINIMUM_PROFITABILITY_TRADES
        and pnl_ucb is not None
        and pnl_ucb <= 0
    )
    if first_half_futile:
        decision = "FROZEN_FIRST_HALF_FREQUENCY"
    elif frequency_futile:
        decision = "FROZEN_FREQUENCY_FUTILITY"
    elif profitability_futile:
        decision = "FROZEN_NEGATIVE_FUTILITY"
    else:
        decision = "CONTINUE"
    return {
        "candidate_id": candidate_id,
        "decision": decision,
        "captured_trades": len(captured_rows),
        "resolved_trades": len(resolved_rows),
        "captured_first_half_trades": first_half_count,
        "minimum_final_trades": minimum_final,
        "minimum_first_half_trades": minimum_first_half,
        "poisson_one_sided_95_upper_count": round(upper_count, 8),
        "projected_24h_upper_trades": round(projected, 8),
        "frequency_futile": frequency_futile,
        "first_half_frequency_futile": first_half_futile,
        "minimum_profitability_trades": MINIMUM_PROFITABILITY_TRADES,
        "one_sided_95_mean_pnl_ucb": (
            round(pnl_ucb, 8) if pnl_ucb is not None else None
        ),
        "profitability_futile": profitability_futile,
    }


def checkpoint_snapshot(
    *,
    database: str | Path,
    prereg: Mapping[str, Any],
    checkpoint_hour: float,
) -> dict[str, Any]:
    database_path = Path(database).resolve()
    meta = read_v028_meta(database_path)
    start = parse_utc(str(meta["experiment_started_at"]))
    checkpoint_at = start + timedelta(hours=checkpoint_hour)
    evaluated_at = datetime.now(timezone.utc)
    midpoint_ms = int((start.timestamp() + 12 * 3600) * 1000)
    records = v028_arm_records(database_path)
    states = dict(meta.get("v028_candidate_states") or _candidate_states())
    candidate_results: dict[str, dict[str, Any]] = {}
    state_updates = {key: dict(value) for key, value in states.items()}
    technical = _technical_snapshot(
        database=database_path,
        checkpoint_at=evaluated_at,
    )
    for candidate_id in CANDIDATE_IDS:
        state = dict(states[candidate_id])
        if state.get("status") == "FROZEN":
            candidate_results[candidate_id] = {
                "candidate_id": candidate_id,
                "decision": str(state["freeze_reason"]),
                "already_frozen": True,
            }
            continue
        captured = records[candidate_id]
        resolved = [row for row in captured if int(row["label_verified"]) == 1]
        first_half_count = sum(
            int(row["market_start_ms"]) < midpoint_ms for row in captured
        )
        result = _candidate_futility(
            candidate_id=candidate_id,
            checkpoint_hour=checkpoint_hour,
            captured_rows=captured,
            resolved_rows=resolved,
            first_half_count=first_half_count,
            prereg=prereg,
        )
        candidate_results[candidate_id] = result
        if (
            technical["passed"] is True
            and technical["safety_passed"] is True
            and result["decision"] != "CONTINUE"
        ):
            state_updates[candidate_id] = {
                "status": "FROZEN",
                "frozen_at": evaluated_at.isoformat(timespec="seconds"),
                "freeze_reason": result["decision"],
                "evaluation_cutoff_market_start_ms": int(
                    evaluated_at.timestamp() * 1000
                ),
            }
    if technical["safety_passed"] is not True:
        overall_decision = "FREEZE_SAFETY"
        state_updates = states
    elif technical["passed"] is not True:
        overall_decision = "RETRY_TECHNICAL"
        state_updates = states
    elif all(
        state_updates[candidate_id].get("status") == "FROZEN"
        for candidate_id in CANDIDATE_IDS
    ):
        overall_decision = "FREEZE_PRIMARY_FUTILITY"
    else:
        overall_decision = "CONTINUE"
    return {
        "schema": "checkpoint_v028_1",
        "variant": VARIANT,
        "checkpoint_hour": checkpoint_hour,
        "checkpoint_at": checkpoint_at.isoformat(timespec="seconds"),
        "evaluated_at": evaluated_at.isoformat(timespec="seconds"),
        "decision": overall_decision,
        "candidate_futility": candidate_results,
        "candidate_states_after": state_updates,
        "captured_arm_counts": {
            arm_id: len(rows) for arm_id, rows in records.items()
        },
        "resolved_arm_counts": {
            arm_id: sum(int(row["label_verified"]) == 1 for row in rows)
            for arm_id, rows in records.items()
        },
        "technical": technical,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


def _save_checkpoint(database: Path, snapshot: Mapping[str, Any]) -> None:
    connection = sqlite3.connect(database, timeout=60)
    try:
        row = connection.execute(
            "SELECT value FROM v028_meta WHERE key='v028_checkpoints'"
        ).fetchone()
        checkpoints = json.loads(str(row[0])) if row else []
        attempt = int(snapshot.get("attempt", 1))
        checkpoints = [
            item
            for item in checkpoints
            if not (
                float(item.get("checkpoint_hour", -1))
                == float(snapshot["checkpoint_hour"])
                and int(item.get("attempt", 1)) == attempt
            )
        ]
        checkpoints.append(dict(snapshot))
        checkpoints.sort(
            key=lambda item: (
                float(item["checkpoint_hour"]),
                int(item.get("attempt", 1)),
            )
        )
        values = {
            "v028_checkpoints": checkpoints,
            "v028_candidate_states": snapshot["candidate_states_after"],
        }
        connection.executemany(
            "INSERT OR REPLACE INTO v028_meta(key,value) VALUES(?,?)",
            [
                (
                    key,
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                for key, value in values.items()
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _mark_completion(database: Path, reason: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection = sqlite3.connect(database, timeout=60)
    try:
        connection.executemany(
            "INSERT OR REPLACE INTO v028_meta(key,value) VALUES(?,?)",
            (
                ("v028_completion_reason", json.dumps(reason)),
                ("v028_observation_ended_at", json.dumps(now)),
            ),
        )
        connection.commit()
    finally:
        connection.close()


async def _wait_for_due(
    collector: asyncio.Task[dict[str, Any]],
    due_timestamp: float,
) -> bool:
    delay = max(0.0, due_timestamp - time.time())
    try:
        await asyncio.wait_for(asyncio.shield(collector), timeout=delay)
        return False
    except TimeoutError:
        return True


async def run_v028(
    *,
    settings: Settings,
    prereg_path: str | Path,
    implementation_path: str | Path,
    launch_path: str | Path,
    output_db: str | Path,
) -> dict[str, Any]:
    prereg_file = Path(prereg_path).resolve()
    implementation_file = Path(implementation_path).resolve()
    launch_file = Path(launch_path).resolve()
    prereg = load_and_verify_frozen_prereg(prereg_file, project_root=ROOT)
    implementation = load_and_verify_implementation(implementation_file)
    if implementation.get("preregistration_sha256") != sha256_file(prereg_file):
        raise V028RunnerError("Implementación y preinscripción V0.28 no coinciden")
    load_and_verify_launch_approval(
        launch_file,
        prereg_path=prereg_file,
        implementation_path=implementation_file,
    )
    database = Path(output_db).resolve()
    enforce_forward_duration(V028_MAXIMUM_HOURS, database)
    lock = V022ProcessLock(f"{database}.lock")
    lock.acquire()
    try:
        bind_v028_database(
            database=database,
            prereg_path=prereg_file,
            launch_path=launch_file,
        )
        meta = read_v028_meta(database)
        start_timestamp = parse_utc(str(meta["experiment_started_at"])).timestamp()
        completed_hours = {
            float(item["checkpoint_hour"])
            for item in meta.get("v028_checkpoints", [])
            if int(item.get("attempt", 1)) >= 1
            and item.get("decision") != "RETRY_TECHNICAL"
        }
        stop_event = asyncio.Event()
        collector = asyncio.create_task(
            run_v028_forward(
                settings=settings,
                output_db=database,
                preregistration_sha256=sha256_file(prereg_file),
                launch_manifest_sha256=sha256_file(launch_file),
                stop_event=stop_event,
            )
        )
        completion_reason: str | None = None
        try:
            for checkpoint_hour in CHECKPOINT_HOURS:
                if checkpoint_hour in completed_hours:
                    continue
                due = start_timestamp + checkpoint_hour * 3600
                if not await _wait_for_due(collector, due):
                    break
                snapshot = checkpoint_snapshot(
                    database=database,
                    prereg=prereg,
                    checkpoint_hour=checkpoint_hour,
                )
                snapshot["attempt"] = 1
                _save_checkpoint(database, snapshot)
                decision = str(snapshot["decision"])
                if decision == "RETRY_TECHNICAL":
                    retry_due = time.time() + TECHNICAL_RETRY_MINUTES * 60
                    if not await _wait_for_due(collector, retry_due):
                        break
                    retry = checkpoint_snapshot(
                        database=database,
                        prereg=prereg,
                        checkpoint_hour=checkpoint_hour,
                    )
                    retry["attempt"] = 2
                    retry["technical_retry"] = True
                    if retry["decision"] == "RETRY_TECHNICAL":
                        retry["decision"] = "FREEZE_TECHNICAL_FAILURE"
                    _save_checkpoint(database, retry)
                    snapshot = retry
                    decision = str(retry["decision"])
                if decision != "CONTINUE":
                    completion_reason = decision
                    _mark_completion(database, decision)
                    stop_event.set()
                    break
            collector_result = await collector
        except BaseException:
            stop_event.set()
            if not collector.done():
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)
            raise
        if collector_result.get("completion_reason") == "FULL_24H_REACHED":
            completion_reason = "FULL_24H_REACHED"
        elif collector_result.get("completion_reason") == "FREEZE_COLLECTOR_SAFETY":
            completion_reason = "FREEZE_COLLECTOR_SAFETY"
        result = dict(collector_result)
        result.update(
            {
                "variant": VARIANT,
                "maximum_hours": V028_MAXIMUM_HOURS,
                "completion_reason": completion_reason,
                "orders_created": 0,
                "paper_orders": 0,
                "wallet_required": False,
                "real_money": "BLOQUEADO",
            }
        )
        return result
    finally:
        lock.release()


def _public_checkpoint(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "checkpoint_hour": item.get("checkpoint_hour"),
        "attempt": item.get("attempt", 1),
        "decision": item.get("decision"),
        "candidate_decisions": {
            key: value.get("decision")
            for key, value in dict(item.get("candidate_futility", {})).items()
        },
        "technical_passed": dict(item.get("technical", {})).get("passed"),
        "safety_passed": dict(item.get("technical", {})).get("safety_passed"),
    }


def v028_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).resolve()
    empty = {str(arm["id"]): 0 for arm in frozen_arm_config()}
    if not database.is_file():
        return {
            "status": "NOT_STARTED",
            "variant": VARIANT,
            "database": str(database),
            "maximum_hours": V028_MAXIMUM_HOURS,
            "checkpoint_hours": list(CHECKPOINT_HOURS),
            "captured_arm_counts": empty,
            "resolved_arm_counts": empty,
            "orders_created": 0,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
    meta = read_v028_meta(database)
    records = v028_arm_records(database)
    checkpoints = list(meta.get("v028_checkpoints", []))
    completion_reason = meta.get("v028_completion_reason")
    target_end = parse_utc(str(meta["target_end_at"])).timestamp()
    connection = open_read_only(database)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        market_counts = connection.execute(
            """
            SELECT COUNT(*),SUM(CASE WHEN feature_status='SAVED' THEN 1 ELSE 0 END)
            FROM v028_markets
            """
        ).fetchone()
    finally:
        connection.close()
    return {
        "status": "COMPLETED_OR_FROZEN" if completion_reason else "RUNNING_OR_RESUMABLE",
        "variant": VARIANT,
        "database": str(database),
        "sqlite_quick_check": quick_check,
        "maximum_hours": V028_MAXIMUM_HOURS,
        "checkpoint_hours": list(CHECKPOINT_HOURS),
        "completion_reason": completion_reason,
        "remaining_hours": max(0.0, target_end - time.time()) / 3600,
        "markets": int(market_counts[0] or 0),
        "features": int(market_counts[1] or 0),
        "captured_arm_counts": {key: len(value) for key, value in records.items()},
        "resolved_arm_counts": {
            key: sum(int(row["label_verified"]) == 1 for row in value)
            for key, value in records.items()
        },
        "candidate_states": meta.get("v028_candidate_states"),
        "completed_checkpoints": len(checkpoints),
        "latest_checkpoint": _public_checkpoint(checkpoints[-1]) if checkpoints else None,
        "intermediate_outcome_metrics_exposed": False,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


__all__ = [
    "CANDIDATE_IDS",
    "CHECKPOINT_HOURS",
    "IMPLEMENTATION_FILES",
    "IMPLEMENTATION_SCHEMA",
    "LAUNCH_SCHEMA",
    "VARIANT",
    "V028RunnerError",
    "bind_v028_database",
    "build_implementation_manifest",
    "checkpoint_snapshot",
    "load_and_verify_implementation",
    "load_and_verify_launch_approval",
    "run_v028",
    "v028_arm_records",
    "v028_status",
]
