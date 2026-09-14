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
flags para intentar aprobarlo. Los mapeos económicos Combo no se pueden
autorizar por input.

## Captura RPC pública

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger capture --first-block 93486482 --last-block 93486482 --contract 0xe111180000d2663c0091e4f400237545b87b996b --output "data/polyledger-sentinel/runs/${p0RunStamp}_logs"
.\.venv\Scripts\python.exe -m polymarket_bot.ledger capture --first-block 93486482 --last-block 93486482 --contract 0xe111180000d2663c0091e4f400237545b87b996b --method receipts --output "data/polyledger-sentinel/runs/${p0RunStamp}_receipts"
```

El RPC público ya usado por `car_onchain` es el valor por defecto. Puede
seleccionarse otro con `--rpc-url https://...`; se rechazan URLs no HTTPS y no
deben incluirse credenciales en línea. Una captura
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

Para construir un registro reproducible desde un informe Sourcify completo:

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger verify-contract --chain 137 --contract 0x006f54f7f9a22e0000cc2ab60031000000ae9fef --block 93486482 --family combo_v2 --proxy-kind eip1967 --implementation 0xcc5de1e9d14a7ab75e872e23fc9d605518bac2d0 --source-report "ruta/al/informe_sourcify.json" --source-url "https://sourcify.dev/server/v2/contract/137/0xCc5De1e9D14a7AB75E872e23FC9D605518Bac2D0?fields=all" --output "data/polyledger-sentinel/p0/${p0RunStamp}_combo_verification"
```

La URL debe ser exactamente el endpoint HTTPS `fields=all` de la dirección que
se verifica. El comando vuelve a observar el proxy, su slot y la implementación
en el bloque solicitado. Un resultado exitoso dice `VERIFIED_AT_BLOCK`; cualquier
contradicción devuelve código 2 y conserva un diagnóstico `BLOCKED`. La ABI de
referencia recortada en `configs/polyledger/combo_position_manager_abi.json` no
sustituye el informe completo usado para registrar el despliegue.

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
de salida. El piloto solicitado usa 24 horas de replay como auditoría de conservación;
los experimentos y capturas individuales nuevos siguen limitados a 24 h.

Antes de intentar convertir un archivo legado en bundle, ejecutar el censo:

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger pilot-readiness --activity data/car_forensics/car_trades.parquet --onchain data/car_forensics/car_onchain.db --identity data/car_forensics/car_wallet_identity.json --wallet 0x7c3db723f1d4d8cb9c550095203b686cb11e5c6b --window-days 7 --output artifacts/polyledger_p0/pilot_readiness_car_20260912.json
```

El comando solo lee y sella hashes; devuelve código 2 porque un censo legado
nunca aprueba P0. Para `car`, el diagnóstico confirma fechas suficientes pero
rechaza los datos por falta de completitud, ciclo onchain, basis, balances al
mismo corte, PnL independiente e importes atómicos. No debe transformarse ese
`BLOCKED` en `PASS` ni rellenarse lo ausente con cero.

## Captura pública sellada de 24 horas

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger capture-wallet-24h --wallet 0x7c3db723f1d4d8cb9c550095203b686cb11e5c6b --identity data/car_forensics/car_wallet_identity.json --state-rpc-url https://polygon.drpc.org --output data/polyledger-sentinel/p0/car_24h_20260913_sealed
```

PublicNode adquiere logs y recibos; dRPC obtiene estado histórico EIP-1898. El
capturador exige que ambos proveedores coincidan en los hashes de apertura y
cierre. Descubre eventos de la wallet por topics, añade transacciones de la API,
guarda el recibo completo de la unión y amplía el universo a cada token tocado.
Después compara saldo inicial más transferencias de recibos contra saldo final.

La ejecución sellada del 13 de septiembre cubrió 86.400 segundos y concilió 101
activos con 422 transferencias y cero diferencias. Su estado correcto sigue siendo
`CAPTURED_BLOCKED`: un `MATCH` de balances no aporta basis ni PnL independiente.
Resumen versionado: `artifacts/polyledger_p0/car_24h_capture_20260913.json`.

## Valoración suplementaria de la captura sellada

La valoración se escribe en un directorio nuevo y nunca modifica la captura raw:

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger value-wallet-24h --capture data/polyledger-sentinel/p0/car_24h_20260913_sealed --wallet 0x7c3db723f1d4d8cb9c550095203b686cb11e5c6b --state-rpc-url https://polygon.drpc.org --output data/polyledger-sentinel/p0/car_24h_20260913_valuation_v4
```

La pasada final consultó 10.907 token IDs, 21.817 activos por corte, 2.367
condiciones CTF y 627 posiciones Combo. No hubo errores de saldo; 2.417 CTF
estaban liquidados al inicio y 2.418 al cierre. Los 627 Combo con saldo positivo
coincidieron con registros `RESOLVED_LOSS` anteriores al inicio y se valoraron en
cero. Cuatro posiciones CTF no resueltas, sin marca fresca y sin cambio de saldo
impiden una cifra puntual.

Después de separar US$3.480,481 de retiros externos, el cambio MTM de 24 horas
queda acotado entre **-US$1.439,257067014 y -US$172,785159014** dentro del
universo observado. El intervalo completo es negativo, pero no equivale a PnL
realizado ni prueba completitud de toda la wallet. Resumen versionado:
`artifacts/polyledger_p0/car_24h_valuation_20260913.json`.

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

## Motor sellado de inventario y basis

Cuando adquisición y normalización produzcan un bundle completo:

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger inventory-basis --bundle data/polyledger-sentinel/p0/inventory_basis_input.json --output data/polyledger-sentinel/p0/inventory_basis_result
```

El comando reconstruye lotes FIFO, basis, PnL realizado/no realizado, flujos
externos y balances. Compara el PnL por patrimonio y por basis, y después exige
un reporte independiente. Salida `COMPLETE` con `basis_gate: BLOCKED` significa
que el motor interno cerró pero falta el segundo pipeline; no es aprobación P0.
Formato, fórmulas y bloqueos:
`docs/architecture/POLYLEDGER_INVENTORY_BASIS_V1.md`.

## Backfill histórico de `car`

La ruta RPC desde bloque 1 requiere un nodo de archivo. Los RPC públicos
probados están podados; el error es un bloqueo de fuente, no un rango vacío.
La ruta pública sin clave ya iniciada se reanuda así:

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger backfill-wallet-blockscout --wallet 0x7c3db723f1d4d8cb9c550095203b686cb11e5c6b --identity data/car_forensics/car_wallet_identity.json --max-transfer-pages 100 --max-log-shards 25 --output data/polyledger-sentinel/p0/car_lifetime_blockscout_20260913
```

No cambie `--scope`, `--first-block`, proveedor, confirmaciones ni tamaño de
shard durante la reanudación. Los presupuestos `--max-*` sí pueden variar. Un
resultado `IN_PROGRESS` es progreso retenido; `CAPTURED_BLOCKED` significa que
la descarga terminó pero el indexador no acreditó cobertura completa. Ninguno
autoriza ejecutar `car`, conectar wallet o usar capital.
