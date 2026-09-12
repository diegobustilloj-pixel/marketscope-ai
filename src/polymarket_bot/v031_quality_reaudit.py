from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v031_capture import open_read_only


PREREG_SCHEMA = "prereg_v031_quality_semantics_reaudit_2"
PREREG_STATUS = "FROZEN_CLOSED_DATA_DIAGNOSTIC_REAUDIT"
IMPLEMENTATION_SCHEMA = "implementation_v031_quality_semantics_reaudit_2"
RESULT_SCHEMA = "result_v031_quality_semantics_reaudit_2"
VARIANT = "V0.31R_QUALITY_SEMANTICS_REAUDIT_V2"
PASS_VERDICT = "PASS_TECHNICAL_CAPTURE_ONLY_REAUDITED_SEMANTICS_V2"

INPUT_PATHS = {
    "database": "data/capture_v031_path_execution_1h.db",
    "original_result": "data/resultado_v031_path_execution_capture.json",
    "original_preregistration": "data/prereg_v031_path_execution_capture.json",
    "original_implementation": "data/implementation_v031_path_execution_capture.json",
    "original_capture_code": "src/polymarket_bot/v031_capture.py",
    "original_auditor_code": "src/polymarket_bot/v031_audit.py",
}
IMPLEMENTATION_FILES = {
    "reauditor": "src/polymarket_bot/v031_quality_reaudit.py",
    "entrypoint": "v031_quality_reaudit_report.py",
    "tests": "tests/test_v031_quality_reaudit.py",
}

EXPECTED_SCOPE = {
    "purpose": "separate_missing_or_stale_data_from_observed_one_sided_liquidity",
    "posthoc_diagnostic": True,
    "independent_validation_claim_allowed": False,
    "closed_database_read_only": True,
    "new_capture_hours": 0,
    "original_database_mutation_allowed": False,
    "original_result_mutation_allowed": False,
    "original_verdict_preserved": "FAIL_TECHNICAL_QUALITY",
    "maximum_positive_result": PASS_VERDICT,
}
EXPECTED_QUALITY_SEMANTICS = {
    "book_initialized": "source_timestamp_ms_received_timestamp_ms_and_age_ms_are_not_null",
    "book_fresh": "book_initialized_and_original_fresh_flag_equals_one",
    "level_arrays_valid": "bid_and_ask_are_json_arrays_of_at_most_five_positive_price_size_pairs_in_book_order",
    "observed_empty_side": "initialized_fresh_valid_book_with_zero_levels_on_that_side",
    "observed_empty_side_is_missing_data": False,
    "book_states": [
        "UNINITIALIZED",
        "STALE",
        "INVALID_LEVELS",
        "EMPTY_OBSERVED",
        "BID_ONLY_OBSERVED",
        "ASK_ONLY_OBSERVED",
        "TWO_SIDED_OBSERVED",
    ],
    "data_complete_v2": "fresh_chainlink_and_exact_fresh_twap_and_both_books_initialized_fresh_with_valid_level_arrays",
    "execution_actions": {
        "buy_up": "fresh_valid_up_ask_has_at_least_one_level",
        "sell_up": "fresh_valid_up_bid_has_at_least_one_level",
        "buy_down": "fresh_valid_down_ask_has_at_least_one_level",
        "sell_down": "fresh_valid_down_bid_has_at_least_one_level",
        "paired_buy": "buy_up_and_buy_down",
        "paired_sell": "sell_up_and_sell_down",
        "full_round_trip": "all_four_actions",
    },
    "synthetic_complement_allowed": False,
    "empty_side_execution_policy": "FAIL_CLOSED_FOR_THE_UNAVAILABLE_ACTION",
}
EXPECTED_GATES = {
    "minimum_data_complete_v2_coverage": 0.9,
    "sqlite_quick_check_required": "ok",
    "database_hash_must_remain_unchanged": True,
    "original_result_hash_must_remain_unchanged": True,
    "all_other_original_technical_gates_must_remain_passed": True,
    "all_original_safety_gates_must_remain_passed": True,
}
EXPECTED_DIAGNOSTICS = [
    "action_availability_by_leg",
    "paired_buy_availability",
    "paired_sell_availability",
    "full_round_trip_availability",
    "one_leg_only_risk",
    "maximum_consecutive_one_sided_seconds",
    "per_market_and_30_second_bin_breakdown",
]
EXPECTED_DATA_POLICY = {
    "outcomes_read": 0,
    "labels_read": 0,
    "pnl_calculated": False,
    "signals_generated": False,
    "economic_strategy_present": False,
    "economic_edge_can_be_claimed": False,
    "cross_date_join_allowed": False,
}
EXPECTED_SAFETY = {
    "orders_enabled": False,
    "paper_orders_enabled": False,
    "wallet_required": False,
    "real_money": "BLOQUEADO",
    "automatic_followup_launch": False,
    "scheduled_supervision": False,
}


