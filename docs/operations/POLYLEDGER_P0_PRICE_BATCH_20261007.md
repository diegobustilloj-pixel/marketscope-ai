# Lotes de precios 21–60 — PolyLedger P0 / `car` / 2026-10-07

Estado: **captura, auditoría y política completas hasta el cursor 60; en los
dos lotes reanudables, 2 candidatos aceptados sólo para un bundle futuro, 4
rechazados por NegRisk y 0 integrados; P0 continúa `BLOCKED`.**

## Alcance

`probe-price-history` usa un cursor estable sobre los 2.870 outcomes CTF
elegibles de la cola. El inventario positivo total sigue siendo 3.498 activos:
los otros 628 son 627 posiciones Combo y un pUSD que el endpoint CLOB no puede
valorar como outcomes. Cada lote queda separado, sellado y limitado a 20
outcomes; nunca escribe marcas ni modifica el bundle v5.

## Intento fallido preservado

La primera ejecución del rango 21–40 quedó bloqueada antes de consultar precios
porque el transporte no completó `eth_chainId`. Se conserva
`car_lifetime_price_probe_20261007_batch_00021_00040.partial/` con su
`configuration.json` y `failure.json`. No se borró ni reutilizó.

## Lote 21–40 sellado

Salida local:
`data/polyledger-sentinel/p0/car_lifetime_price_probe_20261007_batch_00021_00040_retry1/`.

| Campo | Resultado |
|---|---:|
| Cursor de entrada | 20 |
| Rango seleccionado | índices 20–39 / secuencias 21–40 |
| Cursor siguiente | 40 |
| Consultas intentadas / raw guardados | 20 / 20 |
| Candidatos frescos | 5 |
| Candidatos antiguos | 14 |
| Sin observación | 1 |
| Marcas escritas | 0 |

Los cinco candidatos frescos fueron las secuencias 21, 23, 24, 27 y 30. Todos
tenían timestamp `1789348500`, edad 567 segundos y resolución 300 segundos
respecto del corte `1789349067`.

## Auditoría oficial

La salida
`car_lifetime_price_candidate_audit_20261007_batch_00021_00040_v1/`
guardó 20/20 respuestas CLOB/Gamma, sin error de transporte ni parseo. Confirmó
5/5 identidades oficiales actuales y 5/5 mapeos locales capturados antes del
corte. Encontró dos discrepancias del orden vivo `primary/secondary`; el
auditor no confió en ese orden y volvió a derivar las etiquetas desde el par
CLOB/Gamma completo. Los cinco quedaron elegibles para revisión de política,
no integrados.

## Política específica del lote

La política versionada
`configs/polyledger/closing_mark_policy_batch_00021_00040_v1.json` fija por
hash esta auditoría y no reutiliza la raíz del lote anterior. Mantiene
`allow_negrisk=false`; esa regla no se relajó para mejorar artificialmente la
cobertura.

dRPC y Tenderly devolvieron exactamente el mismo bloque y estado: las cinco
condiciones tenían `payoutDenominator=0`, por lo que estaban sin resolver en el
estado posterior del bloque de cierre. Las decisiones fueron:

| Seq. | Outcome y mercado | Precio | Decisión |
|---:|---|---:|---|
| 21 | YES — margen republicano 30–35 % en Arkansas | `0.0555` | Rechazado: NegRisk |
| 23 | NO — Matthew Garwood gana Launceston | `0.455` | Rechazado: NegRisk |
| 24 | YES — margen republicano 25–30 % en Arkansas | `0.075` | Rechazado: NegRisk |
| 27 | YES — Israel cierra su espacio aéreo antes del 30 de septiembre | `0.9685` | `ACCEPTED_FOR_NEW_BUNDLE_COMPILATION` |
| 30 | NO — no ocurre reunión diplomática EE. UU.–Irán antes del 30 de septiembre | `0.0855` | Rechazado: NegRisk |

“Aceptado” sólo habilita una compilación posterior. La evaluación escribió cero
marcas, no calculó PnL y no modificó el bundle.

## Sellos relevantes

