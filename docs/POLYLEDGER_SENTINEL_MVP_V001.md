# PolyLedger Sentinel v0.1

PolyLedger Sentinel audita una proxy wallet pública de Polymarket sin firmar mensajes, usar claves privadas ni enviar órdenes.

## Qué entrega

- Ledger local SQLite, incremental y deduplicado de `TRADE`, `SPLIT`, `MERGE`, `REDEEM`, rewards, rebates y conversiones.
- Snapshots fechados de posiciones abiertas, posiciones cerradas, `/value`, leaderboard y perfil público.
- Copia y huellas SHA-256 del accounting snapshot oficial (`positions.csv` y `equity.csv`).
- Separación explícita entre trading, liquidaciones, conversiones internas, recompensas y movimientos externos.
- PnL oficial de cada fuente sin mezclarlas silenciosamente.
- FIFO diagnóstico solo para operaciones BUY/SELL observables, con ventas sin costo identificadas.
- Bandeja de discrepancias y dos salidas: JSON completo y Markdown legible.

## Uso sencillo

Haga doble clic en `ejecutar_polyledger_sentinel.bat`, pegue una wallet pública y espere el informe. Si deja el campo vacío, usa la wallet pública de Balthazar configurada para la primera auditoría.

También puede ejecutarse así:

```powershell
python polyledger_sentinel_v001.py --wallet 0x0000000000000000000000000000000000000000
```

Archivos predeterminados:

- `data/polyledger/polyledger.db`: ledger y snapshots.
- `data/polyledger/ultimo_reporte.md`: informe para leer.
- `data/polyledger/ultimo_reporte.json`: evidencia estructurada.
- `data/polyledger/accounting_snapshot.zip`: snapshot oficial.
- `data/polyledger/ultimo_reporte_open_positions.csv`: todas las posiciones abiertas capturadas.
- `data/polyledger/ultimo_reporte_closed_positions.csv`: todas las ganancias y pérdidas cerradas capturadas.

Las ejecuciones posteriores reanudan desde el último segundo conservado y vuelven a consultar ese segundo; la huella estable evita duplicados.

Sentinel descarga el historial por días concurrentes. Cuando incluso un día alcanza el límite práctico de paginación pública, lo divide por tiempo y continúa. Así evita truncar silenciosamente las wallets activas.

## Regla contable crítica

`SPLIT`, `MERGE` y `CONVERSION` son movimientos internos, no ganancias ni pérdidas. `REDEEM` es un cobro bruto, no beneficio, hasta conocer el costo de los tokens. Rewards y rebates sí se muestran como ingresos explícitos, pero separados del trading.

El campo `observable_pusd_cash_movement_usd` nunca debe interpretarse como PnL. El API público no contiene por sí solo todos los saldos y transferencias necesarios para reconstruir patrimonio histórico exacto.

Para volver a generar el informe y los CSV sin consultar internet:

```powershell
python polyledger_sentinel_v001.py --wallet 0x0000000000000000000000000000000000000000 --solo-local
```

## Fuentes públicas oficiales

- Actividad: https://data-api.polymarket.com/activity
- Posiciones actuales: https://data-api.polymarket.com/positions
- Posiciones cerradas: https://data-api.polymarket.com/closed-positions
- Valor de posiciones: https://data-api.polymarket.com/value
- Leaderboard: https://data-api.polymarket.com/v1/leaderboard
- Accounting snapshot: https://data-api.polymarket.com/v1/accounting/snapshot
- Perfil: https://gamma-api.polymarket.com/public-profile

## Alcance de esta versión

La versión 0.1 es un auditor público útil para copiar con prudencia y verificar trackers. No es todavía un indexador completo de Polygon. La siguiente fase puede incorporar logs de contratos, transferencias ERC-1155, saldo de pUSD por bloque y clasificación de contrapartes para cerrar el costo base que el Data API no expone.
