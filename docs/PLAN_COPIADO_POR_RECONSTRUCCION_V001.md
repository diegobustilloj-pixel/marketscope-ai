# Plan v0.1 — Copiado por reconstrucción

**Estado:** DISEÑO GUARDADO / PENDIENTE DE CONSTRUCCIÓN  
**Fecha de congelación:** 10 de septiembre de 2026  
**Proyecto base:** PolyLedger Sentinel  
**Wallet piloto:** Balthazar (`0x5a218c7ad04135830a45c41aaed7294df7809318`)

## Decisión

No se copiarán operaciones individuales de una wallet. El sistema observará una
secuencia completa, reconstruirá la cesta y solo mostrará una oportunidad si el
usuario todavía puede replicar una ventaja positiva con los precios disponibles
en ese momento.

La primera versión será **semiautomática y solo shadow**: detecta, reconstruye,
calcula y presenta una alerta; el usuario decide manualmente. No solicita claves,
no firma y no envía órdenes.

## Por qué se eligió este enfoque

La auditoría de Balthazar encontró:

- PnL oficial histórico de aproximadamente **US$1.918.123** sobre **US$69,51
  millones** de volumen. La relación PnL/volumen de 2,76% no es ROI.
- 842.446 trades y una frecuencia aproximada de 1.450 trades diarios durante la
  muestra: el comportamiento no parece manual.
- Aproximadamente US$47 millones de notional interno en `SPLIT`, `MERGE` y
  `CONVERSION`; una compra visible puede ser inventario o una pata de cobertura.
- Las recompensas explícitas fueron aproximadamente US$55.525, cerca del 2,9%
  del PnL oficial: no explican por sí solas la rentabilidad.
- El tamaño visible no representa confianza. La mediana comprada en grupos con
  resultado contable positivo fue cercana a US$366, frente a US$2.430 en grupos
  negativos. Es una señal para **no copiar tamaño**, no una regla predictiva.
- Clima fue la categoría dominante por cantidad de eventos identificados
  (aproximadamente 56%). La clasificación y el PnL por categoría son diagnósticos,
  no una tasa de acierto demostrada.

Los porcentajes de posiciones positivas y las sumas de PnL por fila no se usarán
como hit rate ni como verdad contable. Las conversiones de mercados
multiresultado, el inventario con costo no observable y la paginación pública
producen distorsiones conocidas.

## Objetivo del producto

Transformar la actividad pública de una wallet en una **cesta replicable**:

1. detectar que la wallet entra o modifica un evento;
2. agrupar sus operaciones relacionadas dentro de ventanas temporales;
3. incorporar compras, ventas, splits, merges, conversiones y redenciones;
4. reconstruir inventario por token, resultado, condición y evento;
5. inferir varias hipótesis de cesta cuando la intención no sea identificable;
6. cotizar la cesta completa contra el libro disponible para el usuario;
7. descontar comisiones, spread, deslizamiento y riesgo de ejecución parcial;
8. abstenerse si falta una pata, la intención es ambigua o ya desapareció la
   ventaja;
9. presentar un ticket claro para entrada manual.

## Regla central

Una operación de Balthazar nunca será una señal suficiente. Solo se podrá emitir
una alerta cuando exista una reconstrucción completa o una cota conservadora
verificable.

`actividad observada -> cesta reconstruida -> precio replicable ahora -> riesgo -> alerta/abstención`

## Alertas previstas

Cada alerta deberá incluir, como mínimo:

- wallet origen, evento y hora de detección;
- tipo de estrategia inferida y confianza de la inferencia;
- todas las patas necesarias, lado y cantidad normalizada;
- costo ejecutable actual de la cesta y profundidad usada;
- pago mínimo, pago máximo y escenarios no cubiertos;
- comisiones, spread y deslizamiento estimados;
- margen bruto y margen conservador después de costos;
- retraso desde el primer fill observado;
- riesgo de no completar la cesta;
- capital máximo replicable sin exceder la profundidad permitida;
- resultado: `ALERTA`, `ESPERAR` o `ABSTENERSE`;
- razones concretas del resultado.

Ejemplo conceptual:

> Cesta de 7 resultados. Costo replicable actual: US$0,963 por cada US$1 de
> pago cubierto. Margen conservador: 1,8%. Riesgo de ejecución parcial: alto.
> Resultado: ABSTENERSE hasta que todas las patas tengan profundidad suficiente.

## Filtros obligatorios

- No copiar una sola pata de un evento multiresultado.
- No inferir convicción a partir del tamaño de la operación.
- No usar el PnL por fila de posiciones como PnL de la estrategia.
- No usar porcentaje de posiciones positivas como porcentaje de acierto.
- No usar precios históricos finales como si hubieran sido ejecutables.
- No emitir señal si no se puede identificar el conjunto de resultados y sus
  reglas de resolución.
