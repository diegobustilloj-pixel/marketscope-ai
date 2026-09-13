# PolyLedger P0 — motor de inventario y basis v1

**Estado:** motor implementado; la validación con evidencia real e independiente
continúa pendiente. **Modo:** solo lectura/replay. No contiene claves, firma,
wallet, retiros ni envío de órdenes.

## Propósito

`polymarket_bot.ledger.inventory_basis` convierte un bundle normalizado y
sellado en:

- inventario final por activo y lotes FIFO;
- basis total y promedio en unidades atómicas del activo de cotización;
- consumos de lotes y procedencia de cada acción;
- PnL realizado, no realizado y del periodo;
- conciliación exacta contra balances de cierre;
- contrato de comparación para un segundo pipeline independiente;
- diagnóstico `BLOCKED` cuando la evidencia no permite una cifra.

El motor no obtiene datos por sí mismo. La adquisición, decodificación y
normalización deben producir el bundle; esta separación evita que un mismo error
de API se convierta simultáneamente en dato y en supuesta validación.

## Contrato de entrada

El JSON de entrada usa `schema: 1` y declara explícitamente:

- `chain`, `wallet`, `quote_asset`, `quote_decimals` y `token_decimals`;
- rango inclusivo de bloques, apertura en el padre del primer bloque y cierre;
- cobertura continua, completitud de acciones y mapeos revisados;
- universo completo de activos y evidencia de su descubrimiento;
- cash y lotes de apertura, con costo atómico o `null`;
- acciones ordenadas con `raw_ids`, inputs, outputs y cambio de cash;
- marcas exactas de apertura y cierre como strings decimales, más
  `marks_evidence` para ambos cortes;
- balances completos del cierre en el mismo corte;
- acciones aún no mapeadas;
- opcionalmente, el reporte de un pipeline contable independiente.

Los activos se identifican como `chain:contract:token`. Los importes de cash,
cantidad y basis son enteros atómicos; un `float` es rechazado. Las marcas son
strings `Decimal` en átomos de cotización por átomo de token.

## Política económica

La política `fifo-atomic-proportional-remainder-v1` consume primero los lotes más
antiguos. En una salida múltiple asigna el costo proporcionalmente por cantidad
y entrega todo residuo atómico al último lote. Esto conserva exactamente el
costo total, aunque la atribución entre outcomes sea una política contable y no
un criterio fiscal.

| Acción | Tratamiento |
|---|---|
| `buy` | Cash pagado, incluida comisión, abre basis del token. |
| `sell` | Proceeds menos basis FIFO consumido producen PnL realizado. |
| `split` | Cash o posición padre consumida se distribuye a los outcomes. |
| `merge` | Un set completo devuelve cash; un merge jerárquico lleva basis a la posición padre. |
| `convert` | Lleva basis a los outputs; cash recuperado reduce primero el costo. |
| `redeem` | Pago menos basis consumido produce PnL realizado; payout cero sigue siendo evidencia. |
| `wrap` / `unwrap` | Traslada basis entre representaciones sin realización. |
| `receive` | Nunca nace con costo cero: exige basis y evidencia de valor de frontera, o queda desconocido. |
| `transfer` | Consume y conserva procedencia; para PnL del periodo exige valor externo en el instante de salida. |
| `cash` | Depósito/retiro externo; modifica patrimonio pero no PnL. |
| `reward` | Ingreso realizado separado y trazable. |

Para posiciones binarias estándar, la documentación oficial establece que un
split de una unidad de pUSD crea una unidad YES y una NO, un merge del par
devuelve una unidad, y una posición ganadora redime una unidad. Referencia:
<https://docs.polymarket.com/concepts/positions-tokens>.

## Doble identidad interna de PnL

El resultado se publica únicamente cuando coinciden exactamente:

```text
PnL patrimonio = equity_cierre - equity_apertura - flujos_externos_netos

PnL basis = realizado
          + no_realizado_cierre
          - no_realizado_apertura
          + devengo_de_tokens_transferidos
```

Una transferencia externa de tokens requiere valoración con evidencia en su
instante. Una recepción usa ese mismo valor como basis de frontera. Sin ello,
el motor conserva el inventario, pero devuelve
`EXTERNAL_TOKEN_FLOW_VALUE_MISSING` y no publica PnL completo.

## Gates

El estado interno solo es `COMPLETE` si:

1. rango, raw y mapeos están completos;
2. el universo de activos está declarado completo;
3. la apertura empieza en el origen de la wallet con saldo cero, o posee basis
   inicial probado para cada lote;
4. no existen acciones sin mapear;
5. todos los flujos externos de tokens tienen valor de frontera;
6. todos los activos positivos tienen marcas en ambos cortes aplicables;
7. inventario/cash reconstruidos coinciden con cada balance de cierre;
8. las dos identidades internas de PnL coinciden exactamente.

El `basis_gate` solo pasa cuando, además, otro método con procedencia y commit
propios reproduce el contrato `independent_expected_contract`. La API oficial
ofrece un snapshot contable ZIP con `positions.csv` y `equity.csv`, útil como
fuente externa pero no suficiente por sí solo para demostrar toda la historia:
<https://docs.polymarket.com/api-reference/misc/download-an-accounting-snapshot-zip-of-csvs>.

Este gate no aprueba globalmente el P0; Combo, captura CLOB/WS y los restantes
criterios conservan sus propios bloqueos.

## Ejecución

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger inventory-basis `
  --bundle data/polyledger-sentinel/p0/inventory_basis_input.json `
  --output data/polyledger-sentinel/p0/inventory_basis_result
```

El directorio de salida debe ser nuevo. Se escriben `configuration.json`,
`summary.json` y `run_manifest.json` con hashes, commit y estado del árbol. Una
falla de ejecución conserva `.partial`; un diagnóstico contable completo pero
sin pipeline independiente se sella como `BLOCKED`, no se convierte en éxito.

## Lo que aún falta para aplicarlo a `car`

- compilar un bundle desde el origen de la wallet o producir un snapshot inicial
  con basis independiente y completo;
- completar los mapeos revisados de transacciones mixtas y Combo;
- capturar marcas y balances al mismo corte;
- ejecutar un segundo cálculo contable independiente y comparar su contrato.

Las posiciones y promedios publicados por Data API son evidencia auxiliar, no
reemplazan el historial de lotes. Referencia de campos públicos:
<https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user>.
