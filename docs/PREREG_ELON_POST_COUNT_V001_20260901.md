# PRERREGISTRO — ELON MUSK POST COUNT V0.01

Estado: **CONGELADO ANTES DE LA CAPTURA COMPLETA Y DEL MODELADO**  
Corte informativo: **2026-09-01T22:40:00Z**  
Zona temporal de contrato: **America/New_York**  
Universo: mercados de Polymarket cuyo resultado depende exclusivamente del número de publicaciones de `@elonmusk` contabilizadas por XTracker.

## 1. Fuentes permitidas

- XTracker oficial: usuario, posts y ventanas de tracking de `elonmusk`.
- Gamma API oficial: evento, reglas, mercados, buckets, tokens, volumen y resolución.
- CLOB API oficial: historial de precios y libro ejecutable disponible al corte.
- Sitios oficiales de Tesla, SpaceX, xAI, X, Neuralink y organismos públicos para noticias. Las noticias solo podrán usarse si conservan `timestamp_available` verificable.

No se usarán posts, precios, resultados ni noticias posteriores al instante simulado. El mercado activo se reportará por separado y nunca se incorporará al entrenamiento ni a la selección de parámetros.

## 2. Criterios de inclusión

Una ventana histórica entra al análisis económico si:

1. Tiene enlace de Polymarket y evento Gamma recuperable.
2. La regla identifica a `@elonmusk`, usa XTracker como fuente principal y cuenta main-feed posts, quote posts y reposts, excluyendo replies salvo que aparezcan en el main feed.
3. Tiene inicio y fin inequívocos y compatibles con Eastern Time.
4. Finalizó antes del corte y el bucket ganador es reconstruible sin ambigüedad.
5. Existe al menos un precio observable antes del instante de entrada evaluado.

Ventanas solapadas pueden usarse para descripción, pero los intervalos de confianza y tamaños efectivos se ajustarán por dependencia temporal. Los eventos son la unidad de split económico.

## 3. Datos y auditoría

- Se preservarán respuestas raw, hora de captura, URL, hash SHA-256 y errores.
- Los posts se deduplicarán por `platformId`; se verificará orden temporal e importación posterior a creación.
- El conteo oficial se contrastará con posts XTracker dentro de `[startDate, endDate]` usando límites inclusivos según la API.
- Los tipos de post se marcarán como `UNAVAILABLE` si XTracker no los expone; no se inferirán desde el texto.
- El histórico CLOB demuestra precios observados, no profundidad ejecutable histórica. El backtest económico aplicará un coste conservador y etiquetará la liquidez histórica no observable.

## 4. División temporal

- TRAIN: primeros 60% de eventos resueltos elegibles.
- VALIDATION: siguientes 20%.
- TEST: últimos 20%.
- El orden se define por `endDate`; eventos con idéntico final permanecen juntos.
- Selección de modelo, timing y edge se hace con TRAIN/VALIDATION. TEST se abre una sola vez para confirmar o rechazar.
- Además se ejecutará walk-forward: cada evento solo usa posts y eventos finalizados antes de su instante de predicción.

## 5. Modelos congelados

1. Naive recent-rate.
2. Perfil histórico por hora ET.
3. Poisson con intensidad temporal integrada.
4. Negative Binomial por mezcla Gamma-Poisson y dispersión estimada solo en pasado.
5. Bayesian dynamic rate con prior Gamma y actualización por actividad observada.
6. Hawkes exponencial únicamente si su log-loss y Brier de validación mejoran al mejor modelo simple.
7. Ensemble lineal únicamente con pesos seleccionados en VALIDATION y sin consultar TEST.

Todos producen una distribución discreta del conteo final. La probabilidad de bucket es la suma de sus masas. Los modelos se comparan mediante Brier multicategoría, log loss, calibración y error absoluto; la selección económica exige además EV neto.

## 6. Instantes de evaluación

- Fracción completada: 0%, 25%, 50%, 60%, 70%, 75%, 80%, 85%, 90% y 95%.
- Tiempo restante: 48h, 24h, 12h, 8h, 6h, 4h, 3h, 2h, 1h, 30m, 15m y 5m, cuando caigan dentro de la ventana.
- Para evitar duplicados, observaciones del mismo evento separadas por menos de 60 segundos se fusionan.

## 7. Backtest económico

- Precio de referencia YES: último precio histórico observado antes o en el instante simulado, con antigüedad máxima de 30 minutos. Precio de entrada proxy: referencia más 1 centavo de impacto, acotado a 0.99. Esta redacción fue corregida antes de la captura completa y de cualquier modelado para impedir fuga temporal.
- Precio de entrada NO: `1 - precio YES` más 1 centavo de impacto, acotado a 0.99. Se marca como proxy porque el histórico bid/ask completo puede no estar disponible.
- Fee: se usa el fee oficial del mercado si está disponible; si no, se reporta por separado y no se presume.
- Estrategias: early, midpoint, late, extreme-late, dynamic-edge, burst, silence, YES/NO y cesta de dos buckets adyacentes.
- Se mantienen posiciones hasta resolución para el backtest principal. Take-profit y salida dinámica son secundarios y solo se informan si existe camino de precio posterior observable.
- Stakes de sensibilidad: $100, $250, $500, $1.000, $2.500, $5.000 y $10.000. Si no existe profundidad histórica, los resultados por encima del mínimo se etiquetan `CAPACITY_UNVERIFIED`.

## 8. Gates de despliegue

No se recomienda dinero real salvo que una estrategia preseleccionada cumpla en TEST:

- mínimo 30 operaciones y 12 eventos independientes efectivos;
- ROI neto > 0;
- profit factor > 1.10;
- EV medio > 0;
- Brier mejor que el mercado o un baseline ingenuo comparable;
- PnL positivo sin el mejor trade y sin los tres mejores trades;
- vecindad de parámetros estable;
- drawdown compatible con los límites de riesgo;
- precios ejecutables y capacidad demostrables.

Si faltan precios, profundidad o muestra independiente, el veredicto será `MORE_DATA_REQUIRED` o `NO_TRADE`, no una extrapolación.

## 9. Noticias

Noticias históricas solo entran si puede reconstruirse una cronología suficientemente completa y con timestamp de disponibilidad. Si no, la sección de noticias se limita a una auditoría descriptiva y queda excluida de la selección del modelo para evitar sesgo de disponibilidad y fuga retrospectiva.

## 10. Política de implementación

Este estudio puede habilitar un monitor receive-only/shadow. Wallet, firma y órdenes reales permanecen fuera de alcance hasta superar todos los gates anteriores en una muestra forward independiente.
