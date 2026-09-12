from __future__ import annotations

import math
from collections.abc import Mapping
from statistics import NormalDist
from typing import Any


CANDIDATE_ID = "resolution_twap60_zero_drift_average"
PAIRED_CONTROL_ID = "market_favorite_on_mechanical_candidate_markets"

MECHANICAL_MODEL_SPEC: dict[str, Any] = {
    "family": "zero_fit_resolution_mechanics",
    "distribution": "arithmetic_brownian_small_return_approximation",
    "drift_bps_per_second": 0.0,
    "decision_horizon_seconds": 60,
    "resolution_twap_window_seconds": 60,
    "spot_distance": "10000_times_chainlink_spot_over_opening_twap_minus_one",
    "volatility_estimator": "population_stddev_of_one_second_chainlink_log_returns_over_prior_60s_bps",
    "future_average_standard_deviation": "volatility_bps_times_sqrt(horizon_seconds_over_3)",
    "probability_up": "standard_normal_cdf(spot_distance_bps_over_future_average_standard_deviation_bps)",
    "fit_parameters": False,
    "probability_calibration": False,
    "market_probability_used_by_model": False,
}

SELECTION_SPEC: dict[str, Any] = {
    "up_expected_pnl_per_share": "p_up_minus_all_in_up_entry_cost",
    "down_expected_pnl_per_share": "one_minus_p_up_minus_all_in_down_entry_cost",
    "select": "strictly_larger_positive_expected_pnl",
    "minimum_expected_pnl_per_share_strict": 0.0,
    "tie_action": "ABSTAIN",
    "maximum_sides_per_market": 1,
    "additional_threshold_search": False,
}


class V030StrategyError(ValueError):
    pass


def frozen_model_spec() -> dict[str, Any]:
    return dict(MECHANICAL_MODEL_SPEC)


def frozen_selection_spec() -> dict[str, Any]:
    return dict(SELECTION_SPEC)


def validate_model_spec(value: Any) -> dict[str, Any]:
    expected = frozen_model_spec()
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise V030StrategyError("Modelo mecanico V0.30 incompatible")
    return expected


def validate_selection_spec(value: Any) -> dict[str, Any]:
    expected = frozen_selection_spec()
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise V030StrategyError("Seleccion economica V0.30 incompatible")
    return expected


def mechanical_probability_up(
    *,
    chainlink_spot: float | None,
    opening_twap: float | None,
    volatility_60s_bps: float | None,
    horizon_seconds: int = 60,
    resolution_twap_window_seconds: int = 60,
) -> float | None:
    """Return the zero-fit probability that the final TWAP is at least its open.

    For an arithmetic Brownian path starting at the observed Chainlink spot, the
    variance of its average over T seconds is sigma^2*T/3.  The prior 60-second
    one-second return volatility is the frozen plug-in estimate for sigma.
    Missing, stale or degenerate inputs must be rejected by the caller rather
    than imputed.
    """

    if horizon_seconds != 60 or resolution_twap_window_seconds != 60:
        return None
    if chainlink_spot is None or opening_twap is None or volatility_60s_bps is None:
        return None
    spot = float(chainlink_spot)
    opening = float(opening_twap)
    volatility = float(volatility_60s_bps)
    if not all(math.isfinite(value) for value in (spot, opening, volatility)):
        return None
    if spot <= 0.0 or opening <= 0.0 or volatility <= 0.0:
        return None
    distance_bps = (spot / opening - 1.0) * 10_000.0
    average_std_bps = volatility * math.sqrt(float(horizon_seconds) / 3.0)
    if not math.isfinite(average_std_bps) or average_std_bps <= 0.0:
        return None
    probability = NormalDist().cdf(distance_bps / average_std_bps)
    return min(1.0, max(0.0, float(probability)))


def select_side(
    *,
    probability_up: float,
    up_entry_cost: float,
    down_entry_cost: float,
) -> dict[str, float | str] | None:
    probability = float(probability_up)
    up_cost = float(up_entry_cost)
    down_cost = float(down_entry_cost)
    if not all(math.isfinite(value) for value in (probability, up_cost, down_cost)):
        raise V030StrategyError("Entradas V0.30 no finitas")
    if not 0.0 <= probability <= 1.0:
        raise V030StrategyError("Probabilidad Up V0.30 fuera de rango")
    if up_cost <= 0.0 or down_cost <= 0.0:
        raise V030StrategyError("Coste de entrada V0.30 invalido")
    up_ev = probability - up_cost
    down_ev = 1.0 - probability - down_cost
    if up_ev <= 0.0 and down_ev <= 0.0:
        return None
    if up_ev == down_ev:
        return None
    if up_ev > down_ev:
        return {
            "side": "Up",
            "entry_cost": up_cost,
            "expected_pnl_per_share": up_ev,
        }
    return {
        "side": "Down",
        "entry_cost": down_cost,
        "expected_pnl_per_share": down_ev,
    }


__all__ = [
    "CANDIDATE_ID",
    "MECHANICAL_MODEL_SPEC",
    "PAIRED_CONTROL_ID",
    "SELECTION_SPEC",
    "V030StrategyError",
    "frozen_model_spec",
    "frozen_selection_spec",
    "mechanical_probability_up",
    "select_side",
    "validate_model_spec",
    "validate_selection_spec",
]
