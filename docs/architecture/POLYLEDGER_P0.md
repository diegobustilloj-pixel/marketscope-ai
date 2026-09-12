# PolyLedger P0 — entregas de construcción

Base: `399f032`. La investigación de GitHub permanece cerrada. El P0 se
construye en `src/polymarket_bot/ledger/`, dentro del bot ya registrado
`polyledger-sentinel`. No se crea otro bot ni se trasladan datos históricos.

## Entrega 1: evidencia y recuperación

- SQLite reutiliza el motor del Sentinel; una base P0 nueva usa application ID
  propio y rechaza cualquier base histórica. DuckDB continúa disponible para
  análisis, sin añadir otro escritor ni una base contable paralela.
- Raw append-only mediante restricciones y triggers; bloques huérfanos y
  errores conservados. Las restricciones protegen frente a escrituras normales,
  no frente a un administrador que sustituya el archivo o quite los triggers.
- `BEGIN IMMEDIATE`, WAL y `synchronous=FULL`; lote y cursor atómicos.
- El cursor incluye generación, hash y alcance. Un reorg invalida los snapshots
  y rechaza escritores que adquirieron datos antes de la reorganización.
- Secuencias por fuente/canal/sesión; gaps impiden avanzar el cursor y guardan
  evidencia. Un contador local acredita recepción, nunca prueba que el proveedor
  haya publicado todos sus eventos.
- Importes atómicos; se rechaza `float` en evidencia nueva.

Verificación: suite previa de 707 tests y seis pruebas nuevas de persistencia
(crash entre raw/cursor, deduplicación, mutabilidad, gaps, reorg y protección de
bases históricas). La aprobación P0 requiere además registro/ABI, decodificación,
ledger, reconciliación y evidencia independiente real.

Dinero real, conexiones de wallet, firma, retiros y órdenes automáticas siguen
bloqueados. Ningún módulo P0 contiene un ejecutor o un firmante.

## Entrega 2: contratos y decodificadores

El registro exige rangos finitos sin solapamientos, fuente, ABI y hash de código.
Cada observación de código y slots EIP-1967/beacon está fijada a un bloque/hash.
Un cambio o una observación contradictoria bloquea la decodificación. Coincidir
con un hash de configuración acredita esa correspondencia, no demuestra que un
proveedor RPC sea honesto ni sustituye revisar la vinculación fuente/bytecode.

`configs/polyledger/abi_catalog.json` conserva interfaces de eventos extraídas de
los clones ya investigados, con commit y hash del archivo fuente. No incorpora
implementaciones BUSL ni presume verificados los despliegues. El generador es
`tools/build_polyledger_abi_catalog.py`; no se necesita ejecutarlo para operar.

Hay familias separadas CLOB V1, CLOB V2/CTF, CTF, NegRisk, collateral y Combo.
Combo permanece bloqueado hasta obtener ABI verificada y vectores de IDs; no se
deducen interfaces de las auditorías. Los eventos desconocidos se conservan y
quedan en cuarentena. `TransferBatch` conserva el log padre y el índice interno.
`OrdersMatched` es anotación, no otro fill. La comisión V1 BUY se expresa en
outcomes; V2 la expresa en colateral. Los hashes EIP-712 son cálculos sin firma,
con dominio Exchange v2 y direcciones CTF/NegRisk distintas.

Verificación acumulada: 13 pruebas P0. Incluyen hashes de topic contrastados con
el colector histórico independiente, rangos, drift de implementación, payloads
malformados, lotes ERC-1155 y cuarentena de ABI desconocida.