- No asumir fills maker, prioridad de cola ni profundidad no observada.
- No perseguir una señal cuyo margen ya se consumió por latencia o movimiento de
  precio.
- No operar con dinero real durante desarrollo, backtest ni prueba forward.

## Estrategias que intentará reconocer

1. **Complete set / arbitraje multiresultado:** costo conjunto inferior al pago
   cubierto después de costos.
2. **Neg-risk y conversión de inventario:** transformación entre resultados del
   mismo evento con exposición residual identificada.
3. **Valor relativo entre mercados correlacionados:** incoherencias entre ganador,
   marcador, fecha, rango u otros mercados relacionados.
4. **Market making observable:** acumulación y descarga repetida alrededor del
   spread. Solo se clasificará; no se supondrá que el copiador obtiene la misma
   prioridad de cola.
5. **Direccional simple:** únicamente cuando no existan patas o coberturas
   relacionadas dentro de la ventana y la evidencia sea suficiente.

## Construcción por fases

### Fase R1 — Observador y agrupador

- Reutilizar el ledger incremental de PolyLedger Sentinel.
- Capturar actividad nueva de la wallet piloto con baja latencia.
- Normalizar evento, condición, token, outcome y transacción.
- Crear sesiones por evento con ventanas temporales dinámicas.
- Emitir grupos observables sin recomendar operaciones.

### Fase R2 — Motor de inventario

- Mantener inventario por resultado.
- Modelar BUY, SELL, SPLIT, MERGE, CONVERSION y REDEEM.
- Separar notional interno, flujo de caja y exposición económica.
- Marcar costo base desconocido y ventas sin inventario observable.
- Producir hipótesis alternativas cuando la reconstrucción no sea única.

### Fase R3 — Cotizador replicable

- Consultar libros actuales de todas las patas.
- Simular fills por niveles y tamaño, con latencias configurables.
- Calcular margen conservador, capacidad y peor escenario.
- Bloquear oportunidades dependientes de una cola maker no observable.

### Fase R4 — Shadow prospectivo

- Congelar reglas antes de observar resultados.
- Guardar cada señal y cada abstención con el libro disponible en ese instante.
- Evaluar retrasos de 15, 30, 60 y 120 segundos.
- Comparar cesta teórica, cesta realmente replicable y resolución final.
- Ejecutar por un mínimo de 30 días; preferiblemente 90.

### Fase R5 — Ticket semiautomático

- Mostrar al usuario la mejor probabilidad/cesta, precio límite y tamaño máximo.
- Mantener entrada manual.
- Continuar sin firma, claves privadas ni envío automático de órdenes.

## Condiciones para recomendar una entrada

Los umbrales exactos se congelarán antes del shadow. Como requisitos mínimos, una
alerta deberá tener:

- cesta y reglas de resolución verificadas;
- todas las patas cotizables;
- margen positivo después de costos y un colchón conservador;
- tamaño limitado por la pata menos líquida;
- pérdida máxima explícita;
- ninguna dependencia de un fill maker hipotético;
- reconstrucción suficientemente confiable;
- ausencia de discrepancias contables críticas.

Si cualquiera falla, el sistema se abstiene.

## Validación antes de dinero real

No se considerará copiable por tener PnL histórico positivo. La estrategia deberá
superar una prueba forward prerregistrada con:

- número suficiente de cestas completas e independientes;
- PnL neto positivo usando precios realmente observados después de la señal;
- intervalos de confianza y resultados separados por categoría;
- estabilidad en varios niveles de latencia;
- drawdown, peor pérdida y capital simultáneamente comprometido aceptables;
- análisis de operaciones omitidas y de ejecución parcial;
- comparación contra no copiar y contra una regla simple;
- cero mezcla entre datos de entrenamiento y validación.

La decisión final será `APROBADO PARA PILOTO MANUAL`, `SEGUIR EN SHADOW` o
`DESCARTADO`. Nunca se prometerá rentabilidad.

## Artefactos existentes que sirven como base

- `src/polymarket_bot/polyledger.py`
- `polyledger_sentinel_v001.py`
- `data/polyledger/balthazar.db`
- `data/polyledger/balthazar_ultimo.json`
- `data/polyledger/balthazar_ultimo.md`
- `data/polyledger/balthazar_ultimo_open_positions.csv`
- `data/polyledger/balthazar_ultimo_closed_positions.csv`

## Próximo paso al reanudar

Comenzar por Fase R1. Antes de programar, crear el prerregistro con las reglas de
agrupación temporal, definición de una cesta completa, fuentes admitidas,
latencias, costos y criterios de abstención. Después implementar el observador en
modo de solo lectura y probarlo con Balthazar sin dinero real.

