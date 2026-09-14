# PolyLedger P0 — backfill histórico de wallet v1

**Estado:** capturadores implementados; backfill real de `car` iniciado y aún
incompleto. **Modo:** solo lectura. No usa claves de wallet, firma, órdenes,
retiros ni dinero real.

## Objetivo

El motor de inventario necesita una apertura con basis probado. El camino más
fuerte es reconstruir todos los movimientos desde el origen económico, no tomar
el `avgPrice` actual como si fuera un lote histórico. Este backfill comienza en
el bloque 1 de Polygon, congela el bloque final con 200 confirmaciones y descubre
cada cambio de saldo de la wallet dentro del universo de contratos declarado.

El alcance incluye CTF, Combo PositionManager, USDC.e, USDC nativo, pUSD y el
wrapped collateral histórico de NegRisk. Las direcciones de los exchanges V1 y
V2 y el adaptador NegRisk se conservan como contratos de settlement para la
posterior decodificación. La configuración está en
`configs/polyledger/polygon_lifetime_backfill.json`.

## Dos rutas de adquisición

### RPC de archivo

`backfill-wallet-history` consulta únicamente topics `from`/`to` de
`Transfer`, `TransferSingle` y `TransferBatch`. Divide automáticamente el rango
cuando el proveedor limita el número de bloques, conserva segmentos inmutables,
deduplica por `transactionHash/logIndex` y obtiene el recibo completo de cada
transacción descubierta.

Los dos RPC públicos probados el 13 de septiembre de 2026 no sirven para el
origen: PublicNode respondió que el historial fue podado y dRPC rechazó la
consulta antigua. La captura se detiene; nunca interpreta el rechazo como una
respuesta vacía.

### Blockscout sin clave

`backfill-wallet-blockscout` usa la API pública v2 de Blockscout para:

1. paginar todas las transferencias de cada contrato hasta cursor nulo;
2. congelar cada página con su cursor de entrada, salida y hash de respuesta;
3. deduplicar las transacciones de la wallet;
4. consultar estado, índice y todos los logs de cada transacción;
5. comprobar que cada transferencia semilla existe en el cierre de logs;
6. comparar el hash del bloque final contra el RPC público;
7. conservar el estado de indexación declarado por Blockscout.

La primera comprobación real encontró la primera transferencia CTF conocida de
`car` en el bloque 53.293.431, el 9 de febrero de 2024 a las 01:20:08 UTC, y
recuperó los nueve logs de su transacción. Esto demuestra disponibilidad
histórica de ese registro, no completitud global del indexador.

Blockscout informó `indexed_blocks_ratio=0.98`,
`finished_indexing=false` y `finished_indexing_blocks=false`. Por eso el
resultado puede quedar `CAPTURED_BLOCKED`, pero no puede declarar
`raw_actions_complete=true` aunque toda la paginación termine. La documentación
de Blockscout explica su paginación por `next_page_params` y los endpoints de
transferencias/logs:
<https://blockscout.mintlify.app/devs/apis/rest>.

## Reanudación

La primera ejecución congela el bloque final. Invocaciones posteriores con la
misma configuración leen los archivos existentes y continúan desde el primer
cursor o shard ausente. `--max-transfer-pages` y `--max-log-shards` son
presupuestos operativos de una invocación; no forman parte de la evidencia y
pueden aumentarse sin invalidar el progreso.

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger backfill-wallet-blockscout `
  --wallet 0x7c3db723f1d4d8cb9c550095203b686cb11e5c6b `
  --identity data/car_forensics/car_wallet_identity.json `
  --max-transfer-pages 100 `
  --max-log-shards 25 `
  --output data/polyledger-sentinel/p0/car_lifetime_blockscout_20260913
```

El proceso real está en
`data/polyledger-sentinel/p0/car_lifetime_blockscout_20260913.partial`. Su corte
quedó congelado en el bloque 93.762.690. Las pasadas de arranque guardaron tres
páginas, 150 transferencias y 127 transacciones únicas. Todavía no cerraron ningún contrato
ni descargó shards de transacciones, porque el sistema termina primero la
paginación de transferencias para congelar el índice de transacciones.

El cierre de transacciones acepta `--log-workers` (1–32, 8 por defecto). Las
consultas HTTP de cada shard pueden ejecutarse en paralelo, pero
`executor.map` conserva el orden congelado de transacciones y cada shard se
publica atómicamente solo después de validar todas sus respuestas. El número de
workers es un parámetro operativo y no altera el hash de la evidencia ni la
capacidad de reanudar.

Si Blockscout publica una transferencia semilla pero devuelve una lista vacía
para los logs de su transacción, `--receipt-rpc-url` permite recuperar y validar
el recibo desde un RPC público independiente. El recibo debe coincidir en hash,
estado, bloque e índice con Blockscout; se conserva su hash de respuesta y la
fuente exacta dentro del cierre. Si ambas fuentes carecen del recibo, el proceso
se detiene sin sustituirlo por una lista vacía.

Para una captura grande, `--receipt-batch-size 10` convierte el RPC de recibos
en fuente primaria y reduce hasta diez transacciones a una solicitud JSON-RPC.
`--receipt-batch-workers` permite 1–8 lotes simultáneos. Cada recibo conserva su
fuente y hash, valida estado e identidad, y los archivos continúan publicándose
en shards atómicos de 25 siguiendo exactamente el índice congelado.

## Por qué el snapshot contable no reemplaza el backfill

El endpoint oficial `/v1/accounting/snapshot` entregó `positions.csv` y
`equity.csv`, pero `positions.csv` contiene tamaño, precio actual y hora de
valoración; no contiene costo histórico, lotes ni `avgPrice`. Es útil para
contrastar valor de cierre, no para probar basis de apertura. La definición
oficial del archivo está aquí:
<https://docs.polymarket.com/api-reference/misc/download-an-accounting-snapshot-zip-of-csvs>.

Los subgrafos públicos antiguos de Goldsky tampoco son una alternativa válida:
el propio endpoint los marca pausados, obsoletos y potencialmente incorrectos
tras la migración V2. Goldsky ofrece datasets históricos actuales mediante un
servicio con cuenta. La documentación vigente de Polymarket enumera Goldsky
como recurso on-chain:
<https://docs.polymarket.com/resources/blockchain-data>.

## Gates pendientes

Terminar la descarga no basta. Antes de alimentar `inventory-basis` se requiere:

- paginación completa de los seis contratos;
- cierre de logs de todas las transacciones;
- prueba de completitud del indexador o coincidencia contra una segunda fuente
  de archivo;
- decodificación de receipts/logs V1 y V2;
- revisión de transacciones mixtas, NegRisk y Combo;
- balances y marcas en el corte congelado;
- un cálculo contable independiente.

Hasta entonces, `ledger_approval=false` y el P0 global permanece bloqueado.
