from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import joblib
import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .inventory_rotation import (
    development_eligible,
    executable_cost,
    parse_utc_ms,
    sha256_file,
    simulate_market,
    summarize_trades,
)
from .runtime_policy import backtest_runtime_guard, enforce_backtest_runtime_hours


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
DEFAULT_PREREG = DATA / "prereg_v017_hedge_completion_development.json"
DEFAULT_RESULT = DATA / "resultado_v017_hedge_completion_development.json"
DEFAULT_MODEL = DATA / "modelo_v017_hedge_completion.joblib"
PREREG_SCHEMA = "prereg_v017_hedge_completion_development_1"
RESULT_SCHEMA = "resultado_v017_hedge_completion_development_1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _log1p(value: Any) -> float | None:
    number = _safe_float(value)
    if number is None or number < 0:
        return None
    return math.log1p(number)


def _delta(
    by_offset: Mapping[int, Mapping[str, Any]],
    offset: int,
    field: str,
    lookback: int,
) -> float | None:
    current = _safe_float(by_offset[offset].get(field))
    previous_row = by_offset.get(offset - lookback)
    previous = _safe_float(previous_row.get(field)) if previous_row else None
    if current is None or previous is None:
        return None
    return current - previous


def _range(
    rows: Iterable[Mapping[str, Any]],
    *,
    field: str,
    start_offset: int,
    end_offset: int,
) -> float | None:
    values = [
        number
        for row in rows
        if start_offset <= int(row["second_offset"]) <= end_offset
        if (number := _safe_float(row.get(field))) is not None
    ]
    return max(values) - min(values) if values else None


def execution_config(base: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "order_size_shares": float(base["order_size_shares"]),
        "slippage_per_share": float(base["slippage_per_share"]),
        "fee_rate": float(base["fee_rate"]),
        "required_clear_quality_mask": int(base["required_clear_quality_mask"]),
    }


def strategy_config(base: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "entry_window_second_offsets_inclusive": list(
            base["entry_window_second_offsets_inclusive"]
        ),
        "hedge_deadline_second_offset_inclusive": int(
            base["hedge_deadline_second_offset_inclusive"]
        ),
    }


def candidate_config(base: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": str(base["candidate"]),
        "entry_cost_cap": float(base["entry_cost_cap"]),
        "pair_cost_cap": float(base["pair_cost_cap"]),
    }


