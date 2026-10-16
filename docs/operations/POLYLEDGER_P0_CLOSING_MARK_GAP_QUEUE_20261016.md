# Cola de brechas de marcas de cierre — PolyLedger P0 / `car`

Estado: **implementada, ejecutada y sellada en modo offline; ninguna fila de
la cola es una marca aprobada.**

## Propósito

`build-closing-mark-gap-queue` toma la compilación completa de candidatos y la
cola original de evidencia, vuelve a verificar sus sellos y asigna cada activo
sin marca aceptada a una única causa y una única ruta de recuperación.

La operación verifica los 144 chunks de sonda y sus respuestas raw por hash.
También prueba que las 2.870 identidades CTF están cubiertas una sola vez y
que las exclusiones no CTF corresponden exactamente a los contratos Combo y
pUSD conocidos.

Reglas inmutables:

- una observación antigua queda sólo como diagnóstico;
- no se amplía el límite de edad de 900 o 3.600 segundos fijado por su sonda;
- no se interpola, inventa ni reutiliza un precio rechazado;
- no se modifica el bundle v5 ni el artefacto de 263 propuestas;
- no se escribe `closing_marks.json` en esta salida.

## Ejecución canónica

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger build-closing-mark-gap-queue `
  --compiled-candidates data/polyledger-sentinel/p0/car_closing_mark_candidates_20261016_v1 `
  --evidence-gaps data/polyledger-sentinel/p0/car_lifetime_basis_evidence_gaps_20261006_v1 `
  --output data/polyledger-sentinel/p0/car_closing_mark_gap_queue_20261016_v1
```

## Resultado canónico

La salida local contiene 3.235 activos únicos, que junto a las 263 propuestas
aceptadas particionan exactamente las 3.498 solicitudes originales:

| Categoría exclusiva | Activos | Ruta principal |
|---|---:|---|
| `STALE_CANDIDATE` | 2.398 | Obtener una observación histórica nueva y verificable dentro del límite original |
| `NO_OBSERVATION` | 53 | Buscar una fuente histórica alternativa o conservar ausencia explícita |
| `REJECTED_BY_POLICY` | 141 | Resolver NegRisk, settlement o ventana según el motivo sellado |
| `DEFERRED_HISTORICAL_IDENTITY` | 14 | Recuperar identidad token–condición–outcome anterior al corte |
| `DEFERRED_CURRENT_IDENTITY` | 1 | Recuperar identidad oficial actual o archivada |
| `COMBO_POSITION_NOT_CLOB_OUTCOME` | 627 | Reconstruir componentes de la posición Combo |
| `PUSD_COLLATERAL_NOT_CLOB_OUTCOME` | 1 | Verificar valor de redención o collateral de pUSD |

Desglose operativo de los 141 rechazos:

- 130 se enrutan a reconstrucción NegRisk;
- 6 requieren reconstruir la ventana declarada del mercado;
- 5 tienen settlement CTF al bloque y el estado de payout debe prevalecer
  sobre el historial rechazado.

La salida quedó ligada al commit limpio
`1752079590f1dd938a1661d7ab48938faec6b9d7`. Sellos principales:

- SHA-256 de `run_manifest.json`:
  `8b7f3ab20ded148449a107ae6a78b0ea814842b233df483d32eff6b22f9295c0`;
- SHA-256 de `summary.json`:
  `950ee17a51d5689ab393f0ec629df52d828fc61c26a695cebd4ad8f54972230e`;
- digest canónico de `gap_queue.json`:
  `578db630debc670edfd656bba440ae309cc1b64cbee1a4c75f581942afe1e3c3`;
- SHA-256 del bundle v5, comprobado sin cambios:
  `6244785cc1cdbebc212b5a3cb07875bb031965934d8f213d02df63de108a2edc`.

Los ocho archivos declarados en el manifiesto verificaron. La integración
permanece en cero: `stale_candidates_promoted=0`,
`closing_marks_written_to_bundle=0`, `basis_bundle_modified=false` y
`freshness_threshold_modified=false`.

## Punto exacto de continuación

No repetir el paginador ni recompilar los 263 candidatos mientras no cambie
una entrada. El siguiente trabajo es un recolector **nuevo y separado** para la
ruta `RECOVER_FRESH_HISTORICAL_PRICE`, empezando por
`stale_candidates.json`. Debe guardar nueva evidencia raw, conservar el límite
original de cada fila y producir una salida nueva; nunca debe editar esta cola
ni promover automáticamente un precio.

Aunque las 3.235 brechas lleguen a cerrarse, P0 seguirá necesitando resolver
los 856 flujos externos y pasar un cálculo contable independiente antes de
reevaluar el gate.
