# Evidence Pack — arbitraje BTC 5m con órdenes límite

**Veredicto:** `INSUFFICIENT_EVIDENCE`

**Alcance del veredicto:** No existe evidencia suficiente para reproducir ni invalidar el bot del post fijado.

**Post fuente:** [2032506967923515706](https://x.com/RetroValix/status/2032506967923515706) — 2026-03-13

## Afirmación pública resumida

El post atribuye $178.000 a software que opera 273 veces por hora, arbitra mercados BTC de cinco minutos, usa órdenes límite y mantiene sesgo hacia el lado con edge.

## Alcance y distinción obligatoria

El texto y los comentarios públicos permiten registrar la hipótesis, pero no sustituyen código, ecuaciones, transacciones, ciclo de órdenes ni validación temporal.

**Interpretación:** Hecho observado: contenido público del post y sus comentarios. Hipótesis: arbitraje con inventario y sesgo. No se atribuye una implementación concreta a ningún modelo matemático sin artefactos verificables.

## Especificación comprobable

- **Universo:** Mercados BTC UP/DOWN de cinco minutos.
- **Momento de decisión:** Hasta 273 ejecuciones por hora según el post; timing exacto no publicado.
- **Entradas:**
  - Usar órdenes límite según el fragmento accesible.
  - Arbitrar el mercado y sesgar inventario hacia un edge no definido.
- **Cobertura:**
  - El post sugiere arbitraje, pero no publica la regla de formación de pares.
  - No existe un criterio publicado para cobertura parcial o cancelación.
- **Salidas:**
  - No hay una regla de salida reproducible publicada.
  - No se conoce el tratamiento del inventario al vencimiento.
- **Supuestos de ejecución:**
  - Una orden límite no garantiza fill ni prioridad de cola.
  - La tasa observada puede depender de infraestructura y capital no publicados.

## Desconocidos

- Wallet y dataset exactos.
- Definición numérica del edge.
- Precios, cancelaciones, replacements y partial fills.
- Capital, drawdown, rebates, fees y latencia.

## Comprobaciones ejecutables

| Comprobación | Tipo | Fuente | Campo | Resultado |
|---|---|---|---|---|
| El post afirma 273 trades por hora | identity | data/retrovalix_posts_raw.json | text | PASS (This trader made $178,000 on Polymarket using software, buying markets for $4.6  His trading bot makes 273 trades per hour or 4.55 trades per minute. He arbitrages 5-min Bitcoin markets and leans into the side where he sees an edge  His strategy is simple:  > Buys only with limit) |
| El fragmento accesible exige órdenes límite | identity | data/retrovalix_posts_raw.json | text | PASS (This trader made $178,000 on Polymarket using software, buying markets for $4.6  His trading bot makes 273 trades per hour or 4.55 trades per minute. He arbitrages 5-min Bitcoin markets and leans into the side where he sees an edge  His strategy is simple:  > Buys only with limit) |
| La audiencia objeta la latencia HFT | identity | data/retrovalix_comments_2032506967923515706.json | text | PASS (You will not find any edge if you don’t have HFT server with 1ms latency. This is a serious setup . SEVER is most likely in New York located . I tried this forget it . Already there are entire companies who are way faster then you . It is all about how fast can you place a bet .) |
| La audiencia pregunta si puede hacer copy trade | identity | data/retrovalix_comments_2032506967923515706.json | text | PASS (can do copy trade？) |

## Métricas observadas

No hay métricas de rentabilidad atribuibles a esta estrategia.

## Razones del veredicto

- El post no publica una señal, reglas de cotización ni criterios de cancelación reproducibles.
- Las órdenes límite dependen de fill y prioridad, no solo del precio mostrado.
- Los comentarios identifican latencia, capital, drawdown y compresión del edge como explicaciones alternativas.
- No existe una prueba QuantBot congelada que represente exactamente este bot.

## Evidencia que falta

- Wallet y transacciones exactas usadas por el análisis.
- Regla matemática de edge, spread, inventario y sizing.
- Ciclo completo de órdenes, fees, rebates, latencia y prioridad de cola.
- Evaluación temporal independiente y contemporánea.

## Siguiente prueba válida

- Obtener la wallet antes de inferir reglas adicionales.
- Separar market making, arbitraje y sesgo direccional en hipótesis independientes.
- Capturar como máximo 24 horas de order book y simular fills con delay y profundidad.
- No usar el PnL público como etiqueta para seleccionar parámetros.

## Seguridad

- Wallet: no requerida.
- Órdenes: bloqueadas.
- Dinero real: bloqueado.
- Forward activo: no leído y no modificado.
- Experimento nuevo: no abierto.
- Duración máxima futura: 24 horas.

## Provenance

- Manifest SHA-256: `7e6b612943c1ac5fc00c70cd2f78faf05587e3ffb4988275bd553230bb387e5d`
- `data/retrovalix_posts_raw.json` — `1793412f7ef5d1fd765064c07bdaaf87df52e9996865e611ac5b91e739fd9653` — Texto accesible del post fijado.
- `data/retrovalix_comments_2032506967923515706.json` — `476d48182d81d5b8f01778a3d40d783d8ca59a66f29d075848c59c4c9b132c91` — Comentarios públicos con solicitudes y objeciones técnicas.
