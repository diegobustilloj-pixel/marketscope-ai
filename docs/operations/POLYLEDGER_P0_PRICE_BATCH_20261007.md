# Lote de precios 21–40 — PolyLedger P0 / `car` / 2026-10-07

Estado: **captura, auditoría y política completas; 1 candidato aceptado sólo
para un bundle futuro, 4 rechazados por NegRisk, 0 integrados; P0 continúa
`BLOCKED`.**

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

## Reintento sellado

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

## Continuidad

El siguiente cursor es 40, por lo que la próxima captura corresponde a las
secuencias 41–60 y debe usar otro directorio. Aunque existen ahora tres precios
aceptados entre los dos lotes, las 3.498 solicitudes permanecen abiertas hasta
que una compilación nueva incorpore evidencia sellada. También siguen abiertos
los 856 flujos externos, el basis de 772 recepciones y el informe contable
independiente. No hay autorización para wallet, firma, órdenes ni dinero real.
