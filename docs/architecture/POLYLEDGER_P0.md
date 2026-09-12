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
