# Operar PolyLedger P0 en solo lectura

Estado: núcleo implementado, aprobación P0 bloqueada por evidencia pendiente.
Diseño y límites: `docs/architecture/POLYLEDGER_P0.md`.

Desde la raíz canónica, instalar las dependencias normales del proyecto con
`python -m pip install -e .`. Se añaden `eth-abi` y `eth-hash[pycryptodome]` para
decodificación estricta y Keccak; no se instala un SDK de órdenes ni firmante.

## Comprobar el replay sintético

Use un nombre nuevo en cada ejecución; no se sobrescriben resultados:

```powershell
$p0RunStamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
.\.venv\Scripts\python.exe -m polymarket_bot.ledger fixture --output "data/polyledger-sentinel/p0/$p0RunStamp.json"
.\.venv\Scripts\python.exe -m polymarket_bot.ledger replay --bundle "data/polyledger-sentinel/p0/$p0RunStamp.json" --output "data/polyledger-sentinel/runs/$p0RunStamp"
.\.venv\Scripts\python.exe -m polymarket_bot.ledger status --database "data/polyledger-sentinel/runs/$p0RunStamp/evidence.db"
```

El ejemplo concilia y calcula PnL exacto. **Su aprobación P0 debe ser `BLOCKED`
y el comando replay devuelve código 2**: los bloques, precios, contratos y
observaciones de ese ejemplo son sintéticos, y faltan gates reales. No cambiar
flags para intentar aprobarlo. El gate Combo no se puede autorizar por input.

## Captura RPC pública

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger capture --first-block 93486482 --last-block 93486482 --contract 0xe111180000d2663c0091e4f400237545b87b996b --output "data/polyledger-sentinel/runs/${p0RunStamp}_logs"
.\.venv\Scripts\python.exe -m polymarket_bot.ledger capture --first-block 93486482 --last-block 93486482 --contract 0xe111180000d2663c0091e4f400237545b87b996b --method receipts --output "data/polyledger-sentinel/runs/${p0RunStamp}_receipts"
```

El RPC público ya usado por `car_onchain` es el valor por defecto. Una captura
requiere un rango explícito, un máximo de 1.000 bloques y 200 confirmaciones por
defecto. La versión actual prima verificabilidad, no rendimiento. Solo se usa
HTTPS; no requiere cuenta, credenciales ni conexión de wallet. `RAW_CAPTURE_ONLY`
no es aprobación de ABI, contrato ni ledger.

Si falla la conexión, repetir exactamente el comando reanuda `.partial` y
comprueba los hashes antes de escribir. Si cambia el alcance, usar otra salida.
Si el reorg supera el ancla retenida, capturar de nuevo desde un ancestro
verificado en otra base. No editar, borrar ni reutilizar bases históricas.

## Registrar contratos y construir un bundle real

El catálogo `configs/polyledger/abi_catalog.json` es referencia de interfaces,
no registro de despliegues. `ContractRegistry.register` exige por contrato:

- chain, address, familia y rango inclusivo finito de bloques;
- ABI, hash de bytecode esperado y fuente verificable;
- para proxy: tipo EIP-1967 o beacon, implementación y hash de su código;
- observaciones del código/slots con block hash exacto y fuente.

`ReadOnlyRPC.observe_contract` lee esos datos mediante EIP-1898;
`ContractRegistry.attest` compara contra el registro. Falta de soporte del RPC,
slot desconocido, código diferente o ABI desconocida bloquean la decodificación.
No hay fallback a `latest` ni confianza implícita en una dirección del README.

El bundle tiene el mismo esquema que el fixture, pero debe contener evidencia
real: bloques contiguos con logs completos para su alcance, inventario inicial
en el padre del primer bloque, contratos verificados, observaciones, fills y
órdenes CLOB completos al corte, balances onchain por activo y captura por
recibos. Las respuestas de CLOB/balances deben preservar su procedencia y declarar
completitud; el consumidor no puede demostrar retrospectivamente mensajes WS
que nunca se capturaron. `raw_ids` se generan al guardar las fuentes en la base.

Las marcas usan cadenas decimales de átomos de colateral por átomo de outcome.
Un balance inicial por sí solo no acredita su costo: basis desconocido es `null`.
Se requiere el reporte independiente de PnL y sus hashes/versión para el criterio
de salida. Siete días de replay son una auditoría histórica de conservación;
los experimentos y capturas individuales nuevos siguen limitados a 24 h.

## Pruebas y datos

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe manage_bots.py validate
.\.venv\Scripts\python.exe tools/audit_polyledger_legacy_fills.py --output "data/polyledger-sentinel/p0/${p0RunStamp}_legacy.json"
```

La última utilidad abre `data/car_forensics/car_onchain.db` con `mode=ro`.
Las bases del Sentinel v0.1 en `data/polyledger/` permanecen compatibles e intactas.
Los resultados nuevos P0 viven en `data/polyledger-sentinel/`.

Dinero real, firmas, retiro, conexión de wallet y órdenes permanecen bloqueados
incluso si la reconciliación devuelve `MATCH`.
