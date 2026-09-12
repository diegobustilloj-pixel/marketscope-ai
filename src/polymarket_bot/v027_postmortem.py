from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import _parse_utc, sha256_file
from polymarket_bot.v024_tournament import market_record
from polymarket_bot.v025_audit import sequence_metrics
from polymarket_bot.v026_diagnostic import direction_contrast
from polymarket_bot.v026_strategy import ALL_CONTROL_ID, arm_matches, frozen_arm_config


POSTMORTEM_SCHEMA = "postmortem_v027_from_v026b_closed_1"
RESULT_SCHEMA = "result_v026b_adaptive_checkpoints_1"
FOUR_HOUR_BLOCKS = tuple(f"h{start:02d}_to_h{start + 4:02d}" for start in range(0, 24, 4))
UTC_SESSIONS = ("utc_00_to_08", "utc_08_to_16", "utc_16_to_24")
COST_BANDS = ("cost_050_to_060", "cost_060_to_070", "cost_070_to_080", "cost_080_to_090")
VOLATILITY_REGIMES = ("vol_ratio_low", "vol_ratio_normal", "vol_ratio_high")
TWAP_DISTANCE_BANDS = ("twap_abs_lt_5bps", "twap_abs_5_to_10bps", "twap_abs_ge_10bps")
TWAP_ALIGNMENTS = ("twap_agrees_favorite", "twap_disagrees_favorite", "twap_flat_or_missing")


