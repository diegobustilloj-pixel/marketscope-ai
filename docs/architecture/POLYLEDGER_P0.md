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

## Entrega 3: lotes y reconciliación

`LotLedger` conserva cantidad, costo remanente, origen y consumos FIFO. Los costos
se asignan proporcionalmente en átomos, y el último lote recibe el residuo para
conservar la suma exacta. No se usa redondeo binario. Las marcas son cadenas
`Decimal` en átomos de colateral por átomo de token; los resultados no se
etiquetan como USD sin especificar activo y escala.

El motor admite compra, venta, split, merge, conversión, redención, wrap/unwrap,
transferencias y recompensas. Una transferencia explícita entre dos direcciones
conserva lotes/basis sin inferir identidad común. Un ingreso externo conserva
costo `NULL`; una venta posterior no inventa beneficio. Cash recuperado por una
conversión reduce primero su costo: solo un exceso sobre todo el costo conocido
se registra por separado como realización. Esto es una política contable,
no una valoración económica de cada outcome ni un criterio fiscal.

El normalizador onchain es más limitado que el motor: acepta transacciones
con una causa económica inequívoca. Las transacciones mixtas, wraps con varios
colaterales, fills sin transferencias y movimientos sin explicación quedan
bloqueados. Se requiere ampliar sus mapeos con evidencia específica para cubrir
toda la operativa de NegRisk; la investigación del perfil no se ha reiniciado.

Reconciliación:

1. Fills CLOB asentados contra fills onchain (identidad de orden, tx/log,
   dirección de exchange, cantidad, cash y comisión).
2. Inventario/cash del ledger contra balances `eth_call` fijados a block hash.
3. Otro cálculo de balances que lee directamente topics/data de transferencias,
   sin consumir eventos normalizados ni lotes.

Los snapshots deben declarar el mismo corte y universo de activos. Falta de
balance no equivale a cero. El CLOB no se presenta como una fuente autoritativa
de saldo de outcomes; se comparan sus órdenes/fills. El gate de fuentes bloquea
shadow ante TTL vencido, datos viejos, gap, reconexión pendiente o contrato sin
verificar. Nunca envía cancelaciones ni órdenes reales.

Verificación de esta entrega: FIFO, fees, residuo atómico, conservación de basis,
importes grandes, costos desconocidos, transferencias y reconciliación negativa.

## Entrega 4: operación y evidencia reproducible

Se reutiliza el transporte público de `car_onchain` (con URL/timeout opcionales
compatibles) y el cliente GET del Sentinel. El cliente P0 solo permite métodos
RPC de lectura y selectores de balances/implementación. Captura tanto logs como
recibos, verifica IDs de respuestas, bloque/hash y buffer de confirmaciones.
Cada captura es acotada a 1.000 bloques y 24 h; reanuda `.partial` comprobando la
cadena retenida. No se ha construido un indexador de alto rendimiento de siete
días ni un consumidor WS vivo; los adaptadores de mensajes y sus gates están
disponibles para integrarlos con snapshots explícitos de completitud.

El esquema de evidencia es v2. Además de reorgs, nuevas observaciones de
contrato/fuente invalidan snapshots anteriores. Dos pruebas terminan abruptamente
un proceso después del raw y después del cursor: al reabrir no hay commit parcial.

El replay conserva configuración, manifiesto, base y reporte. Se registran hash
de entradas, hash de fuentes, commit, versiones de bibliotecas, hash de ledger y
de reconciliación. Un reporte `BLOCKED` es un diagnóstico terminado; no una
aprobación P0. Un fallo de ejecución deja `.partial`.

### Límites que impiden cerrar el P0

- ABI, fuente de implementaciones desplegadas y vectores de IDs Combo no están
  disponibles en los artefactos existentes. El decoder Combo se mantiene cerrado.
- Falta una captura real de siete días para la wallet piloto, con inventario y
  basis iniciales verificables, snapshots completos CLOB y balances al mismo corte.
- Falta el PnL de un pipeline externo independiente para las conversiones de esa
  muestra. El segundo cálculo implementado aquí verifica balances, no certifica
  de forma independiente todo el PnL de NegRisk.
- Falta ampliar los mapeos de transacciones mixtas con esa evidencia, completar
  vectores por protocolo y realizar el ensayo adversarial sobre la muestra real.

Los 810 fills reales archivados de `car` coinciden con el decodificador histórico
independiente. Esto verifica decodificación, no demuestra basis, PnL total ni
correspondencia entre fuente y bytecode desplegado.

## Validación sellada — 2026-09-12

Código verificado: `2da535c`, sobre la base solicitada `399f032`.

- Suite completa: **740 tests aprobados**, incluidos 33 P0 y 18 subtests.
  Persisten los mismos 831 warnings de joblib/NumPy de la línea base.
- Catálogo: 11 bots válidos, cero errores y cero warnings.
- Dos replays sintéticos en bases nuevas: reconciliación `MATCH`, integridad
  correcta y hash de ledger idéntico
  `50cbc4816c876ed810bc1276e805803ef01993cd30d17b99e8474da5e2e52c8d`.
- 810 fills reales de `car`: cero discrepancias entre decodificadores; el hash
  del archivo histórico permanece intacto antes y después.
- Bloque Polygon 93486482: 103 logs idénticos obtenidos mediante `eth_getLogs`
  y mediante los recibos de todas sus transacciones. Se usó un mismo RPC público:
  son rutas de adquisición diferentes, no proveedores independientes.
- Dinero real, firmas, wallet, retiros y órdenes continúan bloqueados.

Resumen versionado: `artifacts/polyledger_p0/validation_20260912.json`. El criterio
de salida P0 **no está aprobado**. Los bloqueos anteriores requieren evidencia
externa; ninguna de estas pruebas autoriza capital ni sustituye ese ensayo.
