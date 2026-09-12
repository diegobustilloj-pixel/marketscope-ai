# Runbook de operación

## Comprobación inicial

Desde `C:\ProyectoBotV4\polymarket_quant_bot`:

```powershell
.\.venv\Scripts\python.exe manage_bots.py validate
.\.venv\Scripts\python.exe manage_bots.py list
.\.venv\Scripts\python.exe -m pytest -q
```

Un error de catálogo, test o ruta bloquea cualquier cambio de etapa.

Para el núcleo PolyLedger P0, captura pública, replay y pruebas de recuperación,
consulte `docs/operations/POLYLEDGER_P0_RUNBOOK.md`. La reconciliación `MATCH` no
aprueba el P0 ni permite órdenes; los gates pendientes están documentados allí.

## Crear un bot

```powershell
.\.venv\Scripts\python.exe manage_bots.py create sports-nba-v1 --name "NBA Probability" --domain sports
```

Esto crea:

- `apps/sports-nba-v1/main.py` seguro y bloqueado.
- `apps/sports-nba-v1/README.md`.
- `configs/bots/sports-nba-v1.json`.

No crea credenciales, wallet, orden ni proceso automático.

## Cambiar una etapa

1. Guardar hipótesis y métricas de aceptación.
2. Añadir o actualizar pruebas.
3. Cambiar el manifiesto.
4. Ejecutar `validate` y la suite.
5. Registrar el resultado en changelog/decisión.

No está permitido configurar `live`, `real_money=true`, `automatic_orders=true` o `withdrawals=true`. El validador falla de forma cerrada.

## Iniciar bots existentes

Los BAT y wrappers históricos siguen siendo la autoridad operacional durante la migración. `manage_bots.py` solo cataloga y valida; no inicia ni detiene procesos.

## Incidente

Ante feed stale, gap WebSocket, discrepancia de balances, cambio de contrato, reorg o salida no explicada:

1. No iniciar nuevas acciones.
2. Conservar logs y base sin editarlos.
3. Copiar configuración y hora del incidente al evidence pack.
4. Ejecutar únicamente comandos de estado/auditoría.
5. Reanudar solo después de reconciliación y prueba reproducible.

## Cierre de una ejecución

Cada ejecución nueva debe producir, cuando aplique:

```text
data/<bot-id>/runs/<run-id>/
├── run_manifest.json
├── configuration.json
├── signals.csv
├── orders.csv
├── fills.csv
├── positions.csv
├── pnl.csv
├── errors.log
└── summary.json
```

Formato de `run-id`: `YYYYMMDDTHHMMSSZ_<bot-id>_<code-version>`.
