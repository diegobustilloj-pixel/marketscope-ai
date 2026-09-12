from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Sequence

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


PHASE4_SCHEMA_VERSION = "1"
PHASE4_RANDOM_SEED = 20_260_728
DEFAULT_FEE_RATE = 0.07
DEFAULT_SLIPPAGE_PER_SHARE = 0.005
DEFAULT_EDGE_GRID = (0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10)
MIN_VALIDATION_TRADES = 20
MIN_TEST_TRADES = 15
CALIBRATION_FRACTION = 0.20
PROBABILITY_EPSILON = 1e-6
MAX_VALIDATION_ECE = 0.12
MAX_BRIER_DEGRADATION_VS_MARKET = 0.01


MARKOV_FEATURES = (
    "chainlink_state",
    "chainlink_state_run_length",
    "markov_p_up_60s",
    "chainlink_up_fraction_15s",
    "chainlink_up_fraction_30s",
    "chainlink_up_fraction_60s",
    "chainlink_nonzero_fraction_60s",
)

MOMENTUM_FEATURES = (
    "chainlink_return_5s_bps",
    "chainlink_return_15s_bps",
    "chainlink_return_30s_bps",
    "chainlink_return_60s_bps",
    "chainlink_return_120s_bps",
    "chainlink_vol_15s_bps",
    "chainlink_vol_30s_bps",
    "chainlink_vol_60s_bps",
    "chainlink_vol_120s_bps",
    "chainlink_up_fraction_15s",
    "chainlink_up_fraction_30s",
    "chainlink_up_fraction_60s",
    "chainlink_nonzero_fraction_60s",
    "volatility_regime_ratio",
)

MICROSTRUCTURE_FEATURES = (
    "implied_up_mid_probability",
    "up_spread",
    "down_spread",
    "market_mid_sum",
    "market_ask_overround",
    "up_order_imbalance_1c",
    "up_order_imbalance_5c",
    "down_order_imbalance_1c",
    "down_order_imbalance_5c",
    "up_last_trade_price",
    "down_last_trade_price",
    "polymarket_trade_count_15s",
    "polymarket_trade_volume_15s",
    "polymarket_trade_count_60s",
    "polymarket_trade_volume_60s",
    "price_change_messages_15s",
    "price_change_messages_60s",
    "book_messages_15s",
    "book_messages_60s",
    "best_bid_ask_messages_15s",
    "best_bid_ask_messages_60s",
    "chainlink_updates_15s",
    "chainlink_updates_60s",
)

COMBINED_FEATURES = tuple(
    dict.fromkeys(MARKOV_FEATURES + MOMENTUM_FEATURES + MICROSTRUCTURE_FEATURES)
)

STRIKE_FEATURES = COMBINED_FEATURES + ("distance_to_strike_bps",)

MODEL_FEATURES = {
    "markov_logistic": MARKOV_FEATURES,
    "momentum_logistic": MOMENTUM_FEATURES,
    "combined_logistic": COMBINED_FEATURES,
    "combined_hist_gradient_boosting": COMBINED_FEATURES,
    "strike_logistic": STRIKE_FEATURES,
    "strike_hist_gradient_boosting": STRIKE_FEATURES,
}

MODEL_SCOPES = {
    "markov_logistic": "all_markets",
    "momentum_logistic": "all_markets",
    "combined_logistic": "all_markets",
    "combined_hist_gradient_boosting": "all_markets",
    "strike_logistic": "official_strike_only",
    "strike_hist_gradient_boosting": "official_strike_only",
}

ROW_COLUMNS = tuple(
    dict.fromkeys(
        (
            "source_dataset",
            "condition_id",
            "slug",
            "market_start_ms",
            "decision_timestamp_ms",
            "horizon_seconds",
            "split",
            "y_up",
            "up_best_ask",
            "down_best_ask",
            "implied_up_mid_probability",
            "has_official_strike",
            "distance_to_strike_bps",
        )
        + STRIKE_FEATURES
    )
)


PHASE4_DDL = """
CREATE TABLE IF NOT EXISTS phase4_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS probability_metrics (
    split TEXT NOT NULL,
    horizon_seconds INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    PRIMARY KEY(split, horizon_seconds, model_name)
);

CREATE TABLE IF NOT EXISTS threshold_metrics (
    split TEXT NOT NULL,
    horizon_seconds INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    minimum_edge REAL NOT NULL,
    metrics_json TEXT NOT NULL,
    PRIMARY KEY(split, horizon_seconds, model_name, minimum_edge)
);

CREATE TABLE IF NOT EXISTS predictions (
    split TEXT NOT NULL,
    source_dataset TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    horizon_seconds INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    decision_timestamp_ms INTEGER NOT NULL,
    y_up INTEGER NOT NULL,
    probability_up REAL NOT NULL,
    up_best_ask REAL NOT NULL,
    down_best_ask REAL NOT NULL,
    PRIMARY KEY(
        split,
        source_dataset,
        condition_id,
        horizon_seconds,
        model_name
    )
);

CREATE INDEX IF NOT EXISTS idx_phase4_predictions
    ON predictions(split, horizon_seconds, model_name);
"""


@dataclass(slots=True)
class FittedModel:
    name: str
    horizon_seconds: int
    features: tuple[str, ...]
    estimator: Any
    calibrator: Any | None

    def probabilities(self, rows: Sequence[dict[str, Any]]) -> np.ndarray:
        matrix = _feature_matrix(rows, self.features)
        raw = np.asarray(
            self.estimator.predict_proba(matrix)[:, 1],
            dtype=float,
        )
        return _apply_calibrator(raw, self.calibrator)


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=60,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _number(value: Any) -> float:
    if value is None:
        return math.nan
    try:
        result = float(value)
    except (TypeError, ValueError):
        return math.nan
    return result if math.isfinite(result) else math.nan


