from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.phase41 import audit_shadow_forward
from polymarket_bot.v018_runner import _parse_utc, sha256_file
from polymarket_bot.v024_runner import VARIANT, load_and_verify_prereg
from polymarket_bot.v024_tournament import (
    PAPER_SHARES,
    eligible_strategy_ids,
    market_record,
    validate_strategy_config,
)


RESULT_SCHEMA = "result_v024_parallel_tournament_4h_1"
MINIMUM_TRADES = 10
UNADJUSTED_Z = 1.6448536269514715
BONFERRONI_Z = 2.128045234184984


def _empty_metrics() -> dict[str, Any]:
    return {
        "trades": 0,
        "wins": 0,
        "win_rate": None,
        "net_pnl_per_share_sequence": 0.0,
        "net_pnl_at_5_shares": 0.0,
        "roi_on_cost": None,
        "profit_factor": None,
        "mean_pnl_per_share": None,
        "pnl_standard_deviation": None,
        "unadjusted_one_sided_95_lcb": None,
        "bonferroni_one_sided_lcb": None,
        "maximum_drawdown_per_share": 0.0,
        "first_half": {"trades": 0, "net_pnl_per_share_sequence": 0.0},
        "second_half": {"trades": 0, "net_pnl_per_share_sequence": 0.0},
    }


def strategy_metrics(
    rows: Sequence[Mapping[str, Any]], *, midpoint_ms: int
) -> dict[str, Any]:
    if not rows:
        return _empty_metrics()
    pnls: list[float] = []
    total_cost = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    cumulative = 0.0
    peak = 0.0
    maximum_drawdown = 0.0
    wins = 0
    first_pnls: list[float] = []
    second_pnls: list[float] = []
    for row in sorted(rows, key=lambda item: int(item["market_start_ms"])):
        cost = float(row["entry_cost"])
        won = str(row["favorite_side"]) == str(row["label"])
        pnl = 1.0 - cost if won else -cost
        pnls.append(pnl)
        total_cost += cost
        wins += int(won)
        gross_profit += max(0.0, pnl)
        gross_loss += max(0.0, -pnl)
        cumulative += pnl
        peak = max(peak, cumulative)
        maximum_drawdown = max(maximum_drawdown, peak - cumulative)
        target = first_pnls if int(row["market_start_ms"]) < midpoint_ms else second_pnls
        target.append(pnl)
    mean = statistics.fmean(pnls)
    deviation = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    error = deviation / math.sqrt(len(pnls))
    net = sum(pnls)
    return {
        "trades": len(pnls),
        "wins": wins,
        "win_rate": round(wins / len(pnls), 8),
        "net_pnl_per_share_sequence": round(net, 8),
        "net_pnl_at_5_shares": round(net * PAPER_SHARES, 8),
        "roi_on_cost": round(net / total_cost, 8) if total_cost else None,
        "profit_factor": (
            round(gross_profit / gross_loss, 8) if gross_loss else None
        ),
        "mean_pnl_per_share": round(mean, 8),
        "pnl_standard_deviation": round(deviation, 8),
        "unadjusted_one_sided_95_lcb": round(mean - UNADJUSTED_Z * error, 8),
        "bonferroni_one_sided_lcb": round(mean - BONFERRONI_Z * error, 8),
        "maximum_drawdown_per_share": round(maximum_drawdown, 8),
        "first_half": {
            "trades": len(first_pnls),
            "net_pnl_per_share_sequence": round(sum(first_pnls), 8),
        },
        "second_half": {
            "trades": len(second_pnls),
            "net_pnl_per_share_sequence": round(sum(second_pnls), 8),
        },
    }


