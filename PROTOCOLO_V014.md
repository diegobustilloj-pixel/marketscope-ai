# V0.14: desarrollo disjunto en paralelo

V0.14 es una muestra de desarrollo, no una validacion final ni una
autorizacion para usar dinero real.

## Contrato congelado

- Mantiene la senal `STRICT`, direccion simetrica y ejecucion de 5 shares.
- Usa exclusivamente VWAP ejecutable y fee capturados a 60 segundos.
- `LOW`: coste en `[0.05, 0.10)`.
- `MODERATE`: coste en `(0.22, 0.30]`.
- La banda candidata de v0.13, `[0.10, 0.22]`, queda excluida.
- Los costes mayores a `0.30` quedan excluidos.
- Objetivo balanceado: primeros 6 LOW y primeros 6 MODERATE.
- No se leen outcomes hasta completar ambos estratos.
- Si faltan 6+6 al llegar a 250 elegibles, falla por frecuencia con cero labels.
- Como maximo un estrato puede proponerse para un futuro v0.15 independiente.

Los valores definitivos, cutoff, reglas y hashes estan en:

- `data/prereg_v014_disjoint_development.json`
- `data/prereg_v014_evaluator.json`

## Archivos operativos

- Monitor/evaluador: `v014_monitor.py`
- Paper trader: `paper_trader_v014.py`
- Paper DB: `data/paper_trades_v014.db`
- Resultado futuro: `data/resultado_v014_development12.json`
- Estado manual: `ver_estado_v014.bat`

V0.13 continua sin cambios y conserva su propio stopping rule.
