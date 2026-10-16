# Compilador de candidatos de marca — PolyLedger P0 / `car`

Estado: **implementado y validado en modo offline; no integra marcas ni modifica
el bundle contable.**

## Propósito

`compile-closing-mark-candidates` reúne únicamente decisiones
`ACCEPTED_FOR_NEW_BUNDLE_COMPILATION`. Antes de escribir la salida verifica:

- sellos de política, auditoría y sonda;
- checkpoints y lotes completos de cada paginador;
- cobertura contigua, sin huecos ni solapamientos, de 0 a 2870;
- una sola raíz de confianza para bundle y cola de brechas;
- ausencia de activos o secuencias duplicados;
- precio interior a `(0,1)`, checks aprobados y CTF no resuelto al bloque;
- que ningún input cambie durante la compilación.

La salida contiene `closing_marks.json`, `marks_evidence.json`, las decisiones,
la cobertura y un manifiesto. Son **marcas propuestas**, no `closing_marks`
integradas en v5.

## Ejecución canónica

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger compile-closing-mark-candidates `
  --bundle data/polyledger-sentinel/p0/car_lifetime_basis_bundle_20260915_v5/bundle.json `
  --evidence-gaps data/polyledger-sentinel/p0/car_lifetime_basis_evidence_gaps_20261006_v1 `
  --policy-output data/polyledger-sentinel/p0/car_lifetime_closing_mark_policy_20261007_v1 `
  --policy-output data/polyledger-sentinel/p0/car_lifetime_closing_mark_policy_20261007_batch_00021_00040_v1 `
  --policy-output data/polyledger-sentinel/p0/car_lifetime_closing_mark_policy_20261007_batch_00041_00060_v1 `
  --policy-output data/polyledger-sentinel/p0/car_lifetime_closing_mark_policy_20261009_batch_00061_00080_v1 `
  --policy-output data/polyledger-sentinel/p0/car_lifetime_closing_mark_policy_20261011_batch_00081_00100_v1 `
  --policy-output data/polyledger-sentinel/p0/car_lifetime_closing_mark_policy_20261011_batch_00101_00120_v1 `
  --pagination data/polyledger-sentinel/p0/car_px120_v2 `
  --pagination data/polyledger-sentinel/p0/car_px1540_v3 `
  --pagination data/polyledger-sentinel/p0/car_px1960_v4 `
  --output data/polyledger-sentinel/p0/car_closing_mark_candidates_20261016_v1
```

## Resultado esperado

- cobertura: 2.870/2.870 outcomes CTF en 144 chunks;
- decisiones: 419;
- propuestas: 263;
- diferidas: 15;
- rechazadas: 141;
- marcas solicitadas: 3.498;
- marcas todavía sin candidato aceptado: 3.235;
- marcas escritas al bundle: 0.

Por tanto, el estado sigue `BLOCKED`: faltan marcas, evidencia de flujos
externos y un informe contable independiente. No se debe copiar
`closing_marks.json` al bundle manualmente.

## Resultado canónico ejecutado

El 16 de octubre de 2026 se creó localmente:

`data/polyledger-sentinel/p0/car_closing_mark_candidates_20261016_v1/`

La salida quedó ligada al commit limpio
`e3dea084748319965af9011fe8af547fbd83a85c`. Los seis archivos declarados en
el manifiesto verificaron sin diferencias. Conteos finales: 419 decisiones,
263 marcas propuestas y 263 registros de evidencia. Hashes principales:

- digest canónico de marcas:
  `7780064103aab231bda0ba39fde15e19ef9cc70221f743c78ef9b2fbb75e09f3`;
- SHA-256 de `summary.json`:
  `6b159d63d4a450438bc112c66a53e6f8042436bf4780b340039530714ea0bcec`;
- SHA-256 del bundle v5, comprobado después:
  `6244785cc1cdbebc212b5a3cb07875bb031965934d8f213d02df63de108a2edc`.

La salida no contiene `bundle.json`; `closing_marks_written_to_bundle=0` y
`basis_bundle_modified=false`.
