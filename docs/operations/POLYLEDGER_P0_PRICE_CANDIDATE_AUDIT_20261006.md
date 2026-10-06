# Auditoría de candidatos de precio — PolyLedger P0 / `car` / 2026-10-06

**Estado:** identidad oficial actual y temporalidad auditadas para los cuatro candidatos;
los cuatro son elegibles para revisar una política de marcas, pero **ninguno**
es todavía una marca aprobada o integrada. P0 permanece `BLOCKED`.

## Alcance

Esta auditoría es el paso de evidencia posterior a
`car_lifetime_price_probe_20261006_v2/`. No vuelve a pedir precios ni modifica
el bundle v5. Verifica el manifiesto completo de la sonda, relee sus bytes raw,
recalcula el punto anterior al corte y consulta únicamente metadatos públicos
actuales para demostrar a qué mercado y outcome pertenece cada token.

Las fuentes oficiales consultadas fueron:

- [`GET /markets-by-token/{token_id}`](https://docs.polymarket.com/api-reference/markets/get-market-by-token),
  para condición y par de tokens;
- [`GET /clob-markets/{condition_id}`](https://docs.polymarket.com/api-reference/markets/get-clob-market-info),
  para el par etiquetado `Yes`/`No`;
- [`GET /markets`](https://docs.polymarket.com/api-reference/markets/list-markets)
  de Gamma, con `condition_ids` y vistas separadas `closed=false` y
  `closed=true`, para un segundo mapeo etiquetado y los metadatos del mercado.

La base local `car_metadata.db` se abrió en modo de solo lectura y se usó
únicamente como corroboración auxiliar. Su ausencia nunca se sustituyó por una
suposición.

## Ancla y candidatos revalidados

- bloque de cierre: `93.762.690`;
- hash: `0xcb952565d49457d93bcd4ae549bbfe7a7b31a26fe8855dd6b993d46638f30b72`;
- corte: `2026-09-14T01:24:27Z` (`1789349067`), un segundo antes del timestamp
  del bloque;
- observación de los cuatro candidatos: `2026-09-14T01:15:00Z`;
- edad al corte: 567 segundos;
- resolución publicada: 300 segundos;
- límite usado por la sonda: 3.600 segundos.

La auditoría revalidó por hash y coherencia las dos cabeceras Polygon ya
selladas, el bloque, timestamp, URL exacta, token, `as_of`, hash de solicitud,
HTTP 200, hash de los bytes raw y ausencia de puntos futuros. No volvió a
consultar los RPC del corte.

## Resultado por token

| Sec. | Mercado y outcome del token | Precio candidato | Identidad oficial actual | Metadata local anterior al corte | Orden `primary/secondary` vivo |
|---:|---|---:|---|---|---|
| 2 | `YES` — Will OpenAI acquire Pinterest in 2026? | 0,0215 | consistente | sí | coincide con el orden Yes/No documentado |
| 3 | `NO` — Houthis enter Aden by December 31, 2026? | 0,635 | consistente | no disponible | discrepa del orden Yes/No documentado |
| 7 | `NO` — Houthis seize an oil tanker by September 30, 2026? | 0,915 | consistente | no disponible | discrepa del orden Yes/No documentado |
| 19 | `NO` — Will Apple release a foldable iPhone by October 31? | 0,014 | consistente | sí | discrepa del orden Yes/No documentado |

Todos estaban dentro de la ventana actualmente declarada por Gamma para el
corte. El mercado de la secuencia 7 apareció en la vista `closed=true`; los
otros tres, en `closed=false`.

## Discrepancia oficial preservada

La documentación de `markets-by-token` describe `primary_token_id` como Yes y
`secondary_token_id` como No. En las respuestas vivas, el endpoint devolvió el
token consultado como `primary` en los cuatro casos, aunque CLOB y Gamma lo
etiquetaron como No en tres de ellos. Por eso el auditor v2:

1. usa ese endpoint para condición y par **no ordenado**;
2. exige que CLOB y Gamma coincidan en el par completo y en las etiquetas;
3. registra por separado la discrepancia de orden;
4. bloquea cualquier ambigüedad, duplicado o contradicción.

La ejecución local v1 asumió literalmente el orden documentado: guardó 12/12
respuestas, pero bloqueó tres candidatos. Se conserva sin sobrescribir como
diagnóstico. El motor v2 y la salida v2 son la interpretación vigente.

## Sello local autoritativo

Salida local, ignorada por Git:

`data/polyledger-sentinel/p0/car_lifetime_price_candidate_audit_20261006_v2/`

| Comprobación | Resultado |
|---|---:|
| Consultas oficiales | 16 |
| Respuestas raw guardadas | 16 |
| Errores de transporte | 0 |
| Errores de interpretación | 0 |
| Identidades oficiales consistentes | 4/4 |
| Corroboraciones locales anteriores al corte | 2/4 |
| Archivos cubiertos por el manifiesto | 21 |
| Diferencias de hash | 0 |
| Commit de ejecución | `a0d62f14dd7b5f0ff946c72d681d9ae16dfbaf10` |
| Árbol de trabajo al momento del sellado | limpio |

Hashes principales:

| Artefacto | SHA-256 |
|---|---|
| `run_manifest.json` | `eae2f61d7f2297ebfcb8eba028bec930d2b7f02ccb909ff1ff1041f3d45d1763` |
| `configuration.json` | `360e7fc3535e2a3b3cdaba1854f2a5fcbc74907f8d668b46d60a91a136e2511c` |
| `candidates.json` | `258a7f30a41f7a2b9abcd298bcd40dc0654aab49dba3383a2a7d2d2f2057af57` |
| `request_results.json` | `8ef55d43fbe1b9ed614a73b27ac820b5db4dc005a78093468eee09d69d31a3e2` |
| `candidate_reviews.json` | `34e13cccb76cfdc2fbf5739d7eff85ef52dfc005a3e7c9282c061452543a1373` |
| `summary.json` | `4b8d55537ae86fa0aed8cd47e60ff31fc05d5c4065cdc4fc3c6d6ac4e07d6579` |

## Decisión y siguiente paso

`PENDING_MARK_POLICY_REVIEW` significa solamente que el punto histórico y el
mapeo oficial actual de token, outcome y mercado no presentan contradicciones
en esta muestra. Sólo dos candidatos tienen corroboración local capturada antes
del corte. Esto no demuestra liquidez ejecutable, no convierte un tick de cinco
minutos en precio exacto del bloque y no acredita PnL.

La auditoría escribió cero `closing_marks`, no cambió el bundle v5 y no reduce
las 3.498 marcas pendientes. Antes de una captura mayor se debe fijar y probar
una política explícita de aceptación —edad máxima, granularidad, mercados
abiertos/cerrados, fuente y rechazo—. Después, cualquier evidencia aceptada se
compilará en un bundle nuevo. También siguen pendientes 856 flujos externos,
basis de 772 recepciones y el cálculo contable independiente.

Antes de escalar también conviene endurecer dos bordes que no invalidan esta
ejecución: validar explícitamente el campo compacto `c` de la respuesta CLOB y
sellar cualquier sidecar SQLite WAL si existiera. En esta salida el endpoint
solicitado, el par completo y Gamma corroboraron las cuatro condiciones, y la
base local no tenía WAL con contenido.
