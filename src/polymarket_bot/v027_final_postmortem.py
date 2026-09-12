from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import _parse_utc, sha256_file
from polymarket_bot.v024_tournament import market_record
from polymarket_bot.v025_audit import sequence_metrics
from polymarket_bot.v027_postmortem import (
    COST_BANDS,
    FOUR_HOUR_BLOCKS,
    TWAP_ALIGNMENTS,
    TWAP_DISTANCE_BANDS,
    UTC_SESSIONS,
    cost_band,
    elapsed_four_hour_block,
    twap_alignment,
    twap_distance_band,
    utc_session,
)
from polymarket_bot.v027_strategy import (
    UP_BROAD_CONTROL_ID,
    UP_LOW_VOL_ID,
    arm_matches,
    frozen_arm_config,
)


POSTMORTEM_SCHEMA = "postmortem_v027_up_low_vol_final_1"
RESULT_SCHEMA = "result_v027_regime_replication_tournament_1"
MINIMUM_SCREEN_TRADES = 10
MINIMUM_SCREEN_HALF_TRADES = 3
MINIMUM_SCREEN_REPRESENTED_BLOCKS = 4
MINIMUM_EVALUABLE_BLOCK_TRADES = 2
MINIMUM_POSITIVE_BLOCK_FRACTION = 0.60


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _pnl_per_share(row: Mapping[str, Any]) -> float:
    won = str(row["favorite_side"]) == str(row["label"])
    cost = float(row["entry_cost"])
    return 1.0 - cost if won else -cost


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 8)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 8)
    weight = position - lower
    return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight, 8)


def descriptive_distribution(values: Sequence[float]) -> dict[str, Any]:
    normalized = [float(value) for value in values]
    return {
        "count": len(normalized),
        "minimum": round(min(normalized), 8) if normalized else None,
        "q25": _percentile(normalized, 0.25),
        "median": _percentile(normalized, 0.50),
        "q75": _percentile(normalized, 0.75),
        "maximum": round(max(normalized), 8) if normalized else None,
        "mean": (
            round(statistics.fmean(normalized), 8) if normalized else None
        ),
    }


