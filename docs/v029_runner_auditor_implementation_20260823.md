# V0.29: implementación del runner y auditor holdout

Fecha local: 2026-08-23

## Estado

- Diseño y preinscripción: congelados.
- Runner, collector, monitor y auditor: construidos y probados.
- Implementación: `BUILT_TESTED_AWAITING_LAUNCH_APPROVAL`.
- Lanzamiento: `NOT_STARTED`.
- Aprobación separada: ausente.
- Base y resultado V0.29: ausentes.
- Dinero real: `BLOQUEADO`.

Construir la infraestructura no concede autorización para iniciar la ventana. El
runner exige una aprobación separada ligada por hash a la preinscripción y al
manifiesto definitivo de implementación.

## Archivos

- `src/polymarket_bot/v029_forward.py`: captura y SQLite independientes V0.29.
- `src/polymarket_bot/v029_runner.py`: sellos, bloqueo, checkpoints técnicos y
  duración.
- `src/polymarket_bot/v029_audit.py`: ajuste terminal y auditoría query-only.
- `src/polymarket_bot/v029_strategy.py`: modelo y selección económica congelados.
- `v029_monitor.py`: estado, construcción, ejecución y auditoría.
- `tests/test_v029_runner_audit.py`: pruebas de aislamiento, fuga temporal,
  seguridad y resultado terminal.

## Captura y contrato de datos

- Máximo 24 horas de mercados BTC Up/Down de cinco minutos.
- Decisión a 60 segundos del cierre.
- Solo TWAP oficial exacto de 60 segundos, sin fallback.
- TWAP de apertura y decisión con antigüedad máxima de cinco segundos.
- Feeds Binance, Chainlink, CLOB y TWAP supervisados.
- Tablas y metadatos `v029_*`; no reutiliza la base V0.28.
- Los checkpoints muestran cobertura y salud, nunca labels, PnL ni victorias.
- Ningún modelo se ajusta durante la colección.

## Holdout terminal

- Primeras 12 horas: ajuste del único pipeline congelado.
- Segundas 12 horas: validación sellada; no participa en imputación, escalado ni
  coeficientes.
- Features: probabilidad implícita Up, distancia TWAP a apertura y retorno Binance
  de 60 segundos.
- Pipeline: imputación mediana con indicadores, `StandardScaler` y regresión
  logística con `C=0.5`, `max_iter=2000`, `random_state=17` y `lbfgs`.
- Sin búsqueda de hiperparámetros ni calibración posterior.
- Se calcula el EV neto de Up y Down después de ask, slippage y fee.
- Como máximo se selecciona un lado; si ambos EV son no positivos o empatan, se
  abstiene.
- El control pareado compra el favorito de mercado en los mismos mercados.

Puertas terminales:

- al menos 100 filas de entrenamiento;
- al menos 100 filas de validación;
- al menos 20 señales en validación;
- PnL neto y PnL sin mejor trade positivos;
- profit factor mayor que uno;
- LCB unilateral 95% positivo;
- media superior al control pareado.

Incluso si pasa todo, el máximo resultado es
`HYPOTHESIS_REQUIRES_FRESH_V030_REPLICATION`. V0.29 no puede seleccionar una
estrategia paper ni habilitar dinero real.

## Checkpoints y cierre

Los checkpoints técnicos son 4, 8, 12, 16 y 20 horas. Pueden continuar, reintentar
una vez tras diez minutos o cerrar por seguridad/calidad técnica. No pueden mirar
rentabilidad ni declarar éxito. La evaluación económica ocurre una sola vez al
terminar las 24 horas.

## Pruebas y seguridad

- Pruebas V0.29: 9/9 correctas entre diseño, preinscripción, runner y auditor.
- Pruebas nuevas de esta fase, incluyendo el postmortem V0.28: 12/12 correctas.
- Suite completa: 284/284 correctas.
- La simulación terminal prueba que cambiar las etiquetas de validación no cambia
  el modelo ajustado.
- El auditor verifica query-only y que el hash de la base permanezca idéntico.
- `orders_enabled=false`
- `paper_orders_enabled=false`
- `wallet_required=false`
- `real_money=BLOQUEADO`
- `new_backtest_hours=0`
- Sin tarea programada ni reinicio automático.

## Consulta segura

```bat
cd /d C:\ProyectoBotV4\polymarket_quant_bot
.\.venv\Scripts\python.exe v029_monitor.py --status
```

No ejecutar `--run` mientras no exista una aprobación de lanzamiento explícita y
separada.
