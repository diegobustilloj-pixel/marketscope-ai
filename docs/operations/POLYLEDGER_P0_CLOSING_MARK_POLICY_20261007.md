# Política sellada de marcas de cierre — PolyLedger P0 / `car` / 2026-10-07

Estado: **evaluación completa; 2 candidatos aceptados para una futura
compilación, 2 aplazados, 0 integrados; P0 continúa `BLOCKED`.**

Esta etapa convierte la revisión humana pendiente de la sonda en una política
reproducible. No escribe `closing_marks`, no calcula PnL, no modifica el bundle
v5 y no autoriza órdenes, firma, wallet, retiros ni dinero real.

## Artefactos y código

- Política versionada:
  `configs/polyledger/closing_mark_policy_v1.json`.
- Evaluador:
  `src/polymarket_bot/ledger/price_mark_policy.py`.
- Comando:
  `python -m polymarket_bot.ledger evaluate-price-policy`.
- Auditoría de entrada local:
  `data/polyledger-sentinel/p0/car_lifetime_price_candidate_audit_20261006_v2/`.
- Salida local sellada:
  `data/polyledger-sentinel/p0/car_lifetime_closing_mark_policy_20261007_v1/`.
- Commit registrado por la corrida:
  `f39a06d908ceb988696b704820316a1d9d3bd39d` con árbol limpio.

La salida local contiene únicamente `configuration.json`,
`settlement_evidence.json`, `decisions.json`, `summary.json` y
`run_manifest.json`. No contiene `closing_marks.json`.

## Raíz de confianza validada

La política no acepta una ruta arbitraria como evidencia. Fija y vuelve a
verificar la cadena completa:

| Elemento | SHA-256 o digest fijado |
|---|---|
| Auditoría de candidatos — manifest | `eae2f61d7f2297ebfcb8eba028bec930d2b7f02ccb909ff1ff1041f3d45d1763` |
| Auditoría de candidatos — summary | `4b8d55537ae86fa0aed8cd47e60ff31fc05d5c4065cdc4fc3c6d6ac4e07d6579` |
| Sonda de precios — manifest | `471f55051f5e5b9b112177507bc181906feecd7abe634d6ab59f029e79eae3fe` |
| Cola de brechas — manifest | `87addaf89aeac7bf8508f86ae7a868b6ae6bd0ffa24ce36b6d79d54241045a11` |
| Bundle v5 — manifest | `877bae404f83ec6f52d1bc24c93c269ea886704dd0a394a9f68a914d69c23172` |
| `bundle.json` | `6244785cc1cdbebc212b5a3cb07875bb031965934d8f213d02df63de108a2edc` |
| Digest canónico del bundle | `7dc9c3feb44df00cf64dc88b3a10ec0512026f88aef110a52db1e8911811372b` |
| Política | `5ca1e0ebff4213a80bbbc6718aa2e6d03574d3478ba16a04772b3c81083ca765` |

El directorio completo del bundle, la sonda, la cola y la auditoría se
verifican antes y después de las lecturas de red. También se exige que la base
local de metadata conserve el SHA-256
`2767f82168bd9a308edf47b93d365623af092d2a8fe1ce7a83a4ac7a2f1511b3`
y que su WAL tenga cero bytes antes, después y al final.

## Reglas de aceptación v1

Un precio histórico queda aceptado únicamente para una **futura compilación de
bundle nuevo** cuando cumple todas estas condiciones:

1. precio decimal estrictamente entre 0 y 1;
2. timestamp no posterior al corte y edad recalculada exactamente;
3. edad máxima de 900 segundos;
4. resolución entre 1 y 300 segundos;
5. bucket completo antes o exactamente en el corte;
6. respuesta histórica completa, sin paginación pendiente;
7. identidad actual coherente entre parent market, CLOB y Gamma;
8. campo compacto CLOB `c` igual al condition ID;
9. par binario Yes/No y mercado no NegRisk;
10. corte dentro de la ventana declarada y cierre actual posterior al corte;
11. mapeo local token–condición–outcome capturado antes del corte;
12. estado CTF no resuelto en el estado posterior del bloque de cierre,
    confirmado por los dos RPC archive aprobados.

La regla 11 explica los dos aplazamientos. La política no sustituye evidencia
histórica ausente con metadata obtenida después.

## Dos anclajes temporales, sin mezclarlos

- Corte del precio API: `1789349067`, un segundo antes del timestamp del
  bloque. Sólo se considera un bucket histórico ya cerrado a ese instante.