def extract_entry_record(
    rows: Iterable[Mapping[str, Any]],
    *,
    condition_id: str,
    market_start_ms: int,
    label: str,
    base: Mapping[str, Any],
    feature_names: list[str],
) -> dict[str, Any] | None:
    ordered = sorted(rows, key=lambda row: int(row["second_offset"]))
    execution = execution_config(base)
    strategy = strategy_config(base)
    candidate = candidate_config(base)
    trade = simulate_market(
        ordered,
        condition_id=condition_id,
        market_start_ms=market_start_ms,
        label=label,
        candidate=candidate,
        execution=execution,
        strategy=strategy,
    )
    if trade is None:
        return None

    by_offset = {int(row["second_offset"]): row for row in ordered}
    offset = int(trade["entry_offset"])
    current = by_offset[offset]
    side = str(trade["entry_side"])
    opposite = "DOWN" if side == "UP" else "UP"
    side_key = side.lower()
    opposite_key = opposite.lower()
    opposite_cost = executable_cost(current, opposite, execution)
    opposite_total = (
        opposite_cost["total_cost_per_share"] if opposite_cost is not None else None
    )
    entry_total = float(trade["entry_total_cost_per_share"])
    pair_cap = float(base["pair_cost_cap"])

    chainlink_open = None
    for row in ordered:
        chainlink_open = _safe_float(row.get("chainlink_price"))
        if chainlink_open is not None:
            break
    chainlink_current = _safe_float(current.get("chainlink_price"))
    chainlink_return = None
    if chainlink_open and chainlink_current:
        chainlink_return = 10_000.0 * (chainlink_current / chainlink_open - 1.0)
    signed_return = (
        chainlink_return * (1.0 if side == "UP" else -1.0)
        if chainlink_return is not None
        else None
    )

    raw_features: dict[str, float | None] = {
        "entry_offset": float(offset),
        "side_up": 1.0 if side == "UP" else 0.0,
        "entry_total_cost_per_share": entry_total,
        "opposite_total_cost_per_share": opposite_total,
        "gap_to_pair_cap": (
            entry_total + opposite_total - pair_cap
            if opposite_total is not None
            else None
        ),
        "entry_spread": _safe_float(current.get(f"{side_key}_spread")),
        "opposite_spread": _safe_float(current.get(f"{opposite_key}_spread")),
        "log1p_entry_ask_depth_1c": _log1p(
            current.get(f"{side_key}_ask_depth_1c")
        ),
        "log1p_opposite_ask_depth_1c": _log1p(
            current.get(f"{opposite_key}_ask_depth_1c")
        ),
        "entry_ask_delta_5s": _delta(
            by_offset, offset, f"{side_key}_best_ask", 5
        ),
        "entry_ask_delta_15s": _delta(
            by_offset, offset, f"{side_key}_best_ask", 15
        ),
        "entry_ask_delta_30s": _delta(
            by_offset, offset, f"{side_key}_best_ask", 30
        ),
        "opposite_ask_delta_5s": _delta(
            by_offset, offset, f"{opposite_key}_best_ask", 5
        ),
        "opposite_ask_delta_15s": _delta(
            by_offset, offset, f"{opposite_key}_best_ask", 15
        ),
        "opposite_ask_delta_30s": _delta(
            by_offset, offset, f"{opposite_key}_best_ask", 30
        ),
        "entry_ask_range_30s": _range(
            ordered,
            field=f"{side_key}_best_ask",
            start_offset=max(0, offset - 30),
            end_offset=offset,
        ),
        "opposite_ask_range_30s": _range(
            ordered,
            field=f"{opposite_key}_best_ask",
            start_offset=max(0, offset - 30),
            end_offset=offset,
        ),
        "signed_chainlink_return_bps_from_open": signed_return,
        "absolute_chainlink_return_bps_from_open": (
            abs(chainlink_return) if chainlink_return is not None else None
        ),
        "market_mid_sum": _safe_float(current.get("market_mid_sum")),
        "log1p_polymarket_trade_volume": _log1p(
            current.get("polymarket_trade_volume")
        ),
        "polymarket_trade_count": _safe_float(
            current.get("polymarket_trade_count")
        ),
        "book_messages": _safe_float(current.get("book_messages")),
        "price_change_messages": _safe_float(
            current.get("price_change_messages")
        ),
    }
    if set(raw_features) != set(feature_names):
        missing = sorted(set(feature_names) - set(raw_features))
        extra = sorted(set(raw_features) - set(feature_names))
        raise RuntimeError(f"Features v0.17 incompatibles; missing={missing}; extra={extra}")
    return {
        "condition_id": condition_id,
        "market_start_ms": int(market_start_ms),
        "features": [raw_features[name] for name in feature_names],
        "target_paired": int(bool(trade["paired"])),
        "trade": trade,
    }


def _open_readonly(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=5
    )
    connection.row_factory = sqlite3.Row
    return connection


def load_records(
    connection: sqlite3.Connection,
    *,
    start_ms: int,
    end_ms: int,
    base: Mapping[str, Any],
    feature_names: list[str],
) -> tuple[list[dict[str, Any]], int]:
    markets = connection.execute(
        """
        SELECT condition_id, market_start_ms, label
        FROM markets
        WHERE market_start_ms >= ? AND market_start_ms < ?
        ORDER BY market_start_ms, condition_id
        """,
        (start_ms, end_ms),
    ).fetchall()
    fields = """
        second_offset, quality_flags, chainlink_price,
        up_best_bid, up_best_ask, up_spread, up_ask_depth_1c,
        down_best_bid, down_best_ask, down_spread, down_ask_depth_1c,
        market_mid_sum, polymarket_trade_count, polymarket_trade_volume,
        book_messages, price_change_messages
    """
    records: list[dict[str, Any]] = []
    for market in markets:
        rows = connection.execute(
            f"""
            SELECT {fields}
            FROM second_features
            WHERE condition_id = ? AND second_offset BETWEEN 0 AND ?
            ORDER BY second_offset
            """,
            (
                market["condition_id"],
                int(base["hedge_deadline_second_offset_inclusive"]),
            ),
        ).fetchall()
        record = extract_entry_record(
            [dict(row) for row in rows],
            condition_id=str(market["condition_id"]),
            market_start_ms=int(market["market_start_ms"]),
            label=str(market["label"]),
            base=base,
            feature_names=feature_names,
        )
        if record is not None:
            records.append(record)
    return records, len(markets)


def matrix(records: list[dict[str, Any]]) -> np.ndarray:
    return np.asarray(
        [
            [np.nan if value is None else float(value) for value in record["features"]]
            for record in records
        ],
        dtype=float,
    )


def build_model(spec: Mapping[str, Any]) -> Pipeline:
    return Pipeline(
        [
            (
                "imputer",
                SimpleImputer(
                    strategy="median", add_indicator=True, keep_empty_features=True
                ),
            ),
            ("scaler", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    C=float(spec["C"]),
                    max_iter=int(spec["max_iter"]),
                    random_state=17,
                ),
            ),
        ]
    )


