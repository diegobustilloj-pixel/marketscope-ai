# Protocolo v0.15 Confirmation10

V0.15 es una confirmacion forward independiente y condicional. Su plantilla
se congelo antes de conocer el resultado de v0.14 y permanece inactiva hasta
que exista `data/resultado_v014_development12.json`.

## Activacion automatica

Solo se activa si el evaluador v0.14 congelado devuelve
`DEVELOPMENT_CANDIDATE_SELECTED`, indica `LOW` o `MODERATE` en
`selected_for_v015`, y esa misma banda tiene `qualifies_for_v015=true`.
Si v0.14 no selecciona candidato, v0.15 queda `NOT_APPLICABLE` y no se inicia.

El prerregistro activo fija como corte el primer inicio de mercado de cinco
minutos estrictamente posterior a `created_at` del resultado v0.14. Tambien
guarda los hashes de la plantilla, el resultado que lo activo, el monitor y el
paper trader. Cualquier cambio posterior bloquea el experimento.

## Regla de evaluacion

- usa exclusivamente la banda elegida por v0.14;
- conserva severidad strict, direccion simetrica y costo real de entrada a
  60 segundos para cinco shares;
- toma los primeros 10 trades calificables posteriores al corte;
- no lee ningun label antes de tener los 10 trades;
- si alcanza 500 mercados elegibles con menos de 10 trades, termina
  `FAIL_INSUFFICIENT_FREQUENCY` leyendo cero labels;
- con 10 trades, lee exactamente esos 10 labels una sola vez y aplica los
  mismos gates forward de v0.13;
- no permite retuning, rescate de thresholds, wallet ni dinero real.

## Comandos de consulta

Desde `C:\ProyectoBotV4\polymarket_quant_bot`:

```powershell
.\.venv\Scripts\python.exe v015_monitor.py --status
.\.venv\Scripts\python.exe paper_trader_v015.py --status
```

No hace falta arrancarlos manualmente. El supervisor los deja en espera y los
inicia automaticamente solo si v0.14 hace aplicable la confirmacion.
