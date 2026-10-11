# Lote de precios 61–80 — PolyLedger P0 / `car` / 2026-10-09

Estado: **captura, auditoría y política completas; 2 candidatos aceptados sólo
para un bundle futuro, 2 rechazados por NegRisk, 0 integrados; P0 continúa
`BLOCKED`.**

## Alcance

El lote continuó desde el cursor 60 sobre los 2.870 outcomes CTF elegibles de
la cola sellada. Conservó el bloque de cierre `93.762.690`, el corte histórico
`1789349067`, la ventana máxima de 900 segundos y el límite de 20 outcomes. No
escribió marcas ni modificó el bundle v5.

## Captura sellada

Salida local:
`data/polyledger-sentinel/p0/car_lifetime_price_probe_20261009_batch_00061_00080_v1/`.

| Campo | Resultado |
|---|---:|
| Cursor de entrada | 60 |
| Rango seleccionado | índices 60–79 / secuencias 61–80 |
| Cursor siguiente | 80 |
| Consultas intentadas / raw guardados | 20 / 20 |
| Candidatos frescos | 4 |
| Candidatos antiguos | 16 |
| Sin observación | 0 |
| Marcas escritas | 0 |

Los cuatro candidatos frescos tenían timestamp `1789348500`, edad 567
segundos y resolución 300 segundos respecto del corte.

## Auditoría oficial

La salida
`car_lifetime_price_candidate_audit_20261009_batch_00061_00080_v1/`
guardó 16/16 respuestas oficiales, sin errores de transporte ni parseo.
Confirmó 4/4 identidades actuales y 4/4 mapeos locales anteriores al corte.
Dos respuestas parent discreparon del orden documentado `primary/secondary`;
el auditor derivó las etiquetas desde el par completo CLOB/Gamma. Los cuatro
candidatos quedaron pendientes de una política, nunca integrados.

## Política específica

`configs/polyledger/closing_mark_policy_batch_00061_00080_v1.json` fija por
hash la sonda y la auditoría de este lote. Mantiene `allow_negrisk=false` y no
autoriza escritura, aprobación automática ni ejecución.

dRPC y Tenderly coincidieron exactamente en bloque, timestamp y estado. Las
cuatro condiciones tenían `payoutDenominator=0`, por lo que estaban sin
resolver en el estado posterior del bloque de cierre.

| Seq. | Outcome y mercado | Precio | Decisión |
|---:|---|---:|---|
| 63 | YES — Demócratas ganan GA-13 por 50–55 % | `0.106` | Rechazado: NegRisk |
| 67 | YES — valoración de Anthropic alcanza $2,0 T antes del 31 de diciembre | `0.815` | `ACCEPTED_FOR_NEW_BUNDLE_COMPILATION` |
| 69 | NO — republicanos ganan Senado de West Virginia por 30–35 % | `0.7` | Rechazado: NegRisk |
| 71 | NO — Clavicular visita Corea del Norte en 2026 | `0.97` | `ACCEPTED_FOR_NEW_BUNDLE_COMPILATION` |

“Aceptado” sólo habilita una futura compilación separada. La política escribió
cero marcas, no calculó PnL y no modificó v5.

## Sellos relevantes

| Artefacto | SHA-256 |
|---|---|
| Sonda `run_manifest.json` | `eaa4b878bd9412b2ac578a05f15e4ae134a7ff69a1fc9dc8276fa0a2fbbdc70c` |
| Sonda `summary.json` | `8449d855cdbadaa8bd5bb57d505dd87359b90494cb730cc12d20257058e34bae` |
| Auditoría `run_manifest.json` | `53eda698b6ac274b2d4d03fa145c2d39fb9d667df509467a5ff23d4b845af86d` |
| Auditoría `summary.json` | `23014b74fb47731be501c5ec43af1b1d993ffe3d9fed397d797afaba14d1e83f` |
| Configuración de política | `18cd992c8458386d49fd4a85eab05b9f87a7317821a7a3254465f6f49e6d1059` |
| Política `run_manifest.json` | `88d69360e602a593731ed72442fa87db35bddb0d0ed6cf539d612fbdc44c3da3` |
| Política `summary.json` | `aaaa8d3a29ff422d8c352a54e97eeea8a01f64e1f2b52b75cec5bf2bbb6b87b9` |
| Bundle v5 `bundle.json` | `6244785cc1cdbebc212b5a3cb07875bb031965934d8f213d02df63de108a2edc` |

La política se ejecutó en el commit `f94a0fa` con árbol limpio. Los manifests
y archivos declarados se revalidaron y el bundle mantuvo su hash.
La validación posterior ejecutó 149 pruebas de `test_ledger*` sin fallos y verificó 11
configuraciones de bots sin errores ni advertencias.

## Continuidad

Incluyendo la sonda inicial y los tres lotes reanudables, se revisaron 14
candidatos: 6 aceptados sólo para un bundle futuro, 2 aplazados y 6 rechazados.
Se integraron cero. El siguiente cursor es 80 y corresponde a las secuencias
81–100.

Las 3.498 solicitudes de marca siguen abiertas hasta que una compilación nueva
incorpore evidencia sellada. También continúan pendientes 856 flujos externos,
el basis de 772 recepciones y el informe contable independiente. No hay
autorización para wallet, firma, órdenes, retiros ni dinero real.
