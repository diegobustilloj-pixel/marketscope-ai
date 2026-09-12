# Fase 4 — Modelos probabilísticos y backtesting bloqueado

## Objetivo

Medir si alguna familia matemática estima la resolución de BTC Up/Down 5m
mejor que una referencia razonable y si esa diferencia sobrevive los costes
de ejecución. Esta fase no crea órdenes, no dimensiona posiciones y no usa
wallet.

El éxito técnico del proceso y la aprobación estadística son resultados
distintos. `pipeline_passed=true` significa que el experimento fue reproducible
y no corrompió los datos. No significa que exista una estrategia rentable.

## Modelos comparados

Cada horizonte de 90, 60, 30 y 15 segundos se estudia por separado:

1. probabilidad implícita del midpoint de Polymarket, como benchmark;
2. regresión logística con estado y persistencia Markov;
3. regresión logística de momentum y volatilidad;
4. regresión logística combinada;
5. gradient boosting conservador sobre el conjunto combinado.
6. regresión logística enriquecida con distancia al strike oficial;
7. gradient boosting enriquecido con distancia al strike oficial.

Se excluyen deliberadamente:

- Binance, porque su cobertura histórica actual es insuficiente;
- la etiqueta, el resultado final y cualquier dato posterior a la decisión.

Los cuatro modelos generales usan todos los mercados. Los dos modelos
enriquecidos usan únicamente las observaciones con strike oficial. La distancia
faltante nunca se imputa ni se fabrica, y cada modelo enriquecido se compara
contra Polymarket sobre ese mismo subconjunto.

## Separación temporal

Gold ya contiene `train`, `validation` y `test` cronológicos. Fase 4 añade una
separación interna en `train`:

- primer 80% de `train`: ajuste del modelo;
- último 20% de `train`: calibración Platt;
- `validation`: elección de modelo, horizonte y edge mínimo;
- `test`: una única evaluación final, solamente si validation aprueba un
  candidato.

Si validation no encuentra una alternativa convincente, `test` no se consulta.
Esto impide escoger retrospectivamente el mejor resultado de prueba.

## Gate de validación

Una combinación de modelo, horizonte y umbral debe cumplir simultáneamente:

- al menos 20 operaciones en validation;
- límite inferior del PnL medio mayor que cero;
- error de calibración ECE no mayor que 0,12;
- Brier no más de 0,01 peor que la probabilidad del mercado.

Como se prueban varios modelos, horizontes y umbrales, el límite de validation
aplica una corrección Bonferroni sobre toda la familia de comparaciones. Esto
reduce el riesgo de encontrar una ganancia aparente por probar muchas
combinaciones. Entre los candidatos válidos se elige el mayor límite inferior
de PnL. El umbral, el horizonte y el modelo quedan congelados antes de abrir
`test`, donde se utiliza un único límite unilateral del 95%.

## Costes

La simulación compra al mejor ask observado y añade:

- slippage adverso de 0,005 pUSD por participación;
- comisión taker cripto:
  `participaciones × 0,07 × precio × (1 − precio)`.

No se suponen rebates. Cada operación se mantiene hasta resolución y se evalúa
por participación. No hay capital compuesto, apalancamiento ni Kelly.

Referencia del modelo de comisión:
https://docs.polymarket.com/trading/fees. En una futura ejecución se leerá
además la configuración vigente de cada mercado; esta fase mantiene el
parámetro registrado y reproducible.

## Interpretación final

- `strategy_selected_on_validation=false`: no hubo evidencia suficiente y
  `test_accessed` debe ser `false`.
- `strategy_selected_on_validation=true`: validation congeló un candidato y se
  permite una sola evaluación en test.
- `forward_paper_candidate=true`: validation y test superaron los gates. Solo
  autoriza preparar shadow/paper trading; no autoriza dinero real.
- `forward_paper_candidate=false`: el modelo no debe pasar a ejecución.

El archivo `modelo_fase4_seleccionado.joblib` solo se crea cuando validation
selecciona un candidato. Aun así, el artefacto mantiene `orders_enabled=false`.

## Seguridad y reproducibilidad

- Gold se abre en modo SQLite de solo lectura.
- No se sobrescribe ninguna salida.
- El contrato, semilla, versiones de NumPy/scikit-learn, variables y costes se
  registran en la base de resultados.
- Se guardan predicciones de validation para todos los modelos.
- En test solo se guardan el modelo congelado y el benchmark del mercado.
- No se utiliza Claude, otro LLM ni servicios de pago.
