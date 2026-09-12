from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable

from .climate_research import OPEN_METEO_MODELS, SCHEMA, iso, read_csv, sha256, stable_json, utc_now, write_csv, write_json


MIN_STATION_TRAIN_COMPARABLE = 30
MIN_STATION_TRAIN_MATCH_RATE = 0.95
MIN_LOCAL_CALIBRATION = 20
MIN_SELECTION_EVENTS = 30
SCORE_WEIGHTS = {
    "predictability": 0.35,
    "sample": 0.20,
    "contract_fidelity": 0.15,
    "forecast_coverage": 0.10,
    "liquidity_proxy": 0.10,
    "stability": 0.10,
}


@dataclass(frozen=True)
class Event:
    slug: str
    station_id: str
    date: str
    market_type: str
    unit: str
    precision: float
    split: str
    target: float
    winner_bucket: str
    city: str
    volume: float
    buckets: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Prediction:
    slug: str
    lead: int
    candidate: str
    point: float
    probabilities: tuple[float, ...]
    models_available: int


def _mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else math.nan


def _quantile(values: Iterable[float], probability: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _fit_errors(values: list[float], floor: float) -> dict[str, float | int]:
    bias = _mean(values)
    variance = sum((value - bias) ** 2 for value in values) / max(1, len(values) - 1)
    return {"n": len(values), "bias": bias, "sigma": max(floor, math.sqrt(variance))}


def _bucket_probabilities(mu: float, sigma: float, buckets: tuple[dict[str, Any], ...], precision: float) -> tuple[float, ...]:
    distribution = NormalDist(mu=mu, sigma=max(sigma, 1e-6))
    result = []
    half_step = precision / 2.0
    for bucket in buckets:
        lower = bucket.get("lower")
        upper = bucket.get("upper")
        lower_probability = 0.0 if lower is None else distribution.cdf(float(lower) - half_step)
        upper_probability = 1.0 if upper is None else distribution.cdf(float(upper) + half_step)
        result.append(max(0.0, upper_probability - lower_probability))
    total = sum(result)
    if total <= 0:
        return tuple(1.0 / len(result) for _ in result)
    return tuple(value / total for value in result)


def _winner_index(event: Event) -> int:
    for index, bucket in enumerate(event.buckets):
        if bucket.get("winner") or bucket.get("label") == event.winner_bucket:
            return index
    raise ValueError(f"No se encontró el bucket ganador de {event.slug}")


def _prediction_metrics(predictions: Iterable[Prediction], events: dict[str, Event]) -> dict[str, Any]:
    rows = []
    for prediction in predictions:
        event = events[prediction.slug]
        actual = _winner_index(event)
        predicted = max(range(len(prediction.probabilities)), key=lambda index: prediction.probabilities[index])
        error = prediction.point - event.target
        error_c = error * 5.0 / 9.0 if event.unit == "°F" else error
        brier = sum((probability - (1.0 if index == actual else 0.0)) ** 2 for index, probability in enumerate(prediction.probabilities))
        rows.append(
            {
                "error": error,
                "error_c": error_c,
                "correct": int(predicted == actual),
                "brier": brier,
                "log_loss": -math.log(max(1e-12, prediction.probabilities[actual])),
            }
        )
    if not rows:
        return {
            "n": 0,
            "mae": None,
            "rmse": None,
            "bias": None,
            "mae_c": None,
            "rmse_c": None,
            "bias_c": None,
            "bucket_accuracy": None,
            "brier": None,
            "log_loss": None,
        }
    return {
        "n": len(rows),
        "mae": _mean(abs(row["error"]) for row in rows),
        "rmse": math.sqrt(_mean(row["error"] ** 2 for row in rows)),
        "bias": _mean(row["error"] for row in rows),
        "mae_c": _mean(abs(row["error_c"]) for row in rows),
        "rmse_c": math.sqrt(_mean(row["error_c"] ** 2 for row in rows)),
        "bias_c": _mean(row["error_c"] for row in rows),
        "bucket_accuracy": _mean(row["correct"] for row in rows),
        "brier": _mean(row["brier"] for row in rows),
        "log_loss": _mean(row["log_loss"] for row in rows),
    }


def _load_inputs(root: Path) -> tuple[sqlite3.Connection, dict[str, Event], dict[str, dict[str, Any]], set[str]]:
    derived = root / "derived"
    market_rows = read_csv(derived / "markets.csv")
    markets = {row["slug"]: row for row in market_rows}
    database = sqlite3.connect(derived / "climate_weather.db")
    database.row_factory = sqlite3.Row
    station_quality: dict[str, dict[str, Any]] = {}
    for row in database.execute(
        """
        SELECT station_id, COUNT(*) comparable,
               SUM(CASE WHEN reconstruction_matches=1 THEN 1 ELSE 0 END) matches
        FROM event_truth
        WHERE split='TRAIN' AND reconstruction_matches IS NOT NULL
        GROUP BY station_id
        """
    ):
        comparable = int(row["comparable"])
        matches = int(row["matches"])
        rate = matches / comparable
        station_quality[row["station_id"]] = {
            "station_id": row["station_id"],
            "train_comparable": comparable,
            "train_matches": matches,
            "train_match_rate": rate,
            "eligible": comparable >= MIN_STATION_TRAIN_COMPARABLE and rate >= MIN_STATION_TRAIN_MATCH_RATE,
        }
    eligible = {station for station, quality in station_quality.items() if quality["eligible"]}
    events: dict[str, Event] = {}
    for row in database.execute("SELECT * FROM event_truth WHERE reconstruction_matches=1"):
        if row["station_id"] not in eligible or row["slug"] not in markets or row["observed_value"] is None:
            continue
        market = markets[row["slug"]]
        buckets = tuple(json.loads(market["buckets_json"]))
        events[row["slug"]] = Event(
            slug=row["slug"],
            station_id=row["station_id"],
            date=row["date"],
            market_type=row["market_type"],
            unit=row["unit"],
            precision=float(row["precision"]),
            split=row["split"],
            target=float(row["observed_value"]),
            winner_bucket=row["winner_bucket"],
            city=market["city"],
            volume=float(market["volume"] or 0),
            buckets=buckets,
        )
    return database, events, station_quality, eligible


def _forecast_points(database: sqlite3.Connection, events: dict[str, Event]) -> list[dict[str, Any]]:
    result = []
    for row in database.execute(
        """
        SELECT t.slug,t.station_id,t.market_type,t.unit,t.split,
               f.model,f.lead_days,f.forecast_max_c,f.forecast_min_c
        FROM event_truth t JOIN forecasts_daily f
          ON f.station_id=t.station_id AND f.date=t.date
        WHERE t.reconstruction_matches=1
        """
    ):
        event = events.get(row["slug"])
        if not event:
            continue
        forecast_c = row["forecast_max_c"] if event.market_type == "HIGHEST" else row["forecast_min_c"]
        if forecast_c is None:
            continue
        forecast = float(forecast_c) * 9.0 / 5.0 + 32.0 if event.unit == "°F" else float(forecast_c)
        result.append(
            {
                "slug": event.slug,
                "station_id": event.station_id,
                "market_type": event.market_type,
                "unit": event.unit,
                "split": event.split,
                "model": row["model"],
                "lead": int(row["lead_days"]),
                "forecast": forecast,
                "error": forecast - event.target,
            }
        )
    return result


def _calibrate(points: list[dict[str, Any]], events: dict[str, Event]) -> tuple[dict[tuple[Any, ...], dict[str, Any]], dict[tuple[Any, ...], dict[str, Any]], list[dict[str, Any]]]:
    local_errors: dict[tuple[Any, ...], list[float]] = defaultdict(list)
    global_errors: dict[tuple[Any, ...], list[float]] = defaultdict(list)
    for row in points:
        if row["split"] != "TRAIN":
            continue
        local_errors[(row["station_id"], row["market_type"], row["unit"], row["model"], row["lead"])].append(row["error"])
        global_errors[(row["market_type"], row["unit"], row["model"], row["lead"])].append(row["error"])
    local = {}
    global_ = {}
    audit = []
    for key, values in global_errors.items():
        floor = 1.0 if key[1] == "°F" else 0.5
        global_[key] = _fit_errors(values, floor)
        audit.append({"scope": "GLOBAL", "station_id": "", "market_type": key[0], "unit": key[1], "model": key[2], "lead": key[3], **global_[key]})
    for key, values in local_errors.items():
        floor = 1.0 if key[2] == "°F" else 0.5
        local[key] = _fit_errors(values, floor)
        audit.append({"scope": "STATION", "station_id": key[0], "market_type": key[1], "unit": key[2], "model": key[3], "lead": key[4], **local[key]})
    return local, global_, audit


def _base_predictions(
    points: list[dict[str, Any]],
    events: dict[str, Event],
    local: dict[tuple[Any, ...], dict[str, Any]],
    global_: dict[tuple[Any, ...], dict[str, Any]],
) -> dict[tuple[str, int], dict[str, Prediction]]:
    result: dict[tuple[str, int], dict[str, Prediction]] = defaultdict(dict)
    for row in points:
        event = events[row["slug"]]
        local_key = (event.station_id, event.market_type, event.unit, row["model"], row["lead"])
        global_key = (event.market_type, event.unit, row["model"], row["lead"])
        fit = local.get(local_key)
        if not fit or fit["n"] < MIN_LOCAL_CALIBRATION:
            fit = global_.get(global_key)
        if not fit or fit["n"] < MIN_LOCAL_CALIBRATION:
            continue
        point = row["forecast"] - float(fit["bias"])
        result[(event.slug, row["lead"])][row["model"]] = Prediction(
            slug=event.slug,
            lead=row["lead"],
            candidate=row["model"],
            point=point,
            probabilities=_bucket_probabilities(point, float(fit["sigma"]), event.buckets, event.precision),
            models_available=1,
        )
    return result


def _train_weights(base: dict[tuple[str, int], dict[str, Prediction]], events: dict[str, Event]) -> dict[tuple[str, str, int, str], float]:
    grouped: dict[tuple[str, str, int, str], list[Prediction]] = defaultdict(list)
    for (slug, lead), models in base.items():
        event = events[slug]
        if event.split != "TRAIN":
            continue
        for model, prediction in models.items():
            grouped[(event.market_type, event.unit, lead, model)].append(prediction)
    losses = {key: _prediction_metrics(values, events)["brier"] for key, values in grouped.items()}
    result = {}
    for key, loss in losses.items():
        result[key] = 1.0 / max(0.02, float(loss)) ** 2
    return result


def _ensemble_predictions(
    base: dict[tuple[str, int], dict[str, Prediction]],
    events: dict[str, Event],
    weights: dict[tuple[str, str, int, str], float],
) -> dict[tuple[str, int], dict[str, Prediction]]:
    result = {key: dict(value) for key, value in base.items()}
    for (slug, lead), models in result.items():
        event = events[slug]
        available = list(models.values())
        if len(available) < 2:
            continue
        count = len(available)
        equal_probs = tuple(_mean(prediction.probabilities[index] for prediction in available) for index in range(len(event.buckets)))
        models["ensemble_equal"] = Prediction(slug, lead, "ensemble_equal", _mean(prediction.point for prediction in available), equal_probs, count)
        model_weights = [weights.get((event.market_type, event.unit, lead, prediction.candidate), 1.0) for prediction in available]
        total_weight = sum(model_weights)
        weighted_probs = tuple(
            sum(weight * prediction.probabilities[index] for weight, prediction in zip(model_weights, available)) / total_weight
            for index in range(len(event.buckets))
        )
        weighted_point = sum(weight * prediction.point for weight, prediction in zip(model_weights, available)) / total_weight
        models["ensemble_weighted_train"] = Prediction(slug, lead, "ensemble_weighted_train", weighted_point, weighted_probs, count)
    return result


def _validation_comparison(predictions: dict[tuple[str, int], dict[str, Prediction]], events: dict[str, Event]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int, str], list[Prediction]] = defaultdict(list)
    for (slug, lead), candidates in predictions.items():
        event = events[slug]
        if event.split != "VALIDATION" or not all(model in candidates for model in OPEN_METEO_MODELS):
            continue
        for candidate, prediction in candidates.items():
            grouped[(event.market_type, lead, candidate)].append(prediction)
    result = []
    for (market_type, lead, candidate), values in grouped.items():
        result.append({"market_type": market_type, "lead": lead, "candidate": candidate, "common_eight_model_pool": True, **_prediction_metrics(values, events)})
    return sorted(result, key=lambda row: (row["market_type"], row["lead"], row["brier"], row["candidate"]))


