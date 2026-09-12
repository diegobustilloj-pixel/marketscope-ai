from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import _parse_utc, sha256_file
from polymarket_bot.v024_audit import evaluate_tournament
from polymarket_bot.v024_tournament import (
    PAPER_SHARES,
    eligible_strategy_ids,
    market_record,
    validate_strategy_config,
)


POSTMORTEM_SCHEMA = "postmortem_v024_loss_attribution_1"
V024_RESULT_SCHEMA = "result_v024_parallel_tournament_4h_1"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON incompatible: {path}")
    return value


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def attribution_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Decompose realized mean PnL without fitting or selecting a threshold."""
    if not rows:
        return {
            "trades": 0,
            "wins": 0,
            "win_rate": None,
            "average_market_favorite_probability": None,
            "average_entry_cost": None,
            "transaction_cost_drag_vs_mid": None,
            "realization_gap_vs_market_mid": None,
            "reconstructed_mean_pnl_per_share": None,
            "net_pnl_per_share_sequence": 0.0,
            "net_pnl_at_5_shares": 0.0,
        }
    wins = sum(str(row["favorite_side"]) == str(row["label"]) for row in rows)
    win_rate = wins / len(rows)
    average_probability = statistics.fmean(
        float(row["favorite_probability"]) for row in rows
    )
    average_cost = statistics.fmean(float(row["entry_cost"]) for row in rows)
    cost_drag = average_cost - average_probability
    realization_gap = win_rate - average_probability
    mean_pnl = win_rate - average_cost
    net_pnl = mean_pnl * len(rows)
    return {
        "trades": len(rows),
        "wins": wins,
        "win_rate": round(win_rate, 8),
        "average_market_favorite_probability": round(average_probability, 8),
        "average_entry_cost": round(average_cost, 8),
        "transaction_cost_drag_vs_mid": round(cost_drag, 8),
        "realization_gap_vs_market_mid": round(realization_gap, 8),
        "reconstructed_mean_pnl_per_share": round(mean_pnl, 8),
        "net_pnl_per_share_sequence": round(net_pnl, 8),
        "net_pnl_at_5_shares": round(net_pnl * PAPER_SHARES, 8),
    }


def _cost_band(cost: float) -> str:
    if cost < 0.60:
        return "0.50_to_0.60"
    if cost < 0.70:
        return "0.60_to_0.70"
    if cost < 0.80:
        return "0.70_to_0.80"
    return "0.80_to_0.90"


def _group_metrics(
    rows: Sequence[Mapping[str, Any]], key
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(key(row)), []).append(row)
    return {
        name: attribution_metrics(group)
        for name, group in sorted(groups.items())
    }


def _historical_summary(data_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    v018_path = data_dir / "resultado_v018_multifill.json"
    v019_path = data_dir / "resultado_v019_fifo_pair.json"
    v020_path = data_dir / "diagnostico_v021_safe_pair_design.json"
    v021_path = data_dir / "resultado_v021_safe_pair_observer.json"
    v022_path = data_dir / "resultado_v022_aborted_duration_change.json"
    v022b_path = data_dir / "resultado_v022b_synced_persistent_observer.json"
    v023_path = data_dir / "resultado_v023_filtered_forward_4h.json"
    paths = [
        v018_path,
        v019_path,
        v020_path,
        v021_path,
        v022_path,
        v022b_path,
        v023_path,
    ]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise RuntimeError(f"Fuentes historicas ausentes: {missing}")
    v018 = _read_json(v018_path)
    v019 = _read_json(v019_path)
    v020 = _read_json(v020_path)["v020_closed_result"]
    v021 = _read_json(v021_path)
    v022 = _read_json(v022_path)
    v022b = _read_json(v022b_path)
    v023 = _read_json(v023_path)
    timeline = [
        {
            "version": "V0.18",
            "verdict": v018["status"],
            "net_pnl": v018["performance"]["net_pnl"],
            "roi_on_cost": v018["performance"]["roi_on_cost"],
            "decisive_fact": "weighted_complete_set_cost_above_one",
        },
        {
            "version": "V0.19",
            "verdict": v019["status"],
            "net_pnl": v019["performance"]["net_pnl"],
            "paired_net_pnl": v019["performance"]["paired_net_pnl"],
            "residual_net_pnl": v019["performance"]["residual_net_pnl"],
            "decisive_fact": "unmatched_leg_loss_exceeded_paired_profit",
        },
        {
            "version": "V0.20",
            "verdict": "FAIL_NEGATIVE_DETERMINISTIC_PNL",
            "net_pnl": v020["deterministic_net_pnl"],
            "roi_on_cost": v020["roi"],
            "weighted_complete_set_cost": v020["weighted_complete_set_cost"],
            "final_unmatched_shares": v020["final_unmatched_shares"],
            "decisive_fact": "mandatory_hedge_removed_residual_but_locked_loss",
        },
        {
            "version": "V0.21",
            "verdict": v021["verdict"],
            "opportunity_episodes": v021["opportunities"]["opportunity_episodes"],
            "maximum_persistence_ms": v021["opportunities"][
                "maximum_opportunity_persistence_ms"
            ],
            "decisive_fact": "observer_only_no_execution_evidence",
        },
        {
            "version": "V0.22",
            "verdict": v022["status"],
            "valid_experiment_result": v022["valid_experiment_result"],
            "decisive_fact": "aborted_for_duration_change",
        },
        {
            "version": "V0.22b",
            "verdict": v022b["verdict"],
            "raw_opportunity_episodes": v022b["opportunities"][
                "raw_opportunity_episodes"
            ],
            "confirmed_opportunity_episodes": v022b["opportunities"][
                "confirmed_opportunity_episodes"
            ],
            "decisive_fact": "raw_complete_sets_did_not_persist",
        },
        {
            "version": "V0.23",
            "verdict": v023["verdict"],
            "trades": v023["performance"]["trades"],
            "net_pnl_at_5_shares": v023["performance"]["net_pnl_at_5_shares"],
            "decisive_fact": "filter_excluded_all_signals",
        },
    ]
    reporting_correction = {
        "source": str(v023_path.resolve()),
        "source_sha256": sha256_file(v023_path),
        "field": "meaning",
        "original": v023.get("meaning"),
        "corrected": (
            "FAIL_INSUFFICIENT_FREQUENCY: el filtro produjo cero operaciones; "
            "no hubo PnL positivo ni evidencia de rentabilidad."
        ),
        "reason": (
            "El veredicto y las metricas registran cero operaciones, mientras el "
            "texto historico afirmaba PnL positivo."
        ),
        "historical_file_modified": False,
        "compatibility_reason": (
            "El archivo se conserva porque su hash esta anclado en el diseno V0.24."
        ),
    }
    return timeline, reporting_correction


def build_v024_postmortem(
    *,
    database: str | Path,
    result_path: str | Path,
    design_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    database_path = Path(database).resolve()
    result_file = Path(result_path).resolve()
    design_file = Path(design_path).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    for path in (database_path, result_file, design_file):
        if not path.is_file():
            raise RuntimeError(f"Fuente V0.24 ausente: {path}")
    database_hash_before = sha256_file(database_path)
    result = _read_json(result_file)
    design = _read_json(design_file)
    if result.get("schema") != V024_RESULT_SCHEMA:
        raise RuntimeError("Resultado V0.24 incompatible")
    if result.get("database_sha256") != database_hash_before:
        raise RuntimeError("La base V0.24 no coincide con el resultado auditado")

    connection = _open_read_only(database_path)
    try:
        meta = {
            str(key): json.loads(str(value))
            for key, value in connection.execute("SELECT key,value FROM shadow_meta")
        }
        strategies = validate_strategy_config(meta.get("v024_strategies"))
        records: list[dict[str, Any]] = []
        for row in connection.execute(
            """
            SELECT f.condition_id,f.feature_json,m.market_start_ms,m.label,
             m.label_verified,s.probability_up
            FROM shadow_features AS f
            JOIN shadow_markets AS m USING(condition_id)
            LEFT JOIN shadow_signals AS s
              ON s.condition_id=f.condition_id
             AND s.model_name='twap_transfer_strike_hgb'
            WHERE m.label_verified=1
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
                label=str(row["label"]),
                market_start_ms=int(row["market_start_ms"]),
                condition_id=str(row["condition_id"]),
            )
            if record is None:
                continue
            implied = float(record["implied_up_mid_probability"])
            record["favorite_probability"] = max(implied, 1.0 - implied)
            records.append(record)
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        table_counts = {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in (
                "shadow_markets",
                "shadow_features",
                "shadow_signals",
                "shadow_diagnostics",
                "shadow_twap_ticks",
            )
        }
    finally:
        connection.close()

    start_ms = int(_parse_utc(str(meta["experiment_started_at"])).timestamp() * 1000)
    end_ms = int(_parse_utc(str(meta["target_end_at"])).timestamp() * 1000)
    midpoint_ms = start_ms + (end_ms - start_ms) // 2
    reproduced = evaluate_tournament(
        records, strategies, midpoint_ms=midpoint_ms
    )
    reproduced_keys = (
        "strategies",
        "overlap_counts",
        "rescued_strategies",
        "discarded_strategies",
        "selected_strategy",
    )
    if any(reproduced[key] != result[key] for key in reproduced_keys):
        raise RuntimeError("El postmortem no reproduce la auditoria V0.24")

    rows_by_strategy: dict[str, list[dict[str, Any]]] = {
        str(strategy["id"]): [] for strategy in strategies
    }
    for record in records:
        for strategy_id in eligible_strategy_ids(record, strategies):
            rows_by_strategy[strategy_id].append(record)
    attributions = {
        strategy_id: attribution_metrics(rows)
        for strategy_id, rows in rows_by_strategy.items()
    }
    broad_rows = rows_by_strategy["favorite_cap_090"]
    diagnostic_segments = {
        "warning": (
            "Segmentos descriptivos posteriores al resultado; no se pueden usar "
            "como validacion ni para elegir umbrales sin una prueba futura nueva."
        ),
        "by_half": _group_metrics(
            broad_rows,
            lambda row: (
                "first_half"
                if int(row["market_start_ms"]) < midpoint_ms
                else "second_half"
            ),
        ),
        "by_favorite_side": _group_metrics(
            broad_rows, lambda row: row["favorite_side"]
        ),
        "by_model_agreement": _group_metrics(
            broad_rows,
            lambda row: "agrees" if row["model_agrees"] else "does_not_agree",
        ),
        "by_entry_cost_band": _group_metrics(
            broad_rows, lambda row: _cost_band(float(row["entry_cost"]))
        ),
    }

    fresh_by_id = {
        str(item["strategy_id"]): item["metrics"] for item in result["strategies"]
    }
    development_comparison: dict[str, Any] = {}
    for strategy in design["strategies"]:
        strategy_id = str(strategy["id"])
        fresh = fresh_by_id[strategy_id]
        development_comparison[strategy_id] = {
            "official_7d_selected_development": strategy["official_7d"],
            "v023_selected_development_4h": strategy["v023_development_4h"],
            "v024_fresh_4h": {
                "trades": fresh["trades"],
                "wins": fresh["wins"],
                "win_rate": fresh["win_rate"],
                "net_pnl_per_share_sequence": fresh[
                    "net_pnl_per_share_sequence"
                ],
                "roi_on_cost": fresh["roi_on_cost"],
                "bonferroni_one_sided_lcb": fresh[
                    "bonferroni_one_sided_lcb"
                ],
            },
            "development_positive_fresh_negative": (
                float(strategy["v023_development_4h"][
                    "net_pnl_per_share_sequence"
                ])
                > 0
                and float(fresh["net_pnl_per_share_sequence"]) < 0
            ),
        }

    timeline, reporting_correction = _historical_summary(database_path.parent)
    database_hash_after = sha256_file(database_path)
    payload = {
        "schema": POSTMORTEM_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "closed_data_read_only_no_new_backtest",
        "v024_verdict": result["verdict"],
        "selected_strategy": result["selected_strategy"],
        "rescued_strategies": result["rescued_strategies"],
        "discarded_strategies": result["discarded_strategies"],
        "loss_attribution_by_strategy": attributions,
        "favorite_cap_090_diagnostic_segments": diagnostic_segments,
        "development_vs_fresh": development_comparison,
        "historical_timeline": timeline,
        "historical_reporting_correction": reporting_correction,
        "findings": {
            "all_v023_development_winners_reversed_in_v024": all(
                item["development_positive_fresh_negative"]
                for item in development_comparison.values()
            ),
            "all_fresh_strategy_means_negative": all(
                float(item["reconstructed_mean_pnl_per_share"]) < 0
                for item in attributions.values()
            ),
            "all_fresh_strategies_pay_positive_cost_drag_vs_mid": all(
                float(item["transaction_cost_drag_vs_mid"]) > 0
                for item in attributions.values()
            ),
            "profit_strategy_rescued": False,
            "robust_components_rescued": [
                "single_collector_same_window",
                "fresh_twap_decisions",
                "read_only_audit",
                "multiple_testing_correction",
                "real_money_block",
                "cheap_contrarian_signal_exclusion_as_risk_control_only",
            ],
        },
        "faster_project_path": {
            "principle": (
                "Acortar espera operativa sin acortar la evidencia independiente."
            ),
            "parallel_window_saving": (
                "Hasta tres candidatos preinscritos comparten una sola ventana de "
                "4h; evita tres ventanas secuenciales que sumarian 12h."
            ),
            "stages": [
                "Diagnostico inmediato con bases cerradas, sin esperar mercado.",
                "Maximo tres hipotesis estructurales, congeladas antes del forward.",
                "Una sola ventana futura de 4h para todas las ramas.",
                "Un unico aviso y auditoria al final; sin reportes horarios.",
                "Descartar la familia completa si ninguna pasa todos los gates.",
            ],
            "new_backtest_max_hours": 24,
            "default_fresh_forward_hours": 4,
            "intermediate_scheduled_reports": False,
            "final_result_only": True,
            "cannot_be_safely_shortened": (
                "La llegada de operaciones independientes; V0.23 ya mostro que una "
                "ventana con frecuencia insuficiente no valida rentabilidad."
            ),
        },
        "next_decision": {
            "launch_new_variant_now": False,
            "reason": (
                "V0.24 no rescato ninguna estrategia y sus tres ganadores de "
                "desarrollo revirtieron a PnL negativo en la ventana fresca."
            ),
            "required_before_next_forward": (
                "Una hipotesis estructural nueva que explique coste y calibracion, "
                "no un umbral elegido despues de observar V0.24."
            ),
        },
        "integrity": {
            "database": str(database_path),
            "database_sha256_before": database_hash_before,
            "database_sha256_after": database_hash_after,
            "database_unchanged": database_hash_before == database_hash_after,
            "database_query_only": query_only == 1,
            "sqlite_quick_check": quick_check,
            "table_counts": table_counts,
            "result": str(result_file),
            "result_sha256": sha256_file(result_file),
            "design": str(design_file),
            "design_sha256": sha256_file(design_file),
            "orders_created": False,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
    }
    if output is not None:
        _write_atomic(output, payload)
    return payload


__all__ = [
    "POSTMORTEM_SCHEMA",
    "attribution_metrics",
    "build_v024_postmortem",
]
