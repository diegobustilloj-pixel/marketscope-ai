from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v027_final_postmortem import POSTMORTEM_SCHEMA
from polymarket_bot.v028_strategy import PRIMARY_ID, frozen_arm_config


DESIGN_SCHEMA = "design_evidence_v028_twap_lt5_replication_1"
EXPECTED_SCREEN_HIT = {
    "family": "fixed_twap_distance_bands",
    "segment": "twap_abs_lt_5bps",
}
ONE_SIDED_95_Z = 1.6448536269514722


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_v028_design_evidence(
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
        raise RuntimeError("Postmortem V0.27 incompatible para V0.28")
    if result.get("schema") != "result_v027_regime_replication_tournament_1":
        raise RuntimeError("Resultado V0.27 incompatible para V0.28")
    if postmortem.get("source_hashes", {}).get("result") != sha256_file(result_file):
        raise RuntimeError("Postmortem y resultado V0.27 no coinciden")
    screen = postmortem.get("hypothesis_generation_screen", {})
    if screen.get("screen_hits") != [EXPECTED_SCREEN_HIT]:
        raise RuntimeError("V0.27 no produjo la unica hipotesis esperada")
    if screen.get("selection_or_promotion_allowed") is not False:
        raise RuntimeError("El postmortem V0.27 no esta cerrado a promocion")
    if postmortem.get("decision", {}).get("v028_recommendation") != (
        "NO_LAUNCH_YET_POSTHOC_SEGMENT_REQUIRES_PREREGISTERED_FRESH_TEST"
    ):
        raise RuntimeError("Recomendacion V0.28 incompatible")

    database = Path(str(result["database"])).resolve()
    preregistration = Path(str(result["preregistration"])).resolve()
    implementation = Path(str(result["implementation"])).resolve()
    source_hashes = {
        "postmortem": sha256_file(postmortem_file),
        "result": sha256_file(result_file),
        "database": sha256_file(database),
        "v027_preregistration": sha256_file(preregistration),
        "v027_implementation": sha256_file(implementation),
    }
    if source_hashes["database"] != result.get("database_sha256"):
        raise RuntimeError("Base V0.27 no coincide con el resultado")
    if output is not None and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if existing.get("schema") == DESIGN_SCHEMA and existing.get(
            "source_hashes"
        ) == source_hashes:
            return existing
        raise RuntimeError("Existe otra evidencia de diseno V0.28")

    segment = postmortem["fixed_segment_diagnostics"][
        "fixed_twap_distance_bands"
    ]["twap_abs_lt_5bps"]
    metrics = dict(segment["metrics"])
    temporal = dict(segment["temporal_support"])
    concentration = dict(segment["contribution_diagnostics"])
    observed_mean = float(metrics["mean_pnl_per_share"])
    observed_deviation = float(metrics["pnl_standard_deviation"])
    approximate_n = math.ceil(
        (ONE_SIDED_95_Z * observed_deviation / observed_mean) ** 2
    )
    payload = {
        "schema": DESIGN_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "closed_development_only_single_posthoc_hypothesis",
        "source_files": {
            "postmortem": str(postmortem_file),
            "result": str(result_file),
            "database": str(database),
            "v027_preregistration": str(preregistration),
            "v027_implementation": str(implementation),
        },
        "source_hashes": source_hashes,
        "multiple_segments_examined_in_development": int(
            screen["multiple_segments_examined"]
        ),
        "selected_development_segment": EXPECTED_SCREEN_HIT,
        "arms": frozen_arm_config(),
        "development_evidence_only": {
            "candidate_id": PRIMARY_ID,
            "validated": False,
            "metrics": metrics,
            "first_half_metrics": temporal["first_half"],
            "second_half_metrics": temporal["second_half"],
            "represented_blocks": temporal["represented_blocks"],
            "evaluable_blocks": temporal["evaluable_blocks"],
            "positive_evaluable_blocks": temporal["positive_evaluable_blocks"],
            "positive_evaluable_block_fraction": temporal[
                "positive_evaluable_block_fraction"
            ],
            "net_without_best_trade_at_5_shares": concentration[
                "net_without_best_trade_at_5_shares"
            ],
        },
        "sample_size_planning": {
            "method": "normal_approximation_using_posthoc_observed_mean_and_sd",
            "one_sided_alpha": 0.05,
            "z_value": ONE_SIDED_95_Z,
            "approximate_trades_for_positive_lcb_if_effect_repeats": approximate_n,
            "observed_development_trades": int(metrics["trades"]),
            "observed_24h_frequency": int(metrics["trades"]),
            "guarantee": False,
            "used_as_pass_threshold": False,
        },
        "design_recommendation": {
            "single_primary_hypothesis": True,
            "maximum_hours": 24.0,
            "minimum_final_trades": 20,
            "minimum_first_half_trades": 8,
            "minimum_second_half_trades": 8,
            "checkpoint_hours": [4.0, 8.0, 12.0, 16.0, 20.0],
            "no_early_success": True,
            "requires_positive_total_pnl": True,
            "requires_profit_factor_above_one": True,
            "requires_positive_both_halves": True,
            "requires_positive_without_best_trade": True,
            "requires_minimum_positive_block_fraction": 0.60,
            "requires_mean_above_parent_control": True,
            "requires_positive_one_sided_95_lcb": True,
        },
        "anti_overfit": {
            "threshold_selected_after_examining_13_segments": True,
            "fresh_independent_forward_required": True,
            "old_v027_rows_for_design_only": True,
            "single_hypothesis_in_fresh_forward": True,
            "no_threshold_tuning_allowed": True,
            "no_automatic_launch": True,
            "new_backtest_hours": 0,
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
        or sha256_file(preregistration) != source_hashes["v027_preregistration"]
        or sha256_file(implementation) != source_hashes["v027_implementation"]
    ):
        raise RuntimeError("La evidencia cambio durante el diseno V0.28")
    if output is not None:
        _write_atomic(output, payload)
    return payload


__all__ = [
    "DESIGN_SCHEMA",
    "EXPECTED_SCREEN_HIT",
    "ONE_SIDED_95_Z",
    "build_v028_design_evidence",
]
