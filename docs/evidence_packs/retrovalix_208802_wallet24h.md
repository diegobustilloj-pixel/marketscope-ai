# Evidence Pack — actividad pública 24h de nagi777

**Veredicto:** `INSUFFICIENT_EVIDENCE`

**Alcance del veredicto:** La ventana pública confirma una estructura de acumulación pareada y residual, pero no permite reconstruir las órdenes ni demostrar un edge replicable.

**Post fuente:** [2088025556428501021](https://x.com/RetroValix/status/2088025556428501021) — 2026-08-13

## Afirmación pública resumida

El post atribuye +$126.836 a nagi777 y describe límites en UP/DOWN, coste medio del set 0,9843, 78,7% de capital pareado y 21,3% residual.

## Alcance y distinción obligatoria

Este pack estudia solamente actividad pública ejecutada durante una ventana contemporánea prerregistrada de 24 horas. No reemplaza el pack V0.16/V0.17: aquella interpretación mínima continúa en FAIL y esta reconstrucción observable continúa con evidencia insuficiente.

**Interpretación:** Hecho observado: wallet, registros públicos y métricas descriptivas. Interpretación: el comportamiento es compatible con acumulación multi-fill de ambos lados. No pueden inferirse órdenes simultáneas, maker/taker, prioridad, cancelaciones ni rentabilidad.

## Especificación comprobable

- **Universo:** 276 mercados BTC UP/DOWN de cinco minutos operados por la wallet dentro de la ventana congelada.
- **Momento de decisión:** Múltiples ejecuciones durante cada mercado; el resultado sellado registra 21.807 actividades y 2.716 cambios de lado.
- **Entradas:**
  - La actividad observable contiene compras pequeñas y repetidas.
  - La señal y el precio límite que originan cada compra no son públicos.
- **Cobertura:**
  - La wallet compra ambos lados en 275 de 276 mercados.
  - El pareado se estima con el mínimo de shares compradas UP/DOWN; no es matching exacto de lotes.
- **Salidas:**
  - La ventana no contiene una regla de salida identificable.
  - No se leyeron resoluciones ni se calculó PnL.
- **Supuestos de ejecución:**
  - Cada fila es actividad ejecutada pública y no una orden completa.
  - No se asume que side BUY pruebe que la orden original fuera maker o límite.

## Desconocidos

- Regla de cotización, reprice y cancelación.
- Prioridad de cola, latencia y partial fills.
- Sizing objetivo e inventario máximo.
- Maker/taker, rebates, fees y coste neto.
- Metodología y periodo exactos usados por RetroValix.

## Comprobaciones ejecutables

| Comprobación | Tipo | Fuente | Campo | Resultado |
|---|---|---|---|---|
| El post identifica al trader como nagi777 | identity | data/retrovalix_posts_raw.json | detailText | PASS (This guy built a trading bot and made +$126,836 on Polymarket  I analyzed its trades, and it trades like a high-frequency market maker on 5-minute crypto Up/Down markets  Its algorithm works like this:  1. Places limit orders on Up and Down at the same time  2. Uses fluctuations in the underlying asset to build complete sets at different moments  The average cost of this kind of set is around 98.43c  This means the matched positions are formed with roughly a 1.57c edge  3. Leaves a directional skew on the side it sees as more likely  Around 78.7% of the capital goes into paired Up+Down inventory  The other 21.3% is used as directional residual on one side  Stats:  > Trades/active hour: 51.25 > Average trade: $110.67 > Win Rate: 50%  His Polymarket nickname: nagi777  Using this strategy, he captures a consistent edge. Then he repeats this thousands of times and keeps growing his capital) |
| El snapshot confirma la wallet congelada | identity | data/nagi777_activity_24h_20260814.json | identity.proxy_wallet | PASS (0xbf337426aa856996b8bb79b238345dd1a0276bf7) |
| La captura contiene 21.807 actividades | identity | data/nagi777_activity_24h_20260814.json | activity_count | PASS (21807) |
| El resultado permanece descriptivo | identity | data/resultado_nagi777_wallet24h_20260814.json | verdict | PASS (DESCRIPTIVE_ONLY) |
| Más del 99% de mercados compró ambos lados | identity | data/resultado_nagi777_wallet24h_20260814.json | metrics.markets_with_both_sides_bought_fraction | PASS (0.99637681) |
| El coste ponderado contemporáneo supera uno | identity | data/resultado_nagi777_wallet24h_20260814.json | metrics.estimated_weighted_complete_set_cost | PASS (1.00572207) |
| No se leyeron outcomes ganadores | safety | data/resultado_nagi777_wallet24h_20260814.json | safety.market_outcomes_read | PASS (False) |

## Métricas observadas

| Métrica | Valor | Fuente |
|---|---:|---|
| Actividades públicas BTC 5m | 21807 | data/resultado_nagi777_wallet24h_20260814.json |
| Mercados BTC 5m | 276 | data/resultado_nagi777_wallet24h_20260814.json |
| Mercados con ambos lados | 99.64% | data/resultado_nagi777_wallet24h_20260814.json |
| Capital pareado estimado | 82.27% | data/resultado_nagi777_wallet24h_20260814.json |
| Coste ponderado estimado del set | 1.00572207 | data/resultado_nagi777_wallet24h_20260814.json |
| Cambios observados UP/DOWN | 2716 | data/resultado_nagi777_wallet24h_20260814.json |

## Razones del veredicto

- La identidad y la estructura general de compras en ambos lados están verificadas.
- El proxy pareado de 82,27% está cerca del 78,7% publicado.
- El coste ponderado observado de 1,00572 no reproduce el 0,9843 publicado.
- La API no expone el ciclo de órdenes necesario para reconstruir ejecución y edge.
- Las unidades públicas no reproducen trades/hora ni average trade del post.

## Evidencia que falta

- Order lifecycle completo con órdenes no llenadas, cancelaciones y replacements.
- Atribución maker/taker, prioridad de cola, latencia, rebates y fees.
- Reglas deterministas de precio, sizing, skew y límites de inventario.
- Periodo y metodología exactos usados para las métricas publicadas.
- Prueba paper independiente prerregistrada de máximo 24 horas.

## Siguiente prueba válida

- Diseñar una V0.18 separada de acumulación multi-fill e inventario objetivo.
- Congelar reglas y gates antes de evaluar una ventana paper nueva.
- Modelar delay, profundidad, cancelación y reprice sin asumir fill por tocar precio.
- Limitar la prueba a 24 horas y mantener dinero real bloqueado.

## Seguridad

- Wallet: no requerida.
- Órdenes: bloqueadas.
- Dinero real: bloqueado.
- Forward activo: no leído y no modificado.
- Experimento nuevo: no abierto.
- Duración máxima futura: 24 horas.

## Provenance

- Manifest SHA-256: `80b7b40dcb5d1e993cc26617a50f0b8a95c16f4ff979228ad800edd066439e7f`
- `data/retrovalix_posts_raw.json` — `1793412f7ef5d1fd765064c07bdaaf87df52e9996865e611ac5b91e739fd9653` — Texto del post fuente.
- `data/prereg_nagi777_wallet24h_20260814.json` — `9708fffdaa93a92a1910245d8d27f6284dd10a827ec7a6d4312302ab0f3582cf` — Identidad, ventana, métricas y código congelados antes de la captura completa.
- `data/nagi777_activity_24h_20260814.json` — `7690ac4c7718b13c0674bb5959f21c8c0e70a9603d8bb07ce8de69e2ddec9dea` — Snapshot inmutable de actividad pública.
- `data/resultado_nagi777_wallet24h_20260814.json` — `d1218a75a69d5f16b42c62555f357ed2e329be3bffe2a5b23e578feeab6a4fa6` — Resultado descriptivo reproducible sin outcomes ni PnL.
