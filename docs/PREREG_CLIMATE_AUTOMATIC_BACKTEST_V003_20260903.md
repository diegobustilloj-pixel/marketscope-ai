# PRERREGISTRO — CLIMATE AUTOMATIC REALISTIC BACKTEST V003

Congelado: 2026-09-03T13:08:46-04:00 (America/La_Paz), antes de descargar el
historial económico por token para este estudio.

## Alcance y honestidad

Este estudio prueba una ejecución automática simulada de la señal climática ya
seleccionada. No afirma reconstruir un libro L2 histórico: la API pública conserva
una serie histórica de precio, pero no el spread, la profundidad ni la posición en
cola de cada instante. Por ello se publicarán tres escenarios de coste y la
clasificación de evidencia será `L1_PRICE_PROXY`, nunca `EXECUTABLE_L2`.

El conjunto TEST y sus resultados meteorológicos ya habían sido abiertos durante
la investigación V001. Esto impide llamar a V003 una prueba ciega integral. Sí es
una prueba confirmatoria de la regla económica porque los precios históricos no se
habían descargado ni usado para escoger los umbrales siguientes. Ningún umbral se
modificará después de observar el PnL TEST.

## Entradas congeladas

- Modelo: `model_selection.json`, SHA-256
  `a41ee83c94bc8f16b07de5a0eb5e0851d168a4f1994ebfe54d0b3b89934b64d1`.
- Predicciones TEST: `test_selected_event_predictions.csv`, SHA-256
  `2cad2d23c998943224f11534d3975e41761b1a73bed7b0748868b32b32a9f4e1`.
- Mercados: `markets.csv`, SHA-256
  `6dcc3975372b03586ea503caa0875f127df694ad5f2b6e8d273a5db55dd4223b`.
- Verdad meteorológica: `climate_weather.db`, SHA-256
  `96d26e1966adb7bcb1d1381a148aabf498c5f7e33bf2ac50cb90b67429f639d8`.
- Contratos Gamma: `events.jsonl`, SHA-256
  `03cb17971bb7b3027df6eafb5c3d3d61cae8f76a48866392fb0597da62924610`.

## Universo y señal

- Partición `TEST` exclusivamente.
- Pronóstico D+1 exclusivamente.
- Estaciones `STRONG`, al menos 30 reconstrucciones TRAIN y coincidencia TRAIN
  mínima de 98%.
- Una única compra YES: la cubeta de mayor probabilidad del modelo.
- Primera referencia de precio entre 15:00 y 15:05, hora local de la estación, el
  día anterior a la fecha del contrato.
- Probabilidad mínima del modelo: 30%.
- Precio simulado permitido: 0.10 a 0.75.
- Ventaja neta mínima: 5 puntos porcentuales.
- Una entrada por evento y mantenimiento hasta resolución.

## Precio y ejecución

La referencia será el primer punto de `GET /prices-history` observado desde las
15:00 locales, con fidelidad de un minuto y retraso máximo de cinco minutos.

- Escenario favorable: referencia + 0.01.
- Escenario base primario: referencia + 0.03.
- Escenario severo: referencia + 0.05.

El incremento se redondeará hacia arriba al tick del contrato. La comisión taker
se calculará con el `feeSchedule` del contrato:

`fee/share = rate × (price × (1-price))^exponent`.

La falta de historial, contrato, fee verificable, token, timestamp o tick produce
exclusión explícita; nunca imputación silenciosa.

## Capital automático simulado

- Capital inicial: 1,000 USDC.
- Presupuesto total por operación, comisión incluida: 25 USDC.
- Máximo diez posiciones simultáneas (250 USDC de exposición).
- Sin apalancamiento, sin ventas, sin reentradas y sin reinversión por encima de 25
  USDC.
- Cuando coincidan señales, prioridad descendente por ventaja neta y luego slug.
- El pago es 1 USDC por participación ganadora y 0 por perdedora.

## Métricas y veredicto

Se publicarán cobertura temporal, retraso de precio, accuracy de cubeta, Brier y
log-loss del modelo, Brier del precio de mercado para el token seleccionado,
operaciones, win rate, PnL, ROI, profit factor, drawdown, capital final y un
intervalo bootstrap por bloques diarios.

`EVIDENCE_PASS` exige en el escenario base: al menos 30 operaciones, PnL y ROI
positivos, profit factor mínimo 1.25 y límite inferior del 95% bootstrap del PnL
total mayor que cero. De lo contrario: `EVIDENCE_FAIL` o `MORE_DATA_REQUIRED`.

Las rejillas de probabilidad, edge o coste distintas de la especificación primaria
son análisis poshoc exploratorios y no pueden reemplazar el resultado primario.
