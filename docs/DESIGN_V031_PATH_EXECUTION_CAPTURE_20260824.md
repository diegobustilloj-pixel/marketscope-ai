# Diseño V0.31 — captura de trayectoria y ejecución

Fecha: 2026-08-24 (hora de Bolivia)

## Decisión

Los archivos existentes no permiten evaluar honestamente una estrategia con
entrada y salida intramercado. La decisión es:

`BUILD_CAPTURE_ONLY_V031_BEFORE_ANY_NEW_STRATEGY_TEST`.

Esto no lanza una estrategia. Define una prueba técnica de una hora que debe
demostrar primero que se capturan conjuntamente trayectoria, profundidad,
Chainlink y el TWAP exacto del contrato.

## Qué existe en disco

### V0.18

- 287 mercados y 85.520 filas por segundo;
- entre 294 y 298 filas por mercado, media `297,97909408`;
- 85.471 filas con bid/ask de ambos resultados;
- 72.965 filas con profundidad bid de ambos resultados;
- no guarda profundidad ask, trayectoria Chainlink, TWAP oficial ni contrato
  de resolución en esas filas.

### V0.19

- 287 mercados y 85.506 filas por segundo;
- entre 288 y 298 filas por mercado, media `297,93031359`;
- 85.449 filas con bid/ask de ambos resultados;
- 73.662 filas con profundidad bid de ambos resultados;
- tiene las mismas ausencias críticas que V0.18.

### Forwards con TWAP

- V0.9.4a: 1.915 features, 5.755 diagnósticos y 543.063 ticks TWAP30;
- V0.27: 285 features y 161.162 ticks TWAP30/TWAP60;
- V0.29: 282 features y 81.323 ticks TWAP60.

Estos archivos conservan cortes de decisión, no el camino completo del libro
con profundidad ejecutable durante cada mercado.

### Base cruda de dos horas

Contiene 25 mercados, 99.177 eventos `book`, 3.821.065 `price_change`,
134.422 `best_bid_ask` y 6.847 eventos Chainlink. No contiene eventos TWAP
oficiales y no es compatible por sí sola con el contrato TWAP60 actual.

## Por qué no se deben combinar

Unir la trayectoria V0.18 con el TWAP o Chainlink de otra fecha crearía un
mercado sintético que nunca existió. Unir features T-60 de V0.29 con libros de
V0.18 produciría el mismo problema. Además, todas esas muestras ya fueron
observadas y no constituyen validación fresca.

Ninguna base individual cumple simultáneamente:

- trayectoria CLOB de cada mercado;
- profundidad bid y ask de ambos resultados;
- spot Chainlink;
- TWAP exacto de la ventana oficial;
- contrato de resolución por mercado;
- muestra futura independiente.

## Contrato técnico V0.31

La primera etapa será únicamente una captura de una hora:

- 12 mercados BTC Up/Down 5m esperados;
- una instantánea por segundo durante los 300 segundos de cada mercado;
- cinco niveles bid y ask con precio y tamaño para Up y Down;
- spot Chainlink con timestamp de fuente y recepción;
- TWAP oficial seleccionado de forma fail-closed con ventana, timestamp de
  fuente y recepción;
- timestamp del libro, salud de feeds y conteo de huecos;
- latencia futura de simulación fijada en un segundo;
- cero señales, cero outcomes, cero PnL y cero órdenes.

La prueba técnica no puede aprobar una estrategia. Si pasa, sólo permite
diseñar una única regla de trayectoria y pedir autorización separada para una
muestra económica futura de hasta 24 horas.

## Estado de implementación

Después de congelar el contrato se construyeron, como archivos nuevos:

- collector V0.31 con libro incremental y cinco niveles por lado;
- snapshot conjunto de Chainlink, TWAP exacto y ambos libros a 1 Hz;
- base SQLite exclusiva sin columnas de labels, outcomes, señales, PnL u
  órdenes;
- auditor terminal de sólo lectura;
- entrypoint con estado, build, captura y auditoría;
- bloqueo por manifiesto de lanzamiento separado.

La implementación quedó sellada como
`BUILT_TESTED_AWAITING_LAUNCH_APPROVAL`. La aprobación no existe y la base de
captura tampoco: `--run` se probó y se detiene antes de crearla con
`NOT_LAUNCHED`.

La estrategia económica continúa sin construir. Una captura técnica aprobada
no podrá promover paper ni dinero real.

Pruebas específicas de collector/runner/auditor: `10/10`. Suite completa:
`308/308`.

## Seguridad

- `orders_enabled=false`;
- `paper_orders=0`;
- `outcomes_read=0`;
- `pnl_calculated=false`;
- `wallet_required=false`;
- `real_money=BLOQUEADO`;
- `scheduled_supervision=false`;
- `automatic_followup_launch=false`.

Artefactos:

- `src/polymarket_bot/v031_data_readiness.py`;
- `src/polymarket_bot/v031_prereg.py`;
- `src/polymarket_bot/v031_capture.py`;
- `src/polymarket_bot/v031_runner.py`;
- `src/polymarket_bot/v031_audit.py`;
- `v031_data_readiness_report.py`;
- `v031_prepare.py`;
- `v031_monitor.py`;
- `tests/test_v031_data_readiness.py`;
- `tests/test_v031_capture.py`;
- `tests/test_v031_runner_audit.py`;
- `data/diagnostico_v031_path_execution_readiness.json`.
- `data/prereg_v031_path_execution_capture.json`;
- `data/implementation_v031_path_execution_capture.json`.
