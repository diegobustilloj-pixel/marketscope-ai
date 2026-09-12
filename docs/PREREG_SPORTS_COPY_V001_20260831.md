# Prerregistro — viabilidad de copia deportiva V001

## Dependencia y corte

Este protocolo depende exclusivamente de la captura `sports_wallet_research_v001`,
con corte inclusivo `2026-08-31T15:46:23Z`. No cambia el universo, las
identidades ni la clasificación de deportes ya congelados.

La cinta de mercado se consultará después de congelar estas reglas. Antes de
este prerregistro solo se hizo una consulta técnica de un mercado para confirmar
el esquema del endpoint; ese mercado no se usó para escoger reglas ni umbrales.

## Señal copiable

- Una señal por combinación trader/mercado: el primer `BUY` público.
- Si en el primer segundo aparecen compras de más de un token del mismo mercado,
  la señal es ambigua y se excluye de la prueba principal.
- Duplicados exactos del primer fill se eliminan al construir la señal.
- Solo se puntúan mercados deportivos con payout binario y resolución no
  posterior al corte.
- Los `SELL` no se convierten retrospectivamente en compras del complemento.
- Mercados del mismo evento permanecen en el mismo tramo temporal.

## Ejecución y capacidad

Se evaluarán retrasos de `0, 1, 5, 15, 30, 60, 120 y 300` segundos.

- `0 s` es únicamente el benchmark simultáneo al VWAP del primer segundo del
  líder; no se considerará una ejecución alcanzable.
- Para los demás retrasos se usa el primer negocio público del mismo token a
  partir del instante objetivo y dentro de los siguientes 60 segundos.
- Si no existe ese negocio, la copia se marca como no ejecutada. No se rellena
  con el último precio conocido.
- Si varios negocios aparecen en el primer segundo observable, se usa para un
  `BUY` el precio más alto de ese segundo y solo el tamaño impreso a ese precio.
- El tamaño de la copia se limita al 25% del notional de ese negocio público.
  Es un proxy conservador de capacidad, no profundidad histórica probada.
- La cinta se recupera completa para cada ventana. Una ventana que alcance
  10.000 filas se divide por tiempo; un único segundo saturado invalida esa
  señal para afirmaciones de cobertura.

## Costes y escenarios

- Tamaños solicitados: USD `10, 50, 100, 250, 500, 1000 y 5000`.
- Impacto adverso adicional sobre el siguiente negocio: `0, 1, 2 y 5` centavos.
- La tarifa se calcula como taker con `feeSchedule` del mercado capturado y la
  fórmula oficial `shares × rate × p × (1-p)`, redondeada a cinco decimales.
- No se atribuyen maker rebates porque la regla de copia exige ejecución y no
  existe evidencia de prioridad histórica en cola.

El escenario principal queda congelado en USD 100 solicitados, 15 segundos de
retraso, 25% de participación máxima y un centavo de impacto adverso adicional.

## Separación temporal y selección

Para cada trader, los eventos se ordenan por la primera señal y se asignan
cronológicamente a 60% entrenamiento, 20% validación y 20% prueba, sin dividir
un mismo evento entre tramos.

Un candidato principal debe tener en entrenamiento y validación:

- PnL neto, ROI y profit factor positivos (`PF > 1`);
- al menos 40 ejecuciones en entrenamiento y 15 en validación.

Entre quienes cumplan se escoge antes de abrir prueba por el mayor mínimo de PF
entre entrenamiento/validación; los desempates son el mayor mínimo de ROI y
después más ejecuciones. La prueba confirma al candidato si conserva PnL y ROI
positivos, `PF > 1` y al menos 15 ejecuciones. Si falla, se conservará como
ganador comparativo de investigación pero se marcará explícitamente como no
confirmado y no desplegable.

## Limitaciones obligatorias

- La cinta demuestra negocios, no órdenes disponibles, profundidad, spread,
  cancelaciones ni prioridad de cola histórica.
- El siguiente negocio es un proxy de precio alcanzable, no garantía de fill.
- El endpoint público da resolución de segundos; no ordena negocios dentro del
  mismo segundo.
- Los resultados son paper-only y no autorizan wallet, credenciales ni órdenes.
  Dinero real: `BLOQUEADO`.