class V031QualityReauditError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _require(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise V031QualityReauditError(f"V0.31R incompatible: {field}")


def load_and_verify_quality_prereg(
    path: str | Path,
    *,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(path).resolve()
    if not prereg_file.is_file():
        raise V031QualityReauditError("Preinscripcion V0.31R no encontrada")
    payload = json.loads(prereg_file.read_text(encoding="utf-8"))
    _require(payload.get("schema"), PREREG_SCHEMA, "schema")
    _require(payload.get("status"), PREREG_STATUS, "status")
    _require(payload.get("variant"), VARIANT, "variant")
    _require(payload.get("scope"), EXPECTED_SCOPE, "scope")
    _require(
        payload.get("quality_semantics_v2"),
        EXPECTED_QUALITY_SEMANTICS,
        "quality_semantics_v2",
    )
    _require(payload.get("technical_gates"), EXPECTED_GATES, "technical_gates")
    _require(
        payload.get("reported_execution_diagnostics"),
        EXPECTED_DIAGNOSTICS,
        "reported_execution_diagnostics",
    )
    _require(payload.get("data_policy"), EXPECTED_DATA_POLICY, "data_policy")
    _require(payload.get("safety"), EXPECTED_SAFETY, "safety")
    inputs = payload.get("inputs")
    if not isinstance(inputs, Mapping) or set(inputs) != set(INPUT_PATHS):
        raise V031QualityReauditError("Inventario de entradas V0.31R incompatible")
    for key, relative in INPUT_PATHS.items():
        record = inputs.get(key)
        if not isinstance(record, Mapping):
            raise V031QualityReauditError(f"Entrada V0.31R invalida: {key}")
        _require(record.get("relative_path"), relative, f"inputs.{key}.relative_path")
        source = root / relative
        if not source.is_file():
            raise V031QualityReauditError(f"Entrada V0.31R no encontrada: {relative}")
        _require(record.get("sha256"), sha256_file(source), f"inputs.{key}.sha256")
    return dict(payload)


def build_quality_implementation_manifest(
    *,
    prereg_path: str | Path,
    output_path: str | Path,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    output = Path(output_path).resolve()
    prereg = load_and_verify_quality_prereg(prereg_file, project_root=root)
    code_hashes: dict[str, str] = {}
    for key, relative in IMPLEMENTATION_FILES.items():
        source = root / relative
        if not source.is_file():
            raise V031QualityReauditError(f"Implementacion V0.31R incompleta: {relative}")
        code_hashes[key] = sha256_file(source)
    payload = {
        "schema": IMPLEMENTATION_SCHEMA,
        "status": "BUILT_TESTED_READY_FOR_ONE_CLOSED_DATA_REAUDIT",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "preregistration": str(prereg_file),
        "preregistration_sha256": sha256_file(prereg_file),
        "code_hashes": code_hashes,
        "semantic_patch_only": True,
        "collector_changed": False,
        "original_database_immutable": True,
        "original_result_immutable": True,
        "economic_strategy_built": False,
        "data_policy": prereg["data_policy"],
        "safety": prereg["safety"],
    }
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == IMPLEMENTATION_SCHEMA
            and existing.get("preregistration_sha256") == payload["preregistration_sha256"]
            and existing.get("code_hashes") == code_hashes
        ):
            return existing
        raise V031QualityReauditError("Existe otro manifiesto V0.31R")
    _write_atomic(output, payload)
    return payload


def load_and_verify_quality_implementation(
    path: str | Path,
    *,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    implementation_file = Path(path).resolve()
    if not implementation_file.is_file():
        raise V031QualityReauditError("Implementacion V0.31R no sellada")
    payload = json.loads(implementation_file.read_text(encoding="utf-8"))
    _require(payload.get("schema"), IMPLEMENTATION_SCHEMA, "implementation.schema")
    prereg_file = Path(str(payload.get("preregistration") or "")).resolve()
    prereg = load_and_verify_quality_prereg(prereg_file, project_root=root)
    expected = {
        "status": "BUILT_TESTED_READY_FOR_ONE_CLOSED_DATA_REAUDIT",
        "variant": VARIANT,
        "preregistration_sha256": sha256_file(prereg_file),
        "semantic_patch_only": True,
        "collector_changed": False,
        "original_database_immutable": True,
        "original_result_immutable": True,
        "economic_strategy_built": False,
        "data_policy": prereg["data_policy"],
        "safety": prereg["safety"],
    }
    for key, value in expected.items():
        _require(payload.get(key), value, f"implementation.{key}")
    hashes = payload.get("code_hashes")
    if not isinstance(hashes, Mapping) or set(hashes) != set(IMPLEMENTATION_FILES):
        raise V031QualityReauditError("Inventario de implementacion V0.31R incompatible")
    for key, relative in IMPLEMENTATION_FILES.items():
        _require(hashes.get(key), sha256_file(root / relative), f"code_hashes.{key}")
    return dict(payload)


def _positive_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0.0


def _timestamp_present(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def _age_present(value: Any) -> bool:
    try:
        return int(value) >= 0
    except (TypeError, ValueError):
        return False


def _parse_levels(value: Any, *, side: str) -> tuple[bool, list[list[float]]]:
    try:
        raw = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return False, []
    if not isinstance(raw, list) or len(raw) > 5:
        return False, []
    levels: list[list[float]] = []
    for item in raw:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
            return False, []
        try:
            price = float(item[0])
            size = float(item[1])
        except (TypeError, ValueError):
            return False, []
        if not (math.isfinite(price) and 0.0 < price < 1.0):
            return False, []
        if not (math.isfinite(size) and size > 0.0):
            return False, []
        levels.append([price, size])
    prices = [item[0] for item in levels]
    if side == "bid" and any(left <= right for left, right in zip(prices, prices[1:])):
        return False, []
    if side == "ask" and any(left >= right for left, right in zip(prices, prices[1:])):
        return False, []
    return True, levels


def _book_classification(row: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    bid_valid, bids = _parse_levels(row.get(f"{prefix}_bid_levels_json"), side="bid")
    ask_valid, asks = _parse_levels(row.get(f"{prefix}_ask_levels_json"), side="ask")
    initialized = all(
        (
            _timestamp_present(row.get(f"{prefix}_book_source_timestamp_ms")),
            _timestamp_present(row.get(f"{prefix}_book_received_timestamp_ms")),
            _age_present(row.get(f"{prefix}_book_age_ms")),
        )
    )
    fresh = initialized and bool(row.get(f"{prefix}_book_fresh"))
    structure_valid = bid_valid and ask_valid
    if not initialized:
        state = "UNINITIALIZED"
    elif not fresh:
        state = "STALE"
    elif not structure_valid:
        state = "INVALID_LEVELS"
    elif not bids and not asks:
        state = "EMPTY_OBSERVED"
    elif bids and not asks:
        state = "BID_ONLY_OBSERVED"
    elif asks and not bids:
        state = "ASK_ONLY_OBSERVED"
    else:
        state = "TWO_SIDED_OBSERVED"
    data_valid = fresh and structure_valid
    return {
        "state": state,
        "initialized": initialized,
        "fresh": fresh,
        "structure_valid": structure_valid,
        "data_valid": data_valid,
        "bid_available": data_valid and bool(bids),
        "ask_available": data_valid and bool(asks),
        "bid_levels": len(bids),
        "ask_levels": len(asks),
    }


def classify_snapshot(
    row: Mapping[str, Any],
    *,
    resolution_twap_window_s: int,
) -> dict[str, Any]:
    up = _book_classification(row, "up")
    down = _book_classification(row, "down")
    chainlink_valid = all(
        (
            _positive_number(row.get("chainlink_price")),
            _timestamp_present(row.get("chainlink_source_timestamp_ms")),
            _timestamp_present(row.get("chainlink_received_timestamp_ms")),
            _age_present(row.get("chainlink_age_ms")),
            bool(row.get("chainlink_fresh")),
        )
    )
    twap_valid = all(
        (
            _positive_number(row.get("official_twap_price")),
            _timestamp_present(row.get("official_twap_source_timestamp_ms")),
            _timestamp_present(row.get("official_twap_received_timestamp_ms")),
            _age_present(row.get("official_twap_age_ms")),
            bool(row.get("official_twap_fresh")),
            int(row.get("official_twap_window_s") or 0) == int(resolution_twap_window_s),
        )
    )
    buy_up = bool(up["ask_available"])
    sell_up = bool(up["bid_available"])
    buy_down = bool(down["ask_available"])
    sell_down = bool(down["bid_available"])
    one_sided_states = {"BID_ONLY_OBSERVED", "ASK_ONLY_OBSERVED", "EMPTY_OBSERVED"}
    return {
        "condition_id": str(row.get("condition_id") or ""),
        "second_offset": int(row.get("second_offset") or 0),
        "original_complete": bool(row.get("complete")),
        "original_quality_flags": int(row.get("quality_flags") or 0),
        "chainlink_valid": chainlink_valid,
        "twap_valid": twap_valid,
        "up": up,
        "down": down,
        "data_complete_v2": chainlink_valid and twap_valid and up["data_valid"] and down["data_valid"],
        "one_sided_observed": up["state"] in one_sided_states or down["state"] in one_sided_states,
        "actions": {
            "buy_up": buy_up,
            "sell_up": sell_up,
            "buy_down": buy_down,
            "sell_down": sell_down,
            "paired_buy": buy_up and buy_down,
            "paired_sell": sell_up and sell_down,
            "full_round_trip": buy_up and sell_up and buy_down and sell_down,
            "exactly_one_buy_leg": buy_up != buy_down,
            "exactly_one_sell_leg": sell_up != sell_down,
        },
    }


def _coverage(count: int, rows: int) -> float:
    return round(count / rows, 8) if rows else 0.0


def summarize_classifications(
    classified_rows: Sequence[Mapping[str, Any]],
    *,
    slug_by_condition: Mapping[str, str],
) -> dict[str, Any]:
    rows = len(classified_rows)
    data_complete = sum(bool(row["data_complete_v2"]) for row in classified_rows)
    original_complete = sum(bool(row["original_complete"]) for row in classified_rows)
    reclassified_valid = sum(
        bool(row["data_complete_v2"]) and not bool(row["original_complete"])
        for row in classified_rows
    )
    regressed = sum(
        bool(row["original_complete"]) and not bool(row["data_complete_v2"])
        for row in classified_rows
    )
    state_counts = {
        side: {
            state: sum(row[side]["state"] == state for row in classified_rows)
            for state in EXPECTED_QUALITY_SEMANTICS["book_states"]
        }
        for side in ("up", "down")
    }
    failure_reasons = {
        "chainlink_invalid_or_stale": sum(not bool(row["chainlink_valid"]) for row in classified_rows),
        "twap_invalid_stale_or_misaligned": sum(not bool(row["twap_valid"]) for row in classified_rows),
        "up_uninitialized": state_counts["up"]["UNINITIALIZED"],
        "up_stale": state_counts["up"]["STALE"],
        "up_invalid_levels": state_counts["up"]["INVALID_LEVELS"],
        "down_uninitialized": state_counts["down"]["UNINITIALIZED"],
        "down_stale": state_counts["down"]["STALE"],
        "down_invalid_levels": state_counts["down"]["INVALID_LEVELS"],
    }
    action_names = tuple(EXPECTED_QUALITY_SEMANTICS["execution_actions"]) + (
        "exactly_one_buy_leg",
        "exactly_one_sell_leg",
    )
    action_counts = {
        name: sum(bool(row["actions"][name]) for row in classified_rows)
        for name in action_names
    }

    by_condition: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_bin: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in classified_rows:
        condition_id = str(row["condition_id"])
        by_condition[condition_id].append(row)
        by_bin[(int(row["second_offset"]) // 30) * 30].append(row)

    per_market: list[dict[str, Any]] = []
    maximum_one_sided_streak = 0
    for condition_id, items in by_condition.items():
        ordered = sorted(items, key=lambda row: int(row["second_offset"]))
        current_streak = 0
        market_maximum_streak = 0
        first_one_sided: int | None = None
        last_one_sided: int | None = None
        for row in ordered:
            if bool(row["one_sided_observed"]):
                offset = int(row["second_offset"])
                first_one_sided = offset if first_one_sided is None else first_one_sided
                last_one_sided = offset
                current_streak += 1
                market_maximum_streak = max(market_maximum_streak, current_streak)
            else:
                current_streak = 0
        maximum_one_sided_streak = max(maximum_one_sided_streak, market_maximum_streak)
        item_rows = len(ordered)
        per_market.append(
            {
                "condition_id": condition_id,
                "slug": slug_by_condition.get(condition_id),
                "snapshots": item_rows,
                "original_complete": sum(bool(row["original_complete"]) for row in ordered),
                "data_complete_v2": sum(bool(row["data_complete_v2"]) for row in ordered),
                "data_complete_v2_coverage": _coverage(
                    sum(bool(row["data_complete_v2"]) for row in ordered), item_rows
                ),
                "one_sided_observed": sum(bool(row["one_sided_observed"]) for row in ordered),
                "first_one_sided_offset": first_one_sided,
                "last_one_sided_offset": last_one_sided,
                "maximum_consecutive_one_sided_seconds": market_maximum_streak,
                "paired_buy_ready": sum(bool(row["actions"]["paired_buy"]) for row in ordered),
                "paired_sell_ready": sum(bool(row["actions"]["paired_sell"]) for row in ordered),
                "full_round_trip_ready": sum(bool(row["actions"]["full_round_trip"]) for row in ordered),
            }
        )
    per_market.sort(key=lambda item: str(item.get("slug") or ""))

    by_30_second_bin: list[dict[str, Any]] = []
    for start, items in sorted(by_bin.items()):
        item_rows = len(items)
        by_30_second_bin.append(
            {
                "offset_start": start,
                "offset_end": start + 29,
                "snapshots": item_rows,
                "data_complete_v2": sum(bool(row["data_complete_v2"]) for row in items),
                "data_complete_v2_coverage": _coverage(
                    sum(bool(row["data_complete_v2"]) for row in items), item_rows
                ),
                "one_sided_observed": sum(bool(row["one_sided_observed"]) for row in items),
                "paired_buy_ready": sum(bool(row["actions"]["paired_buy"]) for row in items),
                "paired_sell_ready": sum(bool(row["actions"]["paired_sell"]) for row in items),
                "full_round_trip_ready": sum(bool(row["actions"]["full_round_trip"]) for row in items),
            }
        )

    return {
        "snapshots": rows,
        "original_complete_snapshots": original_complete,
        "original_complete_coverage": _coverage(original_complete, rows),
        "data_complete_v2_snapshots": data_complete,
        "data_complete_v2_coverage": _coverage(data_complete, rows),
        "reclassified_valid_from_original_incomplete": reclassified_valid,
        "original_complete_regressed_under_v2": regressed,
        "one_sided_observed_snapshots": sum(
            bool(row["one_sided_observed"]) for row in classified_rows
        ),
        "maximum_consecutive_one_sided_seconds": maximum_one_sided_streak,
        "book_state_counts": state_counts,
        "data_failure_reason_counts": failure_reasons,
        "action_availability": {
            name: {"snapshots": count, "coverage": _coverage(count, rows)}
            for name, count in action_counts.items()
        },
        "per_market": per_market,
        "by_30_second_bin": by_30_second_bin,
    }


def reaudit_v031_quality(
    *,
    prereg_path: str | Path,
    implementation_path: str | Path,
    result_path: str | Path | None = None,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    prereg_file = Path(prereg_path).resolve()
    implementation_file = Path(implementation_path).resolve()
    output = Path(result_path).resolve() if result_path is not None else None
    prereg = load_and_verify_quality_prereg(prereg_file, project_root=root)
    load_and_verify_quality_implementation(implementation_file, project_root=root)
    inputs = {
        key: root / str(record["relative_path"])
        for key, record in prereg["inputs"].items()
    }
    database = inputs["database"]
    original_result_path = inputs["original_result"]
    database_hash_before = sha256_file(database)
    original_result_hash_before = sha256_file(original_result_path)
    prereg_hash = sha256_file(prereg_file)
    implementation_hash = sha256_file(implementation_file)
    if output is not None and output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == RESULT_SCHEMA
            and existing.get("database_sha256") == database_hash_before
            and existing.get("original_result_sha256") == original_result_hash_before
            and existing.get("preregistration_sha256") == prereg_hash
            and existing.get("implementation_sha256") == implementation_hash
        ):
            return existing
        raise V031QualityReauditError("Existe otro resultado V0.31R")

    original_result = json.loads(original_result_path.read_text(encoding="utf-8"))
    _require(original_result.get("schema"), "result_v031_path_execution_capture_1", "original_result.schema")
    _require(original_result.get("verdict"), "FAIL_TECHNICAL_QUALITY", "original_result.verdict")
    _require(original_result.get("database_sha256"), database_hash_before, "original_result.database_sha256")
    if original_result.get("safety_passed") is not True:
        raise V031QualityReauditError("El resultado V0.31 original no paso seguridad")

    connection = open_read_only(database)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        query_only = bool(int(connection.execute("PRAGMA query_only").fetchone()[0]))
        meta = {
            str(row[0]): json.loads(str(row[1]))
            for row in connection.execute("SELECT key,value FROM v031_meta")
        }
        markets = {
            str(row["condition_id"]): dict(row)
            for row in connection.execute(
                """
                SELECT condition_id,slug,resolution_contract_status,
                 resolution_twap_window_s,capture_status
                FROM v031_markets ORDER BY market_start_ms
                """
            )
        }
        classified: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT * FROM v031_snapshots ORDER BY condition_id,second_offset"
        ):
            values = dict(row)
            market = markets.get(str(values["condition_id"]))
            if market is None or market.get("resolution_twap_window_s") is None:
                raise V031QualityReauditError("Snapshot V0.31 sin contrato de resolucion")
            classified.append(
                classify_snapshot(
                    values,
                    resolution_twap_window_s=int(market["resolution_twap_window_s"]),
                )
            )
    finally:
        connection.close()

    summary = summarize_classifications(
        classified,
        slug_by_condition={key: str(value["slug"]) for key, value in markets.items()},
    )
    original_other_gates = {
        key: bool(value)
        for key, value in original_result["technical_gates"].items()
        if key != "complete_snapshot_coverage_passed"
    }
    original_safety_gates = {
        key: bool(value) for key, value in original_result["safety_gates"].items()
    }
    database_hash_after = sha256_file(database)
    original_result_hash_after = sha256_file(original_result_path)
    technical_gates_v2 = {
        "data_complete_v2_coverage_passed": summary["data_complete_v2_coverage"]
        >= float(prereg["technical_gates"]["minimum_data_complete_v2_coverage"]),
        "sqlite_quick_check_passed": quick_check
        == str(prereg["technical_gates"]["sqlite_quick_check_required"]),
        "database_hash_unchanged_passed": database_hash_after == database_hash_before,
        "original_result_hash_unchanged_passed": original_result_hash_after
        == original_result_hash_before,
        "all_other_original_technical_gates_passed": all(original_other_gates.values()),
        "all_original_safety_gates_passed": all(original_safety_gates.values()),
        "all_markets_have_verified_resolution_contract_passed": bool(markets)
        and all(
            market["resolution_contract_status"] == "VERIFIED"
            and market["capture_status"] == "COMPLETED"
            for market in markets.values()
        ),
    }
    safety_passed = all(original_safety_gates.values()) and all(
        (
            meta.get("orders_enabled") is False,
            meta.get("paper_orders_enabled") is False,
            meta.get("wallet_required") is False,
            meta.get("real_money") == "BLOQUEADO",
            meta.get("outcomes_read") == 0,
            meta.get("pnl_calculated") is False,
            meta.get("signals_generated") is False,
        )
    )
    technical_passed = all(technical_gates_v2.values())
    if not safety_passed:
        verdict = "FAIL_SAFETY"
    elif technical_passed:
        verdict = PASS_VERDICT
    else:
        verdict = "FAIL_TECHNICAL_QUALITY_REAUDITED_SEMANTICS_V2"

    result = {
        "schema": RESULT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "verdict": verdict,
        "meaning": "posthoc_technical_semantics_reaudit_no_economic_claim",
        "posthoc_diagnostic": True,
        "independent_validation_claim": False,
        "database": str(database),
        "database_sha256": database_hash_before,
        "database_sha256_after": database_hash_after,
        "database_read_only_verified": query_only,
        "original_result": str(original_result_path),
        "original_result_sha256": original_result_hash_before,
        "original_result_sha256_after": original_result_hash_after,
        "original_official_verdict_preserved": original_result["verdict"],
        "preregistration_sha256": prereg_hash,
        "implementation_sha256": implementation_hash,
        "sqlite_quick_check": quick_check,
        "markets": len(markets),
        "quality_semantics_v2": prereg["quality_semantics_v2"],
        "quality_summary": summary,
        "original_other_technical_gates": original_other_gates,
        "technical_gates_v2": technical_gates_v2,
        "technical_passed_v2": technical_passed,
        "original_safety_gates": original_safety_gates,
        "safety_passed": safety_passed,
        "economic_strategy_present": False,
        "economic_edge_evaluated": False,
        "outcomes_read": 0,
        "labels_read": 0,
        "pnl_calculated": False,
        "signals_generated": False,
        "orders_created": 0,
        "paper_orders": 0,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
        "future_execution_requirements": {
            "side_specific_liquidity_check_required": True,
            "missing_action_must_fail_closed": True,
            "synthetic_complement_forbidden": True,
            "paired_order_atomicity_assumed": False,
            "one_leg_inventory_contingency_required": True,
            "fresh_strategy_preregistration_required": True,
        },
        "promotion": {
            "automatic_followup_launch": False,
            "paper_or_money_candidate": False,
            "economic_strategy_candidate": False,
        },
    }
    if output is not None:
        _write_atomic(output, result)
    return result


__all__ = [
    "PASS_VERDICT",
    "RESULT_SCHEMA",
    "V031QualityReauditError",
    "build_quality_implementation_manifest",
    "classify_snapshot",
    "load_and_verify_quality_implementation",
    "load_and_verify_quality_prereg",
    "reaudit_v031_quality",
    "summarize_classifications",
]
