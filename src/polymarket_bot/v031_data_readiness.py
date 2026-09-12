from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import sha256_file


READINESS_SCHEMA = "diagnostic_v031_path_execution_readiness_1"
CAPTURE_CONTRACT: dict[str, Any] = {
    "id": "V0.31_PATH_EXECUTION_CAPTURE_ONLY_TECHNICAL_1H",
    "purpose": "verify_data_needed_for_intramarket_entry_and_exit_without_testing_economics",
    "technical_pilot_hours": 1.0,
    "expected_five_minute_markets": 12,
    "snapshot_frequency_hz": 1,
    "market_seconds_required": 300,
    "maximum_new_experiment_hours": 24.0,
    "required_fields": [
        "market_and_resolution_contract",
        "both_outcomes_top_five_bid_levels_with_price_and_size",
        "both_outcomes_top_five_ask_levels_with_price_and_size",
        "chainlink_spot_with_source_and_receive_timestamps",
        "exact_official_twap_with_window_source_and_receive_timestamps",
        "book_source_and_receive_timestamps",
        "feed_health_and_gap_counters",
    ],
    "official_resolution_contract": "FAIL_CLOSED_EXACT_WINDOW_ONLY",
    "decision_latency_seconds_for_future_simulation": 1,
    "outcomes_read_during_technical_pilot": False,
    "pnl_calculated_during_technical_pilot": False,
    "signals_generated_during_technical_pilot": False,
    "scheduled_supervision": False,
    "final_result_only": True,
    "automatic_followup_launch": False,
}


class V031ReadinessError(RuntimeError):
    pass


def frozen_capture_contract() -> dict[str, Any]:
    return {
        **CAPTURE_CONTRACT,
        "required_fields": list(CAPTURE_CONTRACT["required_fields"]),
    }


def validate_capture_contract(value: Any) -> dict[str, Any]:
    expected = frozen_capture_contract()
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise V031ReadinessError("Contrato de captura V0.31 incompatible")
    return expected


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info([{table}])")
    }


def _inspect_per_second_pair_database(
    path: Path, *, seconds_table: str, markets_table: str
) -> dict[str, Any]:
    connection = _open_read_only(path)
    try:
        columns = _columns(connection, seconds_table)
        aggregate = connection.execute(
            f"""
            SELECT COUNT(*) AS rows,COUNT(DISTINCT condition_id) AS markets,
             MIN(second_offset) AS minimum_second,MAX(second_offset) AS maximum_second,
             SUM(up_best_bid IS NOT NULL AND up_best_ask IS NOT NULL
                 AND down_best_bid IS NOT NULL AND down_best_ask IS NOT NULL)
                 AS full_bbo_rows,
             SUM(up_bid_depth_1c IS NOT NULL AND down_bid_depth_1c IS NOT NULL)
                 AS full_bid_depth_rows
            FROM [{seconds_table}]
            """
        ).fetchone()
        coverage = connection.execute(
            f"""
            SELECT MIN(n) AS minimum_rows_per_market,
             MAX(n) AS maximum_rows_per_market,AVG(n) AS average_rows_per_market
            FROM (SELECT COUNT(*) AS n FROM [{seconds_table}] GROUP BY condition_id)
            """
        ).fetchone()
        market_columns = _columns(connection, markets_table)
        return {
            "database": str(path),
            "database_sha256": sha256_file(path),
            "sqlite_quick_check": str(
                connection.execute("PRAGMA quick_check").fetchone()[0]
            ),
            "query_only": bool(
                int(connection.execute("PRAGMA query_only").fetchone()[0])
            ),
            "rows": int(aggregate["rows"]),
            "markets": int(aggregate["markets"]),
            "minimum_second": int(aggregate["minimum_second"]),
            "maximum_second": int(aggregate["maximum_second"]),
            "full_bbo_rows": int(aggregate["full_bbo_rows"]),
            "full_bid_depth_rows": int(aggregate["full_bid_depth_rows"]),
            "minimum_rows_per_market": int(coverage["minimum_rows_per_market"]),
            "maximum_rows_per_market": int(coverage["maximum_rows_per_market"]),
            "average_rows_per_market": round(
                float(coverage["average_rows_per_market"]), 8
            ),
            "has_per_second_bbo_path": {
                "up_best_bid",
                "up_best_ask",
                "down_best_bid",
                "down_best_ask",
            }.issubset(columns),
            "has_bid_depth": {
                "up_bid_depth_1c",
                "down_bid_depth_1c",
            }.issubset(columns),
            "has_ask_depth": any("ask_depth" in name for name in columns),
            "has_chainlink_spot_path": any("chainlink" in name for name in columns),
            "has_official_twap_path": any("twap" in name for name in columns),
            "has_resolution_contract": any(
                "resolution" in name for name in market_columns
            ),
            "has_outcomes_in_database": any(
                name in market_columns for name in {"label", "outcome"}
            ),
            "fresh_independent_sample": False,
        }
    finally:
        connection.close()


