from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v028_final_postmortem import POSTMORTEM_SCHEMA
from polymarket_bot.v029_strategy import (
    CANDIDATE_ID,
    PAIRED_CONTROL_ID,
    frozen_model_spec,
    frozen_selection_spec,
)


DESIGN_SCHEMA = "design_evidence_v029_high_frequency_temporal_holdout_1"


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_v029_design_evidence(
    *,
    postmortem_path: str | Path,
    result_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    postmortem_file = Path(postmortem_path).resolve()
    result_file = Path(result_path).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    postmortem = json.loads(postmortem_file.read_text(encoding="utf-8"))
    result = json.loads(result_file.read_text(encoding="utf-8"))
    if postmortem.get("schema") != POSTMORTEM_SCHEMA:
        raise RuntimeError("Postmortem V0.28 incompatible para V0.29")
    if result.get("schema") != "result_v028_twap_lt5_replication_1":
        raise RuntimeError("Resultado V0.28 incompatible para V0.29")
    if postmortem.get("source_hashes", {}).get("result") != sha256_file(result_file):
        raise RuntimeError("Postmortem y resultado V0.28 no coinciden")
    decision = postmortem.get("branch_decision", {})
    if decision.get("v028") != "CLOSED_REJECTED_BY_FROZEN_CONTRACT":
        raise RuntimeError("La rama V0.28 no esta cerrada")
    if decision.get("direct_threshold_retune_from_six_trades") != (
        "PROHIBITED_OVERFIT_RISK"
    ):
        raise RuntimeError("V0.29 requiere bloqueo explicito de retuning")
    if decision.get("recommended_next_experiment") != (
        "V029_PROSPECTIVE_HIGH_FREQUENCY_TEMPORAL_HOLDOUT"
    ):
        raise RuntimeError("Recomendacion V0.29 incompatible")

    database = Path(str(result["database"])).resolve()
    preregistration = Path(str(result["preregistration"])).resolve()
    implementation = Path(str(result["implementation"])).resolve()
    source_hashes = {
        "postmortem": sha256_file(postmortem_file),
        "result": sha256_file(result_file),
        "database": sha256_file(database),
        "v028_preregistration": sha256_file(preregistration),
        "v028_implementation": sha256_file(implementation),
    }
    if source_hashes["database"] != result.get("database_sha256"):
        raise RuntimeError("Base V0.28 no coincide con el resultado sellado")
    if output is not None and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if existing.get("schema") == DESIGN_SCHEMA and existing.get(
            "source_hashes"
        ) == source_hashes:
            return existing
        raise RuntimeError("Existe otra evidencia de diseno V0.29")

    funnel = dict(postmortem["outcome_blind_frequency_funnel"])
    observed_features = int(funnel["features_saved"])
    elapsed_hours = float(result["window"]["elapsed_wall_hours"])
    projected_24h_features = round(observed_features * 24.0 / elapsed_hours)
    payload = {
        "schema": DESIGN_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "prospective_design_only_no_new_backtest_no_launch",
        "source_files": {
            "postmortem": str(postmortem_file),
            "result": str(result_file),
            "database": str(database),
            "v028_preregistration": str(preregistration),
            "v028_implementation": str(implementation),
        },
        "source_hashes": source_hashes,
        "closed_branch_facts": {
            "v028_verdict": result["verdict"],
            "v028_trades": result["candidates"][
                "favorite_up_low_vol_twap_abs_lt_5bps"
            ]["metrics"]["trades"],
            "twap_lt5_incremental_selections": 0,
            "direct_threshold_retune_allowed": False,
            "prior_directional_candidate_promoted": False,
        },
        "outcome_blind_capacity_planning": {
            "observed_hours": elapsed_hours,
            "observed_markets": funnel["markets_discovered"],
            "observed_usable_features": observed_features,
            "projected_24h_usable_features": projected_24h_features,
            "projected_training_rows_first_12h": observed_features,
            "projected_validation_rows_second_12h": observed_features,
            "projection_is_not_a_guarantee": True,
            "outcomes_used_for_capacity_planning": 0,
        },
        "candidate": {
            "id": CANDIDATE_ID,
            "role": "single_development_candidate",
            "model": frozen_model_spec(),
            "selection": frozen_selection_spec(),
            "paired_control_id": PAIRED_CONTROL_ID,
            "coefficients_known_at_preregistration": False,
            "coefficient_learning_rule_frozen_at_preregistration": True,
        },
        "temporal_holdout": {
            "total_hours": 24.0,
            "training_hours": 12.0,
            "validation_hours": 12.0,
            "split_rule": "market_start_before_experiment_start_plus_12h",
            "model_fit_after_terminal_state_only": True,
            "training_rows_fit_preprocessing_and_coefficients": True,
            "validation_rows_never_fit_preprocessing_or_coefficients": True,
            "training_pnl_is_not_selection_evidence": True,
            "validation_metrics_are_the_only_economic_test": True,
        },
        "design_recommendation": {
            "maximum_hours": 24.0,
            "minimum_training_rows": 100,
            "minimum_validation_rows": 100,
            "minimum_validation_candidate_trades": 20,
            "single_model": True,
            "no_early_success": True,
            "technical_checkpoints_hours": [4.0, 8.0, 12.0, 16.0, 20.0],
            "requires_positive_validation_net_pnl": True,
            "requires_validation_profit_factor_above_one": True,
            "requires_positive_validation_one_sided_95_lcb": True,
            "requires_positive_validation_without_best_trade": True,
            "requires_candidate_mean_above_paired_control": True,
            "maximum_promotion": "HYPOTHESIS_REQUIRES_FRESH_V030_REPLICATION",
        },
        "why_frequency_improves": {
            "collects_all_eligible_markets": True,
            "removes_low_volatility_gate": True,
            "removes_twap_distance_gate": True,
            "removes_fixed_direction_gate": True,
            "removes_fixed_cost_band_gate": True,
            "still_abstains_when_both_cost_adjusted_expected_values_are_nonpositive": True,
            "candidate_trade_frequency_not_guaranteed": True,
        },
        "anti_overfit": {
            "v028_trade_outcomes_used_to_choose_model_features": False,
            "v028_trade_outcomes_used_to_choose_thresholds": False,
            "v028_funnel_used_for_capacity_only": True,
            "fixed_features_before_new_collection": True,
            "fixed_hyperparameters_before_new_collection": True,
            "hyperparameter_search_allowed": False,
            "second_half_is_sealed_holdout": True,
            "pass_does_not_enable_money_or_direct_paper_promotion": True,
            "fresh_v030_replication_required_after_any_pass": True,
            "new_backtest_hours": 0,
            "no_automatic_launch": True,
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
    }
    if (
        sha256_file(postmortem_file) != source_hashes["postmortem"]
        or sha256_file(result_file) != source_hashes["result"]
        or sha256_file(database) != source_hashes["database"]
        or sha256_file(preregistration) != source_hashes["v028_preregistration"]
        or sha256_file(implementation) != source_hashes["v028_implementation"]
    ):
        raise RuntimeError("La evidencia cambio durante el diseno V0.29")
    if output is not None:
        _write_atomic(output, payload)
    return payload


__all__ = ["DESIGN_SCHEMA", "build_v029_design_evidence"]
