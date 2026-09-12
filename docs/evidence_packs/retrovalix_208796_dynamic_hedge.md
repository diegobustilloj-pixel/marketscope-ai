# Evidence Pack — directional strategy con dynamic hedge

**Veredicto:** `INSUFFICIENT_EVIDENCE`

**Alcance del veredicto:** No existe evidencia suficiente para aprobar o rechazar la estrategia atribuida a pspspsps5.

**Post fuente:** [2087965513020580350](https://x.com/RetroValix/status/2087965513020580350) — 2026-08-13

## Afirmación pública resumida

El post atribuye +$185.054 a un bot que compra el resultado considerado infravalorado, cambia de lado cuando se mueve el subyacente y acumula inventario pareado más exposición residual.

## Alcance y distinción obligatoria

El pack conserva la narrativa como hipótesis, no como especificación. Ningún resultado V0.16/V0.17 puede transferirse a esta estrategia porque no usa el mismo modelo de entrada, switching ni sizing.

**Interpretación:** Hecho observado: la descripción y las métricas públicas del post. Hipótesis: existe un modelo privado de probabilidad y una regla dinámica de cambio de exposición. No se conocen sus parámetros.

## Especificación comprobable

- **Universo:** Mercados cripto UP/DOWN de cinco minutos mencionados por el post.
- **Momento de decisión:** Múltiples decisiones dentro de cada mercado; los instantes exactos no están publicados.
- **Entradas:**
  - Comprar el resultado que el modelo privado considera infravalorado.
  - Permitir múltiples cambios entre UP y DOWN dentro del mismo mercado.
- **Cobertura:**
  - Comprar el lado opuesto cuando cambia el movimiento del subyacente.
  - Permitir que parte de las posiciones opuestas forme inventario pareado.
- **Salidas:**
  - No hay una regla de cierre publicada.
  - No hay una regla publicada para liquidar el residual direccional.
- **Supuestos de ejecución:**
  - El post no especifica maker/taker, latencia, fees, slippage ni partial fills.
  - Las métricas públicas no prueban que un tercero alcance los mismos precios.

## Desconocidos

- Fórmula y calibración del modelo de probabilidad.
- Umbral que define infravaloración.
- Regla exacta que detecta reversión del subyacente.
- Sizing, límites de inventario y exposición máxima.
- Reglas de cierre, cancelación y gestión de una pierna incompleta.
- Direcciones de wallet y transacciones usadas en el análisis.

## Comprobaciones ejecutables

| Comprobación | Tipo | Fuente | Campo | Resultado |
|---|---|---|---|---|
| El post describe dynamic hedge | identity | data/retrovalix_posts_raw.json | detailText | PASS (This guy built an HFT bot on Polymarket and made +$185,054  It trades 5-minute crypto Up/Down markets using a directional strategy with a dynamic hedge:  1. Finds the outcome its model sees as undervalued and starts building a position  2. If the underlying asset’s movement changes, it starts buying the opposite side  3. Within a single market, it can switch between Up and Down several times, constantly changing its net exposure  Part of the accumulated Up and Down position turns into matched inventory. The remaining imbalance forms directional exposure  > Average trade: $23.59 > Trades/active hour: 148 > Win Rate: 42%  His Polymarket nickname: pspspsps5  You can also automate your trading in two clicks, free for a trial run:   https:// join.horizon.trade/retrovalix) |
| El post identifica al trader como pspspsps5 | identity | data/retrovalix_posts_raw.json | detailText | PASS (This guy built an HFT bot on Polymarket and made +$185,054  It trades 5-minute crypto Up/Down markets using a directional strategy with a dynamic hedge:  1. Finds the outcome its model sees as undervalued and starts building a position  2. If the underlying asset’s movement changes, it starts buying the opposite side  3. Within a single market, it can switch between Up and Down several times, constantly changing its net exposure  Part of the accumulated Up and Down position turns into matched inventory. The remaining imbalance forms directional exposure  > Average trade: $23.59 > Trades/active hour: 148 > Win Rate: 42%  His Polymarket nickname: pspspsps5  You can also automate your trading in two clicks, free for a trial run:   https:// join.horizon.trade/retrovalix) |

## Métricas observadas

No hay métricas de rentabilidad atribuibles a esta estrategia.

## Razones del veredicto

- La frase 'el modelo ve infravalorado' no define una señal reproducible.
- No se publican reglas de switching, sizing, salida ni ejecución.
- Las cifras de PnL, win rate y trades por hora no bastan para atribuir causalidad a la estrategia descrita.
- No existe un artefacto sellado de QuantBot que implemente exactamente esta hipótesis.

## Evidencia que falta

- Wallet y conjunto exacto de ejecuciones analizadas.
- Modelo de probabilidad con variables, calibración y thresholds.
- Order type, latencia, fills, fees, rebates y slippage.
- Reglas de sizing, inventario, switching y salida.
- Datos contemporáneos suficientes para una prueba forward independiente.

## Siguiente prueba válida

- Conseguir la wallet o un export verificable de ejecuciones.
- Escribir una sola interpretación determinista y marcar todos los supuestos.
- Congelar una ventana paper de máximo 24 horas sin leer el forward activo.
- Mantener `INSUFFICIENT_EVIDENCE` si no puede definirse la señal antes de ver resultados.

## Seguridad

- Wallet: no requerida.
- Órdenes: bloqueadas.
- Dinero real: bloqueado.
- Forward activo: no leído y no modificado.
- Experimento nuevo: no abierto.
- Duración máxima futura: 24 horas.

## Provenance

- Manifest SHA-256: `641b094304543ac74ee716a20f7b513a536ee2223d62e79cbd0d226adfc6ba0f`
- `data/retrovalix_posts_raw.json` — `1793412f7ef5d1fd765064c07bdaaf87df52e9996865e611ac5b91e739fd9653` — Texto completo accesible del post fuente.
