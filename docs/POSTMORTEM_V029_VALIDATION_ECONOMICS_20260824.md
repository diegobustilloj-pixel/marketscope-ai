# Postmortem final V0.29 — fallo económico de validación

Fecha: 2026-08-24  
Variante: `V0.29_HIGH_FREQUENCY_TEMPORAL_HOLDOUT_24H`  
Veredicto fuente: `FAIL_VALIDATION_ECONOMICS`

## Alcance e integridad

Este análisis es terminal, exploratorio y de solo lectura. No añade horas de
backtest, no reutiliza el holdout como evidencia fresca y no puede seleccionar ni
promocionar una estrategia. El generador reproduce exactamente el modelo, las 110
señales, el control y todas las métricas del auditor V0.29 antes de calcular
segmentos descriptivos.

Las fuentes permanecieron inalteradas:

- base V0.29 SHA-256:
  `00682831f049964af16aaf71ee8eef263ec7cf5199c55808f9ec88349a108ff5`;
- resultado V0.29 SHA-256:
  `4d11e1f7f39f2305ed0eed5c352583dceaee186c7b63c413e20498de4f2d153b`;
- postmortem SHA-256:
  `378c830f4011281d2d3b649b934be09e08ca4900644086b67fe48270c5f55ed9`.

## Resultado reproducido

- entrenamiento: 141 filas;
- validación: 141 filas;
- señales: 110;
- abstenciones por EV no positivo: 31;
- PnL a cinco participaciones: `-18.59426145`;
- profit factor: `0.63784832`;
- ROI sobre coste: `-13.918458 %`;
- LCB unilateral 95 %: `-0.07802747`;
- PnL sin la mejor operación a cinco participaciones: `-23.19498020`;
- diferencia contra el control a cinco participaciones: `-4.77817860`.

## Diagnóstico principal

### 1. La capa logística degradó la probabilidad del mercado

En las 141 filas de validación:

| Medida | Modelo V0.29 | Probabilidad implícita del mercado |
|---|---:|---:|
| Brier, menor es mejor | 0.07458343 | 0.06992372 |
| Log loss, menor es mejor | 0.23681116 | 0.21564519 |
| Acierto direccional | 89.361702 % | 90.070922 % |

La probabilidad seleccionada media fue 28.816166 %, pero el acierto realizado de
las señales fue 20.909091 %. El desfase fue de `-7.907075` puntos porcentuales.

### 2. Los costes agravaron la pérdida, pero no la originaron

- pérdida antes de la comisión y el deslizamiento modelados, a cinco
  participaciones: `-13.185`;
- arrastre adicional de ejecución, a cinco participaciones: `-5.40926145`;
- pérdida final: `-18.59426145`.

Aproximadamente 70.9 % de la pérdida ya existía en la selección y 29.1 % fue
arrastre de ejecución. Reducir costes no vuelve rentable el modelo.

### 3. El EV estimado no ordenó rentabilidad real

Los cuatro intervalos fijos de EV terminaron negativos. Incluso las 38 señales con
EV estimado superior a cinco centavos perdieron `-3.35665325` a cinco
participaciones, con profit factor `0.88241174` y LCB negativo.

### 4. La pérdida fue temporalmente inestable

| Bloque de validación | Señales | PnL a 5 | Profit factor |
|---|---:|---:|---:|
| horas 12–16 | 36 | +1.62091310 | 1.18866357 |
| horas 16–20 | 35 | -8.17622125 | 0.58717626 |
| horas 20–24 | 39 | -12.03895330 | 0.47535137 |

El primer bloque nominalmente positivo dependía de su mejor operación: sin ella
quedaba en `-2.97980565` a cinco participaciones.

### 5. Atribución por segmentos

- lado Up: 53 señales, `-13.37562110` a cinco participaciones;
- lado Down: 57 señales, `-5.21864035`;
- selección del favorito: 25 señales, `-11.27479485`;
- selección del no favorito: 85 señales, `-7.31946660`;
- coste entre 0.50 y 0.75: 11 señales, `-10.99304375`;
- TWAP entre 0 y +5 bps: 14 señales, `-25.19696750`;
- volatilidad normal 0.75–1.25: 29 señales, `-13.02879695`.

Las pérdidas aparecen en ambos lados y tanto en favoritos como en no favoritos;
no existe una única corrección direccional que rescate el modelo.

## Bolsillos positivos que no pueden promocionarse

Se examinaron 38 segmentos después de abrir los outcomes. Dos resultados llaman la
atención, pero son únicamente pistas:

- coste menor que 0.25: 75 señales y `+1.72676590` a cinco participaciones, pero
  LCB `-0.03102280` y PnL sin la mejor operación `-2.87395285`;
- TWAP entre -5 y 0 bps: 14 señales y `+10.33065250`, incluso `+5.72993375` sin
  la mejor operación, pero LCB `-0.03035178`, muestra pequeña y selección post hoc.

El segundo bolsillo merece estudio causal independiente, pero no autoriza a crear
una regla `TWAP -5..0` y declararla validada. Este mismo holdout ya fue consumido.

## Decisión

- V0.29: cerrada y rechazada por economía de validación.
- Modelo logístico actual: descartar.
- Ajuste directo de umbral o segmento: prohibido por riesgo de sobreajuste.
- Estrategia seleccionada: ninguna.
- Paper forward o dinero real: bloqueados.

Si el proyecto continúa, debe partir de una hipótesis económica independiente o
cerrar esta familia de modelo. Una nueva prueba tendría que congelarse antes de
observar datos frescos; V0.29 no puede reutilizarse como holdout.

