# Auditoría de evidencia faltante — PolyLedger P0 / `car`

**Corte:** 2026-10-06, a partir del bundle v5 sellado y del resultado de basis v2.
Este documento enumera ausencias comprobadas; no asigna precios, basis ni PnL
por aproximación.

## Estado que ya está resuelto

- Se reprodujeron las 251.107 acciones sin error de reconstrucción.
- La conciliación de balances de cierre es `MATCH`.
- La cobertura declarada, el universo de activos y las acciones sin resolver
  no son el bloqueo actual.

No hace falta redescargar el historial ni rehacer el cierre de recibos para
resolver los bloques siguientes.

## Evidencia que falta

| Bloque | Medición exacta | Consecuencia |
|---|---:|---|
| Valor/evidencia de flujos externos | 856 de 868 acciones (`receive`: 772; `transfer`: 84) | No se puede calcular el flujo externo neto ni el devengo de transferencias. |
| Basis de recepciones | 772 `receive` sin `received_basis` ni `basis_evidence` | Propaga basis desconocido a 159 ventas y a 1.204 activos de inventario de cierre. |
| Marcas de cierre | 0 marcas entregadas para 3.498 activos con inventario | No hay valoración de cierre ni PnL no realizado verificable. |
| Evidencia de marcas de cierre | 0 fuentes declaradas | Incluso una marca numérica sin procedencia seguiría bloqueada. |
| Pipeline independiente | no existe `independent_report` | El `basis_gate` no puede aprobarse aunque el primer cálculo sea completo. |

Los otros 12 flujos externos sí llevaban valor y evidencia en el bundle. Esa
diferencia confirma que el esquema admite pruebas de frontera; no se debe
rellenar los 856 restantes con cero.

## Cola sellada creada

La utilidad offline `audit-basis-evidence` creó localmente
`data/polyledger-sentinel/p0/car_lifetime_basis_evidence_gaps_20261006_v1/`
con el commit `d0784dd`. El manifiesto y sus hashes fueron verificados con el
árbol limpio. La cola contiene referencias compactas, no una copia del bundle:

| Archivo local | Tamaño aproximado | Hash de contenido relevante |
|---|---:|---|
| `external_flow_requests.json` | 476 KB | `0b3b3e2d004af6f7e4ad73af312ec6ac5396d1f97759a719d925e2daeafd3b2b` |
| `closing_mark_requests.json` | 802 KB | `f30c68a1210113b451823256da1a3707f2a3062c7b346f7642cc35b68103f880` |
| `independent_report_request.json` | < 1 KB | sellado en el manifiesto |

Las solicitudes enlazan cada acción por ID, orden y hash; los `raw_ids` siguen
solamente en el bundle v5. La cola se conserva localmente e ignorada por Git.

## Orden seguro de resolución

1. Usar la cola local sellada ya creada para capturar respuestas de valoración
   de frontera de las 772 recepciones y 84 transferencias. Cada respuesta debe
   enlazar la acción y su evidencia del bundle, y conservar fuente,
   bloque/tiempo, valor atómico y explicación de la metodología.
2. Capturar o respaldar marcas de los 3.498 activos al bloque de cierre
   `93.762.690`; cada marca debe ser un `Decimal` exacto, con fuente y corte
   comprobables. No inferir la marca actual para el corte histórico. La sonda
   de 20 CTF y su auditoría de identidad ya existen: los cuatro candidatos
   frescos tienen mapeos oficiales actuales consistentes; sólo dos poseen
   corroboración local anterior al corte. La política v1 aceptó esos dos sólo
   para un bundle futuro y aplazó los otros dos. El lote 21–40 añadió cinco
   candidatos auditados con identidad histórica; su política aceptó uno y
   rechazó cuatro por NegRisk. El lote 41–60 añadió un candidato auditado y
   aceptado sólo para un bundle futuro. Las tres capturas integraron cero
   marcas. Por tanto, las 3.498 solicitudes siguen abiertas y no se reduce este
   conteo.
3. Recompilar un bundle nuevo con esa evidencia, sin cambiar ni sobrescribir el
   v5, y repetir `inventory-basis` en otro directorio nuevo.
4. Construir un segundo cálculo con implementación y procedencia separadas que
   produzca el contrato `independent_expected_contract`.
5. Solo si ambos cálculos y la conciliación coinciden, reevaluar el gate. Esto
   sigue sin autorizar capital, wallet ni copiado automático.

## Artefactos de referencia local

- Bundle: `data/polyledger-sentinel/p0/car_lifetime_basis_bundle_20260915_v5/`
- Resultado sellado: `data/polyledger-sentinel/p0/car_lifetime_basis_result_20261006_v2/`
- Sonda de precios sellada: `data/polyledger-sentinel/p0/car_lifetime_price_probe_20261006_v2/`
- Auditoría de candidatos sellada: `data/polyledger-sentinel/p0/car_lifetime_price_candidate_audit_20261006_v2/`
- Evaluación de política sellada: `data/polyledger-sentinel/p0/car_lifetime_closing_mark_policy_20261007_v1/`
- Sonda sellada 21–40: `data/polyledger-sentinel/p0/car_lifetime_price_probe_20261007_batch_00021_00040_retry1/`
- Auditoría sellada 21–40: `data/polyledger-sentinel/p0/car_lifetime_price_candidate_audit_20261007_batch_00021_00040_v1/`
- Política sellada 21–40: `data/polyledger-sentinel/p0/car_lifetime_closing_mark_policy_20261007_batch_00021_00040_v1/`
- Sonda sellada 41–60: `data/polyledger-sentinel/p0/car_lifetime_price_probe_20261007_batch_00041_00060_v1/`
- Auditoría sellada 41–60: `data/polyledger-sentinel/p0/car_lifetime_price_candidate_audit_20261007_batch_00041_00060_v1/`
- Política sellada 41–60: `data/polyledger-sentinel/p0/car_lifetime_closing_mark_policy_20261007_batch_00041_00060_v1/`
- Compuerta de informe independiente: `data/polyledger-sentinel/p0/car_lifetime_independent_report_gate_20261007_v1/`
- Registro de corrida: `docs/operations/POLYLEDGER_P0_RUN_20261006.md`
- Registro de sonda: `docs/operations/POLYLEDGER_P0_PRICE_PROBE_20261006.md`
- Registro de auditoría: `docs/operations/POLYLEDGER_P0_PRICE_CANDIDATE_AUDIT_20261006.md`
- Registro de política: `docs/operations/POLYLEDGER_P0_CLOSING_MARK_POLICY_20261007.md`

Las tres políticas aceptaron cuatro de diez candidatos únicamente para una
futura compilación: dos quedaron aplazados por falta de identidad histórica y
cuatro fueron rechazados por NegRisk. Como no escribieron `closing_marks` ni
recompilaron el bundle, las 3.498 solicitudes siguen abiertas y sus conteos no
se deben reducir todavía.

La compuerta offline del segundo informe también quedó ejecutada: el informe
externo aún no existe, por lo que el resultado es `INDEPENDENT_REPORT_GATE_BLOCKED`
y no modifica el basis ni el estado P0. El contrato esperado está sellado por
hash en esa salida y deberá producirlo una implementación con método, commit y
evidencia propios.

Los artefactos de datos y resultados se conservan solo
localmente. Este diagnóstico puede
versionarse porque contiene únicamente conteos, hashes y reglas de continuidad.