def evaluate_tournament(
    records: Sequence[Mapping[str, Any]],
    strategies: Sequence[Mapping[str, Any]],
    *,
    midpoint_ms: int,
) -> dict[str, Any]:
    by_strategy: dict[str, list[Mapping[str, Any]]] = {
        str(item["id"]): [] for item in strategies
    }
    overlap: dict[str, int] = {}
    for record in records:
        eligible = eligible_strategy_ids(record, strategies)
        for strategy_id in eligible:
            by_strategy[strategy_id].append(record)
        for index, left in enumerate(eligible):
            for right in eligible[index + 1 :]:
                key = "|".join(sorted((left, right)))
                overlap[key] = overlap.get(key, 0) + 1
    results: list[dict[str, Any]] = []
    for strategy in strategies:
        strategy_id = str(strategy["id"])
        metrics = strategy_metrics(by_strategy[strategy_id], midpoint_ms=midpoint_ms)
        factor = metrics["profit_factor"]
        gates = {
            "minimum_trades_passed": metrics["trades"] >= MINIMUM_TRADES,
            "positive_net_pnl_passed": metrics["net_pnl_per_share_sequence"] > 0,
            "positive_bonferroni_lcb_passed": (
                metrics["bonferroni_one_sided_lcb"] is not None
                and metrics["bonferroni_one_sided_lcb"] > 0
            ),
            "profit_factor_above_one_passed": factor is None or factor > 1.0,
            "positive_first_half_passed": (
                metrics["first_half"]["net_pnl_per_share_sequence"] > 0
            ),
            "positive_second_half_passed": (
                metrics["second_half"]["net_pnl_per_share_sequence"] > 0
            ),
        }
        passed = all(gates.values())
        results.append(
            {
                "strategy_id": strategy_id,
                "rule": dict(strategy),
                "metrics": metrics,
                "gates": gates,
                "passed": passed,
            }
        )
    passed = [item for item in results if item["passed"]]
    ranked = sorted(
        passed,
        key=lambda item: (
            -float(item["metrics"]["bonferroni_one_sided_lcb"]),
            -float(item["metrics"]["net_pnl_per_share_sequence"]),
            str(item["strategy_id"]),
        ),
    )
    selected = str(ranked[0]["strategy_id"]) if ranked else None
    return {
        "strategies": results,
        "overlap_counts": dict(sorted(overlap.items())),
        "rescued_strategies": [str(item["strategy_id"]) for item in passed],
        "discarded_strategies": [
            str(item["strategy_id"]) for item in results if not item["passed"]
        ],
        "selected_strategy": selected,
    }


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def audit_v024(
    *,
    database: str | Path,
    prereg_path: str | Path,
    phase4_db: str | Path,
    result_path: str | Path | None = None,
) -> dict[str, Any]:
    database_path = Path(database).resolve()
    prereg_file = Path(prereg_path).resolve()
    phase4_path = Path(phase4_db).resolve()
    output = Path(result_path).resolve() if result_path is not None else None
    prereg = load_and_verify_prereg(prereg_file)
    if not database_path.is_file():
        raise RuntimeError("Base V0.24 no encontrada")
    database_hash = sha256_file(database_path)
    prereg_hash = sha256_file(prereg_file)
    if output is not None and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == RESULT_SCHEMA
            and existing.get("database_sha256") == database_hash
            and existing.get("preregistration_sha256") == prereg_hash
        ):
            return existing
        raise RuntimeError("Existe un resultado V0.24 para otra evidencia")

    technical = audit_shadow_forward(
        shadow_db=database_path, phase4_db=phase4_path
    )
    connection = _open_read_only(database_path)
    try:
        meta = {
            str(key): json.loads(str(value))
            for key, value in connection.execute("SELECT key,value FROM shadow_meta")
        }
        strategies = validate_strategy_config(meta.get("v024_strategies"))
        records: list[dict[str, Any]] = []
        unresolved_candidate_records = 0
        for row in connection.execute(
            """
            SELECT f.condition_id,f.feature_json,m.market_start_ms,m.label,
             m.label_verified,s.probability_up
            FROM shadow_features AS f
            JOIN shadow_markets AS m USING(condition_id)
            LEFT JOIN shadow_signals AS s
              ON s.condition_id=f.condition_id
             AND s.model_name='twap_transfer_strike_hgb'
            ORDER BY m.market_start_ms
            """
        ):
            record = market_record(
                feature_json=str(row["feature_json"]),
                model_probability_up=(
                    float(row["probability_up"])
                    if row["probability_up"] is not None
                    else None
                ),
                label=str(row["label"]) if row["label"] is not None else None,
                market_start_ms=int(row["market_start_ms"]),
                condition_id=str(row["condition_id"]),
            )
            if record is None:
                continue
            if int(row["label_verified"] or 0) != 1:
                if eligible_strategy_ids(record, strategies):
                    unresolved_candidate_records += 1
                continue
            records.append(record)
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        connection.close()

    start_ms = int(_parse_utc(str(meta["experiment_started_at"])).timestamp() * 1000)
    end_ms = int(_parse_utc(str(meta["target_end_at"])).timestamp() * 1000)
    tournament = evaluate_tournament(
        records, strategies, midpoint_ms=start_ms + (end_ms - start_ms) // 2
    )
    safety_passed = (
        meta.get("orders_enabled") is False
        and meta.get("money_real_enabled") is False
        and meta.get("v024_real_money") == "BLOQUEADO"
        and technical.get("orders_created") is False
        and technical.get("wallet_required") is False
    )
    technical_passed = bool(technical.get("technical_passed")) and quick_check == "ok"
    selected = tournament["selected_strategy"]
    if not safety_passed:
        verdict = "FAIL_SAFETY"
    elif not technical_passed:
        verdict = "FAIL_TECHNICAL_QUALITY"
    elif all(
        int(item["metrics"]["trades"]) < MINIMUM_TRADES
        for item in tournament["strategies"]
    ):
        verdict = "FAIL_INSUFFICIENT_FREQUENCY"
    elif selected is None:
        verdict = "FAIL_NO_ROBUST_STRATEGY"
    else:
        verdict = "PASS_PARALLEL_TOURNAMENT_PAPER_CANDIDATE"

    meanings = {
        "FAIL_SAFETY": "La prueba incumplio el bloqueo operativo.",
        "FAIL_TECHNICAL_QUALITY": "La cobertura o integridad no permite comparar.",
        "FAIL_INSUFFICIENT_FREQUENCY": "Ninguna estrategia alcanzo diez operaciones.",
        "FAIL_NO_ROBUST_STRATEGY": "Hubo frecuencia, pero ninguna estrategia paso PnL, confianza y ambas mitades.",
        "PASS_PARALLEL_TOURNAMENT_PAPER_CANDIDATE": "Se rescato una estrategia para mas validacion paper; dinero real sigue bloqueado.",
    }
    result = {
        "schema": RESULT_SCHEMA,
        "variant": VARIANT,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": verdict,
        "meaning": meanings[verdict],
        "selected_strategy": selected,
        "rescued_strategies": tournament["rescued_strategies"],
        "discarded_strategies": tournament["discarded_strategies"],
        "paper_forward_candidate": selected is not None,
        "money_real_candidate": False,
        "real_money": "BLOQUEADO",
        "wallet_required": False,
        "orders_created": False,
        "paper_orders": 0,
        "database": str(database_path),
        "database_sha256": database_hash,
        "database_read_only_verified": database_hash == sha256_file(database_path),
        "preregistration": str(prereg_file),
        "preregistration_sha256": prereg_hash,
        "window": {
            "target_hours": float(meta.get("target_hours", 0)),
            "started_at": meta.get("experiment_started_at"),
            "target_end_at": meta.get("target_end_at"),
            "completed_at": meta.get("experiment_completed_at"),
            "expected_markets": 48,
        },
        "coverage": {
            "markets": technical.get("markets"),
            "market_coverage": technical.get("market_coverage"),
            "features": technical.get("features"),
            "feature_coverage": technical.get("feature_coverage"),
            "resolved": technical.get("resolved"),
            "resolution_coverage": technical.get("resolution_coverage"),
            "unresolved_candidate_records": unresolved_candidate_records,
        },
        "multiple_testing": {
            "family_size": 3,
            "family_alpha": 0.05,
            "method": "Bonferroni one-sided",
            "adjusted_z": BONFERRONI_Z,
        },
        "strategies": tournament["strategies"],
        "overlap_counts": tournament["overlap_counts"],
        "selection_rule": (
            "Solo estrategias que pasan todos los gates; mayor LCB Bonferroni, "
            "desempate por net PnL y luego id. Los PnL solapados no se suman."
        ),
        "safety_passed": safety_passed,
        "technical_passed": technical_passed,
        "technical_audit": {
            "sqlite_quick_check": technical.get("sqlite_quick_check"),
            "experiment_complete": technical.get("experiment_complete"),
            "technical_passed": technical.get("technical_passed"),
            "phase4_opened_read_only": technical.get("phase4_opened_read_only"),
            "phase4_unchanged": technical.get("phase4_unchanged"),
        },
    }
    if output is not None:
        _write_atomic(output, result)
    return result


__all__ = [
    "BONFERRONI_Z",
    "MINIMUM_TRADES",
    "RESULT_SCHEMA",
    "audit_v024",
    "evaluate_tournament",
    "strategy_metrics",
]
