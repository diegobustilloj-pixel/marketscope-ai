from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


CANDIDATE_ID = "cost_aware_logistic_temporal_holdout"
PAIRED_CONTROL_ID = "market_favorite_on_candidate_markets_control"
MODEL_FEATURES: tuple[str, ...] = (
    "implied_up_mid_probability",
    "twap_distance_to_open_bps",
    "binance_return_60s_bps",
)
MODEL_SPEC: dict[str, Any] = {
    "algorithm": "sklearn_pipeline_logistic_regression",
    "features": list(MODEL_FEATURES),
    "target": "y_up",
    "imputer": {
        "strategy": "median",
        "add_indicator": True,
        "keep_empty_features": True,
        "fit_on": "first_12h_only",
    },
    "scaler": {
        "type": "StandardScaler",
        "fit_on": "first_12h_only",
    },
    "estimator": {
        "type": "LogisticRegression",
        "C": 0.5,
        "max_iter": 2000,
        "random_state": 17,
        "solver": "lbfgs",
        "class_weight": None,
    },
    "hyperparameter_search": False,
    "probability_calibration": False,
}
SELECTION_SPEC: dict[str, Any] = {
    "up_expected_pnl_per_share": "p_up_minus_all_in_up_entry_cost",
    "down_expected_pnl_per_share": "one_minus_p_up_minus_all_in_down_entry_cost",
    "select": "strictly_larger_positive_expected_pnl",
    "minimum_expected_pnl_per_share_strict": 0.0,
    "tie_action": "ABSTAIN",
    "maximum_sides_per_market": 1,
    "volatility_filter": None,
    "twap_distance_threshold": None,
    "direction_filter": None,
    "cost_band_filter": None,
}


class V029StrategyError(ValueError):
    pass


def frozen_model_spec() -> dict[str, Any]:
    return {
        **MODEL_SPEC,
        "features": list(MODEL_FEATURES),
        "imputer": dict(MODEL_SPEC["imputer"]),
        "scaler": dict(MODEL_SPEC["scaler"]),
        "estimator": dict(MODEL_SPEC["estimator"]),
    }


def frozen_selection_spec() -> dict[str, Any]:
    return dict(SELECTION_SPEC)


def validate_model_spec(value: Any) -> dict[str, Any]:
    expected = frozen_model_spec()
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise V029StrategyError("Modelo V0.29 incompatible")
    return expected


def validate_selection_spec(value: Any) -> dict[str, Any]:
    expected = frozen_selection_spec()
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise V029StrategyError("Seleccion economica V0.29 incompatible")
    return expected


def select_side(
    *,
    probability_up: float,
    up_entry_cost: float,
    down_entry_cost: float,
) -> dict[str, float | str] | None:
    probability = float(probability_up)
    if not 0.0 <= probability <= 1.0:
        raise V029StrategyError("Probabilidad Up V0.29 fuera de rango")
    up_ev = probability - float(up_entry_cost)
    down_ev = 1.0 - probability - float(down_entry_cost)
    if up_ev <= 0.0 and down_ev <= 0.0:
        return None
    if up_ev == down_ev:
        return None
    if up_ev > down_ev:
        return {
            "side": "Up",
            "entry_cost": float(up_entry_cost),
            "expected_pnl_per_share": up_ev,
        }
    return {
        "side": "Down",
        "entry_cost": float(down_entry_cost),
        "expected_pnl_per_share": down_ev,
    }


def feature_vector(feature: Mapping[str, Any]) -> list[float | None]:
    return [
        None if feature.get(name) is None else float(feature[name])
        for name in MODEL_FEATURES
    ]


def validate_feature_names(value: Sequence[str]) -> list[str]:
    normalized = [str(item) for item in value]
    if normalized != list(MODEL_FEATURES):
        raise V029StrategyError("Features V0.29 incompatibles")
    return normalized


__all__ = [
    "CANDIDATE_ID",
    "MODEL_FEATURES",
    "MODEL_SPEC",
    "PAIRED_CONTROL_ID",
    "SELECTION_SPEC",
    "V029StrategyError",
    "feature_vector",
    "frozen_model_spec",
    "frozen_selection_spec",
    "select_side",
    "validate_feature_names",
    "validate_model_spec",
    "validate_selection_spec",
]
