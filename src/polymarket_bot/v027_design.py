from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v025_audit import sequence_metrics
from polymarket_bot.v027_postmortem import (
    FOUR_HOUR_BLOCKS,
    POSTMORTEM_SCHEMA,
    load_closed_v026b_rows,
)
from polymarket_bot.v027_strategy import (
    DOWN_BROAD_CONTROL_ID,
    DOWN_MID_COST_ID,
    UP_BROAD_CONTROL_ID,
    UP_LOW_VOL_ID,
    arm_matches,
    frozen_arm_config,
)


DESIGN_EVIDENCE_SCHEMA = "design_evidence_v027_regime_tournament_1"
MINIMUM_TRADES_PER_EVALUABLE_BLOCK = 2
PARENT_BY_CANDIDATE = {
    UP_LOW_VOL_ID: UP_BROAD_CONTROL_ID,
    DOWN_MID_COST_ID: DOWN_BROAD_CONTROL_ID,
}


def candidate_profile(
    rows: Sequence[Mapping[str, Any]], *, candidate_id: str
) -> dict[str, Any]:
    arms = {str(item["id"]): item for item in frozen_arm_config()}
    candidate = arms[candidate_id]
    parent = arms[PARENT_BY_CANDIDATE[candidate_id]]
    selected = [row for row in rows if arm_matches(row, candidate)]
    parent_rows = [row for row in rows if arm_matches(row, parent)]
    first_half = [row for row in selected if float(row["elapsed_hours"]) < 12.0]
    second_half = [row for row in selected if float(row["elapsed_hours"]) >= 12.0]
    blocks = {
        block: sequence_metrics(
            [row for row in selected if row["elapsed_block"] == block]
        )
        for block in FOUR_HOUR_BLOCKS
    }
    evaluable = [
        metrics
        for metrics in blocks.values()
        if int(metrics["trades"]) >= MINIMUM_TRADES_PER_EVALUABLE_BLOCK
    ]
    positive_blocks = sum(
        float(metrics["net_pnl_per_share_sequence"]) > 0 for metrics in evaluable
    )
    metrics = sequence_metrics(selected)
    parent_metrics = sequence_metrics(parent_rows)
    candidate_mean = metrics["mean_pnl_per_share"]
    parent_mean = parent_metrics["mean_pnl_per_share"]
    return {
        "candidate_id": candidate_id,
        "observed_only_not_validated": True,
        "metrics": metrics,
        "first_half_metrics": sequence_metrics(first_half),
        "second_half_metrics": sequence_metrics(second_half),
        "four_hour_blocks": blocks,
        "evaluable_blocks": len(evaluable),
        "positive_evaluable_blocks": positive_blocks,
        "positive_evaluable_block_fraction": (
            round(positive_blocks / len(evaluable), 8) if evaluable else None
        ),
        "parent_control_id": PARENT_BY_CANDIDATE[candidate_id],
        "parent_metrics": parent_metrics,
        "candidate_minus_parent_mean_pnl": (
            round(float(candidate_mean) - float(parent_mean), 8)
            if candidate_mean is not None and parent_mean is not None
            else None
        ),
    }


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_v027_design_evidence(
    *,
    result_path: str | Path,
    reconciliation_path: str | Path,
    postmortem_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    result_file = Path(result_path).resolve()
    reconciliation_file = Path(reconciliation_path).resolve()
    postmortem_file = Path(postmortem_path).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    result = json.loads(result_file.read_text(encoding="utf-8"))
    reconciliation = json.loads(reconciliation_file.read_text(encoding="utf-8"))
    postmortem = json.loads(postmortem_file.read_text(encoding="utf-8"))
    if reconciliation.get("corrected_verdict") != "FAIL_REPLICATION":
        raise RuntimeError("La reconciliacion V0.26b no es FAIL_REPLICATION")
    if postmortem.get("schema") != POSTMORTEM_SCHEMA:
        raise RuntimeError("Postmortem V0.27 incompatible")
    database = Path(str(result["database"])).resolve()
    source_hashes = {
        "result": sha256_file(result_file),
        "reconciliation": sha256_file(reconciliation_file),
        "postmortem": sha256_file(postmortem_file),
        "database": sha256_file(database),
    }
    if output is not None and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if existing.get("schema") == DESIGN_EVIDENCE_SCHEMA and existing.get(
            "source_hashes"
        ) == source_hashes:
            return existing
        raise RuntimeError("Existe evidencia de diseno V0.27 incompatible")
    rows = load_closed_v026b_rows(database=database, result=result)
    profiles = {
        candidate_id: candidate_profile(rows, candidate_id=candidate_id)
        for candidate_id in (UP_LOW_VOL_ID, DOWN_MID_COST_ID)
    }
    payload = {
        "schema": DESIGN_EVIDENCE_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "closed_development_evidence_requires_fresh_replication",
        "source_files": {
            "result": str(result_file),
            "reconciliation": str(reconciliation_file),
            "postmortem": str(postmortem_file),
            "database": str(database),
        },
        "source_hashes": source_hashes,
        "arms": frozen_arm_config(),
        "candidate_profiles": profiles,
        "multiplicity": {
            "co_primary_candidates": 2,
            "family_one_sided_alpha": 0.05,
            "bonferroni_one_sided_alpha_per_candidate": 0.025,
            "selection_allowed_only_in_fresh_forward": True,
        },
        "frozen_design_recommendation": {
            "maximum_hours": 24.0,
            "checkpoint_hours": [4.0, 8.0, 12.0, 16.0, 20.0],
            "no_early_success": True,
            "stop_only_when_both_candidates_are_futile": True,
            "minimum_evaluable_block_trades": MINIMUM_TRADES_PER_EVALUABLE_BLOCK,
            "minimum_evaluable_blocks": 4,
            "minimum_positive_evaluable_block_fraction": 0.60,
            "requires_positive_first_half": True,
            "requires_positive_second_half": True,
            "requires_candidate_mean_above_parent_control": True,
            "tie_break_if_both_pass": [
                "higher_bonferroni_lcb",
                "lower_maximum_drawdown",
                "higher_trade_count",
            ],
        },
        "anti_overfit": {
            "candidate_thresholds_selected_after_v026b": True,
            "therefore_fresh_replication_required": True,
            "no_candidate_promoted_from_closed_data": True,
            "parent_controls_non_selectable": True,
            "new_backtest_hours": 0,
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
    }
    if output is not None:
        _write_atomic(output, payload)
    return payload


__all__ = [
    "DESIGN_EVIDENCE_SCHEMA",
    "MINIMUM_TRADES_PER_EVALUABLE_BLOCK",
    "build_v027_design_evidence",
    "candidate_profile",
]
