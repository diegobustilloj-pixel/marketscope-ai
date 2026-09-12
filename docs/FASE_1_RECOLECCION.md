# Fase 1.3 — Recolección auditable

## Fuentes

| Fuente | Transporte | Contenido | Uso |
|---|---|---|---|
| Gamma | REST público | mercado, reglas, horarios y tokens | catálogo |
| Polymarket CLOB | WebSocket público | book, cambios, trades y resolución | microestructura |
| Polymarket RTDS | WebSocket público | `crypto_prices_chainlink` | precio de resolución |
| Binance | WebSocket público market-data-only | `btcusdt@aggTrade` | precio BTC externo |

Binance no pasa por RTDS en V4. El endpoint configurado es
`wss://data-stream.binance.vision:443/ws/btcusdt@aggTrade` y no admite
operaciones.

## Contrato raw V4

| Campo | Regla |
|---|---|
| `event_id` | UUID único |
| `source` | `gamma`, `rtds`, `binance`, `clob`, `system` o `synthetic` |
| `stream` | tópico o clase original |
| `received_at` | reloj UTC de Windows |
| `monotonic_ns` | reloj local monotónico, inmune a ajustes NTP |
| `source_timestamp_ms` | timestamp declarado por la fuente |
| `sequence` | orden local entero |
| `schema_version` | versión del envelope |
| `market_id` / `token_id` | identificadores cuando existen |
| `payload_raw` | mensaje original sin transformar |
| `checksum` | SHA-256 de fuente, stream y payload |

## Persistencia

SQLite utiliza WAL y bloques `zlib-jsonl-v1`. Las tablas principales son:

- `raw_chunks`, con rango UTC, rango monotónico y hashes;
- `event_counts`;
- `event_hourly_counts`;
- `markets`;
- `collector_runs`;
- `schema_meta`.

`event_hourly_counts` permite detectar fuentes que aparecen una vez pero no
mantienen continuidad. La base V4 usa una ruta nueva; una base V3 se rechaza
antes de cualquier modificación.

## Integridad y reloj

`verify-fast` comprueba:

1. `PRAGMA quick_check`;
2. SHA-256 de cada blob comprimido;
3. concordancia de conteos;
4. rangos de secuencia;
5. rangos del reloj monotónico.

Las regresiones de `received_at` se devuelven en
`wall_clock_adjustments` y `max_clock_rollback_seconds`. No invalidan los datos
cuando la secuencia y el reloj monotónico permanecen correctos.

## Gate V4 de dos horas

La validación se aprueba solamente si:

- duración observada mínima: 1,9 horas;
- cobertura de mercados mínima: 99%;
- Gamma y CLOB tienen datos;
- Binance `aggTrade` mantiene al menos 0,1 eventos/segundo;
- Chainlink mantiene al menos 0,2 eventos/segundo;
- Binance y Chainlink aparecen en al menos 95% de las horas observadas;
- no existen ejecuciones fallidas;
- la integridad rápida es correcta.

Esta prueba valida exclusivamente las correcciones derivadas de la captura V3.
No sustituye ni repite la prueba de 72 horas.

## Exportación V3

`export-v3` abre la base V3 con `mode=ro`, ejecuta `quick_check` y genera un ZIP
con un manifiesto, metadatos de mercados y muestras limitadas. No modifica,
mueve ni elimina la base fuente. Los payloads de muestra mayores a 100.000
caracteres se truncan y quedan marcados explícitamente.

## No incluido

Señales, modelos, Kelly, paper broker, backtesting, dashboard, Telegram, claves y
trading real siguen fuera de esta fase.