def _inspect_feature_database(
    path: Path,
    *,
    features_table: str,
    twap_table: str,
    diagnostics_table: str | None = None,
) -> dict[str, Any]:
    connection = _open_read_only(path)
    try:
        tables = _tables(connection)
        features = int(
            connection.execute(f"SELECT COUNT(*) FROM [{features_table}]").fetchone()[
                0
            ]
        )
        twap_ticks = int(
            connection.execute(f"SELECT COUNT(*) FROM [{twap_table}]").fetchone()[0]
        )
        diagnostic_rows = (
            int(
                connection.execute(
                    f"SELECT COUNT(*) FROM [{diagnostics_table}]"
                ).fetchone()[0]
            )
            if diagnostics_table and diagnostics_table in tables
            else 0
        )
        windows = [
            int(row[0])
            for row in connection.execute(
                f"SELECT DISTINCT window_s FROM [{twap_table}] ORDER BY window_s"
            )
        ]
        return {
            "database": str(path),
            "database_sha256": sha256_file(path),
            "sqlite_quick_check": str(
                connection.execute("PRAGMA quick_check").fetchone()[0]
            ),
            "query_only": bool(
                int(connection.execute("PRAGMA query_only").fetchone()[0])
            ),
            "feature_rows": features,
            "diagnostic_rows": diagnostic_rows,
            "twap_ticks": twap_ticks,
            "twap_windows_seconds": windows,
            "has_per_second_bbo_path": False,
            "has_top_five_bid_and_ask_depth_path": False,
            "has_chainlink_spot_path": False,
            "has_official_twap_path": twap_ticks > 0,
            "fresh_independent_sample": False,
        }
    finally:
        connection.close()


def _inspect_raw_two_hour_database(path: Path) -> dict[str, Any]:
    connection = _open_read_only(path)
    try:
        counts = {
            f"{row['source']}:{row['stream']}": int(row["event_count"])
            for row in connection.execute(
                "SELECT source,stream,event_count FROM event_counts ORDER BY source,stream"
            )
        }
        markets = int(connection.execute("SELECT COUNT(*) FROM markets").fetchone()[0])
        has_twap = any("twap" in key.lower() for key in counts)
        return {
            "database": str(path),
            "database_sha256": sha256_file(path),
            "sqlite_quick_check": str(
                connection.execute("PRAGMA quick_check").fetchone()[0]
            ),
            "query_only": bool(
                int(connection.execute("PRAGMA query_only").fetchone()[0])
            ),
            "markets": markets,
            "event_counts": counts,
            "has_raw_clob_book_events": counts.get("clob:book", 0) > 0,
            "has_raw_clob_price_change_events": counts.get("clob:price_change", 0)
            > 0,
            "has_chainlink_spot_events": counts.get(
                "rtds:crypto_prices_chainlink", 0
            )
            > 0,
            "has_official_twap_events": has_twap,
            "nominal_collection_hours": 2,
            "current_twap60_contract_compatible": False,
            "fresh_independent_sample": False,
        }
    finally:
        connection.close()