def _select(validation: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in validation:
        if row["n"] >= MIN_SELECTION_EVENTS:
            grouped[(row["market_type"], int(row["lead"]))].append(row)
    selected = {}
    for key, rows in grouped.items():
        selected[key] = min(rows, key=lambda row: (row["brier"], row["log_loss"], row["candidate"]))
    return selected


def _selection_coverage(
    selection: dict[tuple[str, int], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected = []
    unavailable = []
    for market_type in ("HIGHEST", "LOWEST"):
        for lead in range(1, 8):
            key = (market_type, lead)
            if key in selection:
                selected.append({"market_type": market_type, "lead": lead, **selection[key]})
            else:
                unavailable.append(
                    {
                        "market_type": market_type,
                        "lead": lead,
                        "status": "NO_EVALUABLE",
                        "reason": (
                            "Fewer than 30 VALIDATION events with a common "
                            "eight-model forecast pool"
                        ),
                    }
                )
    return selected, unavailable


def _selected_predictions(
    predictions: dict[tuple[str, int], dict[str, Prediction]],
    events: dict[str, Event],
    selection: dict[tuple[str, int], dict[str, Any]],
    split: str,
) -> list[Prediction]:
    result = []
    for (slug, lead), candidates in predictions.items():
        event = events[slug]
        if event.split != split or (event.market_type, lead) not in selection:
            continue
        candidate = selection[(event.market_type, lead)]["candidate"]
        if candidate in candidates:
            result.append(candidates[candidate])
    return result


def _selected_metrics_rows(predictions: list[Prediction], events: dict[str, Event]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int, str], list[Prediction]] = defaultdict(list)
    for prediction in predictions:
        event = events[prediction.slug]
        grouped[(event.market_type, prediction.lead, prediction.candidate)].append(prediction)
    result = []
    for (market_type, lead, candidate), values in grouped.items():
        population = sum(1 for event in events.values() if event.split == "TEST" and event.market_type == market_type)
        result.append(
            {
                "market_type": market_type,
                "lead": lead,
                "selected_candidate": candidate,
                "eligible_test_events": population,
                "coverage": len(values) / max(1, population),
                **_prediction_metrics(values, events),
            }
        )
    return sorted(result, key=lambda row: (row["market_type"], row["lead"]))


def _event_prediction_rows(predictions: list[Prediction], events: dict[str, Event]) -> list[dict[str, Any]]:
    result = []
    for prediction in predictions:
        event = events[prediction.slug]
        actual = _winner_index(event)
        predicted = max(range(len(prediction.probabilities)), key=lambda index: prediction.probabilities[index])
        result.append(
            {
                "slug": event.slug,
                "city": event.city,
                "station_id": event.station_id,
                "date": event.date,
                "market_type": event.market_type,
                "unit": event.unit,
                "lead": prediction.lead,
                "candidate": prediction.candidate,
                "target": event.target,
                "point_prediction": prediction.point,
                "winner_bucket": event.winner_bucket,
                "predicted_bucket": event.buckets[predicted]["label"],
                "winner_probability": prediction.probabilities[actual],
                "probabilities_json": stable_json(
                    {bucket["label"]: probability for bucket, probability in zip(event.buckets, prediction.probabilities)}
                ),
                "models_available": prediction.models_available,
            }
        )
    return sorted(result, key=lambda row: (row["date"], row["city"], row["market_type"], row["lead"]))


def _forecastability_scorecard(
    predictions: dict[tuple[str, int], dict[str, Prediction]],
    events: dict[str, Event],
    selection: dict[tuple[str, int], dict[str, Any]],
    station_quality: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped_events: dict[tuple[str, str, str], list[Event]] = defaultdict(list)
    for event in events.values():
        grouped_events[(event.city, event.market_type, event.station_id)].append(event)
    result = []
    for (city, market_type, station), group in grouped_events.items():
        chosen = selection.get((market_type, 1))
        if not chosen:
            continue
        candidate = chosen["candidate"]
        val_predictions = [
            predictions[(event.slug, 1)][candidate]
            for event in group
            if event.split == "VALIDATION" and candidate in predictions.get((event.slug, 1), {})
        ]
        test_predictions = [
            predictions[(event.slug, 1)][candidate]
            for event in group
            if event.split == "TEST" and candidate in predictions.get((event.slug, 1), {})
        ]
        val_metrics = _prediction_metrics(val_predictions, events)
        test_metrics = _prediction_metrics(test_predictions, events)
        test_population = sum(event.split == "TEST" for event in group)
        coverage = len(test_predictions) / max(1, test_population)
        unit = group[0].unit
        mae_c = None if test_metrics["mae"] is None else test_metrics["mae"] * (5.0 / 9.0 if unit == "°F" else 1.0)
        predictability = 0.0 if mae_c is None else 0.55 * max(0.0, 1.0 - mae_c / 4.0) + 0.45 * float(test_metrics["bucket_accuracy"])
        sample = min(1.0, sum(event.split != "SHADOW_FORWARD" for event in group) / 100.0)
        contract = float(station_quality[station]["train_match_rate"])
        median_volume = sorted(event.volume for event in group)[len(group) // 2]
        liquidity = min(1.0, math.log1p(median_volume) / math.log1p(250_000.0))
        if val_metrics["brier"] is None or test_metrics["brier"] is None:
            stability = 0.0
        else:
            stability = max(0.0, 1.0 - abs(float(test_metrics["brier"]) - float(val_metrics["brier"])) / max(0.10, float(val_metrics["brier"])))
        components = {
            "predictability": predictability,
            "sample": sample,
            "contract_fidelity": contract,
            "forecast_coverage": coverage,
            "liquidity_proxy": liquidity,
            "stability": stability,
        }
        score = 100.0 * sum(SCORE_WEIGHTS[key] * components[key] for key in SCORE_WEIGHTS)
        result.append(
            {
                "city": city,
                "market_type": market_type,
                "station_id": station,
                "unit": unit,
                "events": len(group),
                "train": sum(event.split == "TRAIN" for event in group),
                "validation": sum(event.split == "VALIDATION" for event in group),
                "test": test_population,
                "selected_d1_candidate": candidate,
                "test_d1_n": test_metrics["n"],
                "test_d1_mae": test_metrics["mae"],
                "test_d1_bucket_accuracy": test_metrics["bucket_accuracy"],
                "test_d1_brier": test_metrics["brier"],
                "train_resolution_match_rate": contract,
                "median_event_volume_proxy": median_volume,
                **{f"component_{key}": value for key, value in components.items()},
                "forecastability_score": score,
                "sample_grade": "STRONG" if len(group) >= 100 else "PROVISIONAL" if len(group) >= 30 else "INSUFFICIENT",
            }
        )
    return sorted(result, key=lambda row: (-row["forecastability_score"], -row["events"], row["city"]))


def _timing_tables(database: sqlite3.Connection, events: dict[str, Event], root: Path) -> dict[str, Any]:
    derived = root / "derived"
    extreme_groups: dict[tuple[str, str, int], list[float]] = defaultdict(list)
    for row in database.execute("SELECT * FROM observations_daily"):
        matching = [event for event in events.values() if event.station_id == row["station_id"] and event.date == row["date"] and event.split == "TRAIN"]
        for event in matching:
            value = row["max_time_local"] if event.market_type == "HIGHEST" else row["min_time_local"]
            if not value:
                continue
            parsed = datetime.fromisoformat(value)
            hour = parsed.hour + parsed.minute / 60.0
            extreme_groups[(event.station_id, event.market_type, int(event.date[5:7]))].append(hour)
    extreme_rows = []
    for (station, market_type, month), hours in extreme_groups.items():
        extreme_rows.append(
            {
                "station_id": station,
                "market_type": market_type,
                "month": month,
                "train_days": len(hours),
                "p10_hour": _quantile(hours, 0.10),
                "median_hour": _quantile(hours, 0.50),
                "p90_hour": _quantile(hours, 0.90),
                "prob_extreme_by_12": _mean(hour <= 12 for hour in hours),
                "prob_extreme_by_15": _mean(hour <= 15 for hour in hours),
                "prob_extreme_by_18": _mean(hour <= 18 for hour in hours),
            }
        )
    write_csv(derived / "extreme_timing_by_station_month.csv", sorted(extreme_rows, key=lambda row: (row["station_id"], row["market_type"], row["month"])))

    observations: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for row in database.execute("SELECT station_id,local_date,valid_local,temp_c FROM observations_hourly ORDER BY station_id,valid_local"):
        parsed = datetime.fromisoformat(row["valid_local"])
        observations[(row["station_id"], row["local_date"])].append((parsed.hour + parsed.minute / 60.0, float(row["temp_c"])))
    already_set: dict[tuple[str, str, int], list[tuple[int, float]]] = defaultdict(list)
    for event in events.values():
        day = observations.get((event.station_id, event.date), [])
        if not day:
            continue
        for cutoff in (6, 9, 12, 15, 18, 21):
            seen = [temperature for hour, temperature in day if hour <= cutoff]
            if not seen:
                continue
            current_c = max(seen) if event.market_type == "HIGHEST" else min(seen)
            current = current_c * 9.0 / 5.0 + 32.0 if event.unit == "°F" else current_c
            current = round(current / event.precision) * event.precision
            current_bucket = next(
                (
                    bucket["label"]
                    for bucket in event.buckets
                    if (bucket.get("lower") is None or current >= float(bucket["lower"]))
                    and (bucket.get("upper") is None or current <= float(bucket["upper"]))
                ),
                None,
            )
            remaining = event.target - current if event.market_type == "HIGHEST" else current - event.target
            already_set[(event.market_type, event.split, cutoff)].append((int(current_bucket == event.winner_bucket), max(0.0, remaining)))
    set_rows = []
    for (market_type, split, cutoff), values in already_set.items():
        set_rows.append(
            {
                "market_type": market_type,
                "split": split,
                "local_cutoff_hour": cutoff,
                "events": len(values),
                "prob_winning_bucket_already_set": _mean(value[0] for value in values),
                "mean_remaining_extreme_change": _mean(value[1] for value in values),
                "p90_remaining_extreme_change": _quantile((value[1] for value in values), 0.90),
            }
        )
    write_csv(derived / "intraday_already_set.csv", sorted(set_rows, key=lambda row: (row["market_type"], row["split"], row["local_cutoff_hour"])))
    return {"extreme_timing_groups": len(extreme_rows), "already_set_groups": len(set_rows)}


def run_modeling(root: Path) -> dict[str, Any]:
    root = root.resolve()
    derived = root / "derived"
    database, events, station_quality, eligible = _load_inputs(root)
    write_csv(derived / "station_train_quality_gate.csv", sorted(station_quality.values(), key=lambda row: (-int(row["eligible"]), -row["train_match_rate"], -row["train_comparable"], row["station_id"])))
    points = _forecast_points(database, events)
    local, global_, calibration_rows = _calibrate(points, events)
    write_csv(derived / "forecast_calibration_train.csv", calibration_rows)
    base = _base_predictions(points, events, local, global_)
    weights = _train_weights(base, events)
    all_predictions = _ensemble_predictions(base, events, weights)
    validation = _validation_comparison(all_predictions, events)
    write_csv(derived / "validation_model_comparison.csv", validation)
    selection = _select(validation)
    if not selection:
        database.close()
        raise RuntimeError("Ninguna combinación tipo-horizonte superó el gate de selección")
    selected_pairs, unavailable_pairs = _selection_coverage(selection)
    selection_payload = {
        "schema": SCHEMA,
        "created_at": iso(utc_now()),
        "test_opened": False,
        "rules": {
            "station_train_comparable_min": MIN_STATION_TRAIN_COMPARABLE,
            "station_train_match_rate_min": MIN_STATION_TRAIN_MATCH_RATE,
            "local_calibration_min": MIN_LOCAL_CALIBRATION,
            "selection_min_validation_events": MIN_SELECTION_EVENTS,
            "selection_metric": "minimum multiclass Brier on common eight-model validation pool",
        },
        "eligible_stations": sorted(eligible),
        "excluded_stations": sorted(set(station_quality) - eligible),
        "selection": selected_pairs,
        "unavailable_pairs": unavailable_pairs,
    }
    selection_path = derived / "model_selection.json"
    write_json(selection_path, selection_payload)
    selection_hash_before_test = sha256(selection_path)

    test_predictions = _selected_predictions(all_predictions, events, selection, "TEST")
    test_metrics = _selected_metrics_rows(test_predictions, events)
    write_csv(derived / "test_selected_performance.csv", test_metrics)
    write_csv(derived / "test_selected_event_predictions.csv", _event_prediction_rows(test_predictions, events))
    scorecard = _forecastability_scorecard(all_predictions, events, selection, station_quality)
    write_csv(derived / "climate_forecastability_scorecard.csv", scorecard)
    timing = _timing_tables(database, events, root)
    database.close()
    summary = {
        "schema": SCHEMA,
        "completed_at": iso(utc_now()),
        "selection_sha256_before_test": selection_hash_before_test,
        "eligible_stations": len(eligible),
        "excluded_stations": len(set(station_quality) - eligible),
        "eligible_matched_events": len(events),
        "forecast_event_model_rows": len(points),
        "validation_comparison_rows": len(validation),
        "selected_type_lead_pairs": len(selection),
        "no_evaluable_type_lead_pairs": len(unavailable_pairs),
        "unavailable_pairs": unavailable_pairs,
        "test_selected_predictions": len(test_predictions),
        "test_metrics": test_metrics,
        "top_scorecard": scorecard[:20],
        "timing": timing,
        "score_weights": SCORE_WEIGHTS,
    }
    write_json(derived / "modeling_summary.json", summary)
    return summary