| Artefacto | SHA-256 |
|---|---|
| Sonda `run_manifest.json` | `94b02e4b7b09321bdb170011fc4ff6fac1fba4df182abb3f8acc3dffef294fc4` |
| Sonda `summary.json` | `903dd4044fcc5afcdc261243bf4c3156f636f54e041225a0f510762a56f67c79` |
| Auditoría `run_manifest.json` | `e4adc710721a42e9a51a9d95693a75dbb3ee86740549f20b5efc378bb1db24a2` |
| Auditoría `summary.json` | `af1cbcf208300985b2484254b57f5a1e04b2afffc9b835999f9dd5332c894aac` |
| Configuración de política | `37892e3a2a14b7dab2074e89049d1e0388f75e886db36ead1b3e9d1857b7751d` |
| Política `run_manifest.json` | `7ba128ed618ff5948017e76455f95c53802b4e7bc5e375ab77360983b6c3d4c9` |
| Política `summary.json` | `faadb07fdbe0526d791b552e53e53450100559a45f72ece29226d5e889295baa` |
| Bundle v5 `bundle.json` | `6244785cc1cdbebc212b5a3cb07875bb031965934d8f213d02df63de108a2edc` |

La política se ejecutó en el commit `c738956` con árbol limpio. Todos los
manifests fueron revalidados y el hash del bundle v5 permaneció idéntico.

## Lote 41–60 sellado

Salidas locales:

- sonda: `car_lifetime_price_probe_20261007_batch_00041_00060_v1/`;
- auditoría: `car_lifetime_price_candidate_audit_20261007_batch_00041_00060_v1/`;
- política: `car_lifetime_closing_mark_policy_20261007_batch_00041_00060_v1/`.

| Campo | Resultado |
|---|---:|
| Cursor de entrada | 40 |
| Rango seleccionado | índices 40–59 / secuencias 41–60 |
| Cursor siguiente | 60 |
| Consultas intentadas / raw guardados | 20 / 20 |
| Candidatos frescos | 1 |
| Candidatos antiguos | 19 |
| Sin observación | 0 |
| Marcas escritas | 0 |

El único candidato fresco fue la secuencia 59: outcome NO del mercado “Mike
Johnson out as Speaker by December 31?”, precio `0.875`, timestamp
`1789348500`, edad 567 segundos y resolución 300 segundos. La auditoría guardó
4/4 respuestas oficiales, sin errores, confirmó identidad actual y mapeo local
anterior al corte, y detectó una discrepancia de orden `primary/secondary` que
resolvió mediante el par etiquetado CLOB/Gamma.

La política nueva mantuvo `allow_negrisk=false`. El mercado no era NegRisk y
dRPC/Tenderly coincidieron en que su condición tenía `payoutDenominator=0` en
el estado posterior del bloque de cierre. La secuencia 59 quedó
`ACCEPTED_FOR_NEW_BUNDLE_COMPILATION`; no se escribió la marca ni se modificó
el bundle.

| Artefacto 41–60 | SHA-256 |
|---|---|
| Sonda `run_manifest.json` | `7e857f16bf47b1145f1c952795d210bc0c3f6115b2745ed2661b3a1f802e0594` |
| Sonda `summary.json` | `eab6f4905799d0e40040264adf3edb7788172ff5e9580efe1a6365378388ffec` |
| Auditoría `run_manifest.json` | `db8b4d65d0825f1a1409624e249d3a70d2c180999ca1b2bd64d081f026b5c2e8` |
| Auditoría `summary.json` | `ff9cea272548e968135540150a188c2fe5eb58d847264cc6794f1e4917064718` |
| Configuración de política | `ca6c28571c58b037309854d54e0018305a1b6bd048f9290281bbe44be033c292` |
| Política `run_manifest.json` | `f7718a9f23e2141bb1c430618c10ee76eaa8d0b59e1191742d8e2c02f419e9aa` |
| Política `summary.json` | `b8652202f30c8008705dfc60628eababec682413c48161cb9227117e73133cac` |

La política 41–60 se ejecutó en el commit `1648bc5` con árbol limpio. El
`bundle.json` v5 conservó SHA-256
`6244785cc1cdbebc212b5a3cb07875bb031965934d8f213d02df63de108a2edc`.
La validación posterior ejecutó 161 pruebas PolyLedger sin fallos y verificó 11
configuraciones de bots sin errores ni advertencias.

## Continuidad

El siguiente cursor es 60, por lo que la próxima captura corresponde a las
secuencias 61–80 y debe usar otro directorio. Incluyendo la sonda inicial y los
dos lotes reanudables, existen cuatro precios aceptados entre diez candidatos:
dos quedaron aplazados y cuatro rechazados. Las 3.498 solicitudes permanecen
abiertas hasta que una compilación nueva incorpore evidencia sellada. También
siguen abiertos los 856 flujos externos, el basis de 772 recepciones y el
informe contable independiente. No hay autorización para wallet, firma,
órdenes ni dinero real.
