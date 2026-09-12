# PRERREGISTRO — ELON POST COUNT SHADOW FORWARD V0.01

Estado: **CONGELADO ANTES DEL PRIMER CICLO FORWARD**  
Duración máxima: **24 horas**  
Modo: **receive-only / shadow**  
Wallet, claves, firmas y órdenes: **PROHIBIDAS**

## Objetivo

Capturar una muestra forward nueva de mercados de conteo de posts de `@elonmusk` sin reutilizar resultados futuros. El monitor debe determinar en cada ciclo `SHADOW_SIGNAL`, `WAIT` o `NO_TRADE`, pero nunca puede enviar una orden.

## Modelo congelado

- 50% Negative Binomial.
- 50% distribución de ventanas históricas comparables.
- Selección realizada únicamente con VALIDATION del estudio V0.01.
- Features: conteo actual, hora ET, tasa 1h/3h/6h/12h/24h, tiempo desde último post, sobredispersión y ventanas comparables.
- Noticias quedan deshabilitadas hasta disponer de una fuente archivada y auditada.

## Universo

- Trackings XTracker de Elon con enlace de Polymarket.
- Ventana iniciada y no finalizada en el instante de decisión.
- Reglas compatibles: main-feed posts, quote posts y reposts; replies excluidos salvo main-feed replies; XTracker como fuente primaria.
- Buckets Gamma inequívocos y dos tokens CLOB por bucket.

## Frecuencia

- Poll principal: 30 segundos.
- Recalcular tras cada post nuevo, cambio de mejor ask/bid o heartbeat periódico.
- Un ciclo que no pueda completar posts, reglas o libros se registra como degradado y no emite señal.

## Precio, fee y capacidad

- Precio ejecutable proxy: mejor ask del token YES o NO observado en el libro actual.
- Comisión taker Cultura: `shares × 0.05 × p × (1-p)` cuando `feesEnabled=true`.
- Edge neto por share: `probabilidad_modelo - ask - fee_por_share`.
- Notional solicitado: $100.
- Fill shadow máximo: mínimo entre shares solicitadas y tamaño visible en el mejor ask.
- Sin profundidad histórica o libro vigente no existe señal.

## Thresholds congelados

Se registran simultáneamente 5%, 7.5%, 10% y 15%. El threshold primario de observación es 5%; no está autorizado para dinero real. Se emite una señal shadow únicamente si:

1. edge neto >= threshold;
2. ask entre 0.01 y 0.99;
3. mejor ask visible >= 5 shares;
4. ventana y conteo son completos;
5. no existe una señal idéntica para el mismo mercado, bucket, lado, threshold y conteo actual.

## WAIT

`WAIT` se registra cuando no hay señal primaria y quedan más de cinco minutos. No representa una predicción rentable; preserva el estado para medir posteriormente el valor de esperar.

## Persistencia

SQLite en WAL con tablas de metadatos, ciclos, posts, mercados, snapshots, quotes, decisiones, señales y health. Cada fila conserva timestamps UTC y payload hash cuando corresponde. La reanudación no borra datos existentes.

## Cierre y evaluación

- Detener automáticamente al alcanzar 24 horas.
- Resolver señales solo con información observada después de su creación.
- Evaluar Brier, Log Loss, PnL, ROI, PF, drawdown, fill rate, capital-hora y resultados por threshold.
- Ninguna lectura parcial permite cambiar modelo, thresholds o timing.

## Gate

Esta ventana solo puede recomendar otra confirmación shadow. No habilita ejecución real por sí sola, aunque el resultado sea positivo.