def _load_rows(
    connection: sqlite3.Connection,
    *,
    split: str,
    horizon_seconds: int,
) -> list[dict[str, Any]]:
    columns = ",".join(ROW_COLUMNS)
    rows = connection.execute(
        f"""
        SELECT {columns}
        FROM gold_features
        WHERE split=? AND horizon_seconds=?
        ORDER BY decision_timestamp_ms, source_dataset, condition_id
        """,
        (split, horizon_seconds),
    ).fetchall()
    return [
        {column: row[column] for column in ROW_COLUMNS}
        for row in rows
    ]


def _feature_matrix(
    rows: Sequence[dict[str, Any]],
    features: Sequence[str],
) -> np.ndarray:
    return np.asarray(
        [
            [_number(row.get(feature)) for feature in features]
            for row in rows
        ],
        dtype=float,
    )


def _targets(rows: Sequence[dict[str, Any]]) -> np.ndarray:
    return np.asarray([int(row["y_up"]) for row in rows], dtype=int)


def _market_probabilities(
    rows: Sequence[dict[str, Any]],
) -> np.ndarray:
    return np.clip(
        np.asarray(
            [
                float(row["implied_up_mid_probability"])
                for row in rows
            ],
            dtype=float,
        ),
        PROBABILITY_EPSILON,
        1.0 - PROBABILITY_EPSILON,
    )


def _model_rows(
    rows: Sequence[dict[str, Any]],
    model_name: str,
) -> list[dict[str, Any]]:
    scope = MODEL_SCOPES[model_name]
    if scope == "all_markets":
        return list(rows)
    if scope == "official_strike_only":
        return [
            row
            for row in rows
            if int(row["has_official_strike"]) == 1
            and math.isfinite(_number(row["distance_to_strike_bps"]))
        ]
    raise ValueError(f"Scope desconocido: {scope}")


def _build_estimator(model_name: str) -> Any:
    if model_name.endswith("_logistic"):
        return Pipeline(
            steps=[
                (
                    "imputer",
                    SimpleImputer(
                        strategy="median",
                        add_indicator=True,
                        keep_empty_features=True,
                    ),
                ),
                ("scaler", StandardScaler()),
                (
                    "model",
                    LogisticRegression(
                        C=0.5,
                        max_iter=2_000,
                        random_state=PHASE4_RANDOM_SEED,
                    ),
                ),
            ]
        )
    if model_name.endswith("_hist_gradient_boosting"):
        return Pipeline(
            steps=[
                (
                    "imputer",
                    SimpleImputer(
                        strategy="median",
                        add_indicator=True,
                        keep_empty_features=True,
                    ),
                ),
                (
                    "model",
                    HistGradientBoostingClassifier(
                        learning_rate=0.05,
                        max_iter=120,
                        max_leaf_nodes=7,
                        min_samples_leaf=25,
                        l2_regularization=2.0,
                        random_state=PHASE4_RANDOM_SEED,
                    ),
                ),
            ]
        )
    raise ValueError(f"Modelo desconocido: {model_name}")


def _fit_calibrator(
    probabilities: np.ndarray,
    targets: np.ndarray,
) -> Any | None:
    if len(np.unique(targets)) < 2:
        return None
    logits = np.log(
        np.clip(
            probabilities,
            PROBABILITY_EPSILON,
            1.0 - PROBABILITY_EPSILON,
        )
        / np.clip(
            1.0 - probabilities,
            PROBABILITY_EPSILON,
            1.0,
        )
    ).reshape(-1, 1)
    calibrator = LogisticRegression(
        C=1.0,
        max_iter=2_000,
        random_state=PHASE4_RANDOM_SEED,
    )
    calibrator.fit(logits, targets)
    return calibrator


def _apply_calibrator(
    probabilities: np.ndarray,
    calibrator: Any | None,
) -> np.ndarray:
    clipped = np.clip(
        probabilities,
        PROBABILITY_EPSILON,
        1.0 - PROBABILITY_EPSILON,
    )
    if calibrator is None:
        return clipped
    logits = np.log(clipped / (1.0 - clipped)).reshape(-1, 1)
    return np.clip(
        np.asarray(calibrator.predict_proba(logits)[:, 1], dtype=float),
        PROBABILITY_EPSILON,
        1.0 - PROBABILITY_EPSILON,
    )


