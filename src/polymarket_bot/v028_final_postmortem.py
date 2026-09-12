from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v024_tournament import market_record
from polymarket_bot.v028_strategy import (
    PARENT_CONTROL_ID,
    PRIMARY_ID,
    arm_matches,
    frozen_arm_config,
)


POSTMORTEM_SCHEMA = "postmortem_v028_twap_lt5_final_1"
RESULT_SCHEMA = "result_v028_twap_lt5_replication_1"
REQUIRED_VERDICT = "FAIL_INSUFFICIENT_FREQUENCY"


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def outcome_blind_frequency_funnel(database: str | Path) -> dict[str, Any]:
    """Count frozen filter stages without consulting labels or outcomes."""

    database_path = Path(database).resolve()
    connection = _open_read_only(database_path)
    try:
        rows: list[dict[str, Any]] = []
        for raw in connection.execute(
            """
            SELECT m.condition_id,m.market_start_ms,m.resolution_contract_status,
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
                label=None,
                market_start_ms=int(raw["market_start_ms"]),
                condition_id=str(raw["condition_id"]),
            )
            if record is None:
                continue
            record.update(
                {
                    "resolution_contract_status": str(
                        raw["resolution_contract_status"]
                    ),
                    "resolution_twap_window_s": feature.get(
                        "resolution_twap_window_s"
                    ),
                    "volatility_regime_ratio": feature.get(
                        "volatility_regime_ratio"
                    ),
                    "twap_distance_to_open_bps": feature.get(
                        "twap_distance_to_open_bps"
                    ),
                }
            )
            rows.append(record)
        market_counts = connection.execute(
            """
            SELECT COUNT(*) AS markets,
             SUM(CASE WHEN feature_status='SAVED' THEN 1 ELSE 0 END) AS saved,
             SUM(CASE WHEN feature_status='FAILED' THEN 1 ELSE 0 END) AS failed,
             SUM(CASE WHEN feature_status='PENDING' THEN 1 ELSE 0 END) AS pending
            FROM v028_markets
            """
        ).fetchone()
    finally:
        connection.close()

    arms = {str(item["id"]): item for item in frozen_arm_config()}

    def count(predicate: Any) -> int:
        return sum(bool(predicate(row)) for row in rows)

    cost_eligible = lambda row: 0.50 < float(row["entry_cost"]) <= 0.90
    up_cost = lambda row: (
        str(row["favorite_side"]) == "Up" and cost_eligible(row)
    )
    exact_twap = lambda row: (
        up_cost(row)
        and row.get("resolution_twap_window_s") is not None
        and int(row["resolution_twap_window_s"]) == 60
        and row.get("resolution_contract_status") == "VERIFIED"
    )
    low_vol = lambda row: (
        exact_twap(row)
        and row.get("volatility_regime_ratio") is not None
        and float(row["volatility_regime_ratio"]) < 0.75
    )
    primary_count = count(lambda row: arm_matches(row, arms[PRIMARY_ID]))
    parent_count = count(lambda row: arm_matches(row, arms[PARENT_CONTROL_ID]))
    up_cost_count = count(up_cost)
    exact_twap_count = count(exact_twap)
    low_vol_count = count(low_vol)
    return {
        "labels_or_outcomes_read": 0,
        "markets_discovered": int(market_counts["markets"] or 0),
        "features_saved": int(market_counts["saved"] or 0),
        "features_failed": int(market_counts["failed"] or 0),
        "features_pending": int(market_counts["pending"] or 0),
        "usable_market_records": len(rows),
        "all_favorites_cost_gt_050_le_090": count(cost_eligible),
        "favorite_up_cost_gt_050_le_090": up_cost_count,
        "favorite_up_cost_exact_verified_twap60": exact_twap_count,
        "favorite_up_cost_exact_twap60_low_vol_lt_075": low_vol_count,
        "parent_control": parent_count,
        "primary_twap_abs_lt_5bps": primary_count,
        "removed_by_low_vol_filter": exact_twap_count - low_vol_count,
        "removed_incrementally_by_twap_lt5_filter": parent_count - primary_count,
        "projected_24h_up_cost_count_at_observed_12h_rate": up_cost_count * 2,
        "projection_is_outcome_blind_and_not_a_guarantee": True,
    }


def build_v028_final_postmortem(
    *,
    result_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    result_file = Path(result_path).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    result = json.loads(result_file.read_text(encoding="utf-8"))
    if result.get("schema") != RESULT_SCHEMA:
        raise RuntimeError("Resultado final V0.28 incompatible")
    if result.get("verdict") != REQUIRED_VERDICT:
        raise RuntimeError("El postmortem requiere el fallo de frecuencia V0.28")
    if result.get("selected_strategy") is not None:
        raise RuntimeError("V0.28 no puede cerrar con estrategia seleccionada")

    database = Path(str(result["database"])).resolve()
    preregistration = Path(str(result["preregistration"])).resolve()
    implementation = Path(str(result["implementation"])).resolve()
    source_hashes = {
        "result": sha256_file(result_file),
        "database": sha256_file(database),
        "preregistration": sha256_file(preregistration),
        "implementation": sha256_file(implementation),
    }
    expected_hashes = {
        "database": result.get("database_sha256"),
        "preregistration": result.get("preregistration_sha256"),
        "implementation": result.get("implementation_sha256"),
    }
    for key, expected in expected_hashes.items():
        if source_hashes[key] != expected:
            raise RuntimeError(f"Evidencia sellada V0.28 no coincide: {key}")
    if output is not None and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if existing.get("schema") == POSTMORTEM_SCHEMA and existing.get(
            "source_hashes"
        ) == source_hashes:
            return existing
        raise RuntimeError("Existe otro postmortem final V0.28")

    funnel = outcome_blind_frequency_funnel(database)
    candidate = result["candidates"][PRIMARY_ID]
    control = result["controls"][PARENT_CONTROL_ID]
    candidate_metrics = dict(candidate["metrics"])
    control_metrics = dict(control["metrics"])
    if int(candidate_metrics["trades"]) != int(funnel["primary_twap_abs_lt_5bps"]):
        raise RuntimeError("Embudo y auditor V0.28 no reproducen el candidato")
    if int(control_metrics["trades"]) != int(funnel["parent_control"]):
        raise RuntimeError("Embudo y auditor V0.28 no reproducen el control")

    gates = candidate["gates"]
    frequency_gates = dict(gates["frequency"])
    statistical_gates = dict(gates["statistical"])
    economic_gates = dict(gates["economic_replication"])
    payload = {
        "schema": POSTMORTEM_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "closed_read_only_postmortem_no_new_backtest",
        "source_files": {
            "result": str(result_file),
            "database": str(database),
            "preregistration": str(preregistration),
            "implementation": str(implementation),
        },
        "source_hashes": source_hashes,
        "source_verdict": result["verdict"],
        "source_completion_reason": result["window"]["completion_reason"],
        "outcome_blind_frequency_funnel": funnel,
        "audited_candidate": {
            "candidate_id": PRIMARY_ID,
            "metrics": candidate_metrics,
            "frequency_gates": frequency_gates,
            "statistical_gates": statistical_gates,
            "economic_replication_gates": economic_gates,
            "frequency_passed": bool(candidate["frequency_passed"]),
            "full_statistical_passed": bool(candidate["full_statistical_passed"]),
            "economic_replication_passed": bool(
                candidate["economic_replication_passed"]
            ),
        },
        "audited_parent_control": {
            "control_id": PARENT_CONTROL_ID,
            "metrics": control_metrics,
            "selectable": bool(control["selectable"]),
        },
        "decisive_findings": {
            "nominal_net_pnl_at_5_shares": candidate_metrics[
                "net_pnl_at_5_shares"
            ],
            "net_without_best_trade_at_5_shares": candidate_metrics[
                "net_without_best_trade_at_5_shares"
            ],
            "one_sided_95_lcb": candidate_metrics["one_sided_95_lcb"],
            "candidate_minus_parent_mean_pnl": candidate[
                "candidate_minus_parent_mean_pnl"
            ],
            "candidate_and_parent_trade_counts_equal": (
                int(candidate_metrics["trades"]) == int(control_metrics["trades"])
            ),
            "twap_lt5_added_incremental_selection": (
                int(funnel["removed_incrementally_by_twap_lt5_filter"]) > 0
            ),
            "low_vol_filter_was_observed_frequency_bottleneck": (
                int(funnel["removed_by_low_vol_filter"]) > 0
            ),
            "failure_class": "FREQUENCY_WITH_FRAGILE_NOMINAL_PROFIT",
        },
        "branch_decision": {
            "v028": "CLOSED_REJECTED_BY_FROZEN_CONTRACT",
            "direct_twap_lt5_replication": "DISCARD",
            "direct_threshold_retune_from_six_trades": "PROHIBITED_OVERFIT_RISK",
            "favorite_up_low_vol_parent": "NOT_RESCUED_IDENTICAL_TO_PRIMARY",
            "selected_strategy": None,
            "recommended_next_experiment": (
                "V029_PROSPECTIVE_HIGH_FREQUENCY_TEMPORAL_HOLDOUT"
            ),
            "reason": (
                "El filtro TWAP <5 bps no separo ningun trade del control y la "
                "frecuencia fallo. V0.29 debe usar cobertura amplia y un holdout "
                "temporal predefinido, no otro corte post hoc sobre seis outcomes."
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
        raise RuntimeError("La evidencia V0.28 cambio durante el postmortem")
    if output is not None:
        _write_atomic(output, payload)
    return payload


__all__ = [
    "POSTMORTEM_SCHEMA",
    "REQUIRED_VERDICT",
    "build_v028_final_postmortem",
    "outcome_blind_frequency_funnel",
]