def elapsed_four_hour_block(elapsed_hours: float) -> str:
    if elapsed_hours < 0 or elapsed_hours > 24:
        raise ValueError("Hora transcurrida fuera de la ventana V0.26b")
    index = min(5, int(elapsed_hours // 4))
    return FOUR_HOUR_BLOCKS[index]


def utc_session(hour: int) -> str:
    if hour < 0 or hour > 23:
        raise ValueError("Hora UTC invalida")
    if hour < 8:
        return UTC_SESSIONS[0]
    if hour < 16:
        return UTC_SESSIONS[1]
    return UTC_SESSIONS[2]


def cost_band(cost: float) -> str | None:
    if 0.50 < cost < 0.60:
        return COST_BANDS[0]
    if 0.60 <= cost < 0.70:
        return COST_BANDS[1]
    if 0.70 <= cost < 0.80:
        return COST_BANDS[2]
    if 0.80 <= cost <= 0.90:
        return COST_BANDS[3]
    return None


def volatility_regime(value: float | None) -> str | None:
    if value is None:
        return None
    if value < 0.75:
        return VOLATILITY_REGIMES[0]
    if value < 1.25:
        return VOLATILITY_REGIMES[1]
    return VOLATILITY_REGIMES[2]


def twap_distance_band(value: float | None) -> str | None:
    if value is None:
        return None
    absolute = abs(value)
    if absolute < 5.0:
        return TWAP_DISTANCE_BANDS[0]
    if absolute < 10.0:
        return TWAP_DISTANCE_BANDS[1]
    return TWAP_DISTANCE_BANDS[2]


def twap_alignment(value: float | None, favorite_side: str) -> str:
    if value is None or value == 0:
        return TWAP_ALIGNMENTS[2]
    move_side = "Up" if value > 0 else "Down"
    return TWAP_ALIGNMENTS[0] if move_side == favorite_side else TWAP_ALIGNMENTS[1]


def summarize_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    down = [row for row in rows if row["favorite_side"] == "Down"]
    up = [row for row in rows if row["favorite_side"] == "Up"]
    return {
        "all": sequence_metrics(rows),
        "down": sequence_metrics(down),
        "up": sequence_metrics(up),
        "down_minus_up": direction_contrast(down, up),
    }


def grouped_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    labels: Sequence[str],
    key: Callable[[Mapping[str, Any]], str | None],
) -> dict[str, Any]:
    return {
        label: summarize_rows([row for row in rows if key(row) == label])
        for label in labels
    }


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_closed_v026b_rows(
    *, database: str | Path, result: Mapping[str, Any]
) -> list[dict[str, Any]]:
    database_path = Path(database).resolve()
    start = _parse_utc(str(result["window"]["experiment_started_at"]))
    cutoff = _parse_utc(str(result["window"]["target_end_at"]))
    start_ms = int(start.timestamp() * 1000)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    arms = {str(item["id"]): item for item in frozen_arm_config()}
    all_arm = arms[ALL_CONTROL_ID]
    connection = _open_read_only(database_path)
    try:
        rows: list[dict[str, Any]] = []
        for raw in connection.execute(
            """
            SELECT m.condition_id,m.market_start_ms,m.label,m.label_verified,
             f.feature_json
            FROM shadow_markets AS m
            JOIN shadow_features AS f USING(condition_id)
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
            if record is None or not arm_matches(record, all_arm):
                continue
            implied = float(record["implied_up_mid_probability"])
            record["favorite_probability"] = max(implied, 1.0 - implied)
            timestamp_ms = int(raw["market_start_ms"])
            elapsed_hours = (timestamp_ms - start_ms) / 3_600_000
            twap_distance = feature.get("twap_distance_to_open_bps")
            volatility = feature.get("volatility_regime_ratio")
            record.update(
                {
                    "elapsed_hours": elapsed_hours,
                    "elapsed_block": elapsed_four_hour_block(elapsed_hours),
                    "utc_session": utc_session(
                        datetime.fromtimestamp(
                            timestamp_ms / 1000, timezone.utc
                        ).hour
                    ),
                    "cost_band": cost_band(float(record["entry_cost"])),
                    "volatility_regime": volatility_regime(
                        float(volatility) if volatility is not None else None
                    ),
                    "twap_distance_band": twap_distance_band(
                        float(twap_distance) if twap_distance is not None else None
                    ),
                    "twap_alignment": twap_alignment(
                        float(twap_distance) if twap_distance is not None else None,
                        str(record["favorite_side"]),
                    ),
                    "twap_distance_to_open_bps": twap_distance,
                    "volatility_regime_ratio": volatility,
                }
            )
            rows.append(record)
        return rows
    finally:
        connection.close()


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_v027_postmortem(
    *,
    result_path: str | Path,
    reconciliation_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    result_file = Path(result_path).resolve()
    reconciliation_file = Path(reconciliation_path).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    result = json.loads(result_file.read_text(encoding="utf-8"))
    reconciliation = json.loads(reconciliation_file.read_text(encoding="utf-8"))
    if result.get("schema") != RESULT_SCHEMA:
        raise RuntimeError("Resultado V0.26b incompatible")
    if reconciliation.get("corrected_verdict") != "FAIL_REPLICATION":
        raise RuntimeError("V0.26b no tiene reconciliacion FAIL_REPLICATION")
    database = Path(str(result["database"])).resolve()
    source_hashes = {
        "result": sha256_file(result_file),
        "reconciliation": sha256_file(reconciliation_file),
        "database": sha256_file(database),
    }
    if source_hashes["database"] != result.get("database_sha256"):
        raise RuntimeError("Base V0.26b no coincide")
    if output is not None and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if existing.get("schema") == POSTMORTEM_SCHEMA and existing.get(
            "source_hashes"
        ) == source_hashes:
            return existing
        raise RuntimeError("Existe un postmortem V0.27 para otra evidencia")

    rows = load_closed_v026b_rows(database=database, result=result)
    analyses = {
        "overall": summarize_rows(rows),
        "elapsed_four_hour_blocks": grouped_summary(
            rows,
            labels=FOUR_HOUR_BLOCKS,
            key=lambda row: str(row["elapsed_block"]),
        ),
        "utc_sessions": grouped_summary(
            rows,
            labels=UTC_SESSIONS,
            key=lambda row: str(row["utc_session"]),
        ),
        "fixed_cost_bands": grouped_summary(
            rows,
            labels=COST_BANDS,
            key=lambda row: str(row["cost_band"]),
        ),
        "fixed_volatility_regimes": grouped_summary(
            rows,
            labels=VOLATILITY_REGIMES,
            key=lambda row: str(row["volatility_regime"]),
        ),
        "fixed_twap_distance_bands": grouped_summary(
            rows,
            labels=TWAP_DISTANCE_BANDS,
            key=lambda row: str(row["twap_distance_band"]),
        ),
        "twap_favorite_alignment": grouped_summary(
            rows,
            labels=TWAP_ALIGNMENTS,
            key=lambda row: str(row["twap_alignment"]),
        ),
    }
    down_blocks = {
        key: value["down"]["net_pnl_at_5_shares"]
        for key, value in analyses["elapsed_four_hour_blocks"].items()
    }
    payload = {
        "schema": POSTMORTEM_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "closed_read_only_exploratory_no_new_backtest",
        "source_files": {
            "result": str(result_file),
            "reconciliation": str(reconciliation_file),
            "database": str(database),
        },
        "source_hashes": source_hashes,
        "eligible_rows": len(rows),
        "fixed_partitions": {
            "elapsed_four_hour_blocks": list(FOUR_HOUR_BLOCKS),
            "utc_sessions": list(UTC_SESSIONS),
            "cost_bands": list(COST_BANDS),
            "volatility_regimes": {
                "vol_ratio_low": "ratio < 0.75",
                "vol_ratio_normal": "0.75 <= ratio < 1.25",
                "vol_ratio_high": "ratio >= 1.25",
            },
            "twap_distance_bands": {
                "twap_abs_lt_5bps": "abs(distance) < 5",
                "twap_abs_5_to_10bps": "5 <= abs(distance) < 10",
                "twap_abs_ge_10bps": "abs(distance) >= 10",
            },
        },
        "analyses": analyses,
        "decisive_findings": {
            "down_pnl_at_5_shares_by_4h_block": down_blocks,
            "down_sign_changes_across_blocks": sum(
                (left > 0) != (right > 0)
                for left, right in zip(
                    list(down_blocks.values()), list(down_blocks.values())[1:]
                )
                if left != 0 and right != 0
            ),
            "first_half_down_pnl_at_5_shares": result["primary"][
                "first_half_metrics"
            ]["net_pnl_at_5_shares"],
            "second_half_down_pnl_at_5_shares": result["primary"][
                "second_half_metrics"
            ]["net_pnl_at_5_shares"],
            "up_full_window_pnl_at_5_shares": result["controls"][
                "favorite_up_cap_090_control"
            ]["metrics"]["net_pnl_at_5_shares"],
            "up_full_window_lcb": result["controls"][
                "favorite_up_cap_090_control"
            ]["metrics"]["one_sided_95_lcb"],
            "down_full_window_pnl_at_5_shares": result["primary"]["metrics"][
                "net_pnl_at_5_shares"
            ],
            "raw_down_minus_up_mean_pnl": result["direction_contrast"][
                "down_minus_up_mean_pnl"
            ],
            "cost_matched_down_minus_up_mean_pnl": result[
                "cost_matched_direction_contrast"
            ]["matched_down_minus_up_mean_pnl"],
        },
        "design_consequences": {
            "static_down_rejected": True,
            "up_is_independent_replication_candidate": True,
            "up_is_promoted_now": False,
            "segmented_rules_are_exploratory_only": True,
            "next_forward_requires_fresh_window": True,
            "next_forward_requires_block_stability": True,
            "shortening_to_12h_is_rejected": True,
            "reason": (
                "La ganancia Down a 12h se revirtio antes de 24h; cualquier filtro "
                "de segmento observado aqui necesita validacion independiente."
            ),
        },
        "safety": {
            "database_query_only": True,
            "source_evidence_modified": False,
            "new_backtest_hours": 0,
            "orders_created": False,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
    }
    if (
        sha256_file(result_file) != source_hashes["result"]
        or sha256_file(reconciliation_file) != source_hashes["reconciliation"]
        or sha256_file(database) != source_hashes["database"]
    ):
        raise RuntimeError("La evidencia cambio durante el postmortem")
    if output is not None:
        _write_atomic(output, payload)
    return payload


__all__ = [
    "POSTMORTEM_SCHEMA",
    "build_v027_postmortem",
    "cost_band",
    "elapsed_four_hour_block",
    "grouped_summary",
    "summarize_rows",
    "twap_alignment",
    "twap_distance_band",
    "utc_session",
    "volatility_regime",
]