def _rank_average(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    start = 0
    while start < len(order):
        end = start + 1
        while (
            end < len(order)
            and values[order[end]] == values[order[start]]
        ):
            end += 1
        average_rank = (start + 1 + end) / 2.0
        ranks[order[start:end]] = average_rank
        start = end
    return ranks


def _roc_auc(targets: np.ndarray, probabilities: np.ndarray) -> float | None:
    positives = int(np.sum(targets == 1))
    negatives = int(np.sum(targets == 0))
    if positives == 0 or negatives == 0:
        return None
    ranks = _rank_average(probabilities)
    positive_rank_sum = float(np.sum(ranks[targets == 1]))
    auc = (
        positive_rank_sum - positives * (positives + 1) / 2.0
    ) / (positives * negatives)
    return float(auc)


def _expected_calibration_error(
    targets: np.ndarray,
    probabilities: np.ndarray,
    *,
    bins: int = 10,
) -> float:
    total = len(targets)
    error = 0.0
    bin_indexes = np.minimum(
        (probabilities * bins).astype(int),
        bins - 1,
    )
    for bin_index in range(bins):
        mask = bin_indexes == bin_index
        count = int(np.sum(mask))
        if count == 0:
            continue
        observed = float(np.mean(targets[mask]))
        predicted = float(np.mean(probabilities[mask]))
        error += count / total * abs(observed - predicted)
    return error


def _probability_metrics(
    targets: np.ndarray,
    probabilities: np.ndarray,
) -> dict[str, Any]:
    clipped = np.clip(
        probabilities,
        PROBABILITY_EPSILON,
        1.0 - PROBABILITY_EPSILON,
    )
    brier = float(np.mean((clipped - targets) ** 2))
    log_loss = float(
        -np.mean(
            targets * np.log(clipped)
            + (1 - targets) * np.log(1.0 - clipped)
        )
    )
    predicted_class = clipped >= 0.5
    return {
        "rows": len(targets),
        "up_rate": round(float(np.mean(targets)), 8),
        "mean_probability_up": round(float(np.mean(clipped)), 8),
        "brier": round(brier, 8),
        "log_loss": round(log_loss, 8),
        "roc_auc": (
            round(auc, 8)
            if (auc := _roc_auc(targets, clipped)) is not None
            else None
        ),
        "ece_10_bins": round(
            _expected_calibration_error(targets, clipped),
            8,
        ),
        "accuracy_at_0_5": round(
            float(np.mean(predicted_class == targets)),
            8,
        ),
    }


def _taker_cost_per_share(
    ask: float,
    *,
    fee_rate: float,
    slippage_per_share: float,
) -> tuple[float, float, float]:
    fill_price = min(0.999, max(0.001, ask + slippage_per_share))
    fee = fee_rate * fill_price * (1.0 - fill_price)
    return fill_price + fee, fill_price, fee


def _trade_metrics(
    rows: Sequence[dict[str, Any]],
    targets: np.ndarray,
    probabilities: np.ndarray,
    *,
    minimum_edge: float,
    fee_rate: float,
    slippage_per_share: float,
    confidence_z: float,
) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    for row, target, probability_up in zip(
        rows,
        targets,
        probabilities,
    ):
        up_cost, up_fill, up_fee = _taker_cost_per_share(
            float(row["up_best_ask"]),
            fee_rate=fee_rate,
            slippage_per_share=slippage_per_share,
        )
        down_cost, down_fill, down_fee = _taker_cost_per_share(
            float(row["down_best_ask"]),
            fee_rate=fee_rate,
            slippage_per_share=slippage_per_share,
        )
        up_edge = float(probability_up) - up_cost
        down_edge = 1.0 - float(probability_up) - down_cost
        if max(up_edge, down_edge) < minimum_edge:
            continue
        if up_edge >= down_edge:
            side = "Up"
            expected_edge = up_edge
            cost = up_cost
            fill = up_fill
            fee = up_fee
            won = int(target) == 1
        else:
            side = "Down"
            expected_edge = down_edge
            cost = down_cost
            fill = down_fill
            fee = down_fee
            won = int(target) == 0
        pnl = (1.0 - cost) if won else -cost
        trades.append(
            {
                "decision_timestamp_ms": int(
                    row["decision_timestamp_ms"]
                ),
                "side": side,
                "won": won,
                "cost": cost,
                "fill": fill,
                "fee": fee,
                "expected_edge": expected_edge,
                "pnl": pnl,
            }
        )

    trades.sort(key=lambda trade: trade["decision_timestamp_ms"])
    count = len(trades)
    if count == 0:
        return {
            "minimum_edge": minimum_edge,
            "trades": 0,
            "coverage": 0.0,
            "up_trades": 0,
            "down_trades": 0,
            "wins": 0,
            "win_rate": None,
            "total_cost": 0.0,
            "fees": 0.0,
            "net_pnl_per_share_sequence": 0.0,
            "roi_on_cost": None,
            "mean_pnl": None,
            "mean_expected_edge": None,
            "pnl_standard_deviation": None,
            "mean_to_std": None,
            "confidence_z": round(confidence_z, 8),
            "lower_confidence_bound_mean_pnl": None,
            "profit_factor": None,
            "max_drawdown": 0.0,
        }

    pnls = np.asarray([trade["pnl"] for trade in trades], dtype=float)
    costs = np.asarray([trade["cost"] for trade in trades], dtype=float)
    fees = np.asarray([trade["fee"] for trade in trades], dtype=float)
    expected_edges = np.asarray(
        [trade["expected_edge"] for trade in trades],
        dtype=float,
    )
    mean_pnl = float(np.mean(pnls))
    standard_deviation = (
        float(np.std(pnls, ddof=1)) if count > 1 else 0.0
    )
    standard_error = (
        standard_deviation / math.sqrt(count) if count > 1 else 0.0
    )
    lower_bound = mean_pnl - confidence_z * standard_error
    cumulative = np.cumsum(pnls)
    running_peak = np.maximum.accumulate(
        np.concatenate(([0.0], cumulative))
    )[1:]
    drawdowns = running_peak - cumulative
    gross_profit = float(np.sum(pnls[pnls > 0]))
    gross_loss = float(-np.sum(pnls[pnls < 0]))
    total_cost = float(np.sum(costs))
    net_pnl = float(np.sum(pnls))
    return {
        "minimum_edge": round(minimum_edge, 8),
        "trades": count,
        "coverage": round(count / len(rows), 8),
        "up_trades": sum(trade["side"] == "Up" for trade in trades),
        "down_trades": sum(
            trade["side"] == "Down" for trade in trades
        ),
        "wins": sum(bool(trade["won"]) for trade in trades),
        "win_rate": round(
            sum(bool(trade["won"]) for trade in trades) / count,
            8,
        ),
        "total_cost": round(total_cost, 8),
        "fees": round(float(np.sum(fees)), 8),
        "net_pnl_per_share_sequence": round(net_pnl, 8),
        "roi_on_cost": (
            round(net_pnl / total_cost, 8) if total_cost > 0 else None
        ),
        "mean_pnl": round(mean_pnl, 8),
        "mean_expected_edge": round(float(np.mean(expected_edges)), 8),
        "pnl_standard_deviation": round(standard_deviation, 8),
        "mean_to_std": (
            round(mean_pnl / standard_deviation, 8)
            if standard_deviation > 0
            else None
        ),
        "confidence_z": round(confidence_z, 8),
        "lower_confidence_bound_mean_pnl": round(lower_bound, 8),
        "profit_factor": (
            round(gross_profit / gross_loss, 8)
            if gross_loss > 0
            else None
        ),
        "max_drawdown": round(
            float(np.max(drawdowns)) if len(drawdowns) else 0.0,
            8,
        ),
    }


def _candidate_passes_validation(
    probability: dict[str, Any],
    trading: dict[str, Any],
    *,
    market_brier: float,
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if int(trading["trades"]) < MIN_VALIDATION_TRADES:
        failures.append("menos_de_20_trades")
    lower_bound = trading["lower_confidence_bound_mean_pnl"]
    if lower_bound is None or float(lower_bound) <= 0:
        failures.append("lcb_pnl_no_positivo")
    if float(probability["ece_10_bins"]) > MAX_VALIDATION_ECE:
        failures.append("calibracion_ece_insuficiente")
    if (
        float(probability["brier"])
        > market_brier + MAX_BRIER_DEGRADATION_VS_MARKET
    ):
        failures.append("brier_peor_que_mercado")
    return not failures, failures


def _candidate_passes_test(
    probability: dict[str, Any],
    trading: dict[str, Any],
    *,
    market_brier: float,
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if int(trading["trades"]) < MIN_TEST_TRADES:
        failures.append("menos_de_15_trades")
    lower_bound = trading["lower_confidence_bound_mean_pnl"]
    if lower_bound is None or float(lower_bound) <= 0:
        failures.append("lcb_pnl_no_positivo")
    if (
        float(probability["brier"])
        > market_brier + MAX_BRIER_DEGRADATION_VS_MARKET
    ):
        failures.append("brier_peor_que_mercado")
    return not failures, failures


def _contract_hash(
    horizons: Iterable[int],
    edge_grid: Iterable[float],
    *,
    fee_rate: float,
    slippage_per_share: float,
) -> str:
    contract = {
        "schema_version": PHASE4_SCHEMA_VERSION,
        "horizons": list(horizons),
        "models": {
            key: list(value) for key, value in MODEL_FEATURES.items()
        },
        "model_scopes": MODEL_SCOPES,
        "calibration": "chronological_last_20_percent_of_train_platt",
        "selection_split": "validation",
        "test_policy": "locked_until_validation_candidate",
        "edge_grid": list(edge_grid),
        "fee_formula": "shares * fee_rate * p * (1-p)",
        "fee_rate": fee_rate,
        "slippage_per_share": slippage_per_share,
        "kelly": False,
        "orders": False,
    }
    return hashlib.sha256(_json(contract).encode("utf-8")).hexdigest()


def _write_output(
    *,
    path: Path,
    meta: dict[str, Any],
    probability_records: Sequence[dict[str, Any]],
    threshold_records: Sequence[dict[str, Any]],
    prediction_records: Sequence[dict[str, Any]],
) -> str:
    connection = sqlite3.connect(path, timeout=60)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(PHASE4_DDL)
        connection.executemany(
            "INSERT INTO phase4_meta(key,value) VALUES(?,?)",
            [(key, _json(value)) for key, value in sorted(meta.items())],
        )
        connection.executemany(
            "INSERT INTO probability_metrics VALUES(?,?,?,?)",
            [
                (
                    record["split"],
                    record["horizon_seconds"],
                    record["model_name"],
                    _json(record["metrics"]),
                )
                for record in probability_records
            ],
        )
        connection.executemany(
            "INSERT INTO threshold_metrics VALUES(?,?,?,?,?)",
            [
                (
                    record["split"],
                    record["horizon_seconds"],
                    record["model_name"],
                    record["minimum_edge"],
                    _json(record["metrics"]),
                )
                for record in threshold_records
            ],
        )
        connection.executemany(
            """
            INSERT INTO predictions(
                split,
                source_dataset,
                condition_id,
                horizon_seconds,
                model_name,
                decision_timestamp_ms,
                y_up,
                probability_up,
                up_best_ask,
                down_best_ask
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    record["split"],
                    record["source_dataset"],
                    record["condition_id"],
                    record["horizon_seconds"],
                    record["model_name"],
                    record["decision_timestamp_ms"],
                    record["y_up"],
                    record["probability_up"],
                    record["up_best_ask"],
                    record["down_best_ask"],
                )
                for record in prediction_records
            ],
        )
        connection.commit()
        quick_check = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
        return quick_check
    finally:
        connection.close()


def build_phase4_models(
    *,
    gold_db: str | Path,
    output_db: str | Path,
    model_file: str | Path,
    fee_rate: float = DEFAULT_FEE_RATE,
    slippage_per_share: float = DEFAULT_SLIPPAGE_PER_SHARE,
    edge_grid: Iterable[float] = DEFAULT_EDGE_GRID,
) -> dict[str, Any]:
    source = Path(gold_db).expanduser().resolve()
    output = Path(output_db).expanduser().resolve()
    partial_output = output.with_name(f"{output.name}.partial")
    model_path = Path(model_file).expanduser().resolve()
    partial_model = model_path.with_name(f"{model_path.name}.partial")
    if not source.is_file():
        raise ValueError(f"No se encontró la base Gold v2: {source}")
    for candidate in (output, partial_output, model_path, partial_model):
        if candidate.exists():
            raise ValueError(
                f"La salida ya existe y no será sobrescrita: {candidate}"
            )
    if source in {output, partial_output}:
        raise ValueError("La salida Fase 4 no puede ser la base Gold")
    if fee_rate < 0 or fee_rate > 1:
        raise ValueError("fee_rate debe estar entre 0 y 1")
    if slippage_per_share < 0 or slippage_per_share >= 0.10:
        raise ValueError(
            "slippage_per_share debe estar entre 0 y 0.10"
        )
    edge_values = tuple(sorted(set(float(value) for value in edge_grid)))
    if not edge_values or any(value <= 0 or value >= 1 for value in edge_values):
        raise ValueError("Los umbrales de edge deben estar entre 0 y 1")

    started = time.monotonic()
    source_size_before = source.stat().st_size
    connection = _open_read_only(source)
    fitted: dict[tuple[int, str], FittedModel] = {}
    validation_summary: list[dict[str, Any]] = []
    probability_records: list[dict[str, Any]] = []
    threshold_records: list[dict[str, Any]] = []
    prediction_records: list[dict[str, Any]] = []
    selection_candidates: list[dict[str, Any]] = []
    test_summary: dict[str, Any] | None = None
    test_accessed = False
    try:
        source_quick_check = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        )
        gold_meta = {
            str(row["key"]): json.loads(row["value"])
            for row in connection.execute(
                "SELECT key,value FROM gold_meta ORDER BY key"
            )
        }
        if str(gold_meta.get("schema_version")) != "2":
            raise ValueError("La base no corresponde a Gold v2")
        horizons = tuple(
            int(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT horizon_seconds
                FROM gold_features
                ORDER BY horizon_seconds DESC
                """
            )
        )
        if not horizons:
            raise ValueError("Gold no contiene horizontes")
        validation_trials = (
            len(horizons) * len(MODEL_FEATURES) * len(edge_values)
        )
        validation_alpha_per_trial = 0.05 / validation_trials
        validation_confidence_z = NormalDist().inv_cdf(
            1.0 - validation_alpha_per_trial
        )
        test_confidence_z = NormalDist().inv_cdf(0.95)

        for horizon in horizons:
            train_rows = _load_rows(
                connection,
                split="train",
                horizon_seconds=horizon,
            )
            validation_rows = _load_rows(
                connection,
                split="validation",
                horizon_seconds=horizon,
            )
            if len(train_rows) < 100 or len(validation_rows) < 30:
                raise ValueError(
                    f"Datos insuficientes en horizonte {horizon}s"
                )
            y_validation = _targets(validation_rows)
            if len(np.unique(y_validation)) < 2:
                raise ValueError(
                    f"Validation solo contiene una clase en {horizon}s"
                )

            market_validation = _market_probabilities(validation_rows)
            market_metrics = _probability_metrics(
                y_validation,
                market_validation,
            )
            probability_records.append(
                {
                    "split": "validation",
                    "horizon_seconds": horizon,
                    "model_name": "market_implied",
                    "metrics": market_metrics,
                }
            )
            for row, probability in zip(
                validation_rows,
                market_validation,
            ):
                prediction_records.append(
                    {
                        "split": "validation",
                        "source_dataset": str(row["source_dataset"]),
                        "condition_id": str(row["condition_id"]),
                        "horizon_seconds": horizon,
                        "model_name": "market_implied",
                        "decision_timestamp_ms": int(
                            row["decision_timestamp_ms"]
                        ),
                        "y_up": int(row["y_up"]),
                        "probability_up": float(probability),
                        "up_best_ask": float(row["up_best_ask"]),
                        "down_best_ask": float(row["down_best_ask"]),
                    }
                )

            model_summaries: list[dict[str, Any]] = []
            recorded_market_scopes = {"all_markets"}
            for model_name, features in MODEL_FEATURES.items():
                scope = MODEL_SCOPES[model_name]
                scoped_train_rows = _model_rows(train_rows, model_name)
                scoped_validation_rows = _model_rows(
                    validation_rows,
                    model_name,
                )
                if (
                    len(scoped_train_rows) < 100
                    or len(scoped_validation_rows) < 30
                ):
                    model_summaries.append(
                        {
                            "model_name": model_name,
                            "scope": scope,
                            "available": False,
                            "reason": "filas_insuficientes_en_scope",
                            "train_rows": len(scoped_train_rows),
                            "validation_rows": len(
                                scoped_validation_rows
                            ),
                            "features": list(features),
                        }
                    )
                    continue
                calibration_count = max(
                    30,
                    int(
                        len(scoped_train_rows) * CALIBRATION_FRACTION
                    ),
                )
                fit_rows = scoped_train_rows[:-calibration_count]
                calibration_rows = scoped_train_rows[-calibration_count:]
                if len(fit_rows) < 50:
                    model_summaries.append(
                        {
                            "model_name": model_name,
                            "scope": scope,
                            "available": False,
                            "reason": "fit_insuficiente_en_scope",
                            "train_rows": len(scoped_train_rows),
                            "validation_rows": len(
                                scoped_validation_rows
                            ),
                            "features": list(features),
                        }
                    )
                    continue
                y_fit = _targets(fit_rows)
                y_calibration = _targets(calibration_rows)
                y_scoped_validation = _targets(scoped_validation_rows)
                if (
                    len(np.unique(y_fit)) < 2
                    or len(np.unique(y_calibration)) < 2
                    or len(np.unique(y_scoped_validation)) < 2
                ):
                    model_summaries.append(
                        {
                            "model_name": model_name,
                            "scope": scope,
                            "available": False,
                            "reason": "scope_con_una_sola_clase",
                            "train_rows": len(scoped_train_rows),
                            "validation_rows": len(
                                scoped_validation_rows
                            ),
                            "features": list(features),
                        }
                    )
                    continue

                scoped_market_probabilities = _market_probabilities(
                    scoped_validation_rows
                )
                scoped_market_metrics = _probability_metrics(
                    y_scoped_validation,
                    scoped_market_probabilities,
                )
                if scope not in recorded_market_scopes:
                    benchmark_name = (
                        "market_implied_official_strike"
                    )
                    probability_records.append(
                        {
                            "split": "validation",
                            "horizon_seconds": horizon,
                            "model_name": benchmark_name,
                            "metrics": scoped_market_metrics,
                        }
                    )
                    for row, predicted in zip(
                        scoped_validation_rows,
                        scoped_market_probabilities,
                    ):
                        prediction_records.append(
                            {
                                "split": "validation",
                                "source_dataset": str(
                                    row["source_dataset"]
                                ),
                                "condition_id": str(
                                    row["condition_id"]
                                ),
                                "horizon_seconds": horizon,
                                "model_name": benchmark_name,
                                "decision_timestamp_ms": int(
                                    row["decision_timestamp_ms"]
                                ),
                                "y_up": int(row["y_up"]),
                                "probability_up": float(predicted),
                                "up_best_ask": float(
                                    row["up_best_ask"]
                                ),
                                "down_best_ask": float(
                                    row["down_best_ask"]
                                ),
                            }
                        )
                    recorded_market_scopes.add(scope)

                estimator = _build_estimator(model_name)
                estimator.fit(
                    _feature_matrix(fit_rows, features),
                    y_fit,
                )
                raw_calibration = np.asarray(
                    estimator.predict_proba(
                        _feature_matrix(calibration_rows, features)
                    )[:, 1],
                    dtype=float,
                )
                calibrator = _fit_calibrator(
                    raw_calibration,
                    y_calibration,
                )
                model = FittedModel(
                    name=model_name,
                    horizon_seconds=horizon,
                    features=tuple(features),
                    estimator=estimator,
                    calibrator=calibrator,
                )
                fitted[(horizon, model_name)] = model
                probabilities = model.probabilities(
                    scoped_validation_rows
                )
                probability = _probability_metrics(
                    y_scoped_validation,
                    probabilities,
                )
                probability_records.append(
                    {
                        "split": "validation",
                        "horizon_seconds": horizon,
                        "model_name": model_name,
                        "metrics": probability,
                    }
                )
                for row, predicted in zip(
                    scoped_validation_rows,
                    probabilities,
                ):
                    prediction_records.append(
                        {
                            "split": "validation",
                            "source_dataset": str(
                                row["source_dataset"]
                            ),
                            "condition_id": str(row["condition_id"]),
                            "horizon_seconds": horizon,
                            "model_name": model_name,
                            "decision_timestamp_ms": int(
                                row["decision_timestamp_ms"]
                            ),
                            "y_up": int(row["y_up"]),
                            "probability_up": float(predicted),
                            "up_best_ask": float(row["up_best_ask"]),
                            "down_best_ask": float(
                                row["down_best_ask"]
                            ),
                        }
                    )

                threshold_summaries: list[dict[str, Any]] = []
                for minimum_edge in edge_values:
                    trading = _trade_metrics(
                        scoped_validation_rows,
                        y_scoped_validation,
                        probabilities,
                        minimum_edge=minimum_edge,
                        fee_rate=fee_rate,
                        slippage_per_share=slippage_per_share,
                        confidence_z=validation_confidence_z,
                    )
                    eligible, failures = _candidate_passes_validation(
                        probability,
                        trading,
                        market_brier=float(
                            scoped_market_metrics["brier"]
                        ),
                    )
                    threshold_summaries.append(
                        {
                            "minimum_edge": minimum_edge,
                            "eligible": eligible,
                            "failures": failures,
                            "trading": trading,
                        }
                    )
                    threshold_records.append(
                        {
                            "split": "validation",
                            "horizon_seconds": horizon,
                            "model_name": model_name,
                            "minimum_edge": minimum_edge,
                            "metrics": {
                                **trading,
                                "eligible": eligible,
                                "failures": failures,
                            },
                        }
                    )
                    if eligible:
                        selection_candidates.append(
                            {
                                "horizon_seconds": horizon,
                                "model_name": model_name,
                                "scope": scope,
                                "minimum_edge": minimum_edge,
                                "probability": probability,
                                "trading": trading,
                                "selection_score": trading[
                                    "lower_confidence_bound_mean_pnl"
                                ],
                            }
                        )
                model_summaries.append(
                    {
                        "model_name": model_name,
                        "scope": scope,
                        "available": True,
                        "train_rows": len(scoped_train_rows),
                        "fit_rows": len(fit_rows),
                        "calibration_rows": len(calibration_rows),
                        "validation_rows": len(
                            scoped_validation_rows
                        ),
                        "features": list(features),
                        "market_implied": scoped_market_metrics,
                        "probability": probability,
                        "thresholds": threshold_summaries,
                    }
                )
            validation_summary.append(
                {
                    "horizon_seconds": horizon,
                    "train_rows": len(train_rows),
                    "validation_rows": len(validation_rows),
                    "market_implied": market_metrics,
                    "models": model_summaries,
                }
            )

        selection_frozen_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        selection = (
            max(
                selection_candidates,
                key=lambda item: (
                    float(item["selection_score"]),
                    -float(item["probability"]["brier"]),
                    float(
                        item["trading"]["net_pnl_per_share_sequence"]
                    ),
                ),
            )
            if selection_candidates
            else None
        )

        forward_paper_candidate = False
        if selection is not None:
            horizon = int(selection["horizon_seconds"])
            model_name = str(selection["model_name"])
            minimum_edge = float(selection["minimum_edge"])
            selected_model = fitted[(horizon, model_name)]

            # Test remains unopened until the validation choice above is frozen.
            all_test_rows = _load_rows(
                connection,
                split="test",
                horizon_seconds=horizon,
            )
            test_rows = _model_rows(all_test_rows, model_name)
            test_accessed = True
            if not test_rows:
                test_summary = {
                    "horizon_seconds": horizon,
                    "model_name": model_name,
                    "scope": selection["scope"],
                    "minimum_edge": minimum_edge,
                    "rows": 0,
                    "forward_paper_candidate": False,
                    "failures": ["sin_filas_test_en_scope"],
                }
            else:
                y_test = _targets(test_rows)
                test_probabilities = selected_model.probabilities(
                    test_rows
                )
                market_test_probabilities = _market_probabilities(
                    test_rows
                )
                selected_probability = _probability_metrics(
                    y_test,
                    test_probabilities,
                )
                market_probability = _probability_metrics(
                    y_test,
                    market_test_probabilities,
                )
                selected_trading = _trade_metrics(
                    test_rows,
                    y_test,
                    test_probabilities,
                    minimum_edge=minimum_edge,
                    fee_rate=fee_rate,
                    slippage_per_share=slippage_per_share,
                    confidence_z=test_confidence_z,
                )
                forward_paper_candidate, test_failures = (
                    _candidate_passes_test(
                        selected_probability,
                        selected_trading,
                        market_brier=float(
                            market_probability["brier"]
                        ),
                    )
                )
                test_summary = {
                    "horizon_seconds": horizon,
                    "model_name": model_name,
                    "scope": selection["scope"],
                    "minimum_edge": minimum_edge,
                    "rows": len(test_rows),
                    "selected_model_probability": selected_probability,
                    "market_implied_probability": market_probability,
                    "selected_model_trading": selected_trading,
                    "forward_paper_candidate": (
                        forward_paper_candidate
                    ),
                    "failures": test_failures,
                }
                benchmark_name = (
                    "market_implied"
                    if selection["scope"] == "all_markets"
                    else "market_implied_official_strike"
                )
                for name, probabilities, metrics in (
                    (
                        model_name,
                        test_probabilities,
                        selected_probability,
                    ),
                    (
                        benchmark_name,
                        market_test_probabilities,
                        market_probability,
                    ),
                ):
                    probability_records.append(
                        {
                            "split": "test",
                            "horizon_seconds": horizon,
                            "model_name": name,
                            "metrics": metrics,
                        }
                    )
                    for row, predicted in zip(test_rows, probabilities):
                        prediction_records.append(
                            {
                                "split": "test",
                                "source_dataset": str(
                                    row["source_dataset"]
                                ),
                                "condition_id": str(
                                    row["condition_id"]
                                ),
                                "horizon_seconds": horizon,
                                "model_name": name,
                                "decision_timestamp_ms": int(
                                    row["decision_timestamp_ms"]
                                ),
                                "y_up": int(row["y_up"]),
                                "probability_up": float(predicted),
                                "up_best_ask": float(
                                    row["up_best_ask"]
                                ),
                                "down_best_ask": float(
                                    row["down_best_ask"]
                                ),
                            }
                        )
                threshold_records.append(
                    {
                        "split": "test",
                        "horizon_seconds": horizon,
                        "model_name": model_name,
                        "minimum_edge": minimum_edge,
                        "metrics": {
                            **selected_trading,
                            "forward_paper_candidate": (
                                forward_paper_candidate
                            ),
                            "failures": test_failures,
                        },
                    }
                )
    finally:
        connection.close()

    source_size_after = source.stat().st_size
    source_unchanged = source_size_before == source_size_after
    contract_sha256 = _contract_hash(
        horizons,
        edge_values,
        fee_rate=fee_rate,
        slippage_per_share=slippage_per_share,
    )
    selection_public = (
        {
            "horizon_seconds": selection["horizon_seconds"],
            "model_name": selection["model_name"],
            "scope": selection["scope"],
            "minimum_edge": selection["minimum_edge"],
            "selection_score": selection["selection_score"],
            "validation_probability": selection["probability"],
            "validation_trading": selection["trading"],
        }
        if selection is not None
        else None
    )
    meta = {
        "created_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "schema_version": PHASE4_SCHEMA_VERSION,
        "source_gold_database": str(source),
        "source_gold_bytes": source_size_before,
        "source_gold_quick_check": source_quick_check,
        "source_gold_opened_read_only": True,
        "source_gold_unchanged": source_unchanged,
        "source_gold_contract_sha256": gold_meta.get(
            "data_contract_sha256"
        ),
        "phase4_contract_sha256": contract_sha256,
        "python_models": "scikit-learn",
        "sklearn_version": sklearn.__version__,
        "numpy_version": np.__version__,
        "random_seed": PHASE4_RANDOM_SEED,
        "horizons_seconds": list(horizons),
        "model_features": {
            key: list(value) for key, value in MODEL_FEATURES.items()
        },
        "model_scopes": MODEL_SCOPES,
        "features_excluded": {
            "binance": "cobertura_historica_insuficiente",
            "absolute_chainlink_price": "no_estacionario",
        },
        "strike_feature_policy": {
            "general_models": "usan_los_885_mercados_sin_strike",
            "enriched_models": (
                "usan_distance_to_strike_solo_con_strike_oficial"
            ),
            "missing_strikes_imputed": False,
        },
        "fee_model": {
            "execution": "taker",
            "formula": "shares * fee_rate * p * (1-p)",
            "fee_rate": fee_rate,
        },
        "slippage_per_share": slippage_per_share,
        "edge_grid": list(edge_values),
        "selection_policy": {
            "fit": "primer_80_por_ciento_cronologico_de_train",
            "calibration": (
                "ultimo_20_por_ciento_cronologico_de_train_platt"
            ),
            "selection": "validation_solamente",
            "test": "bloqueado_hasta_congelar_seleccion",
            "minimum_validation_trades": MIN_VALIDATION_TRADES,
            "minimum_test_trades": MIN_TEST_TRADES,
            "validation_multiple_testing": {
                "method": "bonferroni",
                "family_alpha": 0.05,
                "trials": validation_trials,
                "alpha_per_trial": validation_alpha_per_trial,
                "confidence_z": validation_confidence_z,
            },
            "test_confidence": {
                "method": "unilateral_95_por_ciento",
                "confidence_z": test_confidence_z,
            },
            "positive_lcb_required": True,
            "max_validation_ece": MAX_VALIDATION_ECE,
            "max_brier_degradation_vs_market": (
                MAX_BRIER_DEGRADATION_VS_MARKET
            ),
        },
        "selection_frozen_at": selection_frozen_at,
        "strategy_selected_on_validation": selection is not None,
        "selection": selection_public,
        "test_accessed": test_accessed,
        "test_summary": test_summary,
        "forward_paper_candidate": forward_paper_candidate,
        "kelly_used": False,
        "orders_created": False,
        "wallet_required": False,
        "validation_summary": validation_summary,
    }
    partial_output.parent.mkdir(parents=True, exist_ok=True)
    output_quick_check = _write_output(
        path=partial_output,
        meta=meta,
        probability_records=probability_records,
        threshold_records=threshold_records,
        prediction_records=prediction_records,
    )
    pipeline_passed = (
        source_quick_check == "ok"
        and source_unchanged
        and output_quick_check == "ok"
        and len(validation_summary) == len(horizons)
        and all(
            len(item["models"]) == len(MODEL_FEATURES)
            for item in validation_summary
        )
        and (selection is not None or not test_accessed)
    )

    artifact_created = False
    if pipeline_passed and selection is not None:
        selected_model = fitted[
            (
                int(selection["horizon_seconds"]),
                str(selection["model_name"]),
            )
        ]
        partial_model.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "artifact_type": "polymarket_phase4_frozen_model",
                "phase4_contract_sha256": contract_sha256,
                "source_gold_contract_sha256": gold_meta.get(
                    "data_contract_sha256"
                ),
                "created_at": meta["created_at"],
                "model": selected_model,
                "minimum_edge": selection["minimum_edge"],
                "fee_rate": fee_rate,
                "slippage_per_share": slippage_per_share,
                "forward_paper_candidate": forward_paper_candidate,
                "orders_enabled": False,
            },
            partial_model,
            compress=3,
        )
        partial_model.replace(model_path)
        artifact_created = True

    if pipeline_passed:
        partial_output.replace(output)
    committed_output = output if pipeline_passed else partial_output
    return {
        "pipeline_passed": pipeline_passed,
        "strategy_selected_on_validation": selection is not None,
        "forward_paper_candidate": forward_paper_candidate,
        "important_interpretation": (
            "pipeline_passed solo valida el proceso; no demuestra rentabilidad"
        ),
        "source_gold_database": str(source),
        "source_gold_opened_read_only": True,
        "source_gold_quick_check": source_quick_check,
        "source_gold_size_before": source_size_before,
        "source_gold_size_after": source_size_after,
        "source_gold_unchanged": source_unchanged,
        "output_database": str(committed_output),
        "output_committed": pipeline_passed,
        "output_quick_check": output_quick_check,
        "output_bytes": committed_output.stat().st_size,
        "model_artifact": str(model_path) if artifact_created else None,
        "model_artifact_created": artifact_created,
        "horizons_seconds": list(horizons),
        "models_compared": [
            "market_implied",
            *MODEL_FEATURES.keys(),
        ],
        "selection": selection_public,
        "test_accessed": test_accessed,
        "test_summary": test_summary,
        "validation_summary": validation_summary,
        "fee_rate": fee_rate,
        "slippage_per_share": slippage_per_share,
        "kelly_used": False,
        "orders_created": False,
        "wallet_required": False,
        "phase4_contract_sha256": contract_sha256,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def phase4_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).expanduser().resolve()
    if not database.is_file():
        raise ValueError(f"No se encontró la base Fase 4: {database}")
    connection = _open_read_only(database)
    try:
        quick_check = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        )
        meta = {
            str(row["key"]): json.loads(row["value"])
            for row in connection.execute(
                "SELECT key,value FROM phase4_meta ORDER BY key"
            )
        }
        probability_metrics = int(
            connection.execute(
                "SELECT COUNT(*) FROM probability_metrics"
            ).fetchone()[0]
        )
        threshold_metrics = int(
            connection.execute(
                "SELECT COUNT(*) FROM threshold_metrics"
            ).fetchone()[0]
        )
        predictions = int(
            connection.execute(
                "SELECT COUNT(*) FROM predictions"
            ).fetchone()[0]
        )
        prediction_splits = [
            {
                "split": str(row[0]),
                "horizon_seconds": int(row[1]),
                "model_name": str(row[2]),
                "rows": int(row[3]),
            }
            for row in connection.execute(
                """
                SELECT split,horizon_seconds,model_name,COUNT(*)
                FROM predictions
                GROUP BY split,horizon_seconds,model_name
                ORDER BY split,horizon_seconds DESC,model_name
                """
            )
        ]
    finally:
        connection.close()
    return {
        "database": str(database),
        "database_bytes": database.stat().st_size,
        "sqlite_quick_check": quick_check,
        "probability_metric_records": probability_metrics,
        "threshold_metric_records": threshold_metrics,
        "predictions": predictions,
        "prediction_splits": prediction_splits,
        "strategy_selected_on_validation": meta.get(
            "strategy_selected_on_validation"
        ),
        "test_accessed": meta.get("test_accessed"),
        "forward_paper_candidate": meta.get(
            "forward_paper_candidate"
        ),
        "selection": meta.get("selection"),
        "test_summary": meta.get("test_summary"),
        "orders_created": meta.get("orders_created"),
        "wallet_required": meta.get("wallet_required"),
        "meta": meta,
    }