- Estado contable/on-chain: estado posterior del bloque `93.762.690`, hash
  `0xcb952565d49457d93bcd4ae549bbfe7a7b31a26fe8855dd6b993d46638f30b72`,
  timestamp `1789349068`. Este anclaje coincide con el inventario de cierre.

Si CTF estuviera resuelto en ese estado posterior, su vector de payout tendría
precedencia sobre el historial. El evaluador consulta denominador y, cuando
corresponde, ambos numeradores; exige que el vector binario sume exactamente el
denominador y conserva la razón exacta, no un decimal aproximado.

## Acuerdo on-chain independiente

El primer intento operativo con PublicNode falló de forma segura porque el
endpoint gratuito devolvió el encabezado, pero no el estado histórico. No creó
salida ni `.partial`.

La ejecución autoritativa fijó dos proveedores públicos distintos:

- `polygon.drpc.org`;
- `tenderly.rpc.polygon.community`.

Ambos devolvieron chain ID `137`, el mismo bloque, hash y timestamp, y el mismo
`payoutDenominator=0` para las cuatro condiciones usando EIP-1898
`blockHash + requireCanonical`. Resultado: 4/4 condiciones
`UNRESOLVED_AT_CLOSING_BLOCK`; cero discrepancias y cero condiciones resueltas.

## Decisiones reales

Todos los puntos tienen timestamp `1789348500`, edad 567 segundos y resolución
300 segundos. El bucket termina antes del corte.

| Seq. | Outcome y mercado | Precio candidato | Decisión | Motivo |
|---:|---|---:|---|---|
| 2 | YES — OpenAI adquiere Pinterest en 2026 | `0.0215` | `ACCEPTED_FOR_NEW_BUNDLE_COMPILATION` | Cumple todos los checks, incluida identidad local pre-corte |
| 3 | NO — Houthis entran en Aden antes de 2027 | `0.635` | `DEFERRED_MISSING_HISTORICAL_IDENTITY` | Falta mapeo local capturado antes del corte |
| 7 | NO — Houthis capturan un petrolero antes del 30 de septiembre | `0.915` | `DEFERRED_MISSING_HISTORICAL_IDENTITY` | Falta mapeo local capturado antes del corte |
| 19 | NO — Apple lanza un iPhone plegable antes del 31 de octubre | `0.014` | `ACCEPTED_FOR_NEW_BUNDLE_COMPILATION` | Cumple todos los checks, incluida identidad local pre-corte |

Resumen: 2 aceptados para futura compilación, 2 aplazados y 0 rechazados por
otra regla. Aceptado no significa integrado ni económicamente validado.

## Sellos de la salida

| Archivo | SHA-256 |
|---|---|
| `run_manifest.json` | `95e4a8eace4f3454090a0553b00c5597168f998349f0d899fac1756772557e9c` |
| `configuration.json` | `31a7a4127aca244ddd3b80d31607584f7916c54ef929903478e69c9fe38f20ee` |
| `settlement_evidence.json` | `2796d044505d4f5a1e14ad2c1dd80670cd4d46fa86c531350e00481dd9b07561` |
| `decisions.json` | `c8cbbc6cb70fb254eea1a68650e34e550fcbfd5706cd633ae427f046bc042f0f` |
| `summary.json` | `b2a4a83f16746ec5290851800ee8b9754c792dda9adb494db7ecbff776682a12` |

Los cuatro hashes de archivos declarados por el manifest se recalcularon y
coincidieron. El hash de `bundle.json` después de la ejecución siguió siendo
`6244785cc1cdbebc212b5a3cb07875bb031965934d8f213d02df63de108a2edc`.

## Qué sigue bloqueado

- Se integraron **cero** marcas; el bundle v5 permanece inmutable.
- La cola original sigue teniendo 3.498 solicitudes de marca. Esta muestra no
  reduce el conteo hasta compilar evidencia aceptada en un bundle nuevo.
- Continúan abiertas 856 evidencias de flujo externo y el basis de 772
  recepciones.
- Falta el segundo cálculo contable independiente.

El siguiente paso correcto no es llamar PnL a estos dos precios. Es ampliar la
captura CTF bajo esta misma política, resolver una fuente histórica admisible
para identidades que hoy quedan aplazadas y, sólo después, construir un bundle
nuevo sin sobrescribir v5. P0 continúa `BLOCKED`.