def verify_prereg(path: Path) -> dict[str, Any]:
    prereg = json.loads(path.read_text(encoding="utf-8"))
    if prereg.get("schema") != PREREG_SCHEMA:
        raise RuntimeError("Preregistro v0.17 incompatible")
    if prereg.get("status") != "FROZEN_DEVELOPMENT_ONLY":
        raise RuntimeError("Desarrollo v0.17 no esta congelado")
    if prereg.get("real_money") != "BLOQUEADO":
        raise RuntimeError("Dinero real debe permanecer bloqueado")
    parent_meta = prereg["parent_result"]
    parent = (ROOT / parent_meta["relative_path"]).resolve()
    if sha256_file(parent) != parent_meta["sha256"]:
        raise RuntimeError("Resultado padre v0.16.1 no coincide")
    parent_payload = json.loads(parent.read_text(encoding="utf-8"))
    if (
        parent_payload.get("verdict") != parent_meta["required_verdict"]
        or parent_payload.get("validation_result") is not None
    ):
        raise RuntimeError("Resultado padre v0.16.1 incompatible")
    partition = prereg["temporal_partition"]
    total_start = parse_utc_ms(partition["total_window_start_utc"])
    total_end = parse_utc_ms(partition["total_window_end_utc"])
    hours = float(partition["total_duration_hours"])
    enforce_backtest_runtime_hours(hours)
    if total_end - total_start != int(hours * 3_600_000):
        raise RuntimeError("Ventana total v0.17 no coincide con 24 horas")
    if parse_utc_ms(partition["development_oof_end_utc"]) != parse_utc_ms(
        partition["sealed_validation_start_utc"]
    ):
        raise RuntimeError("Particion sellada v0.17 tiene hueco o solapamiento")
    if partition.get("validation_must_not_be_read_by_development") is not True:
        raise RuntimeError("Holdout v0.17 no esta protegido")
    protected = prereg["protected_forward"]
    if protected.get("must_not_read") is not True or protected.get(
        "must_not_modify"
    ) is not True:
        raise RuntimeError("Forward activo no esta protegido")
    return prereg


def _model_metrics(target: np.ndarray, probabilities: np.ndarray) -> dict[str, Any]:
    return {
        "brier": float(brier_score_loss(target, probabilities)),
        "roc_auc": (
            float(roc_auc_score(target, probabilities))
            if len(np.unique(target)) == 2
            else None
        ),
        "target_paired_rate": float(np.mean(target)),
    }


