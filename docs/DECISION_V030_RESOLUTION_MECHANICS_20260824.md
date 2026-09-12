# Decisión V0.30 — mecánica de resolución TWAP60

Fecha: 2026-08-24 (hora de Bolivia)

## Resultado ejecutivo

La tercera ruta independiente también queda cerrada. La fórmula mecánica V0.30
produjo señales suficientes, pero perdió más que su control emparejado, fue peor
que la probabilidad del mercado y perdió en ambas mitades de la ventana. No se
preinscribe ni se lanza otro forward de esta familia.

Decisión sellada: `CLOSE_RESOLUTION_MECHANICS_FAMILY`.

## Qué se probó

Se fijó una sola fórmula antes de calcular su resultado económico:

1. decisión a 60 segundos del cierre;
2. referencia de apertura: TWAP oficial Chainlink de 60 segundos;
3. estado actual: spot Chainlink;
4. volatilidad: desviación poblacional de retornos Chainlink de un segundo en
   los 60 segundos previos;
5. proceso sin deriva y sin parámetros entrenados;
6. desviación del promedio futuro de 60 segundos:
   `volatilidad × sqrt(60 / 3)`;
7. probabilidad Up: CDF normal de la distancia spot-apertura dividida por esa
   desviación;
8. compra de una sola pata sólo cuando su EV después de ask, `0.005` de
   slippage y fee `0.07 × precio × (1-precio)` era estrictamente positivo.

No hubo búsqueda de umbrales, calibración, ajuste de parámetros ni variantes de
la fórmula. El precio implícito de Polymarket no entra en el modelo; se usa
únicamente como control.

## Naturaleza de la evaluación

Se reutilizó la ventana cerrada V0.29 de 24 horas. Por tanto, este resultado no
es una validación fresca y sólo se admite como filtro de descarte. Un resultado
positivo habría permitido diseñar una replicación futura; nunca habría aprobado
paper ni dinero real.

La base se abrió en modo SQLite de sólo lectura. Su SHA-256 antes y después fue:

`00682831f049964af16aaf71ee8eef263ec7cf5199c55808f9ec88349a108ff5`

## Métricas

| Medida | V0.30 mecánico | Control favorito emparejado |
|---|---:|---:|
| Mercados evaluables | 265 | 265 |
| Operaciones | 257 | 257 |
| Victorias | 218 | 230 |
| Win rate | 84,82 % | 89,49 % |
| PnL por participación | -11,61017236 | -8,34219004 |
| PnL a 5 participaciones | **-58,05086180** | -41,71095020 |
| ROI sobre coste | -5,06 % | -3,50 % |
| Profit factor | 0,47828646 | 0,59065405 |
| LCB unilateral 95 % | -0,07268092 | -0,06003526 |
| PnL sin mejor operación, a 5 | -61,50307055 | -44,04852895 |

La media del candidato fue `-0,01271589` por participación peor que la del
control sobre exactamente los mismos mercados.

## Calidad probabilística y estabilidad

- Brier mecánico: `0,08857742`.
- Brier de mercado: `0,07227835`.
- Diferencia: `+0,01629907`, por encima del máximo tolerado de `+0,01`.
- Log loss mecánico: `0,37417395`.
- Log loss de mercado: `0,22471708`.
- Primera mitad, PnL a 5: `-35,76548170`.
- Segunda mitad, PnL a 5: `-22,28538010`.
- Bloques positivos de cuatro horas: 1 de 6 (`16,67 %`).

La alta tasa de acierto no produjo rentabilidad porque las posiciones se
compraron a precios que exigían una precisión todavía mayor. Esto confirma que
win rate y edge neto no son equivalentes.

## Puertas

Sólo aprobaron frecuencia, cobertura y representación temporal. Fallaron:

- PnL positivo;
- profit factor mayor que uno;
- LCB positivo;
- PnL positivo sin la mejor operación;
- primera y segunda mitad positivas;
- proporción de bloques positivos;
- mejora frente al control emparejado;
- Brier no más de 0,01 peor que el mercado.

## Qué queda descartado con V0.18–V0.30

La evidencia local ya cierra tres familias para BTC Up/Down 5m con la
infraestructura y el modelo de ejecución actuales:

1. **Par/arbitraje e inventario.** V0.18 perdió incluso con todas las patas
   emparejadas; V0.19 hizo rentable la parte emparejada, pero el residuo de una
   sola pata destruyó el resultado. V0.21/V0.22 no demostraron persistencia
   ejecutable suficiente.
2. **Direccional por filtros y regresión.** V0.23–V0.28 fallaron por frecuencia,
   inestabilidad o no replicación económica. V0.29 produjo 110 señales de
   validación, pero terminó con `-18,59426145` a cinco participaciones y fue peor
   que el mercado.
3. **Probabilidad mecánica sin entrenamiento.** V0.30 tuvo frecuencia amplia,
   pero perdió `-58,05086180` a cinco y fue peor que su control y que la
   probabilidad implícita.

Esto no demuestra que ningún bot de Polymarket pueda ser rentable. Sí demuestra
que no es válido seguir retocando thresholds de estas tres familias sobre las
mismas ventanas.

## Siguiente paso seguro

No lanzar V0.30 ni crear V0.31 como ajuste de esta fórmula. El proyecto debe
cambiar de problema medible. La siguiente investigación admisible es diseñar,
sin ejecutarla todavía, una hipótesis que use una fuente de edge y un resultado
económico diferentes, por ejemplo trayectoria intramercado con salida
ejecutable antes de resolución. Ese diseño necesita cotizaciones de entrada y
salida, profundidad y reglas de fill; no puede inferirse honestamente de una
sola observación a T-60.

Hasta disponer de ese contrato de datos, la decisión correcta es pausa de
estrategias, no otra variante automática.

## Seguridad

- órdenes creadas: `0`;
- órdenes paper: `0`;
- wallet requerida: `false`;
- dinero real: `BLOQUEADO`;
- horas nuevas de backtest: `0`;
- lanzamiento automático: `false`.

Artefactos:

- `src/polymarket_bot/v030_strategy.py`;
- `src/polymarket_bot/v030_development.py`;
- `v030_development_report.py`;
- `tests/test_v030_resolution_mechanics.py`;
- `data/diagnostico_v030_resolution_mechanics_final.json`.
