# Registro de ejecución — PolyLedger P0 / `car` / 2026-10-06

## Alcance

Esta es una constancia pequeña de la corrida local. No sustituye el bundle ni el
resumen sellado, que permanecen fuera de Git por tamaño y política de datos.
La ejecución fue estrictamente de lectura: no conectó wallet, no firmó, no
envió órdenes y no usó dinero real.

## Input y salida

| Elemento | Ubicación o valor |
|---|---|
| Input sellado | `data/polyledger-sentinel/p0/car_lifetime_basis_bundle_20260915_v5/bundle.json` |
| Salida sellada | `data/polyledger-sentinel/p0/car_lifetime_basis_result_20261006_v2/` |
| Código | `19bf5ac48323ec781d48bf7a0edc08dc0c768974` |
| Árbol al ejecutar | limpio |
| Acciones reproducidas | 251.107 |
| Error de reconstrucción | ninguno |
| Conciliación de balances | `MATCH` |
| Hash de balances | `8e7c1d9de81461a7c4d6248a6440f336dcc0666380afdc8f31753ba64686cbf5` |
| Hash del diario compacto | `1ef17559a1481d3211d14ab95c95245a55cbb840d25a6f2b7593955f6ae89b8e` |

El manifiesto de la salida verificó hashes de sus archivos, el hash del resumen
y el SHA-256 del input. La configuración de resultado solo referencia el
bundle sellado (`sealed-input-reference-v1`), sin duplicar sus 200+ MB.

## Resultado correcto

La corrida terminó y se selló correctamente, pero el gate de basis permanece
`BLOCKED`. Los bloqueos son:

- `UNKNOWN_COST_BASIS`
- `CLOSING_MARKS_INCOMPLETE`
- `CLOSING_MARK_EVIDENCE_MISSING`
- `EXTERNAL_TOKEN_FLOW_VALUE_MISSING`
- `EXTERNAL_TRANSFER_ACCRUAL_UNKNOWN`
- `INDEPENDENT_ACCOUNTING_REPORT_MISSING`

Por tanto, no hay PnL final aprobado ni permiso de copiar operaciones. El
siguiente trabajo es completar esa evidencia y producir un cálculo contable
independiente contra el mismo contrato de entrada.

## Sonda posterior de fuente de precios

Después de esta corrida se ejecutó una sonda local, separada y sellada de 20
outcomes CTF contra el historial puntual oficial. Dos RPC de Polygon fijaron
el mismo bloque de cierre; hubo 4 candidatos dentro de la ventana explícita de
una hora y 16 antiguos. No se escribió ninguna marca ni se alteró este bundle
ni su resultado. Consulte `POLYLEDGER_P0_PRICE_PROBE_20261006.md` para los
hashes, el corte temporal y los límites de esa evidencia candidata.
