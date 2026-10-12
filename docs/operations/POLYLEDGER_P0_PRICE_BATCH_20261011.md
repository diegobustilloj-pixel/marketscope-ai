# Lote de precios 81–100 — PolyLedger P0 / `car` / 2026-10-11

Estado: **captura, auditoría y política completas; 3 candidatos aceptados sólo
para un bundle futuro, 0 rechazados, 0 integrados; P0 continúa `BLOCKED`.**

## Alcance

El lote continuó desde el cursor 80 sobre los 2.870 outcomes CTF elegibles.
Conservó el bloque de cierre `93.762.690`, el corte `1789349067`, una antigüedad
máxima de 900 segundos y el límite de 20 outcomes. No escribió marcas ni
modificó el bundle v5.

## Captura sellada

Salida local:
`data/polyledger-sentinel/p0/car_lifetime_price_probe_20261011_batch_00081_00100_v1/`.

| Campo | Resultado |
|---|---:|
| Cursor de entrada | 80 |
| Rango seleccionado | índices 80–99 / secuencias 81–100 |
| Cursor siguiente | 100 |
| Consultas intentadas / raw guardados | 20 / 20 |
| Candidatos frescos | 3 |
| Candidatos antiguos | 17 |
| Sin observación | 0 |
| Marcas escritas | 0 |

Los tres candidatos frescos tenían timestamp `1789348500`, edad 567 segundos
y resolución 300 segundos respecto del corte.

## Auditoría oficial

La salida
`car_lifetime_price_candidate_audit_20261011_batch_00081_00100_v1/` guardó
12/12 respuestas oficiales, sin errores de transporte ni parseo. Confirmó 3/3
identidades y mapeos locales anteriores al corte. Dos respuestas parent
discreparon del orden documentado `primary/secondary`; las etiquetas se
derivaron del par completo CLOB/Gamma. Los tres quedaron pendientes de una
política, nunca integrados.

## Política específica

`configs/polyledger/closing_mark_policy_batch_00081_00100_v1.json` fija por
hash la sonda y auditoría del lote. Mantiene `allow_negrisk=false` y no autoriza
escritura ni ejecución.

dRPC y Tenderly coincidieron exactamente en bloque, timestamp y estado. Las
tres condiciones tenían `payoutDenominator=0`, por lo que estaban sin resolver
en el estado posterior del bloque de cierre.

| Seq. | Outcome y mercado | Precio | Decisión |
|---:|---|---:|---|
| 84 | YES — portada de NYPost muestra `$Laptop` antes del 23 de septiembre | `0.095` | `ACCEPTED_FOR_NEW_BUNDLE_COMPILATION` |
| 89 | NO — Irán cobra tarifas en Ormuz antes del 31 de octubre | `0.765` | `ACCEPTED_FOR_NEW_BUNDLE_COMPILATION` |
| 97 | NO — terremoto de magnitud 10,0 o mayor antes de 2027 | `0.9875` | `ACCEPTED_FOR_NEW_BUNDLE_COMPILATION` |

“Aceptado” sólo habilita una futura compilación separada. La política escribió
cero marcas, no calculó PnL y no modificó v5.

## Sellos relevantes

| Artefacto | SHA-256 |
|---|---|
| Sonda `run_manifest.json` | `9ebaa223a79ce793d4c93a1c15dbf86979d1dad665ba7dec971d11a8b8f11f3e` |
| Sonda `summary.json` | `6946029e9bb4e9992b72e88867737d160e325f4a998151bcaecfcda3fff28646` |
| Auditoría `run_manifest.json` | `388c5c828096ccfd75b43e70da7ca636731c52f520ae935ce4231120ba8c4406` |
| Auditoría `summary.json` | `83487332a7b684b440dd5d889d12eb2e0b069afdcbe5a1c6f450da5f1ce73c52` |
| Configuración de política | `ba052bdebe1725d17defa69592294333a678d0ed14a6d8479ea4750430c31a91` |
| Política `run_manifest.json` | `a5ef9b6e60d886d88ab515cab79e8d1d04cfb1392189f411ef4ec50156338269` |
| Política `summary.json` | `dde6ec836de7c2fd427668e21291a49c60c8c1bd3a001779de491065b575c126` |
| Bundle v5 `bundle.json` | `6244785cc1cdbebc212b5a3cb07875bb031965934d8f213d02df63de108a2edc` |

La política se ejecutó en el commit `14fd1ec` con árbol limpio. Los manifests y
archivos declarados se revalidaron y el bundle mantuvo su hash.
La validación posterior ejecutó 149 pruebas de `test_ledger*` sin fallos y
verificó 11 configuraciones de bots sin errores ni advertencias.

## Continuidad

Incluyendo la sonda inicial y cuatro lotes reanudables, se revisaron 17
candidatos: 9 aceptados sólo para un bundle futuro, 2 aplazados y 6 rechazados.
Se integraron cero. El siguiente cursor es 100 y corresponde a las secuencias
101–120.

Las 3.498 solicitudes de marca siguen abiertas hasta que una compilación nueva
incorpore evidencia sellada. También continúan pendientes 856 flujos externos,
el basis de 772 recepciones y el informe contable independiente. No hay
autorización para wallet, firma, órdenes, retiros ni dinero real.
