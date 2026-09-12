# Politica de 24 horas y resultados al 14 de agosto de 2026

## Politica efectiva

Desde el 14 de agosto de 2026, ningun experimento, backtest temporal, forward o
validacion paper nuevos puede solicitar mas de 24 horas. La configuracion
ejecutable vive en `data/policy_experiment_duration_24h.json`.

Existe una sola excepcion: reanudar
`data/shadow_forward_twap_transfer_v094a.db`, iniciado el 10 de agosto y con
fin ya congelado para el 16 de agosto a las 20:02 de Bolivia. La excepcion
comprueba ruta, inicio, fin, duracion y bloqueos de wallet/ordenes. No permite
crear otra base de siete dias.

El comando generico `run-shadow` usa ahora 24 horas por defecto y rechaza una
duracion superior antes de abrir una base nueva.

## Resultados sellados

- V0.11 encontro en desarrollo `twap_shock_market_lag/standard`, pero su
  forward100 fallo: 7 trades, PnL neto negativo y ROI negativo.
- V0.12 termino `FAIL_DEVELOPMENT`. El mejor resultado descriptivo tuvo PnL
  positivo, pero solo 7 trades y concentracion excesiva; no fue seleccionado.
- V0.13 termino `FAIL_INSUFFICIENT_FREQUENCY`: 5 trades en 300 elegibles,
  cero labels leidos y sin calculo de PnL.
- V0.14 termino `FAIL_INSUFFICIENT_FREQUENCY`: LOW 4 y MODERATE 1 en 250
  elegibles, cero labels leidos y sin calculo de PnL.
- V0.15 no aplica porque V0.14 no selecciono candidato.

Conclusion: la familia TWAP-shock/market-lag queda cerrada en su definicion
actual. No se permite rescatarla cambiando umbrales despues de estos fallos.

Incluso bajo el supuesto optimista de que los 288 mercados de cinco minutos de
un dia fueran elegibles, las tasas observadas proyectan 4,8 trades V0.13, 4,61
LOW y 1,15 MODERATE. Las reglas sin cambios no alcanzarian sus targets en 24h.

El reporte reproducible esta en
`data/analisis_resultados_hasta_20260814.json` y se genera con:

```cmd
.\.venv\Scripts\python.exe analizar_resultados_actuales_24h.py
```

## Uso del forward de siete dias

La auditoria oficial de siete dias sigue siendo la evidencia primaria y no se
abre antes de `experiment_completed_at`.

Antes de comenzar la parte final del experimento se congelo un analisis
secundario de exactamente 24 horas:

- inicio: 16 de agosto 00:02:18 UTC (15 de agosto 20:02:18 Bolivia);
- fin: 17 de agosto 00:02:18 UTC (16 de agosto 20:02:18 Bolivia);
- modelo y umbral: los ya congelados; sin seleccion ni retuning;
- minimo 20 trades, LCB del PnL medio positivo y Brier no mas de 0,01 peor que
  Polymarket;
- cobertura minima de mercados 90%, features 85% y resoluciones 90%;
- se ejecuta solo despues de terminar y auditar oficialmente los siete dias.

El resultado de 24 horas sera diagnostico. Al ser un subconjunto del forward
oficial no habilita dinero real, aunque haya sido fijado antes de comenzar la
ventana temporal.
