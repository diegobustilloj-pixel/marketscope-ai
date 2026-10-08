# Plan de fuentes oficiales para cerrar la evidencia P0

**Estado:** diseño de adquisición con sonda y auditoría de identidad ya
completadas; no se han importado ni imputado marcas en el bundle. La cola local
sellada de brechas sigue siendo la única lista de trabajo autorizada.

## Fuentes candidatas y límites

| Necesidad | Fuente oficial candidata | Uso permitido | Límite que se conserva |
|---|---|---|---|
| Marcas históricas por outcome | [`/v2/prices-history`](https://docs.polymarket.com/api-reference/markets/get-a-tokens-price-history) | Última observación publicada en o antes del corte, con timestamp y resolución conservados | Una serie puede ser escasa o no existir; no se sustituye con precio actual ni con una interpolación. |
| Condición y par de tokens | [`/markets-by-token/{token_id}`](https://docs.polymarket.com/api-reference/markets/get-market-by-token) | Resolver la condición y comprobar que el token pertenece al par | Las respuestas vivas no respetaron en 3/4 casos el orden Yes/No documentado; se trata como par no ordenado. |
| Etiquetas CLOB | [`/clob-markets/{condition_id}`](https://docs.polymarket.com/api-reference/markets/get-clob-market-info) | Obtener el par etiquetado Yes/No | Debe coincidir el par completo, no sólo el token consultado. |
| Contraste de mercado | [`/markets`](https://docs.polymarket.com/api-reference/markets/list-markets) | Contrastar condición, tokens, outcomes y ventana en vistas abiertas y cerradas | Metadata actual no prueba por sí sola el estado histórico; una respuesta vacía en la vista abierta no autoriza a descartar un mercado cerrado. |
| Consulta eficiente de varias posiciones | [`/batch-prices-history`](https://docs.polymarket.com/api-reference/markets/get-batch-prices-history) | Transporte experimental en lotes de hasta 20 asset IDs, después de validar su borde temporal por separado | El batch documenta una ventana `start/end/fidelity`, no la misma semántica puntual `as_of`; no se lo sustituye silenciosamente por el endpoint puntual. |
| Contraste de cuenta | [`/v1/accounting/snapshot`](https://docs.polymarket.com/api-reference/misc/download-an-accounting-snapshot-zip-of-csvs) | ZIP oficial de `positions.csv` y `equity.csv` para la wallet pública | Es un snapshot de cuenta; no prueba por sí solo el PnL histórico ni reemplaza el segundo pipeline. |

Las páginas oficiales describen que la lectura de historial admite `as_of` y
devuelve el último punto observado en o antes de ese instante. También aclaran
que la serie puede ser irregular y que su granularidad disponible depende de
la antigüedad de la consulta. Por eso toda selección debe retener la edad de la
observación y fallar cerrada si supera el umbral acordado.

## Secuencia de captura propuesta

1. Obtener el timestamp del bloque de cierre `93.762.690` y comprobar su hash
   contra dos fuentes Polygon independientes. Sellar ambos encabezados raw
   antes de pedir precios y usar un corte no posterior al bloque.
2. Para la marca puntual, usar primero `/v2/prices-history` por outcome con
   `as_of`; conservar bytes raw, URL, hora de recepción, hash y respuesta
   HTTP. El batch de veinte activos se trata como experimento separado hasta
   demostrar que su frontera temporal coincide con la regla puntual.
3. Para cada activo, escoger únicamente el último tick con timestamp no
   posterior al corte. Persistir `price`, timestamp de observación,
   `resolution_seconds`, origen y distancia al corte. Si no existe un tick o
   no supera el criterio de frescura, dejar la marca ausente.
4. Antes de aceptar un candidato, resolver condición y par con
   `markets-by-token`, y exigir que CLOB y Gamma coincidan en los dos tokens y
   sus etiquetas. Consultar Gamma con `closed=false` y `closed=true`; registrar
   por separado cualquier discrepancia con la documentación.
5. Para las 856 valoraciones de frontera, resolver primero el timestamp exacto
   de cada transacción del bundle y aplicar la misma regla de observación no
   posterior. Una recepción solo recibe `received_basis` cuando el valor y su
   evidencia coinciden; una transferencia conserva su valor de salida por
   separado.
6. Compilar un bundle nuevo con esos artefactos como evidencia. Nunca editar
   el bundle v5 ni reemplazar la salida de basis v2.
7. Construir el cálculo independiente a partir de una procedencia separada;
   el snapshot de cuenta puede ser contraste de posiciones/equity, no un atajo
   para declarar PnL histórico.

## Reglas de rechazo

- No usar precios actuales para un bloque histórico.
- No redondear `float` silenciosamente: retener el texto/raw de origen y
  convertirlo a `Decimal` bajo una política explícita.
- No suponer que un token sin historial vale cero.
- No inferir Yes/No a partir de la posición `primary/secondary` sin contrastar
  el par etiquetado completo.
- No aceptar un ZIP actual como sustituto de la evidencia de un bloque pasado.
- No marcar el gate como aprobado por completar una sola fuente o familia de evidencia.

Antes de iniciar una captura masiva se debe añadir un manifest de fuente,
límites de frescura, reintentos/rate-limit y hashes, y ejecutar primero una
sonda acotada contra la cola sellada.

## Sonda realizada

La sonda requerida ya se completó localmente como
`car_lifetime_price_probe_20261006_v2/`. Validó la cola de 3.498 marcas,
ancló el bloque con dos RPC, consultó 20 outcomes CTF canónicos y conservó los
20 cuerpos raw. Con una ventana explícita de 3.600 segundos hubo 4 candidatos
frescos y 16 antiguos; no se modificó ningún bundle ni se aprobó P0. El
registro, hashes y limitaciones están en
[`POLYLEDGER_P0_PRICE_PROBE_20261006.md`](POLYLEDGER_P0_PRICE_PROBE_20261006.md).

## Auditoría de identidad realizada

La salida autoritativa `car_lifetime_price_candidate_audit_20261006_v2/`
hizo cuatro consultas por candidato: parent market, CLOB etiquetado y las
vistas Gamma abierta/cerrada. Guardó 16/16 respuestas raw, sin errores, y
confirmó 4/4 identidades oficiales actuales; dos tienen corroboración local anterior al corte.
Tres respuestas vivas discreparon del orden Yes/No documentado para
`primary/secondary`, pero CLOB y Gamma coincidieron en el par etiquetado. Se
integraron cero marcas y P0 sigue bloqueado. Véase
[`POLYLEDGER_P0_PRICE_CANDIDATE_AUDIT_20261006.md`](POLYLEDGER_P0_PRICE_CANDIDATE_AUDIT_20261006.md).

## Política de aceptación realizada

El 7 de octubre se ejecutó la política sellada
`car-lifetime-closing-mark-policy-v1`. Revalidó manifests, paginación, campo
CLOB `c`, par binario Gamma, metadata local anterior al corte y estado CTF
post-bloque con los RPC archive fijados `polygon.drpc.org` y
`tenderly.rpc.polygon.community`. Ambos coincidieron en 4/4 condiciones no
resueltas. Dos candidatos pasaron para una futura compilación y dos quedaron
aplazados por falta de identidad histórica anterior al corte. Se escribieron
cero marcas; detalle y hashes en
[`POLYLEDGER_P0_CLOSING_MARK_POLICY_20261007.md`](POLYLEDGER_P0_CLOSING_MARK_POLICY_20261007.md).

## Continuación por lotes

El lote reanudado 21–40 quedó sellado con 20 respuestas de precios: cinco
frescas, catorce antiguas y una sin observación. Su auditoría guardó 20/20
respuestas oficiales de identidad y confirmó 5/5 mapeos. La política separada
del lote obtuvo acuerdo exacto entre dRPC y Tenderly, aceptó un candidato sólo
para un bundle futuro y rechazó cuatro por NegRisk. No escribió marcas ni
modificó v5. El cursor siguiente sobre los 2.870 outcomes CTF elegibles es 40;
los 3.498 activos positivos totales incluyen además 627 Combo y un pUSD. Véase
[`POLYLEDGER_P0_PRICE_BATCH_20261007.md`](POLYLEDGER_P0_PRICE_BATCH_20261007.md).
