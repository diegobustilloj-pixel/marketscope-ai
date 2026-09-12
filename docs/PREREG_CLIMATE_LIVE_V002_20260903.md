# PRERREGISTRO — CLIMATE LIVE V002

Estado: congelado el 3 de septiembre de 2026 antes de que exista una operación V002 con desenlace oficial. En la comprobación inicial, los 156 eventos con probabilidad estaban abiertos y el backtest V002 tenía cero operaciones.

## Objetivo

Recoger evidencia forward diaria y producir una decisión semiautomática. El sistema descarga datos y calcula la señal; el usuario es la única persona que puede colocar una orden. No existe integración con cartera, firma ni envío de órdenes.

## Modelo congelado

- Selección meteorológica V001 congelada antes de TEST, hash SHA-256 `a41ee83c94bc8f16b07de5a0eb5e0851d168a4f1994ebfe54d0b3b89934b64d1`.
- D+1 únicamente.
- AIFS para máximas y ensemble ponderado TRAIN para mínimas.
- Familia `STRONG` y estación con al menos 30 reconstrucciones TRAIN y coincidencia mínima de 98%.

## Descubrimiento y contrato

- Descubrimiento paginado de eventos activos con etiqueta `daily-temperature` en Gamma.
- Se vuelve a analizar cada contrato nuevo; un cambio de estación, unidad o precisión no observado históricamente bloquea el mercado.
- NOAA exige la columna `Temp`, todos los registros del día y que el parámetro `site` coincida con la estación.
- Weather Underground exige `Daily Observations` o resumen diario finalizado, todos los registros del día y estación coincidente.
- La zona IANA se obtiene por las coordenadas exactas de la estación y se conserva en cada forecast. La fecha objetivo continúa siendo la fecha local escrita en el contrato.

## Entrada primaria

Una sola compra `YES` en el bucket con mayor probabilidad del modelo:

1. Primera captura completa entre 15:00 y 16:10 hora local de la estación, el día anterior.
2. Probabilidad mínima: 30%.
3. Precio simulado: mejor ask + 0,01, máximo 0,99.
4. Precio permitido: 0,10–0,75.
5. Comisión taker tomada del `feeSchedule` capturado en la entrada; si falta o cambia al cierre, la fila se bloquea.
6. Ventaja neta mínima después de precio, impacto y comisión: 5 puntos porcentuales.
7. Spread máximo: 0,10.
8. Profundidad visible mínima al mejor ask: 25 USDC.
9. Valor simulado: 25 USDC por evento; mantener hasta resolución.
10. Sin cambio de lado, sin apilar thresholds y sin órdenes automáticas.

## Puerta de rentabilidad

`MANUAL_REVIEW` con dinero real permanece bloqueado hasta que existan al menos 30 operaciones independientes y oficialmente resueltas, PnL/ROI positivos, profit factor mínimo 1,25 y límite inferior bootstrap 95% del PnL medio por operación mayor que cero. Hasta entonces las únicas salidas permitidas son `PAPER_ONLY` y `NO_ENTRY`.

## Fuentes económicas

- Gamma conserva el evento, contrato y `feeSchedule`.
- CLOB conserva ask, spread, profundidad, timestamp y hash del libro en cada ciclo.
- Comisión: `shares × rate × (price × (1-price))^exponent`, usando parámetros del mercado capturados al entrar.

## Regla de verdad

Una probabilidad alta no es una entrada. Falta de horario, contrato, comisión, ventaja neta, spread, profundidad o evidencia económica produce `NO_ENTRY`.
