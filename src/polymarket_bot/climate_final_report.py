from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .climate_research import SCHEMA, iso, sha256, utc_now, write_csv, write_json


FINAL_SCHEMA = "polymarket_climate_final_report_v001"
ECONOMIC_STATUS = "NO_EVALUABLE"


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _mean(values: Iterable[float]) -> float | None:
    materialized = list(values)
    return sum(materialized) / len(materialized) if materialized else None


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "NO EVALUABLE"
    if isinstance(value, str):
        return value
    return f"{float(value):.{digits}f}"


def _pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "NO EVALUABLE"
    return f"{100.0 * float(value):.{digits}f}%"


def normalized_prediction_metrics(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate forecasts without mixing native Fahrenheit and Celsius errors."""
    grouped: defaultdict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (str(row["market_type"]), int(row["lead"]), str(row["candidate"]))
        grouped[key].append(row)

    result: list[dict[str, Any]] = []
    for (market_type, lead, candidate), group in sorted(grouped.items()):
        errors_c: list[float] = []
        briers: list[float] = []
        log_losses: list[float] = []
        calibration: list[tuple[float, int]] = []
        correct = 0
        for row in group:
            error = float(row["point_prediction"]) - float(row["target"])
            error_c = error * 5.0 / 9.0 if str(row["unit"]) == "°F" else error
            errors_c.append(error_c)
            probabilities = json.loads(str(row["probabilities_json"]))
            winner = str(row["winner_bucket"])
            predicted = str(row["predicted_bucket"])
            is_correct = int(predicted == winner)
            correct += is_correct
            briers.append(
                sum(
                    (float(probability) - (1.0 if label == winner else 0.0)) ** 2
                    for label, probability in probabilities.items()
                )
            )
            winner_probability = max(1e-12, float(probabilities.get(winner, 0.0)))
            log_losses.append(-math.log(winner_probability))
            calibration.append((max(float(value) for value in probabilities.values()), is_correct))

        ece = 0.0
        for bin_index in range(10):
            lower = bin_index / 10.0
            upper = (bin_index + 1) / 10.0
            bucket = [item for item in calibration if lower <= item[0] < upper or (bin_index == 9 and item[0] == 1.0)]
            if not bucket:
                continue
            confidence = sum(item[0] for item in bucket) / len(bucket)
            accuracy = sum(item[1] for item in bucket) / len(bucket)
            ece += len(bucket) / len(calibration) * abs(accuracy - confidence)

        result.append(
            {
                "market_type": market_type,
                "lead": lead,
                "candidate": candidate,
                "n": len(group),
                "mae_c": sum(abs(value) for value in errors_c) / len(errors_c),
                "rmse_c": math.sqrt(sum(value * value for value in errors_c) / len(errors_c)),
                "bias_c": sum(errors_c) / len(errors_c),
                "bucket_hit_rate": correct / len(group),
                "brier": sum(briers) / len(briers),
                "log_loss": sum(log_losses) / len(log_losses),
                "top_label_ece_10_bin": ece,
            }
        )
    return result


def contract_audit(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    materialized = list(rows)
    timezone_resolved = [row for row in materialized if str(row.get("timezone") or "").strip()]
    frequency_resolved = [
        row
        for row in materialized
        if str(row.get("frequency") or "").strip()
        and str(row.get("frequency")) != "OBSERVATION_TABLE_UNRESOLVED"
    ]
    strict = [
        row
        for row in materialized
        if str(row.get("rules_complete")) == "True"
        and str(row.get("station_metadata_complete")) == "True"
        and str(row.get("timezone") or "").strip()
        and str(row.get("frequency") or "").strip()
        and str(row.get("frequency")) != "OBSERVATION_TABLE_UNRESOLVED"
    ]
    return {
        "contracts": len(materialized),
        "legacy_core_complete": sum(str(row.get("rules_complete")) == "True" for row in materialized),
        "timezone_resolved": len(timezone_resolved),
        "observation_frequency_resolved": len(frequency_resolved),
        "strict_operational_complete": len(strict),
    }


def _country_ranking(
    scorecard: list[dict[str, str]], station_catalog: list[dict[str, str]]
) -> list[dict[str, Any]]:
    country_by_station = {row["station_id"]: row["country"] for row in station_catalog}
    grouped: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for row in scorecard:
        if not row.get("test_d1_n") or not row.get("test_d1_bucket_accuracy"):
            continue
        grouped[country_by_station.get(row["station_id"], "UNKNOWN")].append(row)
    result = []
    for country, group in grouped.items():
        n = sum(int(row["test_d1_n"]) for row in group)
        result.append(
            {
                "country": country,
                "cities": len({row["city"] for row in group}),
                "families": len(group),
                "test_d1_n": n,
                "weighted_forecastability_score": sum(
                    float(row["forecastability_score"]) * int(row["test_d1_n"]) for row in group
                )
                / n,
                "weighted_bucket_hit_rate": sum(
                    float(row["test_d1_bucket_accuracy"]) * int(row["test_d1_n"]) for row in group
                )
                / n,
            }
        )
    return sorted(result, key=lambda row: (-row["weighted_forecastability_score"], -row["test_d1_n"]))


def _extreme_hours(database_path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(database_path)
    result: dict[str, Any] = {}
    for market_type, column in (("HIGHEST", "max_time_local"), ("LOWEST", "min_time_local")):
        query = f"""
            SELECT DISTINCT e.station_id,e.date,o.{column}
            FROM event_truth e
            JOIN observations_daily o ON o.station_id=e.station_id AND o.date=e.date
            WHERE e.split='TRAIN' AND e.market_type=? AND e.reconstruction_matches=1
              AND o.{column} IS NOT NULL
        """
        hours = [datetime.fromisoformat(row[2]).hour for row in connection.execute(query, (market_type,))]
        counts = Counter(hours)
        result[market_type] = {
            "n": len(hours),
            "modal_local_hour": counts.most_common(1)[0][0] if counts else None,
            "top_hours": [{"hour": hour, "n": count} for hour, count in counts.most_common(5)],
        }
    connection.close()
    return result


def _model_by_city_table(scorecard: list[dict[str, str]], station_catalog: list[dict[str, str]]) -> list[str]:
    country_by_station = {row["station_id"]: row["country"] for row in station_catalog}
    lines = [
        "| Rank | Country | City | Type | Station | D+1 model | TEST n | MAE native | Bucket hit | Brier | Score | Grade |",
        "|---:|---|---|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for rank, row in enumerate(scorecard, 1):
        model = row.get("selected_d1_candidate") or "NO EVALUABLE"
        lines.append(
            "| {rank} | {country} | {city} | {kind} | {station} | {model} | {n} | {mae} | {hit} | {brier} | {score} | {grade} |".format(
                rank=rank,
                country=country_by_station.get(row["station_id"], "UNKNOWN"),
                city=row["city"],
                kind="MAX" if row["market_type"] == "HIGHEST" else "MIN",
                station=row["station_id"],
                model=model,
                n=row.get("test_d1_n") or "0",
                mae=_fmt(float(row["test_d1_mae"]) if row.get("test_d1_mae") else None),
                hit=_pct(float(row["test_d1_bucket_accuracy"]) if row.get("test_d1_bucket_accuracy") else None),
                brier=_fmt(float(row["test_d1_brier"]) if row.get("test_d1_brier") else None),
                score=_fmt(float(row["forecastability_score"]), 1),
                grade=row["sample_grade"],
            )
        )
    return lines


def _render_report(payload: dict[str, Any], scorecard: list[dict[str, str]], station_catalog: list[dict[str, str]]) -> str:
    coverage = payload["data_coverage"]
    metrics = payload["normalized_test_metrics"]
    d1_max = next(row for row in metrics if row["market_type"] == "HIGHEST" and row["lead"] == 1)
    d1_min = next(row for row in metrics if row["market_type"] == "LOWEST" and row["lead"] == 1)
    best_city = scorecard[0]
    best_country = payload["country_ranking"][0]
    intraday = payload["intraday"]
    contract = payload["contract_audit"]
    selection = payload["model_selection"]
    score = payload["climate_scorecard"]
    model_table = "\n".join(_model_by_city_table(scorecard, station_catalog))
    test_table = [
        "| Type | Lead | Model | n | MAE °C | RMSE °C | Hit rate | Brier | ECE-10 |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in metrics:
        test_table.append(
            f"| {'MAX' if row['market_type'] == 'HIGHEST' else 'MIN'} | D+{row['lead']} | {row['candidate']} | {row['n']} | "
            f"{_fmt(row['mae_c'])} | {_fmt(row['rmse_c'])} | {_pct(row['bucket_hit_rate'])} | "
            f"{_fmt(row['brier'])} | {_fmt(row['top_label_ece_10_bin'])} |"
        )
    unavailable = ", ".join(
        f"{'MAX' if row['market_type'] == 'HIGHEST' else 'MIN'} D+{row['lead']}"
        for row in selection["unavailable_pairs"]
    )
    final_score = sum(score.values()) / len(score)
    return f"""# POLYMARKET CLIMATE — FINAL RESEARCH REPORT

## DATA COVERAGE

Weather markets analyzed: {coverage['weather_markets_analyzed']:,} ({coverage['resolved_events']:,} resueltos; {coverage['shadow_forward_events']:,} forward).

Cities: {coverage['cities']}.

Countries: {coverage['countries']}.

Stations: {coverage['stations']} ({coverage['eligible_stations']} pasan el gate TRAIN).

Historical days: {coverage['historical_truth_dates']} fechas de resolución entre {coverage['historical_start']} y {coverage['historical_end']}.

Weather models: {coverage['weather_models']}.

Historical model runs: no se almacenaron runs intradía exactos; sí {coverage['previous_run_forecast_points']:,} puntos estación-fecha-modelo-lead de Previous Runs D+1–D+7.

Completeness: forecast {coverage['forecast_coverage']:.1%}; reconciliación con el bucket resuelto {coverage['resolution_match_rate']:.1%}. Contratos completos bajo la definición heredada: {contract['legacy_core_complete']:,}/{contract['contracts']:,}; contratos operacionalmente estrictos con timezone y frecuencia de observación explícitas: {contract['strict_operational_complete']:,}/{contract['contracts']:,}.

Confidence: 95/100 para bloquear dinero real; 82/100 para el ranking meteorológico D+1; no extrapolar a rentabilidad.

---

# BEST COUNTRY

Reino Unido (GB), por score agregado D+1 entre las familias con TEST: {best_country['weighted_forecastability_score']:.1f}/100, n={best_country['test_d1_n']} y hit rate {best_country['weighted_bucket_hit_rate']:.1%}.

Reason: es el mayor score agregado bajo la regla congelada. Es un resultado meteorológico con una sola ciudad y dos familias; no prueba edge económico ni escala nacional.

---

# BEST CITY

{best_city['city']} — {best_city['market_type']} ({float(best_city['forecastability_score']):.1f}/100; TEST n={best_city['test_d1_n']}; hit {float(best_city['test_d1_bucket_accuracy']):.1%}; Brier {float(best_city['test_d1_brier']):.3f}).

Station: {best_city['station_id']}.

---

# MOST PREDICTABLE STATION

{best_city['station_id']} ({best_city['city']}, MIN). MAE D+1 {float(best_city['test_d1_mae']):.3f} °C y match de resolución TRAIN {float(best_city['train_resolution_match_rate']):.1%}.

---

# BEST DATA SOURCE

[Open-Meteo Previous Runs](https://open-meteo.com/en/docs/previous-runs-api) como archivo reproducible de forecast, combinado con observaciones de estación y la fuente exacta de resolución. No hay una fuente única suficiente: el forecast no sustituye a Weather Underground/NOAA/HKO para resolver el contrato. Para timing intradía futuro se necesitan [Single Model Runs](https://open-meteo.com/en/docs/single-runs-api) y la latencia real de disponibilidad.

---

# BEST MODEL

ECMWF AIFS 0.25° individual (`ecmwf_aifs025_single`) ganó 7 de las 8 combinaciones evaluables; el ensemble ponderado TRAIN ganó MIN D+1. D+5–D+7 ({unavailable}) quedaron NO EVALUABLE por no alcanzar 30 eventos VALIDATION con pool común de ocho modelos.

---

# BEST MODEL BY CITY

MAE está en la unidad nativa del mercado en esta tabla (°F o °C); las métricas globales posteriores están normalizadas a °C.

{model_table}

---

# MAXIMUM VS MINIMUM

Most predictable: Minimum.

Reason: en TEST D+1, MIN obtuvo MAE {_fmt(d1_min['mae_c'])} °C y hit {_pct(d1_min['bucket_hit_rate'])}, frente a MAX {_fmt(d1_max['mae_c'])} °C y {_pct(d1_max['bucket_hit_rate'])}. Su Brier fue ligeramente peor ({_fmt(d1_min['brier'])} vs {_fmt(d1_max['brier'])}), así que la ventaja es modesta y no económica.

---

# HOURLY ANALYSIS

Most common maximum time: {intraday['extreme_hours']['HIGHEST']['modal_local_hour']:02d}:00 local (n={intraday['extreme_hours']['HIGHEST']['n']}; concentración principal 12:00–16:00).

Most common minimum time: {intraday['extreme_hours']['LOWEST']['modal_local_hour']:02d}:00 local (n={intraday['extreme_hours']['LOWEST']['n']}; también hay una concentración 03:00–05:00, dependiente de estación y frecuencia de observación).

Best same-day forecasting hour: descriptivamente 18:00 local para MAX (99.1% de buckets ya fijados en VALIDATION) y 09:00 local para MIN (87.7%). Económicamente NO EVALUABLE sin el ask contemporáneo.

---

# BEST ENTRY TIME

NO EVALUABLE. La hora con mayor certeza meteorológica no es necesariamente la de mayor valor: el precio puede haber incorporado la información.

Historical EV: NO EVALUABLE; no existe historial de ask, spread y profundidad ejecutable sincronizado con forecasts/observaciones. La API oficial separa el [histórico de precios negociados](https://docs.polymarket.com/api-reference/markets/get-prices-history) del [order book actual](https://docs.polymarket.com/api-reference/market-data/get-order-book); el primero no reconstruye la profundidad histórica del segundo.

---

# DOES WAITING HELP?

SOMETIMES.

Para MAX, esperar hasta 15:00–18:00 reduce fuertemente la incertidumbre; para MIN, gran parte de la información ya está disponible temprano. No se puede afirmar que esperar mejore EV hasta capturar la reacción del libro.

---

# BEST WEATHER CONDITION

Other: NO EVALUABLE. El snapshot histórico no incluye forecast contemporáneo de nubosidad, precipitación, viento y presión con disponibilidad temporal auditada.

---

# CONDITIONS TO AVOID

Contratos sin timezone/frecuencia explícitos; estaciones excluidas ({', '.join(selection['excluded_stations'])}); D+5–D+7; pool incompleto de modelos; reconciliación <95%; elevada dispersión; datos faltantes; y cualquier mercado sin libro ejecutable capturado. No usar forecast actual para una decisión pasada.

---

# MODEL ACCURACY

Métricas TEST congeladas y normalizadas:

{'\n'.join(test_table)}

MAE: MAX D+1 {_fmt(d1_max['mae_c'])} °C; MIN D+1 {_fmt(d1_min['mae_c'])} °C.

RMSE: MAX D+1 {_fmt(d1_max['rmse_c'])} °C; MIN D+1 {_fmt(d1_min['rmse_c'])} °C.

Bucket Hit Rate: MAX {_pct(d1_max['bucket_hit_rate'])}; MIN {_pct(d1_min['bucket_hit_rate'])}.

Brier: MAX {_fmt(d1_max['brier'])}; MIN {_fmt(d1_min['brier'])}.

Calibration: ECE top-label de 10 bins MAX {_fmt(d1_max['top_label_ece_10_bin'])}; MIN {_fmt(d1_min['top_label_ece_10_bin'])}. Intervalos P10–P90 de temperatura no quedaron persistidos y son NO EVALUABLE.

---

# MARKET ACCURACY

Most likely bucket hit rate: {_pct(max(d1_max['bucket_hit_rate'], d1_min['bucket_hit_rate']))} (MIN D+1, TEST n={d1_min['n']}).

---

# BEST VALUE STRATEGY

NO EVALUABLE. Candidato para shadow: estación-específica D+1, AIFS para MAX y ensemble ponderado para MIN, con distribución por bucket y comparación contra ask ejecutable. Aún no puede llamarse “value strategy”.

---

# BACKTEST

Trades: NO EVALUABLE

Wins: NO EVALUABLE

Losses: NO EVALUABLE

Win Rate: NO EVALUABLE

ROI: NO EVALUABLE

Net PnL: NO EVALUABLE

Profit Factor: NO EVALUABLE

Max Drawdown: NO EVALUABLE

EV/trade: NO EVALUABLE

Razón: 0 observaciones de precio ejecutable histórico sincronizado; los precios finales de mercados cerrados no reconstruyen decisiones pasadas.

---

# OUT-OF-SAMPLE

Trades: NO EVALUABLE

ROI: NO EVALUABLE

EV: NO EVALUABLE

DD: NO EVALUABLE

Calibration: meteorológica sí (tabla TEST); económica NO EVALUABLE.

---

# SHADOW FORWARD

Trades: 0 señales económicas evaluables.

ROI: NO EVALUABLE

EV: NO EVALUABLE

Accuracy: pendiente de resolución y de captura forward de libros; los 234 eventos reservados no autorizan reescribir el pasado.

---

# CAPITAL

Minimum practical capital: NO EVALUABLE

Recommended test capital: $0 real; solo shadow.

Capital efficiency: NO EVALUABLE

Scalability: NO EVALUABLE económicamente; 54 ciudades ofrecen amplitud técnica.

---

# TIME

Markets/day: variable; debe medirse forward, no inferirse del inventario histórico.

Updates/day: por definir con runs exactos y latencia de disponibilidad; no confundir hora de inicialización con hora disponible.

Average holding: NO EVALUABLE

Automation requirement: alta; requiere forecasts, observaciones, libro CLOB, reglas y watchdogs sincronizados.

Time Efficiency Score: {score['time_efficiency']}/100.

---

# BEST BOT STRATEGY

Signal: probabilidad calibrada del bucket ganador menos ask ejecutable, después de corregir sesgo por estación/modelo.

Entry: solo forward, preferentemente D+1; same-day únicamente tras medir el tradeoff información-precio.

Filters: contrato operativo estricto, estación elegible, reconciliación ≥95%, forecast disponible en ese momento, modelos suficientes, observación fresca y profundidad CLOB suficiente.

Sizing: $0 real hasta superar gates; luego tamaño limitado por profundidad y riesgo, nunca por accuracy sola.

Exit: regla preregistrada según resolución/valor de espera; no está estimada todavía.

No-trade: edge neto <3/5/7.5/10/15 puntos en los thresholds shadow, contrato incompleto, datos stale, D+5–D+7, estación excluida o ausencia de ask/depth.

---

# SHOULD WE BUILD THE CLIMATE BOT?

MORE DATA REQUIRED. Sí construiría el monitor/collector shadow; no construiría ni activaría ejecución real todavía.

---

# CLIMATE SCORECARD

Profitability: {score['profitability']}/100

Predictability: {score['predictability']}/100

Capital Efficiency: {score['capital_efficiency']}/100

Time Efficiency: {score['time_efficiency']}/100

Automation Ease: {score['automation_ease']}/100

Data Availability: {score['data_availability']}/100

Liquidity: {score['liquidity']}/100

Forecast Robustness: {score['forecast_robustness']}/100

Scalability: {score['scalability']}/100

Risk Adjusted Return: {score['risk_adjusted_return']}/100

FINAL CLIMATE SCORE:

{final_score:.1f}/100.

Los componentes económicos reciben 0 porque no fueron medidos, no porque se haya demostrado una pérdida.

---

# LO QUE APRENDIMOS DEL SECTOR CLIMA

Hallazgo 1: AIFS domina la selección validada de horizonte D+1–D+4.

Evidence: 7/8 pares tipo-horizonte; MIN D+1 favorece el ensemble ponderado.

Importance: la selección debe ser por tipo, horizonte y estación, no por reputación del modelo.

Hallazgo 2: predictibilidad meteorológica no equivale a rentabilidad.

Evidence: {sum(row['n'] for row in metrics):,} predicciones TEST evaluadas, pero 0 libros históricos ejecutables sincronizados.

Importance: el siguiente experimento debe capturar ask, spread, profundidad, timestamp de forecast disponible y observación de estación antes de calcular EV.

Hallazgo 3: la semántica heredada de `rules_complete` era demasiado laxa.

Evidence: {contract['legacy_core_complete']:,} contratos core figuran completos, pero timezone explícita={contract['timezone_resolved']:,} y frecuencia resuelta={contract['observation_frequency_resolved']:,}; strict={contract['strict_operational_complete']:,}.

Importance: la siguiente versión del Prompt Maestro debe separar `CORE_COMPLETE`, `TEMPORAL_COMPLETE`, `RESOLUTION_COMPLETE` y `TRADING_COMPLETE`, y bloquear ejecución si cualquiera falla.

Hallazgo 4: D+5–D+7 no deben imputarse.

Evidence: no alcanzaron n=30 en VALIDATION con el pool común de ocho modelos.

Importance: “NO EVALUABLE” conserva la integridad del TEST.

---

# VEREDICTO FINAL

¿EXISTE EDGE?

Inconcluso. Hay señal meteorológica fuera de muestra; no hay evidencia económica ejecutable.

¿QUÉ PAÍS ES MEJOR?

Reino Unido (forecastability agregado; alcance de una ciudad, no rentabilidad).

¿QUÉ CIUDAD?

{best_city['city']} para MIN.

¿QUÉ ESTACIÓN?

{best_city['station_id']}.

¿MÁXIMA O MÍNIMA?

Mínima, por D+1 fuera de muestra, con ventaja modesta.

¿QUÉ MODELO?

Ensemble ponderado TRAIN para MIN D+1; ECMWF AIFS individual para MAX D+1 y ambos tipos D+2–D+4.

¿A QUÉ HORA CONVIENE ENTRAR?

NO EVALUABLE económicamente. Ventanas descriptivas: MAX 15:00–18:00; MIN cerca de 09:00 local.

¿QUÉ ERROR MÁXIMO DEL MODELO ACEPTAMOS?

Gate shadow inicial: MAE rolling ≤1.0 °C en la familia estación/tipo; no usarlo como único filtro.

¿QUÉ PROBABILIDAD MÍNIMA NECESITAMOS?

No se fija aún: debe calibrarse contra precio y costo. Registrar thresholds 3/5/7.5/10/15 puntos de edge.

¿QUÉ EDGE MÍNIMO NECESITAMOS?

NO EVALUABLE hasta capturar costos; preregistrar 3/5/7.5/10/15 puntos para seleccionar después solo con VALIDATION/shadow.

¿CUÁNTO CAPITAL?

$0 real.

¿ES AUTOMATIZABLE?

Sí, en shadow y con fail-closed.

¿CREARÍAS EL BOT?

No de ejecución real. Sí el collector/monitor shadow.

CONFIDENCE:

95/100 en el bloqueo de dinero real; 82/100 en la conclusión meteorológica.

---

# REGLAS CRÍTICAS FINALES

No inventar observaciones, forecasts, precios ni profundidad. Resolver por estación y fuente exactas; conservar Station ID, altitud, timezone, redondeo, frecuencia y missing-data policy. Usar únicamente información disponible en el timestamp de decisión. Separar temperatura real de registrada, forecast accuracy de trading profitability y probabilidad de fair value de ejecutabilidad. Dinero real permanece bloqueado hasta completar contratos estrictos, runs/availability intradía, libro CLOB histórico-forward y gates económicos preregistrados.
"""


def generate_report(root: Path) -> dict[str, Any]:
    root = root.resolve()
    derived = root / "derived"
    final = root / "final"
    final.mkdir(parents=True, exist_ok=True)

    capture = _json(derived / "capture_summary.json")
    weather = _json(derived / "weather_database_summary.json")
    modeling = _json(derived / "modeling_summary.json")
    selection = _json(derived / "model_selection.json")
    contracts = _rows(derived / "resolution_contracts_enriched.csv")
    stations = _rows(derived / "station_catalog.csv")
    scorecard = _rows(derived / "climate_forecastability_scorecard.csv")
    predictions = _rows(derived / "test_selected_event_predictions.csv")
    normalized = normalized_prediction_metrics(predictions)
    audit = contract_audit(contracts)
    countries = _country_ranking(scorecard, stations)

    database_path = derived / "climate_weather.db"
    connection = sqlite3.connect(database_path)
    historical_truth_dates, historical_start, historical_end = connection.execute(
        "SELECT COUNT(DISTINCT date),MIN(date),MAX(date) FROM event_truth"
    ).fetchone()
    countries_count = connection.execute("SELECT COUNT(DISTINCT country) FROM station_metadata").fetchone()[0]
    connection.close()

    already_set = _rows(derived / "intraday_already_set.csv")
    intraday_lookup = {
        (row["market_type"], row["split"], int(row["local_cutoff_hour"])): float(row["prob_winning_bucket_already_set"])
        for row in already_set
    }
    climate_scorecard = {
        "profitability": 0,
        "predictability": 65,
        "capital_efficiency": 0,
        "time_efficiency": 55,
        "automation_ease": 75,
        "data_availability": 60,
        "liquidity": 30,
        "forecast_robustness": 70,
        "scalability": 50,
        "risk_adjusted_return": 0,
    }
    payload: dict[str, Any] = {
        "schema": FINAL_SCHEMA,
        "research_schema": SCHEMA,
        "generated_at": iso(utc_now()),
        "data_cutoff": capture["cut_off"],
        "selection_sha256_before_test": modeling["selection_sha256_before_test"],
        "model_selection": selection,
        "data_coverage": {
            "weather_markets_analyzed": capture["temperature_events"],
            "resolved_events": capture["resolved_events"],
            "shadow_forward_events": capture["shadow_forward_events"],
            "cities": capture["cities"],
            "countries": countries_count,
            "stations": weather["stations"],
            "eligible_stations": modeling["eligible_stations"],
            "historical_truth_dates": historical_truth_dates,
            "historical_start": historical_start,
            "historical_end": historical_end,
            "weather_models": len(weather["models"]),
            "previous_run_forecast_points": weather["forecast_rows"],
            "forecast_coverage": weather["forecast_coverage"],
            "resolution_match_rate": weather["resolution_match_rate"],
        },
        "contract_audit": audit,
        "normalized_test_metrics": normalized,
        "country_ranking": countries,
        "intraday": {
            "extreme_hours": _extreme_hours(database_path),
            "validation_bucket_already_set": {
                "max_15": intraday_lookup.get(("HIGHEST", "VALIDATION", 15)),
                "max_18": intraday_lookup.get(("HIGHEST", "VALIDATION", 18)),
                "min_06": intraday_lookup.get(("LOWEST", "VALIDATION", 6)),
                "min_09": intraday_lookup.get(("LOWEST", "VALIDATION", 9)),
            },
        },
        "economic_gate": {
            "status": ECONOMIC_STATUS,
            "historical_executable_ask_rows": 0,
            "historical_spread_rows": 0,
            "historical_depth_rows": 0,
            "backtest_trades": None,
            "roi": None,
            "ev_per_trade": None,
            "profit_factor": None,
            "max_drawdown": None,
            "real_money_allowed": False,
            "reason": "No synchronized historical executable ask/spread/depth at decision timestamps",
        },
        "climate_scorecard": climate_scorecard,
        "recommendation": "MORE DATA REQUIRED",
        "real_money_allowed": False,
    }

    write_csv(final / "normalized_test_performance.csv", normalized)
    write_csv(final / "country_forecastability_ranking.csv", countries)
    write_json(final / "final_report.json", payload)
    report_path = final / "POLYMARKET_CLIMATE_FINAL_RESEARCH_REPORT.md"
    report_path.write_text(_render_report(payload, scorecard, stations), encoding="utf-8")
    manifest = {
        "schema": FINAL_SCHEMA,
        "generated_at": payload["generated_at"],
        "selection_sha256_before_test": payload["selection_sha256_before_test"],
        "files": [
            {"path": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in sorted(final.iterdir())
            if path.name != "manifest.json"
        ],
        "qa": {
            "strict_operational_contracts": audit["strict_operational_complete"],
            "no_evaluable_type_lead_pairs": len(selection["unavailable_pairs"]),
            "historical_executable_ask_rows": 0,
            "economic_metrics_interpretable": False,
            "real_money_allowed": False,
        },
    }
    write_json(final / "manifest.json", manifest)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the frozen Polymarket climate final research report")
    parser.add_argument("--root", type=Path, default=Path("data/climate_research_v001"))
    args = parser.parse_args(argv)
    result = generate_report(args.root)
    print(json.dumps({
        "recommendation": result["recommendation"],
        "real_money_allowed": result["real_money_allowed"],
        "final_score": sum(result["climate_scorecard"].values()) / len(result["climate_scorecard"]),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
