from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import _parse_utc, sha256_file
from polymarket_bot.v024_postmortem import attribution_metrics
from polymarket_bot.v024_tournament import market_record


SCHEMA = "v024_structural_cross_window_check_1"


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


def _load_records(database: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    connection = sqlite3.connect(
        f"{database.as_uri()}?mode=ro", uri=True, timeout=10
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        meta = {
            str(key): json.loads(str(value))
            for key, value in connection.execute("SELECT key,value FROM shadow_meta")
        }
        rows: list[dict[str, Any]] = []
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
            rows.append(record)
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
    finally:
        connection.close()
    if quick_check != "ok" or query_only != 1:
        raise RuntimeError(f"Base no apta para analisis read-only: {database}")
    return rows, meta


def _favorite_cap(row: Mapping[str, Any]) -> bool:
    cost = float(row["entry_cost"])
    return cost > 0.50 and cost <= 0.90


SEGMENTS: tuple[tuple[str, Callable[[Mapping[str, Any]], bool]], ...] = (
    ("favorite_cap_all", lambda row: _favorite_cap(row)),
    (
        "favorite_cap_up",
        lambda row: _favorite_cap(row) and str(row["favorite_side"]) == "Up",
    ),
    (
        "favorite_cap_down",
        lambda row: _favorite_cap(row) and str(row["favorite_side"]) == "Down",
    ),
    (
        "favorite_cap_model_agrees",
        lambda row: _favorite_cap(row) and bool(row["model_agrees"]),
    ),
    (
        "favorite_cap_model_disagrees",
        lambda row: _favorite_cap(row) and not bool(row["model_agrees"]),
    ),
    (
        "favorite_cap_cost_050_060",
        lambda row: _favorite_cap(row) and float(row["entry_cost"]) < 0.60,
    ),
    (
        "favorite_cap_cost_060_070",
        lambda row: 0.60 <= float(row["entry_cost"]) < 0.70,
    ),
    (
        "favorite_cap_cost_070_080",
        lambda row: 0.70 <= float(row["entry_cost"]) < 0.80,
    ),
    (
        "favorite_cap_cost_080_090",
        lambda row: 0.80 <= float(row["entry_cost"]) <= 0.90,
    ),
)


def segment_metrics(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        name: attribution_metrics([row for row in rows if predicate(row)])
        for name, predicate in SEGMENTS
    }


def _fold_metrics(
    rows: Sequence[Mapping[str, Any]], *, start_ms: int, fold_hours: int, folds: int
) -> dict[str, list[dict[str, Any]]]:
    width_ms = fold_hours * 60 * 60 * 1000
    result: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in SEGMENTS}
    for fold_index in range(folds):
        lower = start_ms + fold_index * width_ms
        upper = lower + width_ms
        fold_rows = [
            row
            for row in rows
            if lower <= int(row["market_start_ms"]) < upper
        ]
        metrics = segment_metrics(fold_rows)
        for name in result:
            result[name].append(metrics[name])
    return result


def build_structural_check(
    *,
    design_path: str | Path,
    v024_result_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    design_file = Path(design_path).resolve()
    result_file = Path(v024_result_path).resolve()
    design = _read_json(design_file)
    v024_result = _read_json(result_file)
    data_dir = result_file.parent
    sources = {
        "official_7d": (
            data_dir / design["closed_sources"]["official_7d_database"][
                "relative_path"
            ].split("data/", 1)[-1]
        ).resolve(),
        "v023_4h": (
            data_dir / design["closed_sources"]["v023_database"][
                "relative_path"
            ].split("data/", 1)[-1]
        ).resolve(),
        "v024_4h": Path(v024_result["database"]).resolve(),
    }
    expected_hashes = {
        "official_7d": design["closed_sources"]["official_7d_database"]["sha256"],
        "v023_4h": design["closed_sources"]["v023_database"]["sha256"],
        "v024_4h": v024_result["database_sha256"],
    }
    for name, path in sources.items():
        if not path.is_file() or sha256_file(path) != expected_hashes[name]:
            raise RuntimeError(f"Fuente cerrada incompatible: {name}")

    loaded = {name: _load_records(path) for name, path in sources.items()}
    window_metrics = {
        name: segment_metrics(rows) for name, (rows, _) in loaded.items()
    }
    seven_rows, seven_meta = loaded["official_7d"]
    seven_start_ms = int(
        _parse_utc(str(seven_meta["experiment_started_at"])).timestamp() * 1000
    )
    daily = _fold_metrics(
        seven_rows, start_ms=seven_start_ms, fold_hours=24, folds=7
    )

    comparisons: dict[str, Any] = {}
    for name, _ in SEGMENTS:
        metrics = {window: window_metrics[window][name] for window in sources}
        pnl_signs = {
            window: (
                1
                if float(value["net_pnl_per_share_sequence"]) > 0
                else -1
                if float(value["net_pnl_per_share_sequence"]) < 0
                else 0
            )
            for window, value in metrics.items()
        }
        positive_days = sum(
            float(item["net_pnl_per_share_sequence"]) > 0 for item in daily[name]
        )
        comparisons[name] = {
            "windows": metrics,
            "pnl_signs": pnl_signs,
            "positive_in_all_three_closed_windows": all(
                sign > 0 for sign in pnl_signs.values()
            ),
            "minimum_trades_across_4h_windows": min(
                int(metrics["v023_4h"]["trades"]),
                int(metrics["v024_4h"]["trades"]),
            ),
            "official_7d_positive_folds": positive_days,
            "official_7d_folds_with_trades": sum(
                int(item["trades"]) > 0 for item in daily[name]
            ),
            "official_7d_daily": daily[name],
        }

    all_window_positive = [
        name
        for name, item in comparisons.items()
        if item["positive_in_all_three_closed_windows"]
    ]
    sufficiently_frequent = [
        name
        for name in all_window_positive
        if int(comparisons[name]["minimum_trades_across_4h_windows"]) >= 10
    ]
    hashes_after = {name: sha256_file(path) for name, path in sources.items()}
    payload = {
        "schema": SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "post_hoc_research_screen_existing_closed_data_only",
        "warning": (
            "Los segmentos se eligieron despues de observar V0.24. Este chequeo "
            "puede descartar ideas, pero no valida ni habilita una estrategia."
        ),
        "segments": comparisons,
        "positive_in_all_three_closed_windows": all_window_positive,
        "positive_and_at_least_10_trades_in_each_4h_window": sufficiently_frequent,
        "conclusion": (
            "NO_DIRECT_STRUCTURAL_CANDIDATE"
            if not sufficiently_frequent
            else "RESEARCH_CANDIDATE_REQUIRES_FRESH_PREREGISTERED_FORWARD"
        ),
        "integrity": {
            "sources": {
                name: {
                    "path": str(path),
                    "sha256_before": expected_hashes[name],
                    "sha256_after": hashes_after[name],
                    "unchanged": hashes_after[name] == expected_hashes[name],
                }
                for name, path in sources.items()
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


__all__ = ["SCHEMA", "build_structural_check", "segment_metrics"]