def run_development(prereg_path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    prereg = verify_prereg(prereg_path)
    source = (ROOT / prereg["historical_source"]["relative_path"]).resolve()
    if sha256_file(source) != prereg["historical_source"]["sha256"]:
        raise RuntimeError("Base historica v0.17 no coincide")
    partition = prereg["temporal_partition"]
    train_start = parse_utc_ms(partition["model_train_start_utc"])
    train_end = parse_utc_ms(partition["model_train_end_utc"])
    oof_start = parse_utc_ms(partition["development_oof_start_utc"])
    oof_end = parse_utc_ms(partition["development_oof_end_utc"])
    if train_end != oof_start:
        raise RuntimeError("Train y OOF v0.17 no son contiguos")
    base = prereg["base_strategy"]
    feature_names = list(prereg["features_at_entry_without_lookahead"])

    model_results: dict[str, Any] = {}
    candidate_rows: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    final_artifact: dict[str, Any] | None = None
    with backtest_runtime_guard(float(partition["total_duration_hours"])):
        connection = _open_readonly(source)
        try:
            train_records, train_markets = load_records(
                connection,
                start_ms=train_start,
                end_ms=train_end,
                base=base,
                feature_names=feature_names,
            )
            oof_records, oof_markets = load_records(
                connection,
                start_ms=oof_start,
                end_ms=oof_end,
                base=base,
                feature_names=feature_names,
            )
        finally:
            connection.close()

        if not train_records or not oof_records:
            raise RuntimeError("Poblacion v0.17 insuficiente")
        x_train = matrix(train_records)
        y_train = np.asarray([record["target_paired"] for record in train_records])
        x_oof = matrix(oof_records)
        y_oof = np.asarray([record["target_paired"] for record in oof_records])
        if len(np.unique(y_train)) != 2:
            raise RuntimeError("Train v0.17 no contiene ambas clases")

        for spec in prereg["models"]:
            model = build_model(spec)
            model.fit(x_train, y_train)
            probabilities = model.predict_proba(x_oof)[:, 1]
            threshold_results = []
            for threshold in prereg["probability_thresholds"]:
                chosen = [
                    record["trade"]
                    for record, probability in zip(oof_records, probabilities)
                    if float(probability) >= float(threshold)
                ]
                summary = summarize_trades(chosen)
                summary["threshold"] = float(threshold)
                summary["eligible"] = development_eligible(
                    summary, prereg["development_gate"]
                )
                threshold_results.append(summary)
                if summary["eligible"]:
                    candidate_rows.append(
                        {
                            "model": str(spec["name"]),
                            "C": float(spec["C"]),
                            "threshold": float(threshold),
                            "summary": summary,
                            "spec": dict(spec),
                        }
                    )
            model_results[str(spec["name"])] = {
                "prediction_metrics": _model_metrics(y_oof, probabilities),
                "threshold_results": threshold_results,
            }

        if candidate_rows:
            selected = sorted(
                candidate_rows,
                key=lambda item: (
                    -float(item["summary"]["net_pnl"]),
                    -float(item["summary"]["paired_rate"]),
                    -float(item["summary"]["roi_on_cost"]),
                    float(item["C"]),
                    -float(item["threshold"]),
                ),
            )[0]
            all_records = train_records + oof_records
            final_model = build_model(selected["spec"])
            final_model.fit(
                matrix(all_records),
                np.asarray([record["target_paired"] for record in all_records]),
            )
            final_artifact = {
                "schema": "modelo_v017_hedge_completion_1",
                "created_at": utc_now(),
                "pipeline": final_model,
                "feature_names": feature_names,
                "selected_model": selected["model"],
                "selected_C": selected["C"],
                "selected_threshold": selected["threshold"],
                "base_strategy": dict(base),
                "preregistration_sha256": sha256_file(prereg_path),
                "training_records": len(all_records),
                "training_end_exclusive_utc": partition["development_oof_end_utc"],
                "sealed_validation_start_utc": partition[
                    "sealed_validation_start_utc"
                ],
                "orders_enabled": False,
                "wallet_required": False,
                "real_money": "BLOQUEADO",
            }

    result = {
        "schema": RESULT_SCHEMA,
        "created_at": utc_now(),
        "verdict": "DEVELOPMENT_CANDIDATE_SELECTED" if selected else "FAIL_DEVELOPMENT",
        "preregistration": str(prereg_path.resolve()),
        "preregistration_sha256": sha256_file(prereg_path),
        "implementation_sha256": sha256_file(Path(__file__).resolve()),
        "source_sha256": prereg["historical_source"]["sha256"],
        "population": {
            "train_markets": train_markets,
            "train_entries": len(train_records),
            "oof_markets": oof_markets,
            "oof_entries": len(oof_records),
        },
        "features": feature_names,
        "model_results": model_results,
        "selected": selected,
        "sealed_validation_read": False,
        "forward_active_read": False,
        "forward_active_modified": False,
        "orders_enabled": False,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }
    return result, final_artifact


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def write_outputs(
    result: Mapping[str, Any],
    artifact: Mapping[str, Any] | None,
    *,
    result_path: Path,
    model_path: Path,
) -> None:
    if result_path.exists() or model_path.exists():
        raise FileExistsError("Las salidas v0.17 son inmutables y ya existen")
    if artifact is not None:
        model_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=model_path.name + ".", suffix=".partial", dir=model_path.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            joblib.dump(dict(artifact), temporary)
            if model_path.exists():
                raise FileExistsError("El modelo v0.17 ya existe")
            os.replace(temporary, model_path)
        finally:
            temporary.unlink(missing_ok=True)
        result = dict(result)
        result["model_file"] = str(model_path.resolve())
        result["model_sha256"] = sha256_file(model_path)
    else:
        result = dict(result)
        result["model_file"] = None
        result["model_sha256"] = None
    try:
        _write_new_json(result_path, result)
    except Exception:
        if artifact is not None:
            model_path.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="V0.17: desarrollo de predictor de cierre de hedge (paper only)"
    )
    parser.add_argument("--prereg", type=Path, default=DEFAULT_PREREG)
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULT)
    parser.add_argument("--model-output", type=Path, default=DEFAULT_MODEL)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result, artifact = run_development(args.prereg.resolve())
    write_outputs(
        result,
        artifact,
        result_path=args.output.resolve(),
        model_path=args.model_output.resolve(),
    )
    print("=" * 88)
    print("V0.17 HEDGE COMPLETION - DESARROLLO - PAPER ONLY")
    print("=" * 88)
    print(f"Veredicto: {result['verdict']}")
    print(f"Seleccion: {result['selected']}")
    print("VALIDACION 8H LEIDA: NO")
    print("DINERO REAL: BLOQUEADO")
    print(f"Resultado: {args.output.resolve()}")
    return 0


__all__ = [
    "build_model",
    "extract_entry_record",
    "load_records",
    "matrix",
    "run_development",
]