def contribution_diagnostics(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    contributions = [
        {
            "condition_id": str(row["condition_id"]),
            "market_start_ms": int(row["market_start_ms"]),
            "entry_cost": round(float(row["entry_cost"]), 8),
            "volatility_regime_ratio": (
                round(float(row["volatility_regime_ratio"]), 8)
                if row.get("volatility_regime_ratio") is not None
                else None
            ),
            "twap_distance_to_open_bps": (
                round(float(row["twap_distance_to_open_bps"]), 8)
                if row.get("twap_distance_to_open_bps") is not None
                else None
            ),
            "label": str(row["label"]),
            "pnl_per_share": round(_pnl_per_share(row), 8),
        }
        for row in rows
    ]
    ordered = sorted(contributions, key=lambda item: item["pnl_per_share"])
    positives = sorted(
        (item for item in ordered if item["pnl_per_share"] > 0),
        key=lambda item: item["pnl_per_share"],
        reverse=True,
    )
    negatives = [item for item in ordered if item["pnl_per_share"] < 0]
    net = sum(float(item["pnl_per_share"]) for item in ordered)
    gross_profit = sum(float(item["pnl_per_share"]) for item in positives)
    gross_loss = -sum(float(item["pnl_per_share"]) for item in negatives)
    best = positives[0] if positives else None
    worst = negatives[0] if negatives else None
    net_without_best = net - (float(best["pnl_per_share"]) if best else 0.0)
    net_without_worst = net - (float(worst["pnl_per_share"]) if worst else 0.0)
    return {
        "net_pnl_per_share": round(net, 8),
        "gross_profit_per_share": round(gross_profit, 8),
        "gross_loss_per_share": round(gross_loss, 8),
        "average_win_per_share": (
            round(gross_profit / len(positives), 8) if positives else None
        ),
        "average_loss_per_share": (
            round(-gross_loss / len(negatives), 8) if negatives else None
        ),
        "best_trade": best,
        "worst_trade": worst,
        "net_without_best_trade_per_share": round(net_without_best, 8),
        "net_without_best_trade_at_5_shares": round(net_without_best * 5.0, 8),
        "net_without_worst_trade_per_share": round(net_without_worst, 8),
        "net_without_worst_trade_at_5_shares": round(net_without_worst * 5.0, 8),
        "top_one_win_fraction_of_gross_profit": (
            round(float(positives[0]["pnl_per_share"]) / gross_profit, 8)
            if positives and gross_profit
            else None
        ),
        "top_three_wins_fraction_of_gross_profit": (
            round(
                sum(float(item["pnl_per_share"]) for item in positives[:3])
                / gross_profit,
                8,
            )
            if positives and gross_profit
            else None
        ),
        "positive_without_best_trade": net_without_best > 0,
    }


def temporal_support(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    first_half = [row for row in rows if float(row["elapsed_hours"]) < 12.0]
    second_half = [row for row in rows if float(row["elapsed_hours"]) >= 12.0]
    blocks = {
        block: sequence_metrics(
            [row for row in rows if str(row["elapsed_block"]) == block]
        )
        for block in FOUR_HOUR_BLOCKS
    }
    represented = [
        block for block, metrics in blocks.items() if int(metrics["trades"]) > 0
    ]
    evaluable = [
        block
        for block, metrics in blocks.items()
        if int(metrics["trades"]) >= MINIMUM_EVALUABLE_BLOCK_TRADES
    ]
    positive = [
        block
        for block in evaluable
        if float(blocks[block]["net_pnl_per_share_sequence"]) > 0
    ]
    return {
        "first_half": sequence_metrics(first_half),
        "second_half": sequence_metrics(second_half),
        "four_hour_blocks": blocks,
        "represented_blocks": represented,
        "evaluable_blocks": evaluable,
        "positive_evaluable_blocks": positive,
        "positive_evaluable_block_fraction": (
            round(len(positive) / len(evaluable), 8) if evaluable else None
        ),
    }


def exploratory_segment_profile(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    metrics = sequence_metrics(rows)
    temporal = temporal_support(rows)
    concentration = contribution_diagnostics(rows)
    first = temporal["first_half"]
    second = temporal["second_half"]
    positive_fraction = temporal["positive_evaluable_block_fraction"]
    screen_gates = {
        "minimum_trades_passed": int(metrics["trades"]) >= MINIMUM_SCREEN_TRADES,
        "minimum_first_half_trades_passed": (
            int(first["trades"]) >= MINIMUM_SCREEN_HALF_TRADES
        ),
        "minimum_second_half_trades_passed": (
            int(second["trades"]) >= MINIMUM_SCREEN_HALF_TRADES
        ),
        "minimum_represented_blocks_passed": (
            len(temporal["represented_blocks"]) >= MINIMUM_SCREEN_REPRESENTED_BLOCKS
        ),
        "positive_first_half_passed": (
            float(first["net_pnl_per_share_sequence"]) > 0
        ),
        "positive_second_half_passed": (
            float(second["net_pnl_per_share_sequence"]) > 0
        ),
        "positive_total_pnl_passed": (
            float(metrics["net_pnl_per_share_sequence"]) > 0
        ),
        "profit_factor_above_one_passed": (
            metrics["profit_factor"] is not None
            and float(metrics["profit_factor"]) > 1.0
        ),
        "positive_block_fraction_passed": (
            positive_fraction is not None
            and float(positive_fraction) >= MINIMUM_POSITIVE_BLOCK_FRACTION
        ),
        "positive_without_best_trade_passed": bool(
            concentration["positive_without_best_trade"]
        ),
    }
    return {
        "metrics": metrics,
        "temporal_support": temporal,
        "contribution_diagnostics": concentration,
        "exploratory_screen_gates": screen_gates,
        "exploratory_screen_passed": all(screen_gates.values()),
    }


def grouped_profiles(
    rows: Sequence[Mapping[str, Any]],
    *,
    labels: Sequence[str],
    key: Callable[[Mapping[str, Any]], str | None],
) -> dict[str, Any]:
    return {
        label: exploratory_segment_profile(
            [row for row in rows if key(row) == label]
        )
        for label in labels
    }


def load_v027_diagnostic_rows(
    *, database: str | Path, result: Mapping[str, Any]
) -> list[dict[str, Any]]:
    database_path = Path(database).resolve()
    start = _parse_utc(str(result["window"]["experiment_started_at"]))
    cutoff = _parse_utc(str(result["window"]["observation_ended_at"]))
    start_ms = int(start.timestamp() * 1000)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    connection = _open_read_only(database_path)
    try:
        rows: list[dict[str, Any]] = []
        for raw in connection.execute(
            """
            SELECT m.condition_id,m.slug,m.market_start_ms,m.label,m.label_verified,
             f.feature_json
            FROM v027_markets AS m
            JOIN v027_features AS f USING(condition_id)
            WHERE m.label_verified=1 AND m.market_start_ms<=?
            ORDER BY m.market_start_ms
            """,
            (cutoff_ms,),
        ):
            feature = json.loads(str(raw["feature_json"]))
            record = market_record(
                feature_json=feature,
                model_probability_up=None,
                label=str(raw["label"]),
                market_start_ms=int(raw["market_start_ms"]),
                condition_id=str(raw["condition_id"]),
            )
            if record is None:
                continue
            timestamp_ms = int(raw["market_start_ms"])
            elapsed_hours = (timestamp_ms - start_ms) / 3_600_000
            twap_distance = feature.get("twap_distance_to_open_bps")
            volatility = feature.get("volatility_regime_ratio")
            favorite_side = str(record["favorite_side"])
            record.update(
                {
                    "slug": str(raw["slug"]),
                    "label_verified": int(raw["label_verified"]),
                    "elapsed_hours": elapsed_hours,
                    "elapsed_block": elapsed_four_hour_block(elapsed_hours),
                    "utc_session": utc_session(
                        datetime.fromtimestamp(
                            timestamp_ms / 1000, timezone.utc
                        ).hour
                    ),
                    "cost_band": cost_band(float(record["entry_cost"])),
                    "volatility_regime_ratio": (
                        float(volatility) if volatility is not None else None
                    ),
                    "twap_distance_to_open_bps": (
                        float(twap_distance) if twap_distance is not None else None
                    ),
                    "twap_distance_band": twap_distance_band(
                        float(twap_distance) if twap_distance is not None else None
                    ),
                    "twap_alignment": twap_alignment(
                        float(twap_distance) if twap_distance is not None else None,
                        favorite_side,
                    ),
                    "resolution_twap_window_s": feature.get(
                        "resolution_twap_window_s"
                    ),
                }
            )
            rows.append(record)
        return rows
    finally:
        connection.close()


def _assert_result_reproduction(
    *, calculated: Mapping[str, Any], expected: Mapping[str, Any]
) -> None:
    keys = (
        "trades",
        "wins",
        "net_pnl_per_share_sequence",
        "net_pnl_at_5_shares",
        "profit_factor",
        "roi_on_cost",
        "maximum_drawdown_per_share",
    )
    for key in keys:
        if calculated.get(key) != expected.get(key):
            raise RuntimeError(f"Postmortem V0.27 no reproduce la métrica: {key}")


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_v027_final_postmortem(
    *,
    result_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    result_file = Path(result_path).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    result = json.loads(result_file.read_text(encoding="utf-8"))
    if result.get("schema") != RESULT_SCHEMA:
        raise RuntimeError("Resultado final V0.27 incompatible")
    if result.get("verdict") != "FAIL_NO_ECONOMIC_REPLICATION":
        raise RuntimeError("El postmortem requiere el fallo económico final V0.27")
    database = Path(str(result["database"])).resolve()
    preregistration = Path(str(result["preregistration"])).resolve()
    implementation = Path(str(result["implementation"])).resolve()
    source_hashes = {
        "result": sha256_file(result_file),
        "database": sha256_file(database),
        "preregistration": sha256_file(preregistration),
        "implementation": sha256_file(implementation),
    }
    if source_hashes["database"] != result.get("database_sha256"):
        raise RuntimeError("Base V0.27 no coincide con el resultado sellado")
    if source_hashes["preregistration"] != result.get("preregistration_sha256"):
        raise RuntimeError("Preinscripción V0.27 no coincide")
    if source_hashes["implementation"] != result.get("implementation_sha256"):
        raise RuntimeError("Implementación V0.27 no coincide")
    if output is not None and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if existing.get("schema") == POSTMORTEM_SCHEMA and existing.get(
            "source_hashes"
        ) == source_hashes:
            return existing
        raise RuntimeError("Existe otro postmortem final V0.27")

    rows = load_v027_diagnostic_rows(database=database, result=result)
    arms = {str(item["id"]): item for item in frozen_arm_config()}
    parent_rows = [row for row in rows if arm_matches(row, arms[UP_BROAD_CONTROL_ID])]
    candidate_rows = [row for row in rows if arm_matches(row, arms[UP_LOW_VOL_ID])]
    candidate_ids = {str(row["condition_id"]) for row in candidate_rows}
    excluded_rows = [
        row for row in parent_rows if str(row["condition_id"]) not in candidate_ids
    ]
    candidate_profile = exploratory_segment_profile(candidate_rows)
    parent_profile = exploratory_segment_profile(parent_rows)
    excluded_profile = exploratory_segment_profile(excluded_rows)
    _assert_result_reproduction(
        calculated=candidate_profile["metrics"],
        expected=result["candidates"][UP_LOW_VOL_ID]["metrics"],
    )
    _assert_result_reproduction(
        calculated=parent_profile["metrics"],
        expected=result["controls"][UP_BROAD_CONTROL_ID]["metrics"],
    )

    fixed_segments = {
        "elapsed_four_hour_blocks": grouped_profiles(
            candidate_rows,
            labels=FOUR_HOUR_BLOCKS,
            key=lambda row: str(row["elapsed_block"]),
        ),
        "utc_sessions": grouped_profiles(
            candidate_rows,
            labels=UTC_SESSIONS,
            key=lambda row: str(row["utc_session"]),
        ),
        "fixed_cost_bands": grouped_profiles(
            candidate_rows,
            labels=COST_BANDS,
            key=lambda row: (
                str(row["cost_band"]) if row.get("cost_band") is not None else None
            ),
        ),
        "fixed_twap_distance_bands": grouped_profiles(
            candidate_rows,
            labels=TWAP_DISTANCE_BANDS,
            key=lambda row: (
                str(row["twap_distance_band"])
                if row.get("twap_distance_band") is not None
                else None
            ),
        ),
        "twap_favorite_alignment": grouped_profiles(
            candidate_rows,
            labels=TWAP_ALIGNMENTS,
            key=lambda row: str(row["twap_alignment"]),
        ),
    }
    screen_hits = [
        {"family": family, "segment": segment}
        for family, segments in fixed_segments.items()
        if family != "elapsed_four_hour_blocks"
        for segment, profile in segments.items()
        if bool(profile["exploratory_screen_passed"])
    ]
    block_metrics = candidate_profile["temporal_support"]["four_hour_blocks"]
    block_pnls = {
        block: float(metrics["net_pnl_per_share_sequence"])
        for block, metrics in block_metrics.items()
    }
    full_net = float(candidate_profile["metrics"]["net_pnl_per_share_sequence"])
    leave_one_block_out = {
        block: {
            "net_pnl_per_share": round(full_net - pnl, 8),
            "net_pnl_at_5_shares": round((full_net - pnl) * 5.0, 8),
            "positive": full_net - pnl > 0,
        }
        for block, pnl in block_pnls.items()
    }
    result_candidate = result["candidates"][UP_LOW_VOL_ID]
    strict_failures = {
        "negative_second_half": (
            float(
                candidate_profile["temporal_support"]["second_half"][
                    "net_pnl_per_share_sequence"
                ]
            )
            <= 0
        ),
        "insufficient_positive_block_fraction": (
            float(
                candidate_profile["temporal_support"][
                    "positive_evaluable_block_fraction"
                ]
            )
            < MINIMUM_POSITIVE_BLOCK_FRACTION
        ),
        "nonpositive_bonferroni_lcb": (
            float(result_candidate["metrics"]["bonferroni_one_sided_97_5_lcb"])
            <= 0
        ),
        "depends_on_best_trade_for_positive_net": (
            not bool(
                candidate_profile["contribution_diagnostics"][
                    "positive_without_best_trade"
                ]
            )
        ),
    }
    recommendation = (
        "NO_LAUNCH_V028_CLOSE_CURRENT_DIRECTIONAL_BRANCH"
        if not screen_hits
        else "NO_LAUNCH_YET_POSTHOC_SEGMENT_REQUIRES_PREREGISTERED_FRESH_TEST"
    )
    payload = {
        "schema": POSTMORTEM_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "closed_read_only_exploratory_no_new_backtest",
        "source_files": {
            "result": str(result_file),
            "database": str(database),
            "preregistration": str(preregistration),
            "implementation": str(implementation),
        },
        "source_hashes": source_hashes,
        "source_verdict": result["verdict"],
        "rows": {
            "all_directional_records": len(rows),
            "up_parent_control": len(parent_rows),
            "up_low_vol_candidate": len(candidate_rows),
            "up_excluded_by_low_vol_filter": len(excluded_rows),
        },
        "fixed_partitions_reused_from_pre_v027_design": {
            "four_hour_blocks": list(FOUR_HOUR_BLOCKS),
            "utc_sessions": list(UTC_SESSIONS),
            "cost_bands": list(COST_BANDS),
            "twap_distance_bands": list(TWAP_DISTANCE_BANDS),
            "twap_alignments": list(TWAP_ALIGNMENTS),
        },
        "up_low_vol_candidate": candidate_profile,
        "up_parent_control": parent_profile,
        "up_excluded_by_low_vol_filter": excluded_profile,
        "volatility_ratio_descriptive_only": {
            "candidate": descriptive_distribution(
                [
                    float(row["volatility_regime_ratio"])
                    for row in candidate_rows
                    if row.get("volatility_regime_ratio") is not None
                ]
            ),
            "excluded": descriptive_distribution(
                [
                    float(row["volatility_regime_ratio"])
                    for row in excluded_rows
                    if row.get("volatility_regime_ratio") is not None
                ]
            ),
            "no_new_threshold_selected": True,
        },
        "fixed_segment_diagnostics": fixed_segments,
        "leave_one_four_hour_block_out": leave_one_block_out,
        "hypothesis_generation_screen": {
            "not_preregistered": True,
            "not_validation_evidence": True,
            "thresholds": {
                "minimum_trades": MINIMUM_SCREEN_TRADES,
                "minimum_trades_per_half": MINIMUM_SCREEN_HALF_TRADES,
                "minimum_represented_blocks": MINIMUM_SCREEN_REPRESENTED_BLOCKS,
                "minimum_evaluable_block_trades": MINIMUM_EVALUABLE_BLOCK_TRADES,
                "minimum_positive_evaluable_block_fraction": (
                    MINIMUM_POSITIVE_BLOCK_FRACTION
                ),
                "requires_positive_both_halves": True,
                "requires_positive_total_and_profit_factor_above_one": True,
                "requires_positive_without_best_trade": True,
            },
            "screen_hits": screen_hits,
            "multiple_segments_examined": sum(
                len(segments)
                for family, segments in fixed_segments.items()
                if family != "elapsed_four_hour_blocks"
            ),
            "selection_or_promotion_allowed": False,
        },
        "decisive_findings": {
            "candidate_net_pnl_at_5_shares": candidate_profile["metrics"][
                "net_pnl_at_5_shares"
            ],
            "parent_control_net_pnl_at_5_shares": parent_profile["metrics"][
                "net_pnl_at_5_shares"
            ],
            "excluded_by_filter_net_pnl_at_5_shares": excluded_profile["metrics"]
            ["net_pnl_at_5_shares"],
            "candidate_minus_parent_mean_pnl": result_candidate[
                "candidate_minus_parent_mean_pnl"
            ],
            "candidate_bonferroni_lcb": result_candidate["metrics"][
                "bonferroni_one_sided_97_5_lcb"
            ],
            "candidate_first_half_pnl_at_5_shares": candidate_profile[
                "temporal_support"
            ]["first_half"]["net_pnl_at_5_shares"],
            "candidate_second_half_pnl_at_5_shares": candidate_profile[
                "temporal_support"
            ]["second_half"]["net_pnl_at_5_shares"],
            "positive_block_fraction": candidate_profile["temporal_support"]
            ["positive_evaluable_block_fraction"],
            "net_without_best_trade_at_5_shares": candidate_profile[
                "contribution_diagnostics"
            ]["net_without_best_trade_at_5_shares"],
            "strict_failures": strict_failures,
        },
        "decision": {
            "favorite_down_cost_070_080": "DISCARD_CONFIRMED_BY_V027",
            "favorite_up_low_vol_lt_075": (
                "HYPOTHESIS_ONLY_NOT_RESCUED_NOT_REPLICATED"
            ),
            "selected_strategy": None,
            "v028_recommendation": recommendation,
            "reason": (
                "V0.27 fue técnicamente válido, pero UP falló estabilidad temporal, "
                "segunda mitad y Bonferroni. Los segmentos se examinan después de "
                "ver outcomes y no pueden rescatar la estrategia."
            ),
        },
        "safety": {
            "database_query_only": True,
            "source_evidence_modified": False,
            "new_backtest_hours": 0,
            "orders_created": 0,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
    }
    if (
        sha256_file(result_file) != source_hashes["result"]
        or sha256_file(database) != source_hashes["database"]
        or sha256_file(preregistration) != source_hashes["preregistration"]
        or sha256_file(implementation) != source_hashes["implementation"]
    ):
        raise RuntimeError("La evidencia V0.27 cambió durante el postmortem")
    if output is not None:
        _write_atomic(output, payload)
    return payload


__all__ = [
    "POSTMORTEM_SCHEMA",
    "build_v027_final_postmortem",
    "contribution_diagnostics",
    "descriptive_distribution",
    "exploratory_segment_profile",
    "load_v027_diagnostic_rows",
    "temporal_support",
]
