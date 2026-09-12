# Climate V001 — congelación de selección antes de TEST

Fecha de congelación: 2026-09-02, antes de calcular métricas predictivas en TEST.

## Datos admitidos

- Solo mercados diarios `Highest temperature` y `Lowest temperature` capturados antes del corte ya registrado.
- La fecha objetivo es la fecha local escrita en el título del contrato; `endDate` se conserva únicamente como cierre UTC.
- Se exige que, en TRAIN, la reconstrucción independiente del resultado tenga al menos 30 comparables y una coincidencia mínima de 95% por estación. Las estaciones que no superen este control se excluyen completas; no se seleccionan únicamente sus días coincidentes.
- Dentro de estaciones elegibles, solo se entrenan/evalúan eventos cuya observación reconstruida coincide con el bucket oficial.
- TRAIN ajusta sesgo, dispersión y pesos. VALIDATION selecciona. TEST permanece cerrado hasta escribir `model_selection.json`.

## Candidatos fijos

- Ocho pronósticos archivados de Open-Meteo Previous Runs: ECMWF IFS, ECMWF AIFS single, GFS, ICON, GEM, JMA, UKMO y CMA GRAPES.
- Cada modelo se corrige por sesgo usando TRAIN. Se usa calibración local estación/tipo/unidad cuando hay al menos 20 ejemplos; de lo contrario, respaldo global por tipo/unidad/modelo/horizonte.
- Probabilidades por bucket: distribución normal de error empírico de TRAIN, respetando la precisión y los límites abiertos/cerrados del contrato.
- Ensamble simple: media de probabilidades disponibles.
- Ensamble ponderado: pesos inversos al Brier de TRAIN, normalizados entre modelos disponibles.

## Regla de selección

- Selección separada por `HIGHEST`/`LOWEST` y horizonte D+1…D+7.
- Comparación en el conjunto común de eventos de VALIDATION con cobertura de los ocho modelos, mínimo 30 eventos.
- Criterio primario: menor Brier multicategoría medio. Desempate: menor log-loss y luego nombre estable.
- Se escribe la selección y su hash antes de abrir TEST.
- TEST informa únicamente el candidato previamente seleccionado, su cobertura, MAE, RMSE, sesgo, precisión de bucket, Brier y log-loss. No se cambia el ganador después de ver TEST.

## Decisión económica

- Las métricas meteorológicas no bastan para afirmar rentabilidad.
- No se declarará EV ejecutable positivo sin precios ask, profundidad, spread, fees y latencia disponibles en el instante histórico de decisión.
- Si solo existe precio negociado o midpoint histórico, el análisis económico se marcará explícitamente como exploratorio/no ejecutable.

