# Sonda histórica oficial — PolyLedger P0 / `car` / 2026-10-06

**Estado:** captura de evidencia completada; no es una valoración aprobada,
no modifica el bundle y no habilita PnL, copia ni ejecución.

## Propósito y alcance limitado

La sonda prueba de forma pequeña y reproducible el endpoint oficial de
historial puntual de Polymarket antes de contemplar una captura masiva. Usa la
cola local sellada de marcas faltantes, no selecciona tokens a mano y no pide
precios para activos cuyo tipo no corresponde al CLOB.

| Elemento | Valor comprobado |
|---|---|
| Bundle de entrada | `car_lifetime_basis_bundle_20260915_v5/bundle.json` |
| SHA-256 del bundle | `6244785cc1cdbebc212b5a3cb07875bb031965934d8f213d02df63de108a2edc` |
| Cola validada | `car_lifetime_basis_evidence_gaps_20261006_v1/` |
| Digest de sus 3.498 solicitudes de marca | `f30c68a1210113b451823256da1a3707f2a3062c7b346f7642cc35b68103f880` |
| Selección | primeros 20 outcomes CTF ordenados canónicamente de la cola |
| Excluidos sin consulta | 627 posiciones Combo y 1 pUSD; no son outcomes CTF del CLOB |
| Fuente | `GET https://data-api.polymarket.com/v2/prices-history?token_id=…&as_of=…` |
| Frescura usada solo para clasificación | 3.600 segundos |

El endpoint puntual y la semántica `as_of` se documentan oficialmente en
[Price history for a token](https://docs.polymarket.com/api-reference/markets/get-a-tokens-price-history).
El endpoint batch sigue fuera de esta prueba: su documentación describe una
ventana temporal y no establece la misma semántica puntual `as_of`, por lo que
no se lo puede tratar como equivalente sin una validación propia.

## Ancla temporal

Dos RPC públicos de Polygon —`polygon-bor-rpc.publicnode.com` y
`polygon.drpc.org`— coincidieron en cadena, número, hash y timestamp del
bloque de cierre:

- bloque `93.762.690`;
- hash `0xcb952565d49457d93bcd4ae549bbfe7a7b31a26fe8855dd6b993d46638f30b72`;
- timestamp del bloque `2026-09-14T01:24:28Z` (`1789349068`).

La consulta usó `as_of=1789349067`, un segundo antes del bloque. Esta regla
evita aceptar una operación posterior dentro del mismo segundo/granularidad
del bloque como si fuera una marca preexistente.

## Resultado local sellado

La salida autoritativa de esta sonda es local y permanece ignorada por Git:

`data/polyledger-sentinel/p0/car_lifetime_price_probe_20261006_v2/`

Su manifiesto verificó 25 archivos (configuración, ancla, solicitudes,
resultados y 20 respuestas raw) con el commit `a349668` y árbol limpio. Los
hashes principales son:

| Artefacto local | SHA-256 |
|---|---|
| `configuration.json` | `12b7278826766fbec08e9775ef170a1f55f1e01e677622a49d366e6b5d137028` |
| `closing_block_header.json` | `eca4ca308e54bfcd821a70995fd379630ce854be7bef71ebe11cbde99a5b5b9f` |
| `requests.json` | `c73007e8930eb6a1ba9689480bbff3e82b6a3a3afee32a2b797247f59e7136c8` |
| `request_results.json` | `dbc826876a9ead599fcf9ab58827e27ea134590a6fd4725b14f271bcdfd1ff2a` |
| `summary.json` | `ce092c1e5b24bc55d7a064ceeca80fec4be783b526671e56ad6434e1594051ff` |

Las 20 consultas devolvieron HTTP 200 y sus bytes exactos se conservaron por
separado. El formato actual de la respuesta fue `data` con `timestamp`,
`price` y, cuando estaba disponible, `resolution_seconds`; el lector conserva
el texto decimal y rechaza puntos posteriores al corte.

| Clasificación de la sonda | Conteo |
|---|---:|
| Candidatos frescos (edad <= 3.600 s) | 4 |
| Candidatos antiguos | 16 |
| Respuestas raw guardadas | 20 |
| Marcas escritas en un bundle | 0 |
| Cambios al bundle v5 | 0 |

Las edades observadas van de 567 a 62.817.867 segundos. Las resoluciones
publicadas fueron 300 o 10.800 segundos. Una marca CLOB es una referencia de
mercado, no prueba de profundidad ejecutable ni de PnL.

## Compatibilidad detectada y preservación

La primera ejecución local `car_lifetime_price_probe_20261006_v1/` conservó
20 respuestas HTTP 200 como bytes raw, pero el lector inicial esperaba el
campo legado `history`. Al encontrar el formato actual `data`, clasificó las
respuestas como no integrables y no escribió marcas. Se preserva como
evidencia de compatibilidad; no se sobrescribió. El commit posterior añadió
el parser estricto de `data`, y la v2 es la salida de interpretación vigente.

## Auditoría posterior de identidad

Los cuatro candidatos de la v2 ya fueron auditados en una salida separada:
`car_lifetime_price_candidate_audit_20261006_v2/`. Se verificaron de nuevo el
manifiesto, las cabeceras Polygon, el corte, las URLs y los bytes raw; después
se hicieron 16 consultas oficiales a CLOB y Gamma. Las 16 respondieron y no
hubo errores de transporte ni interpretación. Los cuatro tokens quedaron
ligados de forma consistente a condición, outcome y mercado en las fuentes
oficiales actuales; dos también
tienen metadata local anterior al corte.

La auditoría detectó una discrepancia real entre el orden Yes/No descrito por
`markets-by-token` y tres respuestas vivas. El motor v2 conserva ese hallazgo,
trata el par devuelto como no ordenado y exige coincidencia de etiquetas entre
CLOB y Gamma. El detalle, tabla de mercados y hashes está en
[`POLYLEDGER_P0_PRICE_CANDIDATE_AUDIT_20261006.md`](POLYLEDGER_P0_PRICE_CANDIDATE_AUDIT_20261006.md).

Identidad consistente significa `PENDING_MARK_POLICY_REVIEW`, no marca
aceptada: la auditoría escribió cero marcas y no cambió el bundle.

## Qué permanece bloqueado

La sonda no cambia el estado de P0. Siguen abiertos, como mínimo:

- 3.498 marcas de cierre requieren revisión e integración en un bundle nuevo;
- 856 valoraciones/evidencias de flujos externos;
- basis de 772 recepciones;
- el cálculo contable independiente requerido por el gate.

No se debe usar los cuatro candidatos frescos como PnL, señal de copia ni
autorización de capital. La semántica de identidad ya fue auditada y la
política de edad, resolución, mercados cerrados y rechazo ya fue ejecutada.
Aceptó dos candidatos sólo para un bundle futuro y aplazó dos por falta de
identidad histórica pre-corte. Véase
`POLYLEDGER_P0_CLOSING_MARK_POLICY_20261007.md`. Cualquier escalado debe usar
esa política, crear otra salida local sellada y nunca editar el bundle v5 ni
esta sonda.
