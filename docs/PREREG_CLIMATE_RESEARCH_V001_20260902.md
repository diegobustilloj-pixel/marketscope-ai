# PRERREGISTRO — POLYMARKET CLIMATE RESEARCH V0.01

Estado: **CONGELADO ANTES DE LA CAPTURA MASIVA Y DEL MODELADO**  
Corte de información: **2026-09-02T00:08:41Z**  
Sector: **Daily maximum / minimum temperature exclusivamente**  
Modo: **investigación y shadow; wallet, firmas y órdenes prohibidas**

## Pregunta

Determinar si puede estimarse, antes de cada decisión, la distribución de la
máxima o mínima que registrará la fuente exacta de resolución y si esa
distribución supera precios ejecutables de Polymarket después de comisión,
spread, slippage, capacidad visible y capital-tiempo.

## Universo

- Eventos Gamma cuyo título siga `Highest temperature in … on …?` o
  `Lowest temperature in … on …?`.
- Solo eventos con reglas, fecha, buckets y ganador inequívocos.
- El target es la observación de la estación/fuente indicada por las reglas,
  no una temperatura genérica de ciudad ni una reanálisis de grilla.
- Máxima y mínima se estudian por separado.
- Otros mercados de clima, récords, lluvia y temperatura global se excluyen.

## Contrato de resolución

Antes de modelar cada familia ciudad/fuente se extraen: país, estación,
ICAO/WMO cuando existan, coordenadas, altitud, zona horaria, fuente primaria,
fallback, unidad, precisión, ventana local, frecuencia de observación y regla
de revisiones. Un campo crítico no resuelto bloquea la recomendación de bot
para esa familia.

## Disponibilidad temporal

- Una observación solo está disponible desde su timestamp publicado/importado.
- Un forecast solo puede usarse desde la inicialización de su run.
- Historical Forecast stitched no sustituye Single/Previous Runs cuando se
  evalúa un lead time histórico.
- Un precio solo puede usarse desde su timestamp CLOB.
- Información publicada después del corte no puede cambiar selección de
  features, modelos, ciudades, thresholds ni timing.

## Partición

Ordenar eventos cerrados por fin de ventana, agrupando el mismo día/ciudad para
evitar contaminación entre buckets:

- TRAIN: primer 60%.
- VALIDATION: siguiente 20%.
- TEST: último 20%, abierto una sola vez después de congelar selección.
- Empates temporales completos permanecen en una sola partición.
- Mercados abiertos al corte pertenecen únicamente a shadow forward.

## Candidatos meteorológicos

1. Climatología de estación por día del año.
2. Perfil horario y distribución condicional del incremento/decremento restante.
3. Forecast individual por proveedor/modelo y lead time.
4. Ensemble simple.
5. Ensemble con pesos y bias por estación aprendidos en TRAIN/VALIDATION.
6. Corrección con observación live disponible a la hora de decisión.
7. `maximum/minimum already set` condicionado por estación, mes y régimen.

Machine learning no se habilita si no supera baselines simples con tamaño y
cobertura suficientes. Los pesos dinámicos no usan TEST.

## Horizontes

Evaluar, cuando exista dato temporal auténtico: T-72h, T-48h, T-24h, T-12h,
T-6h, T-3h y T-1h; mismo día por hora local; después de la hora histórica de
máxima/mínima. Un horizonte sin forecast/observación archivado se marca
`NO EVALUABLE`, nunca se rellena con datos posteriores.

## Métricas meteorológicas

- MAE, RMSE y bias del extremo diario registrado.
- Bucket hit rate, Brier, Log Loss y error de calibración.
- Cobertura P10–P90.
- Probabilidad y calibración de superar el máximo actual o romper el mínimo.
- Error por estación, mes, lead time y régimen.

## Métricas económicas

- Mejor ask ejecutable YES y NO, no último precio.
- Edge neto por share y ROI esperado después de fee.
- Fill limitado a profundidad visible; si solo existe histórico midpoint, el
  resultado se etiqueta no ejecutable y no prueba rentabilidad.
- PnL, ROI, EV/trade, PF, drawdown, peor trade, racha de pérdidas, fill rate,
  capital máximo y PnL/capital-hora.
- Resultados separados por threshold 3%, 5%, 7.5%, 10% y 15%.

## Estrategias congeladas

Las estrategias A–N del Prompt Maestro se implementan únicamente cuando sus
datos históricos existen. La selección en VALIDATION prioriza EV neto ajustado
por riesgo y capital-tiempo, no win rate. `NO TRADE` es una salida válida.

## Gates mínimos

- Conteo de resolución y ganador 100% reconciliados en filas utilizadas.
- Cero fuga temporal comprobada.
- Cobertura reportada por fuente/modelo/horizonte.
- Una ciudad necesita al menos 30 eventos resueltos para ranking provisional y
  100 para una conclusión fuerte; menos se etiqueta exploratorio.
- Rentabilidad no puede declararse con midpoint, último precio o fills
  imposibles.
- Un TEST positivo aislado no autoriza dinero real: exige shadow forward,
  tamaño suficiente, calibración y drawdown controlado.

## Entregables

- Captura raw inmutable con manifest, timestamps y hashes.
- Catálogo de contratos de resolución y estaciones.
- Datasets observacionales, forecasts y mercado con cobertura explícita.
- Auditoría de leakage y particiones cronológicas.
- Ranking de modelos, ciudades y estrategias.
- `POLYMARKET CLIMATE — FINAL RESEARCH REPORT` y `CLIMATE SCORECARD`.
- Si existe candidato: diseño receive-only y preregistro shadow independiente.

## Criterio de verdad

La ausencia de datos históricos ejecutables, forecasts archivados o contratos
de estación completos produce `INCONCLUSO / MORE DATA REQUIRED`; nunca se
convierte en edge mediante supuestos.
