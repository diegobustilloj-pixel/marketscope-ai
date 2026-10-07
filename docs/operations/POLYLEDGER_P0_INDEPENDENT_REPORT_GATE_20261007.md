# Compuerta del informe contable independiente — PolyLedger P0 / `car`

## Qué se preparó

Se añadió `verify-independent-report`, una compuerta offline que no implementa
el segundo cálculo ni inventa valores. Recibe su informe sellado, exige método y
commit distintos del motor primario, evidencia no vacía y coincidencia exacta
del contrato atómico `independent_expected_contract`.

## Ejecución real

| Campo | Valor |
|---|---|
| Resultado primario | `data/polyledger-sentinel/p0/car_lifetime_basis_result_20261006_v2/` |
| Informe externo esperado | `data/polyledger-sentinel/p0/car_lifetime_independent_report_20261007.json` |
| Salida sellada | `data/polyledger-sentinel/p0/car_lifetime_independent_report_gate_20261007_v1/` |
| Estado | `INDEPENDENT_REPORT_GATE_BLOCKED` |
| Contrato | `BLOCKED` — `INDEPENDENT_REPORT_MISSING` |
| Gate global | `BLOCKED` — `PRIMARY_BASIS_GATE_NOT_PASS` |
| Hash del contrato esperado | `8eef2cd8487744ab7026aff4c775e73fb02260b791b4611a1009a3bce0be59aa` |
| Hash de informe observado | `null` |

El resultado primario conserva los bloqueos de marcas, basis de recepciones,
flujos externos, accrual de transferencias y PnL independiente. La compuerta no
modificó el bundle v5, no escribió marcas y no cambió el estado P0.

## Qué debe entregar el segundo cálculo

Debe producir un JSON con el contrato exacto, `method` y `code_commit` propios,
y una lista no vacía de evidencias. Sus importes deben ser strings decimales o
`null` cuando la evidencia realmente no permita una cifra. La compuerta conserva
referencias y hashes; no copia el informe ni lo convierte en aprobación
automática.

Código y pruebas: commit `fd60677`; la compuerta tiene cuatro pruebas dedicadas
y el comando sigue siendo sólo lectura, sin wallet, firma, órdenes ni dinero
real.
