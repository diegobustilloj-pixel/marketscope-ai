# Decisión V0.32 — capacidad de salida de una posición intencional

Fecha: 2026-08-25 (hora de Bolivia)

## Veredicto

`FAIL_EXIT_SAFETY_CAPACITY`.

V0.32 no generó una estrategia ni reabrió el arbitraje de dos patas. Probó si
una posición intencional de cinco participaciones, abierta temprano y con salida
obligatoria 30 segundos después, podía cerrarse sin quedar atrapada.

## Por qué no se repitió el par no atómico

- V0.20 consiguió completar todos los hedges, pero el coste ponderado del set
  fue `1,04942711` y la pérdida determinista fue `-141,85581672`.
- V0.21 encontró oportunidades transitorias que no demostraban atomicidad.
- V0.22b no confirmó ninguna oportunidad sincronizada y persistente.

Volver a colocar dos órdenes independientes no soluciona el riesgo de una sola
pata. V0.32 separó ese problema de una posición direccional intencional y evaluó
únicamente la capacidad de salida.

## Contrato probado

- ambos outcomes evaluados por igual;
- decisiones entre los segundos 30 y 149;
- latencia de entrada: un segundo;
- tamaño: cinco participaciones;
- decisión con datos V2 completos y profundidad para cinco participaciones en
  bid y ask de Up y Down;
- entrada solo con ask seleccionado completo;
- salida obligatoria 30 segundos después;
- hasta cinco reintentos de un segundo;
- la salida ignora fallos de Chainlink/TWAP y depende únicamente del libro
  seleccionado fresco y de profundidad bid suficiente;
- sin complemento sintético, fills parciales, señal, outcomes, PnL, fees ni
  slippage;
- los probes superpuestos miden capacidad y no son operaciones.

## Resultado general

- probes: 2.880;
- decisiones elegibles: 2.872;
- entradas elegibles: 2.870;
- salidas a tiempo: 2.830;
- salidas dentro de la gracia: 2.830;
- posiciones atrapadas: 40;
- éxito de salida: `98,606272 %`;
- reintentos que rescataron una salida: 0;
- máximo retraso entre las salidas exitosas: 0 segundos.

Las puertas de frecuencia, representación de ambos outcomes, integridad de la
base y seguridad pasaron. Fallaron:

- salida programada mínima del 99 %;
- salida dentro de gracia requerida del 100 %;
- máximo de posiciones atrapadas igual a cero.

## Localización del fallo

| Decisión | Entradas | Salidas | Atrapadas |
|---|---:|---:|---:|
| segundos 30–59 | 720 | 720 | 0 |
| segundos 60–89 | 720 | 720 | 0 |
| segundos 90–119 | 714 | 714 | 0 |
| segundos 120–149 | 716 | 676 | 40 |

Las 40 posiciones atrapadas se concentraron en dos mercados:

- 27 posiciones Up en `btc-updown-5m-1787611200`;
- 13 posiciones Down en `btc-updown-5m-1787613600`.

En esos casos la salida objetivo cayó dentro de la fase unilateral persistente.
Esperar cinco segundos no ayudó porque el bid necesario no reapareció.

## Interpretación

La regla completa V0.32 queda rechazada. Una tasa agregada de 98,6 % no es
suficiente: una sola posición atrapada viola el objetivo que dio origen a la
prueba.

El subintervalo de decisiones 30–119 produjo 2.154/2.154 salidas, pero ahora es
un hallazgo post-hoc. No puede reetiquetarse como validación ni usarse para
operar. Sí permite diseñar una replicación fresca con:

1. última decisión como máximo en el segundo 119;
2. salida obligatoria 30 segundos después;
3. guardia proactiva de profundidad durante la posición;
4. salida que nunca dependa de la disponibilidad de la señal;
5. cero tolerancia a posiciones atrapadas;
6. una regla económica separada y preinscrita antes de calcular PnL.

Reducir retrospectivamente la ventana y volver a ejecutar la misma base solo
fabricaría un pase. El siguiente paso honesto es congelar ese envelope como
candidato provisional y verificarlo en datos futuros antes de probar economía.

## Integridad y seguridad

- base V0.31 antes/después:
  `7fee24abb0ad0dd88b3cc984c87e19758b24a8c830f20dc4b1ad28018825eaf6`;
- resultado V0.31R antes/después:
  `0291ec2ee64b0eb881889380acba2c50b93304f3ddb6c6b6e5dc076b25b1c429`;
- resultado V0.32:
  `8f971e65e23974c2d401fe825728af5b97bf11dc82e63dc8b416bd60f6959a8f`;
- outcomes: 0;
- labels: 0;
- PnL: no;
- señales: no;
- operaciones: 0;
- órdenes paper/reales: 0;
- wallet: no;
- dinero real: `BLOQUEADO`.

Pruebas específicas: 6/6. Suite completa: 320/320.

## Artefactos

- `data/prereg_v032_exit_safety_capacity.json`;
- `data/implementation_v032_exit_safety_capacity.json`;
- `src/polymarket_bot/v032_exit_safety.py`;
- `v032_exit_safety_report.py`;
- `tests/test_v032_exit_safety.py`;
- `data/resultado_v032_exit_safety_capacity.json`;
- `docs/DECISION_V032_EXIT_SAFETY_20260825.md`.
