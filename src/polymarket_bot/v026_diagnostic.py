from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v024_postmortem import attribution_metrics
from polymarket_bot.v024_tournament import market_record
from polymarket_bot.v025_audit import CONFIDENCE_Z, sequence_metrics


SCHEMA = "diagnostico_v026_directional_interaction_1"
V024_DESIGN_SCHEMA = "diagnostico_v024_parallel_tournament_design_1"
V024_RESULT_SCHEMA = "result_v024_parallel_tournament_4h_1"
V025_RESULT_SCHEMA = "result_v025_down_asymmetry_1"

COST_BANDS: tuple[tuple[str, float, float], ...] = (
    ("0.50_to_0.60", 0.50, 0.60),
    ("0.60_to_0.70", 0.60, 0.70),
    ("0.70_to_0.80", 0.70, 0.80),
    ("0.80_to_0.90", 0.80, 0.90),
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON incompatible: {path}")
    return value


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_closed_records(database: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = Path(database).resolve()
    connection = _open_read_only(path)
    try:
        meta = {
            str(key): json.loads(str(value))
            for key, value in connection.execute("SELECT key,value FROM shadow_meta")
        }
        records: list[dict[str, Any]] = []
        for row in connection.execute(
            """
            SELECT f.condition_id,f.feature_json,m.market_start_ms,m.label,
             s.probability_up
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
    finally:
        connection.close()
    if quick_check != "ok" or query_only != 1:
        raise RuntimeError(f"Base no apta para diagnostico read-only: {path}")
    return records, meta


def favorite_cap_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in rows
        if 0.50 < float(row["entry_cost"]) <= 0.90
    ]


def _pnl_values(rows: Sequence[Mapping[str, Any]]) -> list[float]:
    return [
        (
            1.0 - float(row["entry_cost"])
            if str(row["favorite_side"]) == str(row["label"])
            else -float(row["entry_cost"])
        )
        for row in rows
    ]


def _side_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result = attribution_metrics(rows)
    result.update(
        {
            key: value
            for key, value in sequence_metrics(rows).items()
            if key
            in {
                "profit_factor",
                "pnl_standard_deviation",
                "one_sided_95_lcb",
                "maximum_drawdown_per_share",
                "roi_on_cost",
            }
        }
    )
    return result


def approximate_sample_size_for_positive_lcb(
    *, mean_pnl_per_share: float, pnl_standard_deviation: float
) -> int | None:
    """Planning approximation only; it is not a power calculation or a gate."""
    if mean_pnl_per_share <= 0 or pnl_standard_deviation <= 0:
        return None
    return math.ceil(
        (CONFIDENCE_Z * pnl_standard_deviation / mean_pnl_per_share) ** 2
    )


def direction_contrast(
    down_rows: Sequence[Mapping[str, Any]],
    up_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not down_rows or not up_rows:
        return {
            "available": False,
            "down_trades": len(down_rows),
            "up_trades": len(up_rows),
        }
    down = attribution_metrics(down_rows)
    up = attribution_metrics(up_rows)
    down_pnls = _pnl_values(down_rows)
    up_pnls = _pnl_values(up_rows)
    delta_mean = statistics.fmean(down_pnls) - statistics.fmean(up_pnls)
    down_variance = statistics.variance(down_pnls) if len(down_pnls) > 1 else 0.0
    up_variance = statistics.variance(up_pnls) if len(up_pnls) > 1 else 0.0
    standard_error = math.sqrt(
        down_variance / len(down_pnls) + up_variance / len(up_pnls)
    )
    realization_contribution = float(down["win_rate"]) - float(up["win_rate"])
    cheaper_entry_contribution = float(up["average_entry_cost"]) - float(
        down["average_entry_cost"]
    )
    return {
        "available": True,
        "down_trades": len(down_rows),
        "up_trades": len(up_rows),
        "down_mean_pnl_per_share": round(statistics.fmean(down_pnls), 8),
        "up_mean_pnl_per_share": round(statistics.fmean(up_pnls), 8),
        "down_minus_up_mean_pnl": round(delta_mean, 8),
        "difference_standard_error": round(standard_error, 8),
        "one_sided_95_lcb_for_difference": round(
            delta_mean - CONFIDENCE_Z * standard_error, 8
        ),
        "decomposition": {
            "down_minus_up_win_rate": round(realization_contribution, 8),
            "up_minus_down_average_entry_cost": round(
                cheaper_entry_contribution, 8
            ),
            "reconstructed_down_minus_up_mean_pnl": round(
                realization_contribution + cheaper_entry_contribution, 8
            ),
        },
    }


def _cost_band(cost: float) -> str | None:
    for index, (name, lower, upper) in enumerate(COST_BANDS):
        if cost > lower and (cost < upper or (index == len(COST_BANDS) - 1 and cost <= upper)):
            return name
        if index > 0 and cost == lower:
            return name
    return None


def cost_matched_direction_contrast(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    bands: dict[str, dict[str, list[Mapping[str, Any]]]] = {
        name: {"Down": [], "Up": []} for name, _, _ in COST_BANDS
    }
    for row in rows:
        band = _cost_band(float(row["entry_cost"]))
        side = str(row["favorite_side"])
        if band is not None and side in {"Down", "Up"}:
            bands[band][side].append(row)
    band_results: dict[str, Any] = {}
    weighted_sum = 0.0
    total_weight = 0
    for name, _, _ in COST_BANDS:
        contrast = direction_contrast(bands[name]["Down"], bands[name]["Up"])
        weight = min(len(bands[name]["Down"]), len(bands[name]["Up"]))
        contrast["matched_weight"] = weight
        band_results[name] = contrast
        if contrast["available"] and weight:
            weighted_sum += float(contrast["down_minus_up_mean_pnl"]) * weight
            total_weight += weight
    return {
        "method": "Fixed pre-existing 0.10 cost bands weighted by the smaller directional count; descriptive only.",
        "matched_weight": total_weight,
        "matched_down_minus_up_mean_pnl": (
            round(weighted_sum / total_weight, 8) if total_weight else None
        ),
        "bands": band_results,
    }


def window_diagnostic(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    eligible = favorite_cap_rows(rows)
    down = [row for row in eligible if str(row["favorite_side"]) == "Down"]
    up = [row for row in eligible if str(row["favorite_side"]) == "Up"]
    return {
        "favorite_cap_all": _side_metrics(eligible),
        "favorite_cap_down": _side_metrics(down),
        "favorite_cap_up": _side_metrics(up),
        "raw_direction_contrast": direction_contrast(down, up),
        "cost_matched_direction_contrast": cost_matched_direction_contrast(eligible),
    }


def build_v026_diagnostic(
    *,
    data_dir: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    directory = Path(data_dir).resolve()
    v024_design_path = directory / "diagnostico_v024_parallel_tournament_design.json"
    v024_result_path = directory / "resultado_v024_parallel_tournament_4h.json"
    v025_result_path = directory / "resultado_v025_down_asymmetry.json"
    v024_design = _read_json(v024_design_path)
    v024_result = _read_json(v024_result_path)
    v025_result = _read_json(v025_result_path)
    if v024_design.get("schema") != V024_DESIGN_SCHEMA:
        raise RuntimeError("Diseno V0.24 incompatible")
    if v024_result.get("schema") != V024_RESULT_SCHEMA:
        raise RuntimeError("Resultado V0.24 incompatible")
    if v025_result.get("schema") != V025_RESULT_SCHEMA:
        raise RuntimeError("Resultado V0.25 incompatible")

    sources = {
        "official_7d": (
            directory / Path(v024_design["closed_sources"]["official_7d_database"]["relative_path"]).name,
            str(v024_design["closed_sources"]["official_7d_database"]["sha256"]),
        ),
        "v023_4h": (
            directory / Path(v024_design["closed_sources"]["v023_database"]["relative_path"]).name,
            str(v024_design["closed_sources"]["v023_database"]["sha256"]),
        ),
        "v024_4h": (
            Path(str(v024_result["database"])).resolve(),
            str(v024_result["database_sha256"]),
        ),
        "v025_12h": (
            Path(str(v025_result["database"])).resolve(),
            str(v025_result["database_sha256"]),
        ),
    }
    for name, (path, expected_hash) in sources.items():
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise RuntimeError(f"Fuente cerrada incompatible: {name}")

    loaded = {name: load_closed_records(path) for name, (path, _) in sources.items()}
    windows = {name: window_diagnostic(rows) for name, (rows, _) in loaded.items()}
    down_positive_windows = [
        name
        for name, value in windows.items()
        if float(value["favorite_cap_down"]["net_pnl_per_share_sequence"]) > 0
    ]
    raw_contrast_positive_windows = [
        name
        for name, value in windows.items()
        if float(value["raw_direction_contrast"]["down_minus_up_mean_pnl"]) > 0
    ]
    matched_contrast_positive_windows = [
        name
        for name, value in windows.items()
        if value["cost_matched_direction_contrast"]["matched_down_minus_up_mean_pnl"]
        is not None
        and float(
            value["cost_matched_direction_contrast"]["matched_down_minus_up_mean_pnl"]
        )
        > 0
    ]
    lcb_positive_windows = [
        name
        for name, value in windows.items()
        if value["favorite_cap_down"]["one_sided_95_lcb"] is not None
        and float(value["favorite_cap_down"]["one_sided_95_lcb"]) > 0
    ]

    v025_down = windows["v025_12h"]["favorite_cap_down"]
    observed_v025_rate = int(v025_down["trades"]) / 12.0
    projected_24h = observed_v025_rate * 24.0
    official_down = windows["official_7d"]["favorite_cap_down"]
    official_rate = int(official_down["trades"]) / 168.0
    combined_down_rows = [
        row
        for rows, _ in loaded.values()
        for row in favorite_cap_rows(rows)
        if str(row["favorite_side"]) == "Down"
    ]
    combined_down = _side_metrics(combined_down_rows)
    planning = {}
    for name, metrics, rate in (
        ("v025_observed", v025_down, observed_v025_rate),
        ("official_7d_observed", official_down, official_rate),
        ("combined_closed_posthoc", combined_down, observed_v025_rate),
    ):
        approximate_n = approximate_sample_size_for_positive_lcb(
            mean_pnl_per_share=float(metrics["reconstructed_mean_pnl_per_share"]),
            pnl_standard_deviation=float(metrics["pnl_standard_deviation"]),
        )
        planning[name] = {
            "observed_trades": int(metrics["trades"]),
            "observed_mean_pnl_per_share": metrics[
                "reconstructed_mean_pnl_per_share"
            ],
            "observed_pnl_standard_deviation": metrics[
                "pnl_standard_deviation"
            ],
            "normal_approximation_total_n_for_positive_one_sided_95_lcb": (
                approximate_n
            ),
            "hours_at_reference_rate": (
                round(approximate_n / rate, 8)
                if approximate_n is not None and rate > 0
                else None
            ),
        }
    hashes_after = {name: sha256_file(path) for name, (path, _) in sources.items()}
    payload = {
        "schema": SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "post_hoc_closed_data_read_only_no_new_backtest",
        "warning": (
            "El analisis usa resultados ya observados; puede justificar una nueva "
            "hipotesis prospectiva, pero no valida ni habilita una estrategia."
        ),
        "windows": windows,
        "cross_window": {
            "window_count": len(windows),
            "down_positive_windows": down_positive_windows,
            "down_positive_in_all_windows": len(down_positive_windows) == len(windows),
            "raw_down_minus_up_positive_windows": raw_contrast_positive_windows,
            "raw_down_minus_up_positive_in_all_windows": (
                len(raw_contrast_positive_windows) == len(windows)
            ),
            "cost_matched_down_minus_up_positive_windows": matched_contrast_positive_windows,
            "cost_matched_down_minus_up_positive_in_all_windows": (
                len(matched_contrast_positive_windows) == len(windows)
            ),
            "down_absolute_lcb_positive_windows": lcb_positive_windows,
            "down_absolute_lcb_positive_in_any_window": bool(lcb_positive_windows),
        },
        "frequency": {
            "v025_down_trades_in_12h": int(v025_down["trades"]),
            "v025_observed_down_trades_per_hour": round(observed_v025_rate, 8),
            "v025_rate_projected_down_trades_in_24h": round(projected_24h, 8),
            "projection_warning": "Proyeccion descriptiva de una sola ventana; no es garantia.",
        },
        "sample_size_planning": {
            "method": (
                "Aproximacion normal (z*desviacion/media)^2 con parametros "
                "observados; no es potencia formal ni validacion."
            ),
            "estimates": planning,
            "single_24h_full_confidence_is_realistic_at_v025_rate": (
                planning["v025_observed"][
                    "normal_approximation_total_n_for_positive_one_sided_95_lcb"
                ]
                is not None
                and projected_24h
                >= planning["v025_observed"][
                    "normal_approximation_total_n_for_positive_one_sided_95_lcb"
                ]
            ),
            "warning": (
                "Todas las estimaciones usan datos ya observados y solo sirven "
                "para dimensionar una prueba futura."
            ),
        },
        "integrity": {
            "sources": {
                name: {
                    "path": str(path),
                    "sha256_before": expected_hash,
                    "sha256_after": hashes_after[name],
                    "unchanged": expected_hash == hashes_after[name],
                }
                for name, (path, expected_hash) in sources.items()
            },
            "read_only": True,
            "orders_created": False,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
    }
    if output_path is not None:
        _write_atomic(Path(output_path).resolve(), payload)
    return payload


__all__ = [
    "COST_BANDS",
    "SCHEMA",
    "build_v026_diagnostic",
    "approximate_sample_size_for_positive_lcb",
    "cost_matched_direction_contrast",
    "direction_contrast",
    "favorite_cap_rows",
    "load_closed_records",
    "window_diagnostic",
]