def build_readiness_report(*, project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root).resolve()
    sources = {
        "v018_per_second": _inspect_per_second_pair_database(
            root / "data" / "paper_v018_multifill.db",
            seconds_table="v018_seconds",
            markets_table="v018_markets",
        ),
        "v019_per_second": _inspect_per_second_pair_database(
            root / "data" / "paper_v019_fifo_pair.db",
            seconds_table="v019_seconds",
            markets_table="v019_markets",
        ),
        "v094a_horizon_features": _inspect_feature_database(
            root / "data" / "shadow_forward_twap_transfer_v094a.db",
            features_table="shadow_features",
            twap_table="shadow_twap_ticks",
            diagnostics_table="shadow_diagnostics",
        ),
        "v027_horizon_features": _inspect_feature_database(
            root / "data" / "paper_v027_regime_tournament.db",
            features_table="v027_features",
            twap_table="v027_twap_ticks",
        ),
        "v029_horizon_features": _inspect_feature_database(
            root / "data" / "paper_v029_high_frequency_holdout.db",
            features_table="v029_features",
            twap_table="v029_twap_ticks",
        ),
        "raw_two_hour_v4": _inspect_raw_two_hour_database(
            root / "data" / "polymarket_fuentes_2h_v4.db"
        ),
    }
    database_hashes_after = {
        key: sha256_file(Path(value["database"])) for key, value in sources.items()
    }
    unchanged = all(
        database_hashes_after[key] == str(value["database_sha256"])
        for key, value in sources.items()
    )
    blockers = [
        "no_single_dataset_has_full_intramarket_clob_path_plus_chainlink_spot_plus_exact_official_twap",
        "v018_v019_lack_ask_depth_chainlink_twap_and_resolution_contract_columns",
        "v094a_v027_v029_store_horizon_features_not_executable_full_market_paths",
        "raw_two_hour_v4_has_clob_and_chainlink_but_no_official_twap_events",
        "all_existing_samples_are_already_observed_and_not_fresh_validation",
    ]
    return {
        "schema": READINESS_SCHEMA,
        "status": "BLOCKED_REQUIRES_NEW_CAPTURE",
        "purpose": "decide_whether_existing_local_data_can_measure_intramarket_entry_and_exit",
        "sources": sources,
        "source_databases_unchanged": unchanged,
        "requirements": {
            "single_compatible_dataset_required": True,
            "per_second_or_better_clob_path": True,
            "both_outcomes_bid_and_ask_depth": True,
            "chainlink_spot_path": True,
            "exact_official_twap_path": True,
            "resolution_contract_per_market": True,
            "fresh_independent_sample_for_economic_validation": True,
        },
        "all_requirements_met_by_one_existing_dataset": False,
        "blockers": blockers,
        "decision": "BUILD_CAPTURE_ONLY_V031_BEFORE_ANY_NEW_STRATEGY_TEST",
        "capture_contract": frozen_capture_contract(),
        "implementation": {
            "collector_built": False,
            "technical_auditor_built": False,
            "economic_strategy_built": False,
            "launched": False,
        },
        "promotion_cap": {
            "technical_pilot_pass_allows": "design_fresh_path_strategy_and_request_separate_launch_approval",
            "technical_pilot_can_approve_economic_edge": False,
            "automatic_launch_allowed": False,
        },
        "safety": {
            "orders_enabled": False,
            "paper_orders": 0,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
            "outcomes_read": 0,
            "pnl_calculated": False,
            "new_backtest_hours": 0,
        },
    }


def write_readiness_report(
    *, project_root: str | Path, output_path: str | Path
) -> dict[str, Any]:
    output = Path(output_path).resolve()
    payload = build_readiness_report(project_root=project_root)
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if existing != payload:
            raise V031ReadinessError("Ya existe otro diagnostico V0.31")
        return existing
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return payload


__all__ = [
    "CAPTURE_CONTRACT",
    "READINESS_SCHEMA",
    "V031ReadinessError",
    "build_readiness_report",
    "frozen_capture_contract",
    "validate_capture_contract",
    "write_readiness_report",
]
